"""Persist opt-in preferences for the proactive Context Index."""

from __future__ import annotations

from agent.runtime.paths import state_path

from dataclasses import dataclass
import json
import os
from pathlib import Path

from agent.runtime.json_preferences import update_preferences
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MODES = frozenset({"off", "session", "all", "shadow"})


@dataclass(frozen=True)
class ContextIndexPreferences:
    mode: str = "off"
    char_budget: int = 900
    invalid_mode: str = ""


def context_index_settings_path() -> Path:
    override = os.getenv("AGENT_SETTINGS_PATH", "").strip()
    return Path(override).expanduser() if override else state_path("settings.json", root=PROJECT_ROOT)


def _read_settings() -> dict[str, Any]:
    try:
        loaded = json.loads(context_index_settings_path().read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return dict(loaded) if isinstance(loaded, dict) else {}


def load_context_index_preferences() -> ContextIndexPreferences:
    """Load Context Index preferences, fail-closing an invalid mode."""
    data = _read_settings()
    raw_mode = data.get("context_index_mode", "off")
    valid_mode = isinstance(raw_mode, str) and raw_mode in MODES
    mode = raw_mode if valid_mode else "off"
    invalid = "" if valid_mode else str(raw_mode)[:80]
    raw_budget = data.get("context_index_char_budget", 900)
    budget = max(400, min(int(raw_budget), 2000)) if isinstance(raw_budget, int) else 900
    return ContextIndexPreferences(mode, budget, invalid)


def save_context_index_preferences(mode: str, char_budget: int | None = None) -> Path:
    """Atomically save Context Index preferences without replacing other settings."""
    if mode not in MODES:
        raise ValueError("mode must be off, session, all, or shadow")
    def change(data):
        data["context_index_mode"] = mode
        if char_budget is not None:
            data["context_index_char_budget"] = max(400, min(int(char_budget), 2000))
    return update_preferences(context_index_settings_path(), change)
