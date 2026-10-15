"""Auditable structured long-term memory records backed by SQLite."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator


MEMORY_KINDS = ("persona", "user_fact", "preference", "episode", "observation", "task_ref")
MEMORY_STATUSES = ("active", "superseded", "expired", "forgotten")
_MEMORY_FTS_TABLE_SQL = """
CREATE VIRTUAL TABLE memory_records_fts USING fts5(
    id UNINDEXED,
    content,
    tags,
    tokenize='trigram'
);
"""
_MEMORY_FTS_TRIGGERS_SQL = """
CREATE TRIGGER IF NOT EXISTS memory_records_ai AFTER INSERT ON memory_records BEGIN
    INSERT INTO memory_records_fts(id, content, tags)
    VALUES (new.id, new.content, new.tags_json);
END;
CREATE TRIGGER IF NOT EXISTS memory_records_ad AFTER DELETE ON memory_records BEGIN
    DELETE FROM memory_records_fts WHERE id = old.id;
END;
CREATE TRIGGER IF NOT EXISTS memory_records_au AFTER UPDATE ON memory_records BEGIN
    DELETE FROM memory_records_fts WHERE id = old.id;
    INSERT INTO memory_records_fts(id, content, tags)
    VALUES (new.id, new.content, new.tags_json);
END;
"""
_DROP_MEMORY_FTS_TRIGGERS_SQL = """
DROP TRIGGER IF EXISTS memory_records_ai;
DROP TRIGGER IF EXISTS memory_records_ad;
DROP TRIGGER IF EXISTS memory_records_au;
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_object(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _json_strings(raw: str) -> tuple[str, ...]:
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return ()
    if not isinstance(value, list):
        return ()
    return tuple(str(item) for item in value if str(item).strip())


@dataclass(frozen=True)
class MemoryRecord:
    record_id: str
    kind: str
    content: str
    source_session_id: str
    source_message_id: str
    created_at: str
    last_confirmed_at: str
    valid_from: str
    valid_until: str
    confidence: float
    salience: float
    status: str
    supersedes_id: str
    tags: tuple[str, ...]
    metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.record_id,
            "kind": self.kind,
            "content": self.content,
            "source_session_id": self.source_session_id,
            "source_message_id": self.source_message_id,
            "created_at": self.created_at,
            "last_confirmed_at": self.last_confirmed_at,
            "valid_from": self.valid_from,
            "valid_until": self.valid_until,
            "confidence": self.confidence,
            "salience": self.salience,
            "status": self.status,
            "supersedes_id": self.supersedes_id,
            "tags": list(self.tags),
            "metadata": dict(self.metadata),
        }


class MemoryRecordRepository:
    def __init__(self, path: str | Path, lock: threading.RLock | None = None):
        self.path = Path(path)
        self._lock = lock or threading.RLock()
        self.fts_enabled = False
        self._initialize()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _initialize(self) -> None:
        with self._lock, self._connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS memory_records (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    content TEXT NOT NULL,
                    source_session_id TEXT NOT NULL DEFAULT '',
                    source_message_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    last_confirmed_at TEXT NOT NULL,
                    valid_from TEXT NOT NULL,
                    valid_until TEXT NOT NULL DEFAULT '',
                    confidence REAL NOT NULL,
                    salience REAL NOT NULL,
                    status TEXT NOT NULL,
                    supersedes_id TEXT NOT NULL DEFAULT '',
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_memory_records_status_kind
                    ON memory_records(status, kind);
                CREATE INDEX IF NOT EXISTS idx_memory_records_source_session
                    ON memory_records(source_session_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_memory_records_supersedes
                    ON memory_records(supersedes_id);
                CREATE TABLE IF NOT EXISTS memory_usage (
                    record_id TEXT PRIMARY KEY,
                    recalls INTEGER NOT NULL DEFAULT 0,
                    reads INTEGER NOT NULL DEFAULT 0,
                    last_used_at TEXT NOT NULL DEFAULT ''
                );
                """
            )
            try:
                self._initialize_fts(db)
            except sqlite3.DatabaseError:
                if db.in_transaction:
                    db.rollback()
                self.fts_enabled = self._fts_usable(db)
                if not self.fts_enabled:
                    self._detach_fts_triggers(db)

    @staticmethod
    def _fts_sql(db: sqlite3.Connection) -> str:
        row = db.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name='memory_records_fts'"
        ).fetchone()
        return "" if row is None else str(row["sql"] or "")

    @classmethod
    def _fts_uses_trigram(cls, db: sqlite3.Connection) -> bool:
        definition = " ".join(cls._fts_sql(db).casefold().split())
        return re.search(r"tokenize\s*=\s*['\"]?trigram\b", definition) is not None

    @classmethod
    def _fts_usable(cls, db: sqlite3.Connection) -> bool:
        if not cls._fts_sql(db):
            return False
        try:
            db.execute(
                "SELECT rowid FROM memory_records_fts "
                "WHERE memory_records_fts MATCH ? LIMIT 1",
                ('"contextindexprobe"',),
            ).fetchone()
            db.execute(
                "INSERT INTO memory_records_fts(memory_records_fts) "
                "VALUES('integrity-check')"
            )
        except sqlite3.DatabaseError:
            if db.in_transaction:
                db.rollback()
            return False
        return True

    @staticmethod
    def _detach_fts_triggers(db: sqlite3.Connection) -> None:
        try:
            db.executescript(
                "BEGIN IMMEDIATE;"
                + _DROP_MEMORY_FTS_TRIGGERS_SQL
                + "COMMIT;"
            )
        except BaseException:
            if db.in_transaction:
                db.rollback()
            raise

    def _initialize_fts(self, db: sqlite3.Connection) -> None:
        if not self._fts_uses_trigram(db):
            self._rebuild_fts_trigram(db)
        else:
            db.executescript(_MEMORY_FTS_TRIGGERS_SQL)
            db.execute(
                "INSERT INTO memory_records_fts(id, content, tags) "
                "SELECT r.id, r.content, r.tags_json FROM memory_records AS r "
                "WHERE NOT EXISTS ("
                "SELECT 1 FROM memory_records_fts AS f WHERE f.id = r.id)"
            )
        if not self._fts_usable(db):
            raise sqlite3.DatabaseError("memory records FTS integrity check failed")
        self.fts_enabled = True

    @staticmethod
    def _rebuild_fts_trigram(db: sqlite3.Connection) -> None:
        try:
            db.executescript(
                "BEGIN IMMEDIATE;"
                + _DROP_MEMORY_FTS_TRIGGERS_SQL
                + "DROP TABLE IF EXISTS memory_records_fts;"
                + _MEMORY_FTS_TABLE_SQL
                + "INSERT INTO memory_records_fts(id, content, tags) "
                "SELECT id, content, tags_json FROM memory_records;"
                + _MEMORY_FTS_TRIGGERS_SQL
                + "COMMIT;"
            )
        except BaseException:
            if db.in_transaction:
                db.rollback()
            raise

    @staticmethod
    def _validate_kind(kind: str) -> str:
        value = str(kind).strip().lower()
        if value not in MEMORY_KINDS:
            raise ValueError(f"Unknown memory kind: {kind}")
        return value

    @staticmethod
    def _score(value: float, label: str) -> float:
        score = float(value)
        if not 0.0 <= score <= 1.0:
            raise ValueError(f"{label} must be between 0 and 1")
        return score

    @staticmethod
    def _timestamp(value: str | None, *, default: str = "") -> str:
        text = str(value or default).strip()
        if not text:
            return ""
        try:
            datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"Invalid ISO timestamp: {text}") from exc
        return text

    @staticmethod
    def _tags(values: Iterable[str]) -> tuple[str, ...]:
        clean = []
        for raw in values:
            value = " ".join(str(raw).strip().split())
            if value and value not in clean:
                clean.append(value)
        if len(clean) > 32:
            raise ValueError("Memory records support at most 32 tags")
        if any(len(item) > 80 for item in clean):
            raise ValueError("Memory tag is too long (max 80 characters)")
        return tuple(clean)

    def add(
        self,
        *,
        kind: str,
        content: str,
        source_session_id: str = "",
        source_message_id: str = "",
        valid_from: str | None = None,
        valid_until: str | None = None,
        confidence: float = 1.0,
        salience: float = 0.5,
        tags: Iterable[str] = (),
        metadata: dict[str, Any] | None = None,
        supersedes_id: str = "",
        record_id: str | None = None,
    ) -> MemoryRecord:
        now = _now()
        values = {
            "id": str(record_id or uuid.uuid4().hex),
            "kind": self._validate_kind(kind),
            "content": str(content),
            "source_session_id": str(source_session_id),
            "source_message_id": str(source_message_id),
            "created_at": now,
            "last_confirmed_at": now,
            "valid_from": self._timestamp(valid_from, default=now),
            "valid_until": self._timestamp(valid_until),
            "confidence": self._score(confidence, "confidence"),
            "salience": self._score(salience, "salience"),
            "status": "active",
            "supersedes_id": str(supersedes_id),
            "tags_json": json.dumps(self._tags(tags), ensure_ascii=False),
            "metadata_json": json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
        }
        with self._lock, self._connection() as db:
            db.execute(
                """
                INSERT INTO memory_records(
                    id, kind, content, source_session_id, source_message_id,
                    created_at, last_confirmed_at, valid_from, valid_until,
                    confidence, salience, status, supersedes_id, tags_json, metadata_json
                ) VALUES (
                    :id, :kind, :content, :source_session_id, :source_message_id,
                    :created_at, :last_confirmed_at, :valid_from, :valid_until,
                    :confidence, :salience, :status, :supersedes_id, :tags_json, :metadata_json
                )
                """,
                values,
            )
        record = self.get(values["id"])
        if record is None:
            raise RuntimeError("memory record insert was not persisted")
        return record

    def get(self, record_id: str) -> MemoryRecord | None:
        with self._lock, self._connection() as db:
            row = db.execute("SELECT * FROM memory_records WHERE id=?", (str(record_id),)).fetchone()
        return self._from_row(row) if row else None

    def list_records(self, *, status: str = "", limit: int = 1000) -> list[MemoryRecord]:
        """List records without invoking FTS ranking.

        Core Block assembly needs a complete, deterministic view rather than a
        query-ranked recall result capped at the normal prompt recall limit.
        """
        clean_status = str(status).strip().lower()
        if clean_status and clean_status not in MEMORY_STATUSES:
            raise ValueError(f"Unknown memory status: {status}")
        limit = max(1, min(int(limit), 5000))
        sql = "SELECT * FROM memory_records"
        params: list[Any] = []
        if clean_status:
            sql += " WHERE status=?"
            params.append(clean_status)
        sql += " ORDER BY created_at, id LIMIT ?"
        params.append(limit)
        with self._lock, self._connection() as db:
            rows = db.execute(sql, params).fetchall()
        return [self._from_row(row) for row in rows]

    def resolve_prefix(self, record_id: str, *, active_only: bool = False) -> MemoryRecord | None:
        """Resolve a full or abbreviated record id without guessing on ambiguity."""
        needle = str(record_id).strip().lower().lstrip("#")
        if not needle:
            return None
        clauses = ["lower(id) LIKE ?"]
        params: list[Any] = [f"{needle}%"]
        if active_only:
            clauses.append("status = 'active'")
        with self._lock, self._connection() as db:
            rows = db.execute(
                "SELECT * FROM memory_records WHERE " + " AND ".join(clauses) + " ORDER BY created_at DESC LIMIT 2",
                params,
            ).fetchall()
        if len(rows) > 1:
            raise ValueError(f"Memory id prefix is ambiguous: {record_id}")
        return self._from_row(rows[0]) if rows else None

    def recall(
        self,
        query: str = "",
        *,
        kinds: Iterable[str] = (),
        limit: int = 8,
        now: str | None = None,
    ) -> list[MemoryRecord]:
        clean_kinds = tuple(self._validate_kind(kind) for kind in kinds)
        limit = max(1, min(int(limit), 50))
        current = self._timestamp(now, default=_now())
        query = " ".join(str(query).strip().split())
        if query and self.fts_enabled:
            try:
                matches = self._recall_fts(query, clean_kinds, limit, current)
                if matches:
                    return matches
            except sqlite3.OperationalError:
                pass
        return self._recall_like(query, clean_kinds, limit, current)

    @staticmethod
    def _fts_query(query: str) -> str:
        terms = re.findall(r"[^\s\"'()]+", query)
        return " OR ".join(f'"{term.replace(chr(34), "")}"' for term in terms[:12])

    def _recall_fts(self, query: str, kinds: tuple[str, ...], limit: int, now: str) -> list[MemoryRecord]:
        match = self._fts_query(query)
        if not match:
            return []
        kind_sql = ""
        params: list[Any] = [match, now, now]
        if kinds:
            kind_sql = f" AND r.kind IN ({','.join('?' for _ in kinds)})"
            params.extend(kinds)
        params.append(limit)
        with self._lock, self._connection() as db:
            rows = db.execute(
                """
                SELECT r.* FROM memory_records_fts f
                JOIN memory_records r ON r.id = f.id
                WHERE memory_records_fts MATCH ?
                  AND r.status = 'active'
                  AND r.valid_from <= ?
                  AND (r.valid_until = '' OR r.valid_until > ?)
                """ + kind_sql + " ORDER BY bm25(memory_records_fts), r.salience DESC LIMIT ?",
                params,
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def _recall_like(self, query: str, kinds: tuple[str, ...], limit: int, now: str) -> list[MemoryRecord]:
        clauses = ["status = 'active'", "valid_from <= ?", "(valid_until = '' OR valid_until > ?)"]
        params: list[Any] = [now, now]
        if query:
            # Short trigram terms and environments without FTS still need a
            # deterministic local fallback without an embedding service.
            terms = [query]
            terms.extend(term for term in query.split() if term not in terms)
            terms = terms[:12]
            term_clauses = []
            for term in terms:
                term_clauses.append("(content LIKE ? OR tags_json LIKE ?)")
                needle = f"%{term}%"
                params.extend([needle, needle])
            clauses.append("(" + " OR ".join(term_clauses) + ")")
        if kinds:
            clauses.append(f"kind IN ({','.join('?' for _ in kinds)})")
            params.extend(kinds)
        params.append(limit)
        with self._lock, self._connection() as db:
            rows = db.execute(
                "SELECT * FROM memory_records WHERE " + " AND ".join(clauses)
                + " ORDER BY salience DESC, created_at DESC LIMIT ?",
                params,
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def supersede(
        self,
        record_id: str,
        *,
        content: str,
        source_session_id: str = "",
        source_message_id: str = "",
        confidence: float = 1.0,
        salience: float | None = None,
        tags: Iterable[str] | None = None,
        metadata: dict[str, Any] | None = None,
        expected_content: str | None = None,
    ) -> MemoryRecord:
        replacement_id = uuid.uuid4().hex
        now = _now()
        with self._lock, self._connection() as db:
            # A proposal may wait while another process corrects this record.
            # Check and replace under one SQLite write lock, with no orphaned
            # replacement if validation or the old-record update fails.
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM memory_records WHERE id=? AND status='active'", (record_id,)).fetchone()
            if row is None:
                raise ValueError(f"Active memory record not found: {record_id}")
            old = self._from_row(row)
            if expected_content is not None and old.content != expected_content:
                raise ValueError("Memory changed; inspect it before correcting")
            db.execute(
                """INSERT INTO memory_records(
                    id, kind, content, source_session_id, source_message_id,
                    created_at, last_confirmed_at, valid_from, valid_until,
                    confidence, salience, status, supersedes_id, tags_json, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, 'active', ?, ?, ?)""",
                (replacement_id, old.kind, str(content), str(source_session_id), str(source_message_id),
                 now, now, now, self._score(confidence, "confidence"),
                 old.salience if salience is None else self._score(salience, "salience"), old.record_id,
                 json.dumps(self._tags(old.tags if tags is None else tags), ensure_ascii=False),
                 json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True)),
            )
            db.execute(
                "UPDATE memory_records SET status='superseded', valid_until=? WHERE id=? AND status='active'",
                (now, old.record_id),
            )
        replacement = self.get(replacement_id)
        if replacement is None:
            raise RuntimeError("Memory correction was not persisted")
        return replacement

    def forget(self, record_id: str) -> bool:
        with self._lock, self._connection() as db:
            cursor = db.execute(
                "UPDATE memory_records SET status='forgotten', valid_until=? WHERE id=? AND status='active'",
                (_now(), str(record_id)),
            )
            return cursor.rowcount > 0

    def note_use(self, record_ids: Iterable[str], *, read: bool = False, now: str | None = None) -> None:
        """Count a recall into a turn, or an explicit read, for each record."""
        ids = tuple(dict.fromkeys(str(record_id) for record_id in record_ids if record_id))
        if not ids:
            return
        column = "reads" if read else "recalls"
        current = self._timestamp(now, default=_now())
        with self._lock, self._connection() as db:
            db.executemany(
                f"INSERT INTO memory_usage(record_id, {column}, last_used_at) VALUES (?, 1, ?) "
                f"ON CONFLICT(record_id) DO UPDATE SET {column} = {column} + 1, last_used_at = excluded.last_used_at",
                [(record_id, current) for record_id in ids],
            )

    def usage(self) -> dict[str, tuple[int, int, str]]:
        """Recalls, reads and last use per record that has been used."""
        with self._lock, self._connection() as db:
            rows = db.execute("SELECT record_id, recalls, reads, last_used_at FROM memory_usage").fetchall()
        return {str(row["record_id"]): (int(row["recalls"]), int(row["reads"]), str(row["last_used_at"]))
                for row in rows}

    def stats(self) -> dict[str, Any]:
        with self._lock, self._connection() as db:
            rows = db.execute(
                "SELECT status, COUNT(*) AS count FROM memory_records GROUP BY status"
            ).fetchall()
        counts = {status: 0 for status in MEMORY_STATUSES}
        counts.update({str(row["status"]): int(row["count"]) for row in rows})
        return {"fts_enabled": self.fts_enabled, "counts": counts, "total": sum(counts.values())}

    @staticmethod
    def _from_row(row: sqlite3.Row) -> MemoryRecord:
        return MemoryRecord(
            record_id=str(row["id"]),
            kind=str(row["kind"]),
            content=str(row["content"]),
            source_session_id=str(row["source_session_id"]),
            source_message_id=str(row["source_message_id"]),
            created_at=str(row["created_at"]),
            last_confirmed_at=str(row["last_confirmed_at"]),
            valid_from=str(row["valid_from"]),
            valid_until=str(row["valid_until"]),
            confidence=float(row["confidence"]),
            salience=float(row["salience"]),
            status=str(row["status"]),
            supersedes_id=str(row["supersedes_id"]),
            tags=_json_strings(row["tags_json"]),
            metadata=_json_object(row["metadata_json"]),
        )
