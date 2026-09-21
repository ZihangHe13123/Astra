"""Durable, frontend-safe approval requests for paused tool calls."""

from __future__ import annotations

from agent.runtime.paths import state_path

import json
import os
import sqlite3
import threading
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PENDING_APPROVAL_STATES = frozenset({"pending"})
TERMINAL_APPROVAL_STATES = frozenset({"approved", "denied", "cancelled", "orphaned"})
APPROVAL_DECISIONS = frozenset({"once", "session", "deny"})

_REQUEST_TEXT_FIELDS = (
    "call_id",
    "tool_name",
    "risk",
    "kind",
    "reason",
    "agent_reason",
    "reason_source",
    "detail",
    "target",
    "operation",
    "access",
    "approval_title",
    "approval_summary",
    "approval_effect",
    "approval_boundary",
    "approval_question",
    "scope_kind",
    "scope",
    "session_scope_label",
    "workspace",
    "verifier",
    "preview",
)
_REQUEST_BOOL_FIELDS = ("outside_workspace",)
_REQUEST_INT_FIELDS = ("compound_command_count",)
_TAKEOVER_TEXT_FIELDS = frozenset({
    "call_id",
    "tool_name",
    "risk",
    "kind",
})
_TAKEOVER_ARGUMENT_FIELDS = frozenset({
    "application", "window", "reason", "action_classes", "expected_effect",
})
_TAKEOVER_ACTION_CLASSES = frozenset({"press", "text", "click", "double_click", "scroll", "drag"})
_TEXT_LIMITS = {
    "reason": 4_000,
    "agent_reason": 2_000,
    "approval_question": 2_000,
    "reason_source": 32,
    "detail": 8_000,
    "preview": 24_000,
}


def approval_db_path() -> Path:
    configured = os.getenv("ASTRA_APPROVAL_DB", "").strip()
    if configured:
        return Path(configured).expanduser()
    return state_path("approvals.db")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _bounded_text(value: Any, limit: int = 2_000) -> str:
    text = str(value or "")
    return text if len(text) <= limit else f"{text[:limit]}…"


def safe_approval_request(request: Mapping[str, Any]) -> dict[str, Any]:
    """Keep only the already-redacted frontend contract and bound its size."""
    result: dict[str, Any] = {}
    takeover = request.get("kind") == "computer_foreground_takeover"
    if isinstance(request.get("choices"), (list, tuple)):
        result["choices"] = list(dict.fromkeys(
            choice for choice in request["choices"]
            if isinstance(choice, str) and choice in {"once", "session", "deny"}
        ))
    for key in _REQUEST_TEXT_FIELDS:
        if key in request and (not takeover or key in _TAKEOVER_TEXT_FIELDS):
            result[key] = _bounded_text(request[key], _TEXT_LIMITS.get(key, 2_000))

    for key in (() if takeover else _REQUEST_BOOL_FIELDS):
        if key in request and isinstance(request[key], bool):
            result[key] = request[key]

    for key in (() if takeover else _REQUEST_INT_FIELDS):
        if (
            key in request
            and isinstance(request[key], int)
            and not isinstance(request[key], bool)
            and request[key] >= 0
        ):
            result[key] = request[key]

    arguments = request.get("arguments")
    if isinstance(arguments, Mapping):
        safe_arguments: dict[str, Any] = {}
        for raw_key, value in list(arguments.items())[:50]:
            key = _bounded_text(raw_key, 120)
            if takeover and key not in _TAKEOVER_ARGUMENT_FIELDS:
                continue
            if takeover and key == "action_classes":
                if not isinstance(value, (list, tuple)):
                    continue
                safe_arguments[key] = list(dict.fromkeys(
                    item for item in value
                    if isinstance(item, str) and item in _TAKEOVER_ACTION_CLASSES
                ))[: len(_TAKEOVER_ACTION_CLASSES)]
            else:
                safe_arguments[key] = _bounded_text(value, 4_000)
        result["arguments"] = safe_arguments

    scopes = request.get("scopes")
    if not takeover and isinstance(scopes, (list, tuple)):
        result["scopes"] = [_bounded_text(value, 500) for value in scopes[:50]]

    targets = request.get("targets")
    if not takeover and isinstance(targets, (list, tuple)):
        result["targets"] = [_bounded_text(value, 2_000) for value in targets[:50]]

    compound_commands = request.get("compound_commands")
    if not takeover and isinstance(compound_commands, (list, tuple)):
        result["compound_commands"] = [
            _bounded_text(value, 2_000) for value in compound_commands[:16]
        ]

    change_summary = request.get("change_summary")
    if not takeover and isinstance(change_summary, Mapping):
        result["change_summary"] = {
            key: change_summary[key]
            for key in ("kind", "files", "additions", "deletions")
            if key in change_summary
        }
    return result


@dataclass(frozen=True)
class ApprovalRecord:
    request_id: str
    session_id: str
    task_id: str
    surface: str
    channel: str
    state: str
    decision: str
    request: dict[str, Any]
    created_at: str
    updated_at: str
    resolved_at: str | None

    def public(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "surface": self.surface,
            "channel": self.channel,
            "state": self.state,
            "decision": self.decision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "resolved_at": self.resolved_at,
            **self.request,
        }


class ApprovalInbox:
    """SQLite inbox whose rows never contain raw tool arguments."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else approval_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
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
            db.execute("PRAGMA journal_mode = WAL")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS approval_requests (
                    request_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL DEFAULT '',
                    task_id TEXT NOT NULL DEFAULT '',
                    surface TEXT NOT NULL DEFAULT 'tui',
                    channel TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL,
                    decision TEXT NOT NULL DEFAULT '',
                    request_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    resolved_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_approval_state_created
                    ON approval_requests(state, created_at DESC);
                """
            )

    @staticmethod
    def _record(row: sqlite3.Row) -> ApprovalRecord:
        return ApprovalRecord(
            request_id=str(row["request_id"]),
            session_id=str(row["session_id"]),
            task_id=str(row["task_id"]),
            surface=str(row["surface"]),
            channel=str(row["channel"]),
            state=str(row["state"]),
            decision=str(row["decision"]),
            request=json.loads(row["request_json"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            resolved_at=str(row["resolved_at"]) if row["resolved_at"] else None,
        )

    def create(
        self,
        request: Mapping[str, Any],
        *,
        request_id: str | None = None,
        session_id: str = "",
        task_id: str = "",
        surface: str = "tui",
        channel: str = "",
    ) -> ApprovalRecord:
        resolved_id = str(request_id or request.get("request_id") or uuid.uuid4().hex)
        payload = json.dumps(safe_approval_request(request), ensure_ascii=False, separators=(",", ":"))
        now = _now()
        with self._lock, self._connection() as db:
            db.execute(
                """INSERT OR IGNORE INTO approval_requests
                   (request_id, session_id, task_id, surface, channel, state,
                    request_json, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)""",
                (
                    resolved_id,
                    str(session_id),
                    str(task_id),
                    str(surface),
                    str(channel),
                    payload,
                    now,
                    now,
                ),
            )
            row = db.execute(
                "SELECT * FROM approval_requests WHERE request_id=?",
                (resolved_id,),
            ).fetchone()
        assert row is not None
        return self._record(row)

    def get(self, request_id: str) -> ApprovalRecord | None:
        with self._lock, self._connection() as db:
            row = db.execute(
                "SELECT * FROM approval_requests WHERE request_id=?",
                (str(request_id),),
            ).fetchone()
        return self._record(row) if row is not None else None

    def list(self, *, states: set[str] | None = None, limit: int = 100) -> list[ApprovalRecord]:
        bounded_limit = max(1, min(int(limit), 500))
        with self._lock, self._connection() as db:
            if states:
                normalized = sorted(str(state) for state in states)
                placeholders = ",".join("?" for _ in normalized)
                rows = db.execute(
                    f"""SELECT * FROM approval_requests
                        WHERE state IN ({placeholders})
                        ORDER BY created_at DESC LIMIT ?""",
                    [*normalized, bounded_limit],
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM approval_requests ORDER BY created_at DESC LIMIT ?",
                    (bounded_limit,),
                ).fetchall()
        return [self._record(row) for row in rows]

    def resolve(self, request_id: str, decision: str) -> ApprovalRecord | None:
        normalized = str(decision).lower()
        if normalized not in APPROVAL_DECISIONS:
            normalized = "deny"
        state = "denied" if normalized == "deny" else "approved"
        now = _now()
        with self._lock, self._connection() as db:
            cursor = db.execute(
                """UPDATE approval_requests
                   SET state=?, decision=?, updated_at=?, resolved_at=?
                   WHERE request_id=? AND state='pending'""",
                (state, normalized, now, now, str(request_id)),
            )
            if cursor.rowcount != 1:
                return None
            row = db.execute(
                "SELECT * FROM approval_requests WHERE request_id=?",
                (str(request_id),),
            ).fetchone()
        assert row is not None
        return self._record(row)

    def cancel(self, request_id: str) -> ApprovalRecord | None:
        return self._transition_pending(request_id, "cancelled")

    def recover_orphaned(self, *, session_id: str | None = None) -> list[ApprovalRecord]:
        """Mark approvals whose waiting coroutine belonged to an old process."""
        now = _now()
        with self._lock, self._connection() as db:
            rows = db.execute(
                "SELECT request_id FROM approval_requests WHERE state='pending'"
                + (" AND session_id=?" if session_id is not None else ""),
                (session_id,) if session_id is not None else (),
            ).fetchall()
            request_ids = [str(row["request_id"]) for row in rows]
            if request_ids:
                db.execute(
                    """UPDATE approval_requests
                       SET state='orphaned', updated_at=?, resolved_at=?
                       WHERE state='pending'""" + (" AND session_id=?" if session_id is not None else ""),
                    (now, now, session_id) if session_id is not None else (now, now),
                )
        return [
            record
            for request_id in request_ids
            if (record := self.get(request_id)) is not None
        ]

    def _transition_pending(self, request_id: str, state: str) -> ApprovalRecord | None:
        if state not in TERMINAL_APPROVAL_STATES:
            raise ValueError(f"invalid approval state: {state}")
        now = _now()
        with self._lock, self._connection() as db:
            cursor = db.execute(
                """UPDATE approval_requests
                   SET state=?, updated_at=?, resolved_at=?
                   WHERE request_id=? AND state='pending'""",
                (state, now, now, str(request_id)),
            )
            if cursor.rowcount != 1:
                return None
            row = db.execute(
                "SELECT * FROM approval_requests WHERE request_id=?",
                (str(request_id),),
            ).fetchone()
        assert row is not None
        return self._record(row)
