"""Temporary application state for evaluation runs, plus a fail-closed path guard.

Every state location Astra reads from the environment is pointed at a private
directory, embeddings are disabled, and optional external memory is switched
off. ``PathGuard`` observes file and SQLite opens through a Python audit hook
and refuses any access to installation state outside the evaluation root
(``.astra``/``.sessions`` directories, ``.env`` files, per-user Astra folders).
"""

from __future__ import annotations

import contextlib
import os
import sys
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

# Environment variable -> path relative to the evaluation root.
STATE_PATHS: dict[str, str] = {
    "ASTRA_HOME": "home",
    "AGENT_SESSION_DIR": "home/sessions",
    "AGENT_MEMORY_PATH": "home/memory.db",
    "ASTRA_SESSION_RECALL_DB": "home/sessions.db",
    "ASTRA_CONTEXT_INDEX_SESSIONS_DB": "home/sessions.db",
    "AGENT_TASK_DB": "home/tasks.db",
    "ASTRA_CONTEXT_INDEX_VECTORS_DB": "home/context-vectors.db",
    "ASTRA_CONTEXT_INDEX_FEEDBACK_DB": "home/context-index-feedback.db",
    "ASTRA_CONTEXT_INDEX_ACTIVITY_DB": "home/activity.db",
    "ASTRA_ACTIVITY_DB": "home/activity.db",
    "ASTRA_ACTIVITY_LOG_DIR": "home/activity-logs",
    "ASTRA_ACTIVITY_ROOT": "home/activity",
    "ASTRA_EMBEDDING_RUNTIME_DIR": "home/embedding-runtime",
    "AGENT_SETTINGS_PATH": "home/settings.json",
    "AGENT_SKILLS_PATH": "home/skills",
    "AGENT_LEARNING_PATH": "home/learning.db",
    "AGENT_USER_MODELS_FILE": "home/models.json",
    "AGENT_LOG_DIR": "home/logs",
    "AGENT_MCP_CONFIG": "home/mcp.json",
    "AGENT_FILESYSTEM_CONFIG": "home/filesystem.json",
    "AGENT_CONNECTIONS_DIR": "home/connections",
    "AGENT_MODEL_CACHE_DIR": "home/model-cache",
    "AGENT_BROWSER_DB": "home/browser.db",
    "AGENT_BROWSER_RUNTIME_DIR": "home/browser-runtime",
    "ASTRA_APPROVAL_DB": "home/approvals.db",
    "ASTRA_EVENT_DB": "home/events.db",
    "ASTRA_PEER_DB": "home/peers.db",
    "TOOL_RESULT_DIR": "home/tool-results",
    "WEB_EXTRACT_CACHE_DIR": "home/web-cache",
    "ASTRA_COMPUTER_CACHE_ROOT": "home/computer-cache",
    # Never read a real .env: point the loader at a file that does not exist.
    "ASTRA_ENV_FILE": "home/absent.env",
    "SANDBOX_WORKDIR": "workspace",
    "ASTRA_WORKSPACE": "workspace",
}
FLAGS: dict[str, str] = {
    "ASTRA_CONTEXT_INDEX_EMBEDDING": "off",
    "HINDSIGHT_ENABLED": "0",
    "MEMORY_AUTO_RETAIN": "0",
    "LEARNING_REVIEW_AUTO": "0",
    "ASTRA_GUI_DISABLE_APPSHOT": "1",
}
# Process overrides that would change prompt assembly or budgets.
CLEARED = (
    "AGENT_SESSION",
    "AGENT_PERSONA",
    "CONTEXT_MAX_PROMPT_TOKENS",
    "ASTRA_TURN_TIMEOUT_SECONDS",
    "MEMORY_RETENTION_MODE",
    "TIMING_LOG_ALL",
    "ASTRA_TRUSTED_PROJECTS",
    "ASTRA_PROJECT_TRUST",
    "ASTRA_EMBEDDING_BACKEND",
    "ASTRA_EMBEDDING_BASE_URL",
    "ASTRA_EMBEDDING_MODEL",
)


@dataclass(frozen=True)
class StatePaths:
    root: Path
    home: Path
    workspace: Path
    sessions: Path
    memory_db: Path
    session_recall_db: Path
    task_db: Path


_FORBIDDEN_NAMES = frozenset({".astra", ".sessions"})
_WATCHED_EVENTS = frozenset({
    "open", "sqlite3.connect", "os.listdir", "os.scandir", "os.mkdir", "os.remove",
    "os.rmdir", "os.rename", "os.replace", "shutil.copyfile", "shutil.rmtree",
})
_guards: list["PathGuard"] = []
_hook_installed = False
_local = threading.local()


def _within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


@dataclass
class PathGuard:
    """Refuse file access to real Astra state while an evaluation runs."""

    allowed_root: Path
    strict: bool = True
    violations: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._allowed = os.path.realpath(self.allowed_root)
        home = os.path.realpath(os.path.expanduser("~"))
        self._forbidden_roots = tuple(os.path.join(home, part) for part in (
            ".astra",
            os.path.join("Library", "Application Support", "Astra"),
            os.path.join(".cache", "astra"),
            os.path.join(".local", "share", "astra"),
            os.path.join("AppData", "Local", "Astra"),
            os.path.join(".config", "hermes"),
            os.path.join(".codex", "memories"),
        ))

    def forbidden(self, raw: object) -> str:
        if isinstance(raw, int) or raw is None:
            return ""
        try:
            text = os.fsdecode(os.fspath(raw))  # type: ignore[arg-type]
        except TypeError:
            return ""
        if not text or text == ":memory:" or text.startswith("file::memory:"):
            return ""
        if text.startswith("file:"):
            text = text[5:].split("?", 1)[0]
        real = os.path.realpath(text)
        if _within(real, self._allowed):
            return ""
        parts = Path(real).parts
        name = parts[-1] if parts else ""
        if _FORBIDDEN_NAMES & set(parts) or name == ".env" or name.startswith(".env."):
            return real
        if any(_within(real, root) for root in self._forbidden_roots):
            return real
        return ""

    def check(self, event: str, args: tuple) -> None:
        candidates = args[:2] if event in {"os.rename", "os.replace", "shutil.copyfile"} else args[:1]
        for candidate in candidates:
            path = self.forbidden(candidate)
            if path:
                self.violations.append(f"{event}: {path}")
                if self.strict:
                    raise PermissionError(f"memory evaluation isolation refused {event} on {path}")


def _audit(event: str, args: tuple) -> None:
    if not _guards or event not in _WATCHED_EVENTS or getattr(_local, "busy", False):
        return
    _local.busy = True
    try:
        for guard in tuple(_guards):
            guard.check(event, args)
    finally:
        _local.busy = False


@contextlib.contextmanager
def path_guard(allowed_root: Path, *, strict: bool = True) -> Iterator[PathGuard]:
    """Activate a guard for the duration of the block (audit hooks are permanent)."""
    global _hook_installed
    if not _hook_installed:
        sys.addaudithook(_audit)
        _hook_installed = True
    guard = PathGuard(Path(allowed_root), strict=strict)
    _guards.append(guard)
    try:
        yield guard
    finally:
        _guards.remove(guard)


def state_environment(root: Path) -> dict[str, str]:
    values = {name: str(root / relative) for name, relative in STATE_PATHS.items()}
    values.update(FLAGS)
    return values


@contextlib.contextmanager
def isolated_state(root: Path, *, chdir: bool = True) -> Iterator[StatePaths]:
    """Point every Astra state path at ``root`` and restore the environment after."""
    root = Path(root).resolve()
    paths = StatePaths(
        root=root,
        home=root / "home",
        workspace=root / "workspace",
        sessions=root / "home" / "sessions",
        memory_db=root / "home" / "memory.db",
        session_recall_db=root / "home" / "sessions.db",
        task_db=root / "home" / "tasks.db",
    )
    for directory in (paths.home, paths.workspace, paths.sessions):
        directory.mkdir(parents=True, exist_ok=True)
    values = state_environment(root)
    saved = {name: os.environ.get(name) for name in (*values, *CLEARED)}
    try:
        for name in CLEARED:
            os.environ.pop(name, None)
        os.environ.update(values)
        with contextlib.chdir(paths.workspace) if chdir else contextlib.nullcontext():
            yield paths
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def resolved_state_locations() -> dict[str, Path]:
    """Where the runtime itself resolves its state under the current environment."""
    from agent.runtime.context_index.vector_index import default_vectors_db_path
    from agent.runtime.memory import default_memory_path
    from agent.runtime.paths import sessions_dir, state_dir
    from agent.runtime.session_recall import configured_session_recall_path
    from agent.runtime.skills import default_skills_path
    from agent.runtime.task_store import task_db_path

    return {
        "state_dir": state_dir(),
        "sessions_dir": sessions_dir(),
        "memory_db": default_memory_path(),
        "session_recall_db": configured_session_recall_path(),
        "task_db": task_db_path(),
        "vectors_db": default_vectors_db_path(),
        "skills": default_skills_path(),
    }


__all__ = [
    "PathGuard",
    "StatePaths",
    "isolated_state",
    "path_guard",
    "resolved_state_locations",
    "state_environment",
]
