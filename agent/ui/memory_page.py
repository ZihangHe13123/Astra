"""The desktop's memory page: what Astra keeps about the person, and the person's own changes to it.

``memory_overview`` reads; ``main`` applies one change the person made on the page (no approval:
it is their own memory) and is run as ``python -m agent.ui.memory_page ACTION ...``.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from agent.runtime.memory import CORE_FILES, MemoryStore, default_memory_path

SAVED_LIMIT = 500
_CORE_ORDER = ("user", "memory")


def _store() -> MemoryStore | None:
    return MemoryStore() if default_memory_path().is_file() else None


def memory_overview(*, history: bool = False) -> dict[str, Any]:
    """Core entries with their room, saved memories (and with ``history`` the replaced and forgotten
    ones), how often each was used, and the consolidation proposals waiting."""
    store = _store()
    if store is None:
        return {"core": [{"scope": scope, "file": CORE_FILES[scope], "entries": [], "usage": None}
                         for scope in _CORE_ORDER], "saved": [], "proposals": []}
    core = [{"scope": scope, "file": CORE_FILES[scope], "entries": store.list_core(scope),
             "usage": store.core_usage(scope)} for scope in _CORE_ORDER]
    usage = store.record_store.usage()
    saved = []
    records = store.record_store.list_records(status="" if history else "active", limit=5000)
    records.sort(key=lambda record: (record.valid_from or record.created_at, record.record_id), reverse=True)
    for record in records[:SAVED_LIMIT]:
        recalls, reads, last_used = usage.get(record.record_id, (0, 0, ""))
        saved.append({
            "id": record.record_id, "summary": store.memory_summary(record), "content": record.content,
            "kind": record.kind, "status": record.status, "scope": str(record.metadata.get("scope") or "global"),
            "saved_by_astra": record.metadata.get("origin") == "memory_tool",
            "created_at": record.valid_from or record.created_at, "valid_until": record.valid_until,
            "supersedes": record.supersedes_id, "source_session": record.source_session_id,
            "uses": recalls + reads, "last_used_at": last_used,
        })
    proposals = [{"id": item["id"], "action": item.get("action", ""), "summary": item.get("summary", ""),
                  "content": item.get("content", ""), "previous": item.get("previous", ""),
                  "memory_id": item.get("memory_id", ""), "reason": item.get("reason", ""),
                  "evidence": [{"date": ref.get("date", ""), "text": ref.get("text", "")} for ref in item.get("evidence", [])]}
                 for item in store.record_store.proposals()]
    return {"core": core, "saved": saved, "proposals": proposals}


def apply(action: str, args: list[str]) -> str:
    """One change from the page; returns what changed, or raises ValueError."""
    from agent.cli.memory_commands import execute_memory_command

    store = MemoryStore()
    if action == "core-add" and len(args) == 2 and args[0] in CORE_FILES:
        item = store.add_core(args[0], args[1])
        return f"已记在 {CORE_FILES[args[0]]}：{item['content']}"
    if action == "core-edit" and len(args) == 3:
        item = store.replace_core(args[0], expected_content=args[1], content=args[2])
        return f"已修改：{item['content']}"
    if action == "core-remove" and len(args) == 1:
        if not store.remove_core(args[0]):
            raise ValueError("这条核心记忆已经不在了。")
        return "已删除这条核心记忆。"
    if action in {"forget", "correct", "accept", "dismiss"} and args:
        output, error = execute_memory_command(store, [action, *args], session_id="desktop-memory-page")
        if error:
            raise ValueError(error)
        return output
    raise ValueError("Unsupported memory change.")


def main(argv: list[str] | None = None) -> int:
    from pathlib import Path

    from agent.cli.environment import load_project_env

    load_project_env(Path(__file__).resolve().parents[2])
    arguments = sys.argv[1:] if argv is None else argv
    try:
        message = apply(arguments[0] if arguments else "", arguments[1:])
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps({"ok": True, "message": message}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
