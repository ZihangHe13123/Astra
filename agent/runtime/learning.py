"""Learning settings and the archived proposal history.

The candidate review queue is retired; /learn migrate and session_search still read
the stored proposals.
"""

from __future__ import annotations

from agent.runtime.paths import state_path

import json
import logging
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


LEARNING_MODES = ("off", "review")
PROPOSAL_KINDS = ("memory", "observation", "skill_create", "skill_patch", "skill_write_file")
_SECRET = re.compile(r"(?i)\b(api[_-]?key|token|secret|password)\s*[:=]\s*\S+")
logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _positive_int_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


def default_learning_path() -> Path:
    override = os.getenv("AGENT_LEARNING_PATH", "").strip()
    if override:
        return Path(override).expanduser()
    return state_path("learning.db")


class LearningStore:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else default_learning_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS learning_proposals (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    status TEXT NOT NULL,
                    before_json TEXT,
                    result_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS learning_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_learning_proposals_status_created
                    ON learning_proposals(status, created_at DESC);
                CREATE TABLE IF NOT EXISTS learning_reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_learning_reviews_created
                    ON learning_reviews(created_at DESC, id DESC);
                """
            )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def mode(self) -> str:
        with self._lock, self._connection() as db:
            row = db.execute("SELECT value FROM learning_settings WHERE key='mode'").fetchone()
        value = row["value"] if row else os.getenv("LEARNING_REVIEW_MODE", "review")
        return value if value in LEARNING_MODES else "review"

    def count(self, status: str = "pending") -> int:
        with self._connection() as db:
            return db.execute("SELECT count(*) FROM learning_proposals WHERE status=?", (status,)).fetchone()[0]

    def set_mode(self, mode: str) -> str:
        value = str(mode).strip().lower()
        if value not in LEARNING_MODES:
            raise ValueError(f"Learning mode must be one of: {', '.join(LEARNING_MODES)}")
        with self._lock, self._connection() as db:
            db.execute(
                "INSERT INTO learning_settings(key, value) VALUES ('mode', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (value,),
            )
        return value

    def get(self, proposal_id: str) -> dict[str, Any] | None:
        with self._lock, self._connection() as db:
            row = db.execute("SELECT * FROM learning_proposals WHERE id=?", (proposal_id,)).fetchone()
        return self._decode(row) if row else None

    def list(self, status: str | None = "pending", limit: int = 50) -> list[dict[str, Any]]:
        with self._lock, self._connection() as db:
            if status:
                rows = db.execute(
                    "SELECT * FROM learning_proposals WHERE status=? ORDER BY created_at DESC LIMIT ?",
                    (status, max(1, min(limit, 10_000))),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM learning_proposals ORDER BY created_at DESC LIMIT ?",
                    (max(1, min(limit, 10_000)),),
                ).fetchall()
        return [self._decode(row) for row in rows]

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        for source, target in (("payload_json", "payload"), ("before_json", "before"), ("result_json", "result")):
            raw = result.pop(source)
            result[target] = json.loads(raw) if raw else None
        return result
