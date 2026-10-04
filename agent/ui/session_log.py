"""Read-only inspection of effective canonical messages, not provider wire data.

The rebuildable history index supplies provenance and bounded raw excerpts. A
source reference is verified before navigation: positions alone cannot survive
compaction or replacement. No inspection call recovers or resumes a session.
"""
from __future__ import annotations

import hashlib
import re

from agent.runtime.session_store import SessionStore
from agent.runtime.message_source import message_source_ref as message_source_ref

MAX_RECORD_BYTES = 16 * 1024
MAX_RECORDS = 50



def checked_source_ref(value: object) -> dict:
    if (not isinstance(value, dict) or set(value) != {"index", "digest"}
            or type(value["index"]) is not int or not 0 <= value["index"] <= 2**53 - 1
            or not isinstance(value["digest"], str) or not re.fullmatch(r"[0-9a-f]{64}", value["digest"])):
        raise ValueError("Invalid message source reference")
    return value


def resolve_source(db, reference: dict) -> tuple[str, int | None]:
    row = db.execute("SELECT digest FROM raw_messages WHERE position=?", (reference["index"],)).fetchone()
    if row and row[0] == reference["digest"]:
        return "found", reference["index"]
    matches = db.execute("SELECT position FROM raw_messages WHERE digest=? LIMIT 2", (reference["digest"],)).fetchall()
    if len(matches) == 1:
        return "found", matches[0][0]
    return ("ambiguous" if matches else "stale"), None


def chat_source(db, index: int) -> tuple[dict | None, int | None]:
    row = db.execute("SELECT digest,chat_position FROM raw_messages WHERE position=?", (index,)).fetchone()
    if row and row[1] is not None:
        return {"index": index, "digest": row[0]}, row[1]
    # A tool outcome can point at its unique displayed invocation. Duplicate
    # call ids (legacy providers) are deliberately not disambiguated by order.
    matches = db.execute("SELECT DISTINCT r.position,r.digest,r.chat_position FROM raw_messages r JOIN calls_messages c ON c.position=r.position WHERE r.role='assistant' AND r.chat_position IS NOT NULL AND c.call_id IN (SELECT call_id FROM calls_messages WHERE position=?) LIMIT 2", (index,)).fetchall()
    if len(matches) == 1:
        return {"index": matches[0][0], "digest": matches[0][1]}, matches[0][2]
    return None, None


def session_log(store: SessionStore, *, before: int | None = None, limit: int = 25,
                source_ref: dict | None = None, call_id: str | None = None, branch_id: str | None = None) -> dict:
    """Return at most 50 records and 16 KiB per raw excerpt from one session."""
    from agent.ui.history_index import _database, _cache_path, _signature, check_query_branch, history_page

    if type(limit) is not int or not 1 <= limit <= MAX_RECORDS:
        raise ValueError("Log limit must be between 1 and 50")
    if before is not None and (type(before) is not int or not 0 <= before <= 2**53 - 1):
        raise ValueError("Invalid log cursor")
    if source_ref is not None:
        checked_source_ref(source_ref)
    if call_id is not None and (not isinstance(call_id, str) or not 1 <= len(call_id) <= 512):
        raise ValueError("Invalid tool call identity")
    if source_ref is not None and call_id is not None:
        raise ValueError("Use one log target at a time")
    if not store.exists:
        raise FileNotFoundError("Session does not exist")
    check_query_branch(store, branch_id)
    for _attempt in range(3):
        history_page(store, limit=1, branch_id=branch_id)
        with _database(_cache_path(store)) as db:
            db.execute("BEGIN")
            signature = _signature(store)
            metadata = db.execute("SELECT signature FROM metadata WHERE id=1").fetchone()
            if not metadata or metadata[0] != signature or not store.exists:
                continue
            total = db.execute("SELECT COALESCE(MAX(position)+1,0) FROM raw_messages").fetchone()[0]
            target_status = "none"
            target_index = None
            if source_ref is not None:
                target_status, target_index = resolve_source(db, source_ref)
            elif call_id is not None:
                matches = db.execute("SELECT r.position,r.role FROM raw_messages r JOIN calls_messages c ON c.position=r.position WHERE c.call_id=? ORDER BY r.position LIMIT 3", (call_id,)).fetchall()
                # Exactly one invocation plus one outcome is a linked pair. A
                # repeated provider id must never choose a convenient turn.
                if len(matches) == 1 or len(matches) == 2 and [row[1] for row in matches] == ["assistant", "tool"]:
                    target_status, target_index = "found", matches[0][0]
                else:
                    target_status = "ambiguous" if matches else "missing"
            if target_index is not None:
                start = max(0, target_index - limit // 2)
                end = min(total, start + limit)
            else:
                end = min(total, before) if before is not None else total
                start = max(0, end - limit)
            records = []
            if target_status not in {"ambiguous", "stale", "missing"}:
                for index, digest, raw, size, role, chat_position in db.execute(
                        "SELECT position,digest,payload,raw_bytes,role,chat_position FROM raw_messages WHERE position>=? AND position<? ORDER BY position", (start, end)):
                    calls = [row[0] for row in db.execute("SELECT call_id FROM calls_messages WHERE position=? LIMIT 100", (index,))]
                    chat_reference, chat_position = chat_source(db, index)
                    records.append({"source_ref": {"index": index, "digest": digest}, "role": role,
                                    "raw": raw.decode("utf-8", errors="ignore"), "raw_truncated": size > MAX_RECORD_BYTES,
                                    "raw_bytes": size, "tool_call_ids": calls, "chat_position": chat_position, "chat_source_ref": chat_reference})
            if signature != _signature(store):
                continue
            check_query_branch(store, branch_id)
            return {"records": records, "before": start, "has_more": start > 0,
                    "total": total, "revision": hashlib.sha256(signature.encode()).hexdigest(),
                    "target_status": target_status, "target_index": target_index, "branch_id": store.branch_id}
    raise OSError("Session changed while reading logs; retry the page")
