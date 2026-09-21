"""Persist and restore the user's selected model."""

from agent.runtime.paths import state_path

import json
import os
from pathlib import Path

from agent.runtime.json_preferences import update_preferences

from .models import model_profiles, resolve_profile_key


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def model_settings_path() -> Path:
    override = os.getenv("AGENT_SETTINGS_PATH", "").strip()
    return Path(override).expanduser() if override else state_path("settings.json", root=PROJECT_ROOT)


def read_selected_model() -> str | None:
    """Read the persisted selection without requiring a live/static catalog match."""
    path = model_settings_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    selected = data.get("selected_model") if isinstance(data, dict) else None
    return selected.strip() if isinstance(selected, str) and selected.strip() else None


def load_selected_model(valid_models: set[str] | None = None) -> str | None:
    """Load a valid persisted model name, ignoring missing/corrupt settings."""
    selected = read_selected_model()
    if not selected:
        return None
    if valid_models is None:
        profiles = model_profiles()
        selected = resolve_profile_key(selected, profiles)
        allowed = set(profiles)
    else:
        allowed = valid_models
    return selected if selected in allowed else None


def save_selected_model(model: str, valid_models: set[str] | None = None) -> Path:
    """Atomically persist the selected model name for future launches."""
    allowed = valid_models if valid_models is not None else set(model_profiles())
    if model not in allowed:
        raise ValueError(f"Unknown model: {model}")

    path = model_settings_path()
    def change(data):
        data["selected_model"] = model
        previous = data.get("recent_models", [])
        data["recent_models"] = [model, *[m for m in previous if isinstance(m, str) and m != model]][:8] if isinstance(previous, list) else [model]
    return update_preferences(path, change)


def resolve_startup_model(env_model: str, env_base_url: str) -> tuple[str, str]:
    """Prefer a valid persisted selection, then fall back to environment."""
    selected = load_selected_model()
    if selected:
        profile = model_profiles()[selected]
        return selected, profile.base_url

    profiles = model_profiles()
    env_model = resolve_profile_key(env_model, profiles)
    if env_model in profiles:
        return env_model, profiles[env_model].base_url
    return env_model, env_base_url


def recent_models() -> list[str]:
    try:
        data = json.loads(model_settings_path().read_text(encoding="utf-8"))
        items = data.get("recent_models", [])
        return [m for m in items if isinstance(m, str)][:8] if isinstance(items, list) else []
    except (OSError, ValueError, AttributeError):
        return []
