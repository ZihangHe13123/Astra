"""Local read models used by both desktop and terminal adapters. No inference."""

from __future__ import annotations

import json
import difflib
from dataclasses import asdict
from functools import lru_cache
from pathlib import Path

from agent.cli import sessions
from agent.runtime.session_store import SessionStore
from agent.ui.history_index import history_page
from agent.ui.delegates import delegate_history


@lru_cache(maxsize=1)
def command_catalog() -> list[dict]:
    return json.loads(Path(__file__).with_name("commands.json").read_text(encoding="utf-8"))


def _session_path(name: str, mode: str = "work") -> Path:
    resolver = {"work": sessions.session_path, "bar": sessions.bar_session_path,
                "minimal": sessions.minimal_session_path}.get(mode)
    if resolver is None:
        raise ValueError("Unknown session mode")
    return resolver(name)


def list_sessions() -> list[dict]:
    result = []
    for mode, names in (("work", sessions.list_sessions), ("bar", sessions.list_bar_sessions),
                        ("minimal", sessions.list_minimal_sessions)):
        for name in names():
            store = SessionStore(_session_path(name, mode))
            files = [p for p in (store.jsonl_path, store.legacy_path, store.snapshot_path) if p.exists()]
            result.append({"name": name, "mode": mode, "modified": max((p.stat().st_mtime for p in files), default=0)})
    return sorted(result, key=lambda s: s["modified"], reverse=True)


def history(name: str, mode: str = "work", *, before: int | None = None, limit: int = 200) -> dict:
    store = SessionStore(_session_path(name, mode))
    if not store.exists:
        raise FileNotFoundError("Session does not exist")
    result = history_page(store, before=before, limit=limit)
    for message in result["messages"]:
        message["id"] = f"{mode}:{name}:{message.pop('position')}"
    return {"session_id": name, "mode": mode, **result, "delegates": delegate_history(store)}


def changes(agent, turn: int = 1, index: int | None = None) -> dict:
    store = agent.turn_change_store()
    if store is None:
        return {"turns": [], "files": [], "available": False}
    listing = store.completed_turns()
    if not listing.ok:
        raise OSError("Change index unavailable")
    records = list(listing.records)
    if not records:
        return {"turns": [], "files": [], "available": True}
    if turn < 1 or turn > len(records):
        raise ValueError("Turn index out of range")
    record = records[turn - 1]
    manifest = store.manifest_for(record.turn_seq) if record.available and not record.empty else None
    entries = [*manifest.files, *manifest.unknown] if manifest else []
    confirmed_count = len(manifest.files) if manifest else 0
    result = {"turns": [asdict(r) for r in records], "turn": turn,
              "files": [{**asdict(e), "confirmed": i < confirmed_count} for i, e in enumerate(entries)],
              "available": bool(manifest), "record": asdict(record)}
    if index is not None:
        if index < 0 or index >= len(entries):
            raise ValueError("File index out of range")
        sides = store.load_sides_for(record.turn_seq, index)
        result["sides"] = preview_sides(sides, entries[index].compare)
    return result


def preview_sides(sides, compare: str) -> dict:
    """Bounded presentation of held snapshots; never read the live target."""
    maximum = 128 * 1024
    binary = any(value is not None and b"\x00" in value[:8192] for value in (sides.before, sides.after))
    try:
        text = [value.decode("utf-8") if value is not None else None for value in (sides.before, sides.after)]
    except UnicodeDecodeError:
        binary = True
        text = [None, None]
    truncated = any(value is not None and len(value) > maximum for value in (sides.before, sides.after))
    unified = None
    if not binary and not truncated and compare == "full" and "uncaptured" not in (sides.before_state, sides.after_state):
        before, after = [(value or "").splitlines() for value in text]
        if max(len(before), len(after)) <= 2000:
            unified = "\n".join(difflib.unified_diff(before, after, fromfile="before", tofile="after", n=3, lineterm=""))
            if all(value is not None for value in text) and (text[0] or "").endswith("\n") != (text[1] or "").endswith("\n"):
                unified += "\n末尾换行状态不同。"
    return {"before": None if binary or text[0] is None else text[0][:maximum],
            "after": None if binary or text[1] is None else text[1][:maximum],
            "before_state": sides.before_state, "after_state": sides.after_state,
            "binary": binary, "truncated": truncated, "unified": unified}


def query(request: dict, *, agent=None) -> object:
    method = request.get("method")
    params = request.get("params") or {}
    if not isinstance(params, dict):
        raise ValueError("Query params must be an object")
    if method == "commands":
        return command_catalog()
    if method == "sessions":
        return list_sessions()
    if method == "history":
        return history(str(params.get("name", "")), str(params.get("mode", "work")),
                       before=params.get("before"), limit=int(params.get("limit", 200)))
    if method == "changes" and agent is not None:
        return changes(agent, int(params.get("turn", 1)), params.get("index"))
    raise ValueError("Unknown UI query")


def main() -> None:
    import sys
    from agent.cli.environment import load_project_env
    load_project_env(Path(__file__).resolve().parents[2])
    # sessions resolves its root at import time; refresh it after loading the
    # chosen installation environment for direct source/debug launches too.
    from agent.runtime.paths import sessions_dir
    sessions.SESSION_DIR = sessions_dir(sessions.PROJECT_ROOT)
    sessions.SESSION_DEFAULT = sessions.SESSION_DIR / "default.json"
    serving = "--serve" in sys.argv[1:]
    for line in sys.stdin:
        request = None
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("Query must be an object")
            response = {"ok": True, "result": query(request)}
        except Exception as exc:
            response = {"ok": False, "error": str(exc)}
        if serving:
            response["id"] = request.get("id") if isinstance(request, dict) else None
        print(json.dumps(response, ensure_ascii=False), flush=True)
        if not serving:
            break


if __name__ == "__main__":
    main()
