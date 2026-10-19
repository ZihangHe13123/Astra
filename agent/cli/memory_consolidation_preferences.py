"""Persisted mode and the side model for idle memory consolidation."""

from agent.runtime.paths import state_path

import json
import os
from pathlib import Path

from agent.runtime.json_preferences import update_preferences
from agent.runtime.memory_proposals import CONSOLIDATION_MODEL, CONSOLIDATION_MODES, ChatClient

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def consolidation_settings_path() -> Path:
    override = os.getenv("AGENT_SETTINGS_PATH", "").strip()
    return Path(override).expanduser() if override else state_path("settings.json", root=PROJECT_ROOT)


def load_memory_consolidation_mode() -> str:
    """off, review or auto; review unless set, so the user accepts each change."""
    try:
        data = json.loads(consolidation_settings_path().read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return "review"
    value = data.get("memory_consolidation", "review") if isinstance(data, dict) else "review"
    if isinstance(value, bool):  # Saved as on/off before there were three modes.
        return "review" if value else "off"
    return value if value in CONSOLIDATION_MODES else "review"


def save_memory_consolidation_mode(mode: str) -> Path:
    if mode not in CONSOLIDATION_MODES:
        raise ValueError(f"memory consolidation mode must be one of {', '.join(CONSOLIDATION_MODES)}")
    return update_preferences(consolidation_settings_path(), lambda data: data.update(memory_consolidation=mode))


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
