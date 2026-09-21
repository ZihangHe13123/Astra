"""Explicit desktop session actions, run in a short-lived local process.

The host owns the save dialog and delete confirmation. This service accepts a
session name and namespace only; it never writes a renderer-supplied path.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from agent.runtime.session_store import SessionStore
from agent.ui.history_index import discard_history_index
from agent.ui.session_ownership import claim_session


def _session_path(name: str, mode: str) -> Path:
    # Import after main() loads the installation environment: sessions fixes
    # its data root at import time, including AGENT_SESSION_DIR overrides.
    from agent.cli import sessions

    resolver = {
        "work": sessions.session_path,
        "minimal": sessions.minimal_session_path,
        "bar": sessions.bar_session_path,
    }.get(mode)
    if resolver is None:
        raise ValueError("Unknown session mode")
    return resolver(name)


@contextmanager
def _clear_working_state(name: str, mode: str) -> Iterator[None]:
    """Clear only existing work-session state; never initialize core memory."""
    from agent.runtime.memory import default_memory_path

    path = default_memory_path()
    if mode != "work" or not path.is_file():
        yield
        return
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=rw", uri=True, timeout=10)
    try:
        if db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='working_memories'"
        ).fetchone():
            # Same targeted deletion as MemoryStore.clear_working. Keep the
            # transaction open until file cleanup succeeds, so a refusal does
            # not discard the session's working state.
            db.execute("DELETE FROM working_memories WHERE session_id=?", (name,))
        yield
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def act(request: object) -> dict:
    if not isinstance(request, dict):
        raise ValueError("Session action must be an object")
    action = request.get("action")
    if action not in ("export", "delete"):
        raise ValueError("Unknown session action")
    name, mode = request.get("name"), request.get("mode", "work")
    if not isinstance(name, str) or not isinstance(mode, str):
        raise ValueError("Session name and mode must be strings")
    name = name.strip()
    path = _session_path(name, mode)
    store = SessionStore(path)
    if not store.exists:
        raise FileNotFoundError(f"Session '{name}' not found")
    result = {"name": name, "mode": mode}
    if action == "export":
        return {**result, "markdown": store.markdown(name)}

    # This process is separate from all backend writers, including this GUI's
    # own backend. The host must close an idle target before calling delete.
    lease = claim_session(path)
    try:
        if not store.exists:
            raise FileNotFoundError(f"Session '{name}' not found")
        with _clear_working_state(name, mode):
            store.appshot_media.delete()
            for related in store.related_paths():
                if related.exists():
                    related.unlink()
            discard_history_index(store)
    finally:
        del lease
    return {**result, "deleted": True}


def main() -> None:
    from agent.cli.environment import load_project_env

    try:
        load_project_env(Path(__file__).resolve().parents[2])
        result = act(json.loads(sys.stdin.readline()))
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
