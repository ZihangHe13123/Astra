"""Bounded, read-only desktop projection of delegate lifecycle sidecars.

Only lifecycle metadata and public reports enter this projection. Assistant
messages, reasoning, tool arguments and tool output are never returned. Reading
history does not recover, resume, cancel or otherwise mutate a worker.
"""

from __future__ import annotations

import json
import math
import os
import stat
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, TypeGuard

from agent.runtime.session_store import SessionStore

_TERMINAL = frozenset({"completed", "failed", "cancelled", "timed_out", "partial", "interrupted"})
_STATUSES = _TERMINAL | {"queued", "running", "idle"}
_TEXT_LIMITS = {
    "process_id": 128, "task_id": 128, "session_id": 256,
    "goal": 400, "worker_type": 64, "current_tool": 128,
    "result": 4096, "error": 1024, "team_id": 128, "agent_id": 128,
}
_NUMBER_FIELDS = {"started_at", "updated_at", "completed_at", "duration_ms", "turns_used", "max_turns"}
_MAX_HISTORY_BYTES = 2 * 1024 * 1024
_MAX_LINE_BYTES = 256 * 1024
_MAX_WORKERS = 200
_CACHE_SIZE = 8
_CACHE: OrderedDict[str, tuple[tuple[int, ...], list[dict[str, Any]]]] = OrderedDict()
_CACHE_LOCK = threading.Lock()


def _number(value: Any) -> TypeGuard[int | float]:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and 0 <= value <= 2**53 - 1 and math.isfinite(value))


def sanitize_delegate_event(event: dict[str, Any]) -> dict[str, Any]:
    """Return a flat display-safe status event, or {} for invalid identity/state.

    An idle result is an explicit public episode report supplied by the runtime;
    ordinary running progress cannot introduce a result. Callers must not map
    assistant content or tool output to this field.
    """
    if not isinstance(event, dict):
        return {}
    process_id, status_value = event.get("process_id"), event.get("status")
    if (not isinstance(process_id, str) or not process_id.strip()
            or len(process_id) > _TEXT_LIMITS["process_id"]
            or not isinstance(status_value, str) or status_value not in _STATUSES):
        return {}
    result: dict[str, Any] = {"type": "delegate_status", "status": status_value}
    for key, limit in _TEXT_LIMITS.items():
        if key == "result" and status_value not in _TERMINAL | {"idle"}:
            continue
        value = event.get(key)
        if not isinstance(value, str):
            continue
        if key == "result" and value == "(no result — subagent did not produce output)":
            value = ""
        result[key] = value[:limit]
        if len(value) > limit or event.get(f"{key}_truncated") is True:
            result[f"{key}_truncated"] = True
    for key in _NUMBER_FIELDS:
        value = event.get(key)
        if _number(value) and (key not in {"turns_used", "max_turns"} or int(value) == value):
            result[key] = value
    for key in ("partial", "history_truncated"):
        if isinstance(event.get(key), bool):
            result[key] = event[key]
    if status_value == "partial":
        result["partial"] = True
    return result


def _signature(info: os.stat_result) -> tuple[int, ...]:
    # Inode + ctime also invalidate same-size atomic replacement and rewrites
    # whose mtime was restored. Nothing is cached on disk beside the source.
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _tail(path: Path) -> tuple[bytes, bool, tuple[int, ...] | None]:
    with path.open("rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode):
            return b"", False, None
        start = max(0, info.st_size - _MAX_HISTORY_BYTES)
        source.seek(max(0, start - 1))
        raw = source.read(min(info.st_size, _MAX_HISTORY_BYTES + bool(start)))
        if start:
            # Keep a complete first line when the boundary follows a newline;
            # otherwise discard that partial record without decoding it.
            preceding, raw = raw[:1], raw[1:]
            if preceding != b"\n":
                boundary = raw.find(b"\n")
                raw = raw[boundary + 1:] if boundary >= 0 else b""
        signature = _signature(info)
        if _signature(os.fstat(source.fileno())) != signature:
            signature = None  # A concurrent append/rewrite must be reread next time.
        return raw, bool(start), signature


def _legacy_status(event: dict[str, Any]) -> str:
    if event.get("type") == "terminal":
        value = event.get("status")
        return value if isinstance(value, str) and value in _TERMINAL else "interrupted"
    if event.get("state") == "terminal":
        reason = event.get("completion_reason")
        if isinstance(reason, str) and reason in _TERMINAL:
            return reason
        if reason == "reported":
            return "completed"
        if reason in ("active_timeout", "queue_timeout", "keep_alive_lifetime"):
            return "timed_out"
        if reason in ("startup_failed", "execution_failed"):
            return "failed"
        return "interrupted"
    return "idle" if event.get("state") == "idle" else "running"


def _project_record(event: Any, session_id: str) -> dict[str, Any]:
    if not isinstance(event, dict):
        return {}
    kind = event.get("type")
    if kind == "delegate_status":
        projected = sanitize_delegate_event(event)
        if projected:
            projected.setdefault("session_id", session_id)
            if "updated_at" not in projected and _number(event.get("recorded_at")):
                projected["updated_at"] = event["recorded_at"]
        return projected
    if kind not in ("started", "lifecycle", "assistant", "tool", "terminal"):
        return {}

    # Never carry raw content, reasoning, arguments, tool_calls, output or nested
    # provider data beyond this point, including on malformed/unknown records.
    candidate = {key: event[key] for key in _TEXT_LIMITS if key in event and key not in {"result", "error"}}
    candidate.update(type="delegate_status", status=_legacy_status(event))
    candidate.setdefault("session_id", session_id)
    recorded_at = event.get("recorded_at", event.get("observed_at"))
    if _number(recorded_at):
        candidate["updated_at"] = recorded_at
        if kind == "started":
            candidate["started_at"] = recorded_at
        if candidate["status"] in _TERMINAL:
            candidate["completed_at"] = recorded_at
    lifecycle = event if kind == "lifecycle" else event.get("lifecycle")
    if isinstance(lifecycle, dict):
        if _number(lifecycle.get("started_at")):
            candidate["started_at"] = lifecycle["started_at"]
        limits = lifecycle.get("limits")
        if isinstance(limits, dict):
            candidate["max_turns"] = limits.get("max_turns")
    if kind in ("assistant", "tool"):
        candidate["turns_used"] = event.get("turn")
    if kind == "terminal":
        for key in ("result", "error", "turns_used", "max_turns", "partial"):
            if key in event:
                candidate[key] = event[key]
    projected = sanitize_delegate_event(candidate)
    if projected:
        projected["_record_type"] = kind
    return projected


def _reduce(raw: bytes, session_id: str, truncated: bool) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    selected: set[str] = set()
    # Select workers by their most recent occurrence, but replay each selected
    # worker chronologically to retain start metadata within the bounded tail.
    for line in reversed(raw.splitlines()):
        if len(line) > _MAX_LINE_BYTES:
            truncated = True
            continue
        try:
            event = _project_record(json.loads(line), session_id)
        except (ValueError, UnicodeError, RecursionError):
            continue
        if not event:
            continue
        process_id = event["process_id"]
        if process_id not in selected:
            if len(selected) >= _MAX_WORKERS:
                truncated = True
                continue
            selected.add(process_id)
        records.append(event)

    workers: OrderedDict[str, dict[str, Any]] = OrderedDict()
    for event in reversed(records):
        process_id = event["process_id"]
        current = workers.setdefault(process_id, {})
        kind = event.pop("_record_type", "delegate_status")
        if kind != "lifecycle" or event["status"] != "idle":
            # A new episode must not inherit a previous episode's final report.
            for key in ("result", "result_truncated", "error", "error_truncated", "partial", "completed_at"):
                current.pop(key, None)
        current.update(event)
        workers.move_to_end(process_id)

    result = []
    for current in workers.values():
        if current["status"] not in _TERMINAL:
            current["status"] = "interrupted"
            current.pop("completed_at", None)
        current["current_tool"] = ""
        started_at, updated_at = current.get("started_at"), current.get("updated_at")
        if _number(started_at) and _number(updated_at):
            current["duration_ms"] = round(max(0, updated_at - started_at) * 1000)
        if truncated:
            current["history_truncated"] = True
        result.append(sanitize_delegate_event(current))
    return result


def delegate_history(store: SessionStore) -> list[dict[str, Any]]:
    """Recover up to 200 recent delegates from at most the last 2 MiB.

    Historical nonterminal states are interrupted, never proof of a live worker.
    Every returned item has history_truncated when the byte/worker/line limit
    omitted history. An absent, unreadable or entirely invalid tail returns [].
    """
    path = store.subagent_path
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            return []
        signature, cache_key = _signature(info), str(path.absolute())
        with _CACHE_LOCK:
            cached = _CACHE.get(cache_key)
            if cached is not None and cached[0] == signature:
                _CACHE.move_to_end(cache_key)
                return [dict(item) for item in cached[1]]
        raw, truncated, read_signature = _tail(path)
        result = _reduce(raw, store.legacy_path.stem, truncated)
        if read_signature is not None and _signature(path.stat()) == read_signature:
            with _CACHE_LOCK:
                _CACHE[cache_key] = (read_signature, result)
                _CACHE.move_to_end(cache_key)
                while len(_CACHE) > _CACHE_SIZE:
                    _CACHE.popitem(last=False)
        return [dict(item) for item in result]
    except OSError:
        return []
