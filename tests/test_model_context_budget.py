import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.channels.base import ChannelMessage
from agent.channels.router import AgentChannelRouter
from agent.cli.connections import sync_context_budget
from agent.cli.models import (
    ModelProfile, _load_yaml, _profile_from_data, prompt_token_budget, save_model_profile,
)
from agent.runtime.llm import LLMConfig
from agent.runtime.context import AgentContext
from agent.runtime.subagent_replay import replay_budget


@pytest.mark.parametrize("model,provider", [
    ("gpt-6-astra", "openai-codex"),
    ("gpt-5.5", "openai-compatible"),
    ("gpt-4.1", "openai-compatible"),
    ("chatgpt-4o-latest", "openai-compatible"),
    ("openai/gpt-5.5", "openai-compatible"),
    ("codex::gpt-6-astra", "openai-codex"),
    ("custom-codex-model", "openai-codex"),
])
def test_gpt_budget_uses_window_minus_output_reserve(model, provider):
    assert prompt_token_budget(272_000, 4_096, model=model, provider=provider) == 263_808
    assert prompt_token_budget(1_000_000, 32_768, model=model, provider=provider) == 967_232


@pytest.mark.parametrize("model", ["deepseek-flash", "qwen3.8-flash", "not-gpt-5", "local-model", ""])
def test_other_models_keep_existing_proactive_threshold(model):
    assert prompt_token_budget(272_000, 4_096, model=model) == 136_000


def test_claude_code_models_compact_at_150k_whatever_their_window():
    """Live Astra 2026-11-11: a thread on Opus at 484 messages and 226k prompt tokens waited 11 to
    41 s for its first event, because the CLI is started again for every request and takes each
    earlier turn before it asks; half of the 1M window was nowhere near."""
    assert prompt_token_budget(1_000_000, 32_000, model="opus", provider="claude-code") == 150_000
    # A window whose half is less keeps its half.
    assert prompt_token_budget(200_000, 32_000, model="haiku", provider="claude-code") == 100_000
    # The model's own setting decides, later or earlier.
    assert prompt_token_budget(1_000_000, 32_000, model="opus", provider="claude-code",
                               auto_compact_token_limit=400_000) == 400_000
    assert prompt_token_budget(1_000_000, 32_000, model="opus", provider="claude-code",
                               auto_compact_token_limit=80_000) == 80_000
    # The CLI is what a long conversation is slow through: the same model over an API keeps half.
    assert prompt_token_budget(1_000_000, 32_000, model="claude-opus-5", provider="openai-compatible") == 500_000


def test_every_entry_compacts_a_claude_code_conversation_at_the_same_point(tmp_path, monkeypatch):
    config = LLMConfig(model="opus", provider="claude-code", max_tokens=32_000)
    profile = ModelProfile(base_url="claude-code://local", context_limit=1_000_000, provider="claude-code",
                           model_id="opus")
    # A model switch in the window.
    window = SimpleNamespace(llm=SimpleNamespace(config=config), context=SimpleNamespace(max_prompt_tokens=1))
    assert sync_context_budget(window, profile) == window.context.max_prompt_tokens == 150_000
    assert config.context_limit == 1_000_000, "the window itself is not what changed"
    # A subagent's history.
    monkeypatch.delenv("ASTRA_SUBAGENT_MAX_PROMPT_TOKENS", raising=False)
    assert replay_budget(SimpleNamespace(config=config)) == 150_000
    # A channel conversation answered by its own model, beside a window on another one.
    seen = []

    class Agent:
        def __init__(self) -> None:
            self.context = AgentContext(system_prompt="system", max_messages=20, max_prompt_tokens=500_000)
            self.llm = SimpleNamespace(config=SimpleNamespace(context_limit=None))

        async def reply_stream(self, message):
            seen.append(self.context.max_prompt_tokens)
            yield {"type": "done", "content": "ok"}

    router = AgentChannelRouter(Agent(), asyncio.Lock(), tmp_path, model_for=lambda channel: SimpleNamespace(config=config))
    asyncio.run(router.handle(ChannelMessage("weixin", "owner", "owner", "在吗")))
    assert seen == [150_000]


def test_small_gpt_window_still_reserves_room_for_output():
    assert prompt_token_budget(16_384, 12_000, model="gpt-4") == 4_384
    assert prompt_token_budget(4_096, 8_192, model="gpt-4") == 2_048


def test_refresh_and_model_switch_recompute_family_budget():
    config = LLMConfig(model="gpt-6-astra", provider="openai-codex", max_tokens=4_096)
    agent = SimpleNamespace(llm=SimpleNamespace(config=config), context=SimpleNamespace(max_prompt_tokens=1))
    codex = ModelProfile(base_url="https://chatgpt.com/backend-api/codex", context_limit=272_000,
                         provider="openai-codex", model_id="gpt-6-astra")
    assert sync_context_budget(agent, codex) == 263_808
    assert config.context_limit == 272_000

    config.model, config.provider = "deepseek-flash", "openai-compatible"
    other = ModelProfile(base_url="https://api.deepseek.com", context_limit=1_000_000, model_id=config.model)
    assert sync_context_budget(agent, other) == 500_000

    config.model = "openai/gpt-5.5"
    gpt = ModelProfile(base_url="https://example.test/v1", context_limit=252_000, model_id=config.model)
    assert sync_context_budget(agent, gpt) == 243_808
    assert agent.context.max_prompt_tokens == 243_808
    assert config.context_limit == 252_000


def test_gpt_history_is_not_compacted_at_half_the_window(monkeypatch):
    context = AgentContext()
    context.max_prompt_tokens = prompt_token_budget(272_000, 4_096, model="gpt-6-astra")
    compress = AsyncMock(return_value=True)
    monkeypatch.setattr(context, "_compress_history", compress)

    asyncio.run(context.compress_if_needed(measure_tokens=lambda: 200_000))
    compress.assert_not_awaited()
    asyncio.run(context.compress_if_needed(measure_tokens=lambda: 263_808))
    compress.assert_awaited_once()


def test_explicit_window_and_compaction_round_trip_and_output_reserve(tmp_path, monkeypatch):
    profile = _profile_from_data("gpt-6.1-sol", {
        "base_url": "https://chatgpt.com/backend-api/codex", "provider": "openai-codex",
        "context_window": 512_000, "auto_compact_token_limit": 450_000,
    })
    saved = save_model_profile("gpt-6.1-sol", profile, tmp_path / "models.yaml")
    restored = _profile_from_data("gpt-6.1-sol", _load_yaml(saved)["models"]["gpt-6.1-sol"])
    config = LLMConfig(model="gpt-6.1-sol", **restored.generation_settings())
    context = AgentContext()
    agent = SimpleNamespace(llm=SimpleNamespace(config=config), context=context)
    assert sync_context_budget(agent, restored) == 450_000
    assert config.context_limit == 512_000
    assert config.auto_compact_token_limit == 450_000

    compress = AsyncMock(return_value=True)
    monkeypatch.setattr(context, "_compress_history", compress)
    asyncio.run(context.compress_if_needed(measure_tokens=lambda: 449_999))
    compress.assert_not_awaited()
    asyncio.run(context.compress_if_needed(measure_tokens=lambda: 450_000))
    compress.assert_awaited_once()
    assert prompt_token_budget(512_000, 100_000, auto_compact_token_limit=450_000) == 412_000


@pytest.mark.parametrize("settings", [
    {"context_window": 0}, {"context_window": True}, {"context_window": 512_000.5},
    {"auto_compact_token_limit": -1}, {"auto_compact_token_limit": "450000"},
    {"context_window": 272_000, "auto_compact_token_limit": 450_000},
])
def test_invalid_explicit_context_settings_are_rejected(settings):
    with pytest.raises(ValueError):
        _profile_from_data("test", {"base_url": "https://example.test/v1", **settings})
