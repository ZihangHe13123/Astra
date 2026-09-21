"""Persist and restore the selected web-search provider."""

import json
import os
from pathlib import Path

from agent.runtime.json_preferences import update_preferences

from .model_preferences import model_settings_path


SEARCH_PROVIDERS = ("auto", "exa", "searxng")


def _read_settings(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def load_selected_search_provider() -> str | None:
    selected = _read_settings(model_settings_path()).get("search_provider")
    return selected if isinstance(selected, str) and selected in SEARCH_PROVIDERS else None


def resolve_startup_search_provider() -> str:
    selected = load_selected_search_provider()
    if selected:
        return selected
    configured = os.getenv("WEB_SEARCH_PROVIDER", "auto").strip().lower()
    return configured if configured in SEARCH_PROVIDERS else "auto"


def save_selected_search_provider(provider: str) -> Path:
    provider = provider.strip().lower()
    if provider not in SEARCH_PROVIDERS:
        raise ValueError(f"Unknown search provider: {provider}")

    return update_preferences(model_settings_path(), lambda data: data.update(search_provider=provider))
