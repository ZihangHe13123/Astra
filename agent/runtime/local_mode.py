"""A single optional installation-local mode. No mode is bundled here."""

from __future__ import annotations

import importlib.util
import os
import re
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from .paths import state_path
from .session_store import SessionStore

if TYPE_CHECKING:
    from .react import ReActAgent
    from agent.ui.session_ownership import SessionLease

API_VERSION = 1
_COMMAND = re.compile(r"/[a-z][a-z0-9_-]{0,31}\Z")
_NAMESPACE = re.compile(r"[a-z][a-z0-9_-]{0,47}\Z")
# Local commands must never shadow a built-in route, including frontend routes.
_RESERVED = frozenset("image bar minimal sip reset compress undo retry think model models mode connect persona search tool gallery memory skills learn tools computer conclave appshot tasks budget resume cancel goal today session handoff theme timeline health doctor diagnostics maintenance sandbox vision-tiles context-index mcp yolo permissions reload reconnect help exit quit".split())


class ModeController(Protocol):
    @property
    def active(self) -> bool: ...
    @property
    def session_name(self) -> str: ...
    def enter(self, path: Path) -> bool: ...
    def switch(self, path: Path) -> None: ...
    def leave(self) -> bool: ...
    def undo_last_reply(self) -> tuple[bool, str]: ...
    def undo_last_exchanges(self, count: int) -> tuple[bool, str]: ...
    def take_last_request(self) -> tuple[bool, str, str]: ...


class LocalMode:
    """Host metadata and session routing for a trusted local controller."""

    def __init__(
        self, controller: ModeController | None = None, *, command: str = "",
        label: str = "Local", description: str = "", namespace: str = "",
        status_template: str = "", ui: dict[str, str] | None = None,
    ) -> None:
        if controller is not None:
            if not _COMMAND.fullmatch(command) or command[1:] in _RESERVED:
                raise ValueError("Invalid or reserved local mode command")
            if not _NAMESPACE.fullmatch(namespace) or namespace in {"bar", "minimal", "exports"}:
                raise ValueError("Invalid or reserved local mode namespace")
            if not label.strip() or not description.strip():
                raise ValueError("Local mode needs a label and description")
            if not isinstance(controller.active, bool) or not isinstance(controller.session_name, str):
                raise ValueError("Invalid local mode state")
            for method in ("enter", "switch", "leave", "undo_last_reply", "undo_last_exchanges", "take_last_request"):
                if not callable(getattr(controller, method, None)):
                    raise ValueError("Incomplete local mode controller")
        self.controller = controller
        # Extensions predate context-level ownership and may create their own
        # AgentContext. Keep the shared writer lease in the host so they cannot
        # accidentally bypass multi-window/TUI/GUI session exclusion.
        self._session_lease: SessionLease | None = None
        self.command = command
        self.label = label
        self.description = description
        self.namespace = namespace
        self.status_template = status_template
        self.ui = {
            "brand": f"{label.upper()} MODE", "exitLabel": f"leave {label.lower()}",
            "headerTag": "PRIVATE · NO TOOLS · NO MEMORY",
            "idle": f"{label.upper()} OPEN", "busy": f"{label.upper()}…",
            "placeholder": "type a message", **(ui or {}),
        }
        for value in (label, description, *self.ui.values()):
            if not isinstance(value, str) or len(value) > 240 or any(ord(c) < 32 or ord(c) == 127 for c in value):
                raise ValueError("Invalid local mode display text")

    @property
    def enabled(self) -> bool:
        return self.controller is not None

    @property
    def active(self) -> bool:
        return self.controller is not None and self.controller.active

    @property
    def session_name(self) -> str:
        return self.controller.session_name if self.controller is not None else ""

    @property
    def tool_name(self) -> str:
        return self.command.lstrip("/")

    def matches(self, command: str) -> bool:
        return self.enabled and (command == self.command or command.startswith(self.command + " "))

    def _controller(self) -> ModeController:
        if self.controller is None:
            raise RuntimeError("No local mode is installed")
        return self.controller

    def enter(self, path: Path) -> bool:
        from agent.ui.session_ownership import claim_session

        controller = self._controller()
        lease = claim_session(path)
        try:
            entered = controller.enter(path)
            if entered:
                self._session_lease = lease
            return entered
        finally:
            del lease

    def switch(self, path: Path) -> None:
        from agent.ui.session_ownership import claim_session

        controller = self._controller()
        lease = claim_session(path)
        try:
            controller.switch(path)
            self._session_lease = lease
        finally:
            del lease

    def leave(self) -> bool:
        left = self._controller().leave()
        if left:
            self._session_lease = None
        return left

    def undo_last_reply(self) -> tuple[bool, str]:
        return self._controller().undo_last_reply()

    def undo_last_exchanges(self, count: int) -> tuple[bool, str]:
        return self._controller().undo_last_exchanges(count)

    def take_last_request(self) -> tuple[bool, str, str]:
        return self._controller().take_last_request()

    def session_path(self, name: str) -> Path:
        from agent.cli.sessions import SESSION_DIR, SessionNameError, validate_session_name

        self._controller()
        root = (SESSION_DIR / self.namespace).resolve()
        path = (root / f"{validate_session_name(name)}.json").resolve()
        if path.parent != root:
            raise SessionNameError("Local session path escapes its directory")
        return path

    def session_exists(self, name: str) -> bool:
        return SessionStore(self.session_path(name)).exists

    def new_session_name(self) -> str:
        base = f"{self.namespace}_{time.strftime('%Y%m%d_%H%M%S')}"
        name, suffix = base, 2
        while self.session_exists(name):
            name, suffix = f"{base}_{suffix}", suffix + 1
        return name

    def list_sessions(self) -> list[str]:
        if not self.enabled:
            return []
        root = self.session_path("default").parent
        names: set[str] = set()
        for path in root.glob("*.json"):
            if not path.name.endswith((".snapshot.json", ".header.json")):
                names.add(path.stem)
        names.update(p.stem for p in root.glob("*.jsonl"))
        names.update(p.name.removesuffix(".snapshot.json") for p in root.glob("*.snapshot.json"))

        def modified_at(name: str) -> float:
            paths = SessionStore(self.session_path(name)).related_paths()
            return max((p.stat().st_mtime for p in paths if p.exists()), default=0.0)

        return sorted(names, key=modified_at, reverse=True)

    def session_msg_count(self, name: str) -> int:
        store = SessionStore(self.session_path(name))
        if not store.exists:
            return 0
        try:
            return len(store.load(readonly=True).get("messages", []))
        except KeyError:
            return 0

    def menu_event(self) -> dict:
        return {
            "type": "local_mode_info",
            "definition": {"command": self.command, "label": self.label,
                           "description": self.description, "ui": self.ui} if self.enabled else None,
            "sessions": [{"name": name, "messages": self.session_msg_count(name),
                          "current": name == self.session_name} for name in self.list_sessions()],
        }


def local_mode_path() -> Path:
    configured = os.environ.get("ASTRA_LOCAL_MODE_FILE", "").strip()
    return Path(configured).expanduser().resolve() if configured else state_path("local_mode.py")


def load_local_mode(agent: ReActAgent) -> LocalMode:
    """Load trusted local code once at backend startup; never search a project."""
    path = local_mode_path()
    if not path.exists():
        return LocalMode()
    name = "_astra_installation_local_mode"
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise ValueError
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        if getattr(module, "API_VERSION", None) != API_VERSION:
            raise ValueError
        mode = module.create_mode(agent)
        if not isinstance(mode, LocalMode) or not mode.enabled:
            raise ValueError
        return mode
    except Exception:
        sys.modules.pop(name, None)
        raise ValueError("Cannot load local mode; check ASTRA_LOCAL_MODE_FILE or the installation's local_mode.py.") from None
