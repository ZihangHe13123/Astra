"""Persist and apply the runtime sandbox selection."""

from agent.runtime.paths import state_path

import json
import os
from pathlib import Path

from agent.runtime.json_preferences import update_preferences

from agent.sandbox.router import SandboxRouter


PROJECT_ROOT = Path(__file__).resolve().parents[2]
VALID_SANDBOX_MODES = ("docker", "local")


def sandbox_settings_path() -> Path:
    override = os.getenv("AGENT_SETTINGS_PATH", "").strip()
    return Path(override).expanduser() if override else state_path("settings.json", root=PROJECT_ROOT)


def load_selected_sandbox_mode() -> str | None:
    try:
        data = json.loads(sandbox_settings_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    selected = data.get("sandbox_mode") if isinstance(data, dict) else None
    return selected if selected in VALID_SANDBOX_MODES else None


def resolve_startup_sandbox_mode(env_mode: str) -> str:
    selected = load_selected_sandbox_mode()
    if selected == "docker":
        return "true"
    if selected == "local":
        return "false"
    return env_mode


def save_selected_sandbox_mode(mode: str) -> Path:
    if mode not in VALID_SANDBOX_MODES:
        raise ValueError(f"Unknown sandbox mode: {mode}")
    return update_preferences(sandbox_settings_path(), lambda data: data.update(sandbox_mode=mode))


def execute_sandbox_command(router: SandboxRouter, args: list[str]) -> tuple[str, str]:
    action = args[0].lower() if args else "status"
    if action in {"status", "show"}:
        return (
            f"Sandbox: {'ON' if router.enabled else 'OFF'}\n"
            f"Execution backend: {router.description}\n"
            "Use /sandbox on for Docker isolation or /sandbox off for guarded host execution.",
            "",
        )
    try:
        mode = router.switch(action)
        path = save_selected_sandbox_mode(mode)
    except (OSError, ValueError) as exc:
        return "", str(exc)
    warning = (
        "Shell/Python now run in an isolated Docker container."
        if mode == "docker"
        else "Shell/Python now run on the host with timeout and dangerous-command checks still enabled."
    )
    return (
        f"Sandbox switched {'ON' if router.enabled else 'OFF'} immediately.\n"
        f"{warning}\nSaved as startup default in {path}.",
        "",
    )
