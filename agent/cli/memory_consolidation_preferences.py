"""Persisted on/off state and the side model for idle memory consolidation."""

from agent.runtime.paths import state_path

import json
import os
from pathlib import Path

from agent.runtime.json_preferences import update_preferences
from agent.runtime.memory_proposals import CONSOLIDATION_MODEL, ChatClient

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def consolidation_settings_path() -> Path:
    override = os.getenv("AGENT_SETTINGS_PATH", "").strip()
    return Path(override).expanduser() if override else state_path("settings.json", root=PROJECT_ROOT)


def load_memory_consolidation_enabled() -> bool:
    """On unless turned off: passes only propose, the user accepts each change."""
    try:
        data = json.loads(consolidation_settings_path().read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return True
    value = data.get("memory_consolidation", True) if isinstance(data, dict) else True
    return value if isinstance(value, bool) else True


def save_memory_consolidation_enabled(enabled: bool) -> Path:
    return update_preferences(consolidation_settings_path(), lambda data: data.update(memory_consolidation=bool(enabled)))


def consolidation_client() -> tuple[ChatClient | None, str]:
    """A client for the consolidation model, or None and the reason it is unavailable."""
    from agent.cli.connections import is_local_url
    from agent.cli.environment import load_project_env
    from agent.cli.model_catalog import configured_model_catalog
    from agent.runtime.llm import LLMClient, LLMConfig

    load_project_env(PROJECT_ROOT)
    entry = configured_model_catalog().resolve(CONSOLIDATION_MODEL)
    if entry is None:
        return None, f"model {CONSOLIDATION_MODEL} is not configured"
    profile = entry.profile
    api_key = profile.api_key() or ("local" if is_local_url(entry.base_url) else "")
    if not api_key:
        return None, f"{profile.api_key_env or 'the API key'} is not set for {CONSOLIDATION_MODEL}"
    return LLMClient(LLMConfig(
        provider=profile.provider, model=entry.model_id, api_key=api_key, base_url=entry.base_url,
        capabilities=profile.capabilities, **profile.generation_settings(),
    )), entry.key
