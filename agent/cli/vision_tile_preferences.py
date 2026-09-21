"""Persist and apply the opt-in state for DeepSeek vision image tiling."""

from agent.runtime.paths import state_path

import json
import os
from pathlib import Path

from agent.runtime.json_preferences import update_preferences
from typing import Protocol

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class VisionTileAgent(Protocol):
    vision_tiles_enabled: bool

    @property
    def llm(self) -> object: ...

    def set_vision_tiles_enabled(self, enabled: bool) -> None: ...


def vision_tile_settings_path() -> Path:
    override = os.getenv("AGENT_SETTINGS_PATH", "").strip()
    return Path(override).expanduser() if override else state_path("settings.json", root=PROJECT_ROOT)


def load_vision_tiles_enabled() -> bool:
    """Load the persisted preference, treating unavailable settings as enabled."""
    try:
        data = json.loads(vision_tile_settings_path().read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return True
    value = data.get("vision_tiles_enabled", True) if isinstance(data, dict) else True
    return value if isinstance(value, bool) else True


def save_vision_tiles_enabled(enabled: bool) -> Path:
    """Atomically persist the tiling preference without discarding other settings."""
    return update_preferences(vision_tile_settings_path(), lambda data: data.update(vision_tiles_enabled=bool(enabled)))


def execute_vision_tiles_command(agent: VisionTileAgent, args: list[str]) -> tuple[str, str]:
    """Return slash-command output after optionally changing the live preference."""
    if len(args) > 1:
        return "", "Usage: /vision-tiles [on|off]"

    action = args[0].lower() if args else "status"
    if action not in {"status", "show", "on", "off"}:
        return "", "Usage: /vision-tiles [on|off]"

    if action in {"on", "off"}:
        enabled = action == "on"
        try:
            path = save_vision_tiles_enabled(enabled)
        except OSError as exc:
            return "", str(exc)
        agent.set_vision_tiles_enabled(enabled)
        return _status_message(agent, saved_path=path), ""

    return _status_message(agent), ""


def _status_message(agent: VisionTileAgent, saved_path: Path | None = None) -> str:
    enabled = bool(agent.vision_tiles_enabled)
    policy = getattr(getattr(agent.llm, "config", None), "vision_preprocess", None)
    active_check = getattr(agent, "_vision_tile_tool_enabled", None)
    active = (
        bool(active_check())
        if callable(active_check)
        else enabled and policy is not None
    )
    lines = [
        f"Vision tiles: {'ON' if enabled else 'OFF'}",
        "Scope: configured DeepSeek vision models only.",
        f"Active for current model: {'YES' if active else 'NO'}",
    ]
    if policy is None:
        lines.append("Inactive: this model has no tiled preprocessing policy.")
    else:
        lines.extend([
            f"Policy: {policy.tile_width}x{policy.tile_height}, {policy.overlap}px overlap",
            f"Budget: {policy.max_images_per_request} images/request, {policy.max_inline_body_bytes} bytes",
        ])
    if not enabled:
        lines.append(
            "Warning: large images are sent directly when tiles are OFF; "
            "DeepSeek provider downscaling may reduce image detail."
        )
    if saved_path is not None:
        lines.append(f"Saved as startup default in {saved_path}.")
    return "\n".join(lines)
