"""Persist reasoning intensity independently of model budgets and tool exposure."""

from agent.runtime.paths import state_path

import json
import os
from dataclasses import replace
from pathlib import Path

from agent.runtime.json_preferences import update_preferences

from agent.runtime.deepseek import is_deepseek_model
from agent.runtime.llm import LLMConfig

# xhigh sits between high and max: deeper reasoning than high at far less than max's cost
# (claude.ai labels max "5.5x or more usage"; Claude Code's docs call max prone to overthinking).
REASONING_EFFORTS = ("low", "high", "xhigh", "max")
DEFAULT_REASONING_EFFORT = "high"
_LEGACY_MODES = {"coding": "max", "chat": "high"}
MODE_USAGE = "Usage: /mode [low|high|xhigh|max]"


def _settings_path() -> Path:
    override = os.getenv("AGENT_SETTINGS_PATH", "").strip()
    if override:
        return Path(override).expanduser()
    return state_path("settings.json")


def _read_settings() -> dict:
    try:
        data = json.loads(_settings_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def read_reasoning_effort() -> str:
    data = _read_settings()
    if "reasoning_effort" in data:
        effort = data["reasoning_effort"]
        return effort if effort in REASONING_EFFORTS else DEFAULT_REASONING_EFFORT
    legacy = data.get("agent_mode")
    return _LEGACY_MODES.get(legacy, DEFAULT_REASONING_EFFORT) if isinstance(legacy, str) else DEFAULT_REASONING_EFFORT


def _validate(effort: str) -> None:
    if effort not in REASONING_EFFORTS:
        raise ValueError(f"Unknown reasoning effort: {effort}. {MODE_USAGE}")


def save_reasoning_effort(effort: str) -> Path:
    """Save the intensity and retire both old mode keys without touching other settings."""
    _validate(effort)
    def change(data):
        data["reasoning_effort"] = effort
        data.pop("agent_mode", None)
        data.pop("code_mode", None)
    return update_preferences(_settings_path(), change)


def apply_reasoning_effort(config: LLMConfig, effort: str | None = None) -> LLMConfig:
    effort = read_reasoning_effort() if effort is None else effort
    _validate(effort)
    return replace(config, reasoning_effort=effort)


def set_reasoning_effort(llm, effort: str) -> None:
    """Switch only effort, preserving the model's context limit and output allowance."""
    _validate(effort)
    config = llm.config
    llm.switch_model(
        config.model,
        reasoning_effort=effort,
        context_limit=config.context_limit,
    )
    save_reasoning_effort(effort)


def reasoning_effort_status(config: LLMConfig) -> str:
    text = f"Reasoning effort: {config.reasoning_effort}"
    if config.provider == "openai-codex":
        text += ("\nCodex returns reasoning summaries; max uses the model's highest advertised effort, "
                 "and xhigh the highest one up to xhigh.")
    elif config.provider == "claude-code":
        text += "\nClaude Code applies it as --effort (low, medium, high, xhigh, max)."
    elif not is_deepseek_model(config.model):
        text += "\nSaved preference only; the current model adapter does not apply reasoning effort."
    elif config.reasoning_effort == "xhigh":
        text += "\nDeepSeek runs xhigh as high."
    return text
