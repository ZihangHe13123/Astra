"""Rebuildable, archive-scoped vectors for canonical session and memory records.

Activity's existing vector table and encoding stay unchanged. This table contains
locators, revisions and vectors, never memory bodies. Query readers do not migrate it.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import os
from pathlib import Path
import sqlite3
import struct
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence

from ..activity_store import _redact_text
from . import embedder
from .record_source import _eligible_metadata, _epoch, _revision
from .session_source import session_revision
from .sqlite_reader import open_readonly
from .vector_index import VectorStore, matrix_from_blobs

ENCODE_CHARS = 900
ARCHIVE_LIMIT = 5000
_LAYOUT = "memory-evidence-v1"
_cache_lock = threading.Lock()
_cache: OrderedDict[tuple, tuple] = OrderedDict()


def archive_key(path: Path) -> str:
    return hashlib.sha256(os.path.normcase(str(path.expanduser().resolve())).encode()).hexdigest()


def file_version(path: Path) -> tuple:
    def stat(candidate: Path) -> tuple:
        try:
            value = candidate.stat()
            return value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns
        except OSError:
            return ()
    return stat(path), stat(Path(str(path) + "-wal"))


@dataclass(frozen=True)
class IndexItem:
    item_id: str
    revision: str
    timestamp: float
    available_at: float
    text: str


def canonical_items(path: Path, source: str, *, limit: int = ARCHIVE_LIMIT) -> list[IndexItem]:
    with open_readonly(path, deadline_ms=2000) as db:
        if source == "session":
            rows = db.execute(
                "SELECT m.id, m.session_id, m.role, m.content, m.timestamp FROM messages m "
                "JOIN sessions s ON s.id = m.session_id "
                "WHERE m.role IN ('user', 'assistant') AND length(trim(m.content)) > 1 "
                "ORDER BY m.timestamp DESC, m.id DESC LIMIT ?", (limit,),
            ).fetchall()
            return [
                IndexItem(str(row["id"]), session_revision(row["session_id"], row["id"], row["role"],
                          row["content"], row["timestamp"]), float(row["timestamp"]), float(row["timestamp"]),
                          _redact_text(str(row["content"]))[:ENCODE_CHARS])
                for row in rows
            ]
        if source != "memory":
            raise ValueError("Unknown semantic source")
        # The shared index spans workspaces; enforce learning scope when a
        # request reads canonical candidates, not while building the index.
        db.create_function("memory_eligible", 1, lambda raw: _eligible_metadata(raw, respect_scope=False), deterministic=True)
        rows = db.execute(
            "SELECT * FROM memory_records WHERE status = 'active' AND memory_eligible(metadata_json) "
            "AND length(trim(content)) > 1 ORDER BY last_confirmed_at DESC, id LIMIT ?", (limit,),
        ).fetchall()
        return [IndexItem(str(row["id"]), _revision(row), _epoch(row["last_confirmed_at"]),
                          _epoch(row["created_at"]), _redact_text(str(row["content"]))[:ENCODE_CHARS])
                for row in rows]


def _blob(vector: Sequence[float]) -> bytes:
    values = [float(value) for value in vector]
    if not values or len(values) > 16384 or not all(math.isfinite(value) for value in values):
        raise ValueError("Invalid semantic vector")
    if not any(values):
        raise ValueError("Zero semantic vector")
    return struct.pack(f"<{len(values)}f", *values)


class EvidenceVectorStore(VectorStore):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self._connection.execute(
            """CREATE TABLE IF NOT EXISTS context_memory_vectors (
                source TEXT NOT NULL, archive TEXT NOT NULL, item_id TEXT NOT NULL,
                revision TEXT NOT NULL, layout TEXT NOT NULL, timestamp REAL NOT NULL,
                available_at REAL NOT NULL, vec BLOB NOT NULL,
                PRIMARY KEY(source, archive, item_id))"""
        )
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS context_memory_vectors_order ON context_memory_vectors "
            "(source, archive, layout, timestamp DESC, item_id)"
        )
        self._connection.commit()

    def revisions(self, source: str, archive: str) -> dict[str, str]:
        return dict(self._connection.execute(
            "SELECT item_id, revision FROM context_memory_vectors WHERE source=? AND archive=? AND layout=?",
            (source, archive, _LAYOUT),
        ))

    def write_batch(self, source: str, archive: str, items: Sequence[IndexItem],
                    vectors: Sequence[Sequence[float]]) -> None:
        if len(items) != len(vectors):
            raise ValueError("Embedding count mismatch")
        blobs = [_blob(vector) for vector in vectors]
        if len({len(value) for value in blobs}) > 1:
            raise ValueError("Embedding dimension mismatch")
        existing = self._connection.execute(
            "SELECT length(vec) FROM context_memory_vectors LIMIT 1",
        ).fetchone()
        if blobs and existing is not None and existing[0] != len(blobs[0]):
            raise ValueError("Embedding dimension changed; build a separate vector index")
        rows = [(source, archive, item.item_id, item.revision, _LAYOUT,
                 item.timestamp, item.available_at, blob) for item, blob in zip(items, blobs)]
        with self._connection:
            self._connection.executemany(
                "INSERT INTO context_memory_vectors VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(source, archive, item_id) DO UPDATE SET revision=excluded.revision, "
                "layout=excluded.layout, timestamp=excluded.timestamp, "
                "available_at=excluded.available_at, vec=excluded.vec", rows,
            )

    def prune(self, source: str, archive: str, retained: set[str]) -> int:
        ids = [row[0] for row in self._connection.execute(
            "SELECT item_id FROM context_memory_vectors WHERE source=? AND archive=?", (source, archive),
        ) if row[0] not in retained]
        with self._connection:
            self._connection.executemany(
                "DELETE FROM context_memory_vectors WHERE source=? AND archive=? AND item_id=?",
                ((source, archive, item) for item in ids),
            )
        return len(ids)


def rebuild_source(path: Path, vectors_db: Path, source: str, *, backend=None,
                   max_encode: int | None = None, force: bool = False) -> dict[str, int]:
    items = canonical_items(path, source)
    archive = archive_key(path)
    vectors_db.parent.mkdir(parents=True, exist_ok=True)
    store = EvidenceVectorStore(vectors_db)
    try:
        existing = {} if force else store.revisions(source, archive)
        pending = [item for item in items if existing.get(item.item_id) != item.revision]
        selected = pending if max_encode is None else pending[:max(0, max_encode)]
        encoded = 0
        if selected:
            backend = backend or embedder.get_embedder()
            if backend is None:
                raise RuntimeError("Embedding backend unavailable")
            for item in selected:
                vectors = backend.encode([item.text])
                if vectors is None:
                    raise RuntimeError("Embedding batch failed")
                store.write_batch(source, archive, [item], vectors)
                encoded += 1
        removed = store.prune(source, archive, {item.item_id for item in items})
        return {"encoded": encoded, "pending": len(pending) - encoded, "removed": removed, "retained": len(items)}
    finally:
        store.close()


def read_snapshot(vectors_db: Path, source: str, archive_path: Path) -> tuple:
    """Cache at most four immutable archive snapshots; WAL commits invalidate it."""
    return _read_snapshot(vectors_db, source, archive_path, deadline_ms=75)[0]


def warm_snapshot(vectors_db: Path, source: str, archive_path: Path, *,
                  cancelled: Callable[[], bool] = lambda: False) -> bool:
    """Fill the cache within a cooperative 2 s budget; false means version changed."""
    deadline = time.perf_counter() + 2.0

    def stopped() -> bool:
        return cancelled() or time.perf_counter() >= deadline

    _check_cancelled(stopped)
    if not archive_path.is_file():
        raise FileNotFoundError(archive_path)
    return _read_snapshot(vectors_db, source, archive_path, deadline_ms=2000, cancelled=stopped)[1]


def _check_cancelled(cancelled: Callable[[], bool] | None) -> None:
    if cancelled is not None and cancelled():
        raise sqlite3.OperationalError("interrupted")


def _read_snapshot(vectors_db: Path, source: str, archive_path: Path, *, deadline_ms: int,
                   cancelled: Callable[[], bool] | None = None) -> tuple[tuple, bool]:
    _check_cancelled(cancelled)
    identity = embedder.embedding_identity()
    archive = archive_key(archive_path)
    scope = (str(vectors_db.resolve()), source, archive, identity)
    version = file_version(vectors_db)
    with _cache_lock:
        previous = _cache.get(scope)
        if previous is not None and previous[0] == version:
            _check_cancelled(cancelled)
            _cache.move_to_end(scope)
            return previous[1], True
    _check_cancelled(cancelled)
    reader = (open_readonly(vectors_db, deadline_ms=deadline_ms, cancelled=cancelled)
              if cancelled is not None else open_readonly(vectors_db, deadline_ms=deadline_ms))
    with reader as db:
        _check_cancelled(cancelled)
        model = db.execute("SELECT identity FROM embedding_metadata LIMIT 1").fetchone()
        if model is None or model[0] != identity:
            raise ValueError("Vector model identity mismatch")
        rows = db.execute(
            "SELECT item_id, revision, timestamp, available_at, vec FROM context_memory_vectors "
            "WHERE source=? AND archive=? AND layout=? ORDER BY timestamp DESC, item_id LIMIT ?",
            (source, archive, _LAYOUT, ARCHIVE_LIMIT),
        ).fetchall()
    _check_cancelled(cancelled)
    if any(len(row["vec"]) > 65536 for row in rows):
        raise ValueError("Invalid vector dimensions")
    matrix = matrix_from_blobs([row["vec"] for row in rows])
    matrix.flags.writeable = False
    result = (tuple((str(row["item_id"]), str(row["revision"]), float(row["timestamp"]),
                     float(row["available_at"])) for row in rows), matrix)
    published = False
    with _cache_lock:
        _check_cancelled(cancelled)
        # A slower read must not replace a snapshot published after a WAL
        # commit or file replacement. Recheck while holding the publication lock.
        if file_version(vectors_db) == version:
            _cache[scope] = (version, result)
            _cache.move_to_end(scope)
            while len(_cache) > 4:
                _cache.popitem(last=False)
            published = True
    return result, published
