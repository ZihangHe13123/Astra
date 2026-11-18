"""Durable, redacted runtime event envelopes with cursor-based replay."""

from __future__ import annotations

from agent.runtime.paths import state_path
from agent.runtime.sqlite_wal import enable_wal

import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

from agent.runtime.approval_inbox import safe_approval_request

REPLAYABLE_EVENT_TYPES = frozenset(
    {
        "process_status",
        "agent_team",
        "tool_calls",
        "tool_progress",
        "tool_approval_request",
        "approval_resolved",
        "approval_response_rejected",
        "task_started",
        "task_status",
        "vision_preprocess",
        "done",
    }
)

_PROCESS_FIELDS = (
    "event",
    "process_id",
    "kind",
    "label",
    "task_id",
    "status",
    "started_at",
    "completed_at",
    "duration_ms",
    "exit_code",
    "output_chars",
    "stdout_chars",
    "stderr_chars",
    "artifact_path",
)
_PROGRESS_FIELDS = (
    "call_id",
    "name",
    "stage",
    "status",
    "current",
    "total",
    "percent",
    "unit",
)
_TASK_FIELDS = ("id", "status", "step_count", "resume_count", "updated_at")
_AGENT_TEAM_FIELDS = (
    "event",
    "kind",
    "team_kind",
    "group",
    "team_id",
    "name",
    "goal",
    "status",
    "lead_agent_id",
    "agent_id",
    "parent_agent_id",
    "role",
    "process_id",
    "sender_agent_id",
    "recipient_agent_ids",
    "message_kind",
    "message_count",
    "acknowledged",
    "team_task_id",
    "title",
    "owner_agent_id",
)


def event_db_path() -> Path:
    configured = os.getenv("ASTRA_EVENT_DB", "").strip()
    if configured:
        return Path(configured).expanduser()
    return state_path("events.db")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bounded_text(value: Any, limit: int = 2_000) -> str:
    text = str(value or "")
    return text if len(text) <= limit else f"{text[:limit]}…"


def _selected(source: Mapping[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {field: source[field] for field in fields if field in source}


def safe_replay_event(event: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return a replay-safe lifecycle event, never model text or raw tool data."""
    event_type = str(event.get("type") or "")
    if event_type not in REPLAYABLE_EVENT_TYPES:
        return None
    if event_type == "process_status":
        return {"type": event_type, **_selected(event, _PROCESS_FIELDS)}
    if event_type == "agent_team":
        return {"type": event_type, **_selected(event, _AGENT_TEAM_FIELDS)}
    if event_type == "tool_calls":
        calls = event.get("calls")
        safe_calls = []
        if isinstance(calls, list):
            safe_calls = [
                {
                    "id": _bounded_text(call.get("id"), 200),
                    "name": _bounded_text(call.get("name"), 200),
                }
                for call in calls[:100]
                if isinstance(call, Mapping)
            ]
        return {"type": event_type, "calls": safe_calls}
    if event_type == "tool_progress":
        return {"type": event_type, **_selected(event, _PROGRESS_FIELDS)}
    if event_type == "tool_approval_request":
        return {
            "type": event_type,
            "request_id": _bounded_text(event.get("request_id"), 200),
            **safe_approval_request(event),
            "surface": _bounded_text(event.get("surface"), 50),
            "channel": _bounded_text(event.get("channel"), 100),
            "state": _bounded_text(event.get("state"), 50),
            "choices": [
                choice
                for choice in event.get("choices", [])
                if choice in {"once", "session", "deny"}
            ],
        }
    if event_type in {"approval_resolved", "approval_response_rejected"}:
        return {
            "type": event_type,
            "request_id": _bounded_text(event.get("request_id"), 200),
            "state": _bounded_text(event.get("state"), 50),
            "decision": _bounded_text(event.get("decision"), 50),
            "reason": _bounded_text(event.get("reason"), 2_000),
        }
    if event_type in {"task_started", "task_status"}:
        task = event.get("task")
        return {
            "type": event_type,
            "task": _selected(task, _TASK_FIELDS) if isinstance(task, Mapping) else None,
        }
    if event_type == "vision_preprocess":
        def _non_negative_int(value: object) -> int:
            if isinstance(value, bool) or not isinstance(value, int):
                return 0
            return value if value >= 0 else 0

        return {
            "type": event_type,
            "message": _bounded_text(event.get("message"), 1_000),
            "protected": bool(event.get("protected")),
            "protected_local_images": _non_negative_int(event.get("protected_local_images")),
            "unprotected_external_images": _non_negative_int(
                event.get("unprotected_external_images")
            ),
        }
    return {"type": event_type}


class RuntimeEventStream:
    """Adds live envelopes and stores only replay-safe lifecycle events."""

    def __init__(self, path: str | Path | None = None, *, retention: int | None = None):
        self.path = Path(path) if path is not None else event_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.retention = max(
            100,
            int(retention if retention is not None else os.getenv("ASTRA_EVENT_RETENTION", "5000")),
        )
        self._lock = threading.RLock()
        self._sequence = 0
        self._cursor = 0
        self.scope = os.getenv("ASTRA_EVENT_SCOPE", "")
        self.generation = uuid.uuid4().hex
        self._initialize()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._lock, self._connection() as db:
            enable_wal(db)
            # Serialize the additive migration across simultaneous TUI starts.
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """CREATE TABLE IF NOT EXISTS runtime_events (
                    cursor INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )"""
            )
            columns = {row["name"] for row in db.execute("PRAGMA table_info(runtime_events)")}
            if "scope" not in columns:
                db.execute("ALTER TABLE runtime_events ADD COLUMN scope TEXT NOT NULL DEFAULT ''")
            db.execute("CREATE INDEX IF NOT EXISTS runtime_events_scope ON runtime_events(scope, cursor)")
            row = db.execute("SELECT COALESCE(MAX(cursor), 0) AS cursor FROM runtime_events").fetchone()
            self._cursor = int(row["cursor"])

    def publish(self, event: Mapping[str, Any]) -> dict[str, Any]:
        payload = dict(event)
        event_id = str(payload.get("event_id") or uuid.uuid4().hex)
        with self._lock:
            self._sequence += 1
            sequence = self._sequence
            replay_payload = safe_replay_event(payload)
            cursor = self._cursor
            if replay_payload is not None:
                with self._connection() as db:
                    inserted = db.execute(
                        """INSERT INTO runtime_events(event_id, type, payload_json, created_at, scope)
                           VALUES (?, ?, ?, ?, ?)""",
                        (
                            event_id,
                            str(replay_payload["type"]),
                            json.dumps(replay_payload, ensure_ascii=False, separators=(",", ":")),
                            _now(),
                            self.scope,
                        ),
                    ).lastrowid
                    if inserted is None:
                        raise RuntimeError("Runtime event insert did not return a cursor")
                    cursor = int(inserted)
                    cutoff = cursor - self.retention
                    if cutoff > 0:
                        db.execute("DELETE FROM runtime_events WHERE cursor<=?", (cutoff,))
                    fault_hook = getattr(self, "_fault_hook", None)
                    if callable(fault_hook):
                        fault_hook("before_commit")
                fault_hook = getattr(self, "_fault_hook", None)
                if callable(fault_hook):
                    fault_hook("after_commit")
                self._cursor = cursor
            return {
                **payload,
                "event_id": event_id,
                "sequence": sequence,
                "generation": self.generation,
                "cursor": cursor,
                "replayable": replay_payload is not None,
            }

    def replay(self, after_cursor: int, *, limit: int = 500) -> list[dict[str, Any]]:
        bounded_limit = max(1, min(int(limit), 2_000))
        with self._lock, self._connection() as db:
            rows = db.execute(
                """SELECT cursor, event_id, payload_json FROM runtime_events
                   WHERE cursor>? AND scope=? ORDER BY cursor ASC LIMIT ?""",
                (max(0, int(after_cursor)), self.scope, bounded_limit),
            ).fetchall()
        return [
            {
                **json.loads(row["payload_json"]),
                "event_id": str(row["event_id"]),
                "sequence": int(row["cursor"]),
                "cursor": int(row["cursor"]),
                "replayable": True,
                "replayed": True,
            }
            for row in rows
        ]

    @property
    def cursor(self) -> int:
        return self._cursor
