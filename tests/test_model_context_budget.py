import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.cli.connections import sync_context_budget
from agent.cli.models import ModelProfile, prompt_token_budget
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
