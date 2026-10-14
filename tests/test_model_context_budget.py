import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.cli.connections import sync_context_budget
from agent.cli.models import (
    ModelProfile, _load_yaml, _profile_from_data, prompt_token_budget, save_model_profile,
)
from agent.runtime.llm import LLMConfig
from agent.runtime.context import AgentContext


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
