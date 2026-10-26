"""Shared, bounded local-surface commands for macOS Computer Use.

This module deliberately knows nothing about model tools or private Computer Use
artifacts.  It gives the three local frontends one small command contract and a
safe event payload for their status UI.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
import unicodedata
import weakref
from collections.abc import Callable, Mapping

_ACCESSIBILITY_PANE = "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
_SCREEN_RECORDING_PANE = "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"
_CLOSE_TASKS: weakref.WeakKeyDictionary[object, asyncio.Task[object]] = weakref.WeakKeyDictionary()
_ANSI_ESCAPE = re.compile(
    r"(?:\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[PX^_][\s\S]*?\x1b\\|\x1b\[[0-?]*[ -/]*[@-~]|[\x90\x9b\x9d\x9e\x9f][\s\S]*?\x9c)"
)
_BIDI_CONTROLS = frozenset("\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")
_DEFAULT_IGNORABLE_CONTROLS = frozenset("\u00ad\u034f\u180e\u200b\u200c\u2060\ufeff")
_COMPUTER_CONTROLS = frozenset({"inactive", "background", "foreground_takeover", "user_control", "paused"})
_APPROVAL_CHOICES = ("once", "session", "deny")
# What a request may offer; the registry decides which of them it does.
_KNOWN_CHOICES = ("once", "task", "session", "always", "deny")


def _is_emoji_base(character: str) -> bool:
    codepoint = ord(character)
    return 0x1F000 <= codepoint <= 0x1FAFF or 0x2600 <= codepoint <= 0x27BF


def _is_grapheme_extension(character: str) -> bool:
    codepoint = ord(character)
    return (
        unicodedata.category(character) in {"Mn", "Me"}
        or 0x1F3FB <= codepoint <= 0x1F3FF
        or 0xFE00 <= codepoint <= 0xFE0F
    )


def _is_emoji_format(characters: list[str], index: int) -> bool:
    character = characters[index]
    if character == "\ufe0f":
        return index > 0 and _is_emoji_base(characters[index - 1])
    if character != "\u200d" or index == 0 or index + 1 >= len(characters):
        return False
    previous = index - 1
    while previous >= 0 and _is_grapheme_extension(characters[previous]):
        previous -= 1
    return previous >= 0 and _is_emoji_base(characters[previous]) and _is_emoji_base(characters[index + 1])


def _graphemes(value: str):
    cluster = ""
    join_next = False
    for character in value:
        if not cluster:
            cluster = character
        elif character == "\u200d":
            cluster += character
            join_next = True
        elif join_next or _is_grapheme_extension(character):
            cluster += character
            join_next = False
        else:
            yield cluster
            cluster = character
    if cluster:
        yield cluster


def _safe_application(value: object) -> str:
    """Return an app label only; strip paths, controls, and unbounded text."""
    if not isinstance(value, str):
        return ""
    text = _ANSI_ESCAPE.sub("", unicodedata.normalize("NFC", value))
    characters = list(text)
    safe_characters: list[str] = []
    for index, character in enumerate(characters):
        if character.isspace():
            safe_characters.append(" ")
        elif (
            ord(character) < 0x20
            or 0x7F <= ord(character) <= 0x9F
            or character in _BIDI_CONTROLS
            or character in _DEFAULT_IGNORABLE_CONTROLS
            or character in {"\u200d", "\ufe0f"} and not _is_emoji_format(characters, index)
        ):
            continue
        else:
            safe_characters.append(character)
    text = "".join(safe_characters)
    text = " ".join(text.split())
    for separator in ("/", "\\"):
        if separator in text:
            text = text.split(separator, 1)[0].rstrip()
    return "".join(list(_graphemes(text))[:64])


def _permission_from_status(status: Mapping[str, object] | None, manager: object) -> str:
    if status is not None:
        permissions = status.get("permissions")
        if isinstance(permissions, Mapping):
            accessibility = permissions.get("accessibility")
            screen_recording = permissions.get("screen_recording")
            if accessibility is True and screen_recording is True:
                return "available"
            if isinstance(accessibility, bool) and isinstance(screen_recording, bool):
                return "degraded"
        return "degraded"
    capability_status = getattr(manager, "capability_status", None)
    if callable(capability_status):
        value = capability_status()
        if isinstance(value, tuple) and value and value[0] in {"available", "degraded", "unsupported"}:
            return value[0]
    return "degraded"


def computer_state(
    manager: object | None,
    *,
    platform_name: str | None = None,
    status: Mapping[str, object] | None = None,
    application: object = "",
) -> dict[str, object]:
    """Produce the only state shape local UIs may receive.

    No private helper data, target references, window titles, paths, screenshots,
    typed text, or session identifiers are permitted in this event.
    """
    platform_name = platform_name or sys.platform
    if platform_name != "darwin":
        return {
            "platform": "unsupported",
            "configured": False,
            "probed": False,
            "permission": "unsupported",
            "active": False,
            "handed_off": False,
            "control": "inactive",
        }
    if manager is None:
        return {
            "platform": "macOS",
            "configured": False,
            "probed": False,
            "permission": "degraded",
            "active": False,
            "handed_off": False,
            "control": "inactive",
        }
    target = getattr(manager, "target", None)
    closed = bool(getattr(manager, "closed", False))
    active = bool(target is not None and not closed)
    handed_off = bool(active and getattr(manager, "handed_off", False))
    result: dict[str, object] = {
        "platform": "macOS",
        "configured": True,
        "probed": bool(getattr(manager, "helper_started", False) or status is not None),
        "permission": _permission_from_status(status, manager),
        "active": active,
        "handed_off": handed_off,
        "control": "user_control" if handed_off else "background" if active else "inactive",
    }
    safe_application = _safe_application(application)
    if active and safe_application:
        result["application"] = safe_application
    return result


def format_computer_state(state: Mapping[str, object]) -> str:
    """Format bounded command output without identifiers or private artifacts."""
    platform = str(state.get("platform", "unsupported"))
    configured = "yes" if state.get("configured") is True else "no"
    probed = "yes" if state.get("probed") is True else "no"
    permission = str(state.get("permission", "degraded"))
    active = "active" if state.get("active") is True else "inactive"
    handoff = "user control" if state.get("handed_off") is True else "none"
    lines = [
        f"Computer Use status\nplatform: {platform}",
        f"configured: {configured}",
        f"probed: {probed}",
        f"permission: {permission}",
        f"session: {active}",
        f"target: {'selected' if state.get('active') is True else 'none'}",
        f"handoff: {handoff}",
    ]
    application = _safe_application(state.get("application"))
    if application:
        lines.append(f"application: {application}")
    return "\n".join(lines)


def approval_choices(request: Mapping[str, object]) -> list[str]:
    """Honor backend choices; legacy takeover requests default to once/deny."""
    if request.get("kind") == "workflow_design":
        return ["once", "deny"]
    raw = request.get("choices")
    if isinstance(raw, (list, tuple)):
        choices = list(dict.fromkeys(choice for choice in raw if choice in _KNOWN_CHOICES))
        if choices:
            return choices
    if request.get("kind") == "computer_foreground_takeover":
        return ["once", "deny"]
    return list(_APPROVAL_CHOICES)


def _default_open_url(url: str) -> None:
    result = subprocess.run(["open", url], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if result.returncode != 0:
        raise OSError("macOS settings launcher failed")


async def _read_status(manager: object) -> Mapping[str, object]:
    status = await manager.status()  # type: ignore[attr-defined]
    return status if isinstance(status, Mapping) else {}


def _cached_status(manager: object) -> Mapping[str, object] | None:
    getter = getattr(manager, "cached_status", None)
    if not callable(getter):
        return None
    value = getter()
    return value if isinstance(value, Mapping) else None


async def _close_once(manager: object) -> None:
    task = _CLOSE_TASKS.get(manager)
    if task is None or task.done():
        task = asyncio.create_task(manager.close())  # type: ignore[attr-defined]
        _CLOSE_TASKS[manager] = task
    await asyncio.shield(task)


async def execute_computer_command(
    argv: list[str],
    manager: object | None,
    *,
    platform_name: str | None = None,
    open_url: Callable[[str], None] | None = None,
    emit_state: Callable[[dict[str, object]], object] | None = None,
) -> tuple[str, str]:
    """Run `/computer status|setup|stop` for a local frontend."""
    command = argv[0].lower() if argv else "status"
    platform_name = platform_name or sys.platform
    if command not in {"status", "setup", "stop"}:
        return "", "Usage: /computer [status|setup|stop]"
    if platform_name != "darwin":
        if command == "status":
            return format_computer_state(computer_state(None, platform_name=platform_name)), ""
        return "", "Computer Use is currently only available on macOS."
    if manager is None:
        return "", "Computer Use is not configured on this local surface."
    if command == "stop":
        try:
            await _close_once(manager)
        except Exception as exc:  # noqa: BLE001 - public output stays bounded
            return "", f"Computer Use stop failed ({type(exc).__name__})."
        state = computer_state(manager, platform_name=platform_name)
        if emit_state is not None:
            emit_state(state)
        return "Computer Use stopped.", ""

    if command == "status" and callable(getattr(manager, "cached_status", None)):
        status = _cached_status(manager)
    else:
        try:
            status = await _read_status(manager)
        except Exception as exc:  # noqa: BLE001 - do not expose helper diagnostics
            state = computer_state(manager, platform_name=platform_name)
            if emit_state is not None:
                emit_state(state)
            return "", f"Unable to query Computer Use status ({type(exc).__name__})."
    state = computer_state(manager, platform_name=platform_name, status=status)
    if emit_state is not None:
        emit_state(state)
    if command == "status":
        return format_computer_state(state), ""

    permissions = status.get("permissions") if isinstance(status, Mapping) else None
    if not isinstance(permissions, Mapping) or any(
        not isinstance(permissions.get(name), bool)
        for name in ("accessibility", "screen_recording")
    ):
        return "", "Computer Use permission status is unavailable; run setup again after the helper is available."
    missing: list[tuple[str, str]] = []
    if isinstance(permissions, Mapping):
        if permissions.get("accessibility") is False:
            missing.append(("Accessibility", _ACCESSIBILITY_PANE))
        if permissions.get("screen_recording") is False:
            missing.append(("Screen Recording", _SCREEN_RECORDING_PANE))
    if not missing:
        return "Computer Use permissions are already available.", ""
    opener = open_url or _default_open_url
    try:
        for _label, url in missing:
            opener(url)
    except Exception as exc:  # noqa: BLE001 - users get an actionable but safe failure
        return "", f"Unable to open macOS privacy settings ({type(exc).__name__})."
    return "Opened macOS privacy settings for " + " and ".join(label for label, _url in missing) + ".", ""


class ComputerStateEmitter:
    """Convert Computer Use tool outcomes into redacted, ordered TUI events."""

    def __init__(
        self,
        manager: object | None,
        emit: Callable[[dict[str, object]], None],
        *,
        platform_name: str | None = None,
    ) -> None:
        self._manager = manager
        self._emit = emit
        self._platform_name = platform_name or sys.platform
        self._applications: dict[str, str] = {}
        self._control = "inactive"
        self._runtime_application = ""

    def observe_catalog(self, payload: Mapping[str, object]) -> None:
        apps = payload.get("apps")
        if not isinstance(apps, list):
            return
        for app in apps[:100]:
            if not isinstance(app, Mapping):
                continue
            app_ref = app.get("app_ref")
            label = _safe_application(app.get("name"))
            if isinstance(app_ref, str) and label:
                self._applications[app_ref[:256]] = label

    def emit(
        self,
        state: Mapping[str, object] | None = None,
        *,
        status: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        target = getattr(self._manager, "target", None)
        app_ref = getattr(target, "app_ref", "") if target is not None else ""
        application = self._applications.get(app_ref, "") if isinstance(app_ref, str) else ""
        if state is None:
            state = computer_state(
                self._manager,
                platform_name=self._platform_name,
                status=status,
                application=application,
            )
        elif application and state.get("active") is True and "application" not in state:
            state = {**state, "application": application}
        active = state.get("active") is True
        handed_off = active and state.get("handed_off") is True
        control = self._control
        if not active:
            self._control = "inactive"
            self._runtime_application = ""
        elif handed_off:
            control = "user_control"
        elif self._control in {"foreground_takeover", "paused"}:
            control = self._control
        else:
            raw_control = state.get("control")
            control = raw_control if raw_control in _COMPUTER_CONTROLS else "background"
            if control in {"inactive", "user_control"}:
                control = "background"
            self._control = control
        if not handed_off:
            control = self._control

        # Keep replayable TUI events smaller than command output: private
        # helper/session metadata can never enter the JSON event stream.
        event = {
            "type": "computer_state",
            "active": state["active"],
            "handed_off": state["handed_off"],
            "control": control,
            "permission": state["permission"],
        }
        safe_application = _safe_application(state.get("application")) or self._runtime_application
        if active and safe_application:
            event["application"] = safe_application
        self._emit(event)
        return event

    def observe_tool_result(self, name: str, _args: dict[str, object], result: dict[str, object], _tool: object) -> None:
        if not name.startswith("computer_"):
            return
        payload: Mapping[str, object] | None = None
        raw = result.get("fresh_output") or result.get("output")
        if isinstance(raw, str):
            try:
                decoded = json.loads(raw)
            except json.JSONDecodeError:
                decoded = None
            if isinstance(decoded, Mapping):
                payload = decoded
        if name == "computer_apps" and payload is not None:
            self.observe_catalog(payload)
        if name in {"computer_focus", "computer_resume"}:
            self._control = "background"
        status = payload if name == "computer_status" and payload is not None else None
        self.emit(status=status)

    def observe_tool_error(self, name: str, _args: dict[str, object], _error: str, _tool: object) -> None:
        if name.startswith("computer_"):
            self.emit()

    def observe_session_end(self, _session_id: str, _reason: str) -> None:
        """Emit after runtime's registered session teardown hook has run."""
        self.emit()

    def observe_runtime_event(self, event: Mapping[str, object]) -> None:
        """Project bounded Task 5 audit events into immediate public UI state."""
        event_type = event.get("type")
        if event_type == "foreground_takeover_begin":
            self._control = "foreground_takeover"
        elif event_type in {"foreground_takeover_user_activity_paused", "user_activity_paused"}:
            self._control = "paused"
        elif event_type == "foreground_takeover_end" and self._control != "paused":
            self._control = "background"
        elif event_type not in {
            "foreground_takeover_focus_restored",
            "foreground_takeover_focus_preserved",
            "foreground_takeover_restore_failed",
        }:
            return
        application = _safe_application(event.get("application"))
        if application:
            self._runtime_application = application
        self.emit()


__all__ = [
    "ComputerStateEmitter",
    "approval_choices",
    "computer_state",
    "execute_computer_command",
    "format_computer_state",
]
