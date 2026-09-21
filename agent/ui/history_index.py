"""Rebuildable, disk-backed read projection of the existing session formats.

Sources stay untouched. An unchanged page reads only stat metadata and SQLite
rows; rebuilding streams one message at a time, including giant replace records.
Any source change rebuilds the projection, rather than guessing that growth means
append-only (a writer may have rewritten an earlier record).
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, TextIO

from agent.cli.images import message_display_text
from agent.runtime.paths import state_path
from agent.runtime.session_store import SessionStore

_VERSION = 1
_CHUNK = 64 * 1024


class _JSONStream:
    """Small incremental object reader; large message arrays are never decoded whole."""

    def __init__(self, source: TextIO, *, lines: bool = False):
        self.source = source
        self.lines = lines
        self.buffer = ""
        self.pos = 0
        self.eof = False
        self.decoder = json.JSONDecoder()

    def _fill(self) -> None:
        self.buffer = self.buffer[self.pos:]
        self.pos = 0
        part = self.source.read(_CHUNK)
        self.buffer += part
        self.eof = not part

    def peek(self) -> str:
        if self.pos >= len(self.buffer) and not self.eof:
            self._fill()
        return self.buffer[self.pos:self.pos + 1]

    def whitespace(self) -> None:
        allowed = " \t\r" if self.lines else " \t\r\n"
        while (char := self.peek()) and char in allowed:
            self.pos += 1

    def take(self, expected: str) -> None:
        self.whitespace()
        if self.peek() != expected:
            raise ValueError("Invalid session JSON")
        self.pos += 1

    def value(self):
        self.whitespace()
        # Compact consumed prefixes before decoding another potentially large
        # message. Memory is proportional to the largest individual value.
        if self.pos:
            self.buffer = self.buffer[self.pos:]
            self.pos = 0
        while True:
            boundary = self.buffer.find("\n") if self.lines else -1
            available = self.buffer if boundary < 0 else self.buffer[:boundary]
            try:
                result, end = self.decoder.raw_decode(available)
            except json.JSONDecodeError:
                if self.eof or boundary >= 0:
                    raise ValueError("Invalid session JSON") from None
                self._fill()
                continue
            # A number may end at a chunk boundary but continue in the next
            # chunk. Wait for a delimiter/EOF before accepting that value.
            if end == len(available) and boundary < 0 and not self.eof:
                self._fill()
                continue
            # raw_decode accepts the integer prefix of an incomplete fraction
            # or exponent ("1." / "1e+"). Fetch the rest before accepting it.
            if (isinstance(result, (int, float)) and not isinstance(result, bool)
                    and available[end:end + 1] in (".", "e", "E")
                    and boundary < 0 and not self.eof):
                self._fill()
                continue
            self.pos = end
            return result

    def object(self, begin: Callable[[], None], add: Callable[[object], None]) -> dict:
        self.take("{")
        result = {}
        self.whitespace()
        if self.peek() == "}":
            self.pos += 1
            return result
        while True:
            key = self.value()
            if not isinstance(key, str):
                raise ValueError("Invalid session JSON key")
            self.take(":")
            self.whitespace()
            if key == "messages" and self.peek() == "[":
                self.pos += 1
                begin()
                self.whitespace()
                if self.peek() != "]":
                    while True:
                        add(self.value())
                        self.whitespace()
                        if self.peek() != ",":
                            break
                        self.pos += 1
                self.take("]")
                result[key] = True
            else:
                result[key] = self.value()
            self.whitespace()
            if self.peek() != ",":
                break
            self.pos += 1
        self.take("}")
        self.whitespace()
        if self.peek() not in (("", "\n") if self.lines else ("",)):
            raise ValueError("Trailing session JSON")
        return result

    def next_line(self) -> bool:
        while True:
            end = self.buffer.find("\n", self.pos)
            if end >= 0:
                self.pos = end + 1
                return True
            if self.eof:
                return False
            self.pos = len(self.buffer)
            self._fill()


class _Projection:
    def __init__(self, db: sqlite3.Connection, table: str):
        self.db, self.table = db, table
        self.raw_count = self.total = 0
        self.hidden = False

    def reset(self) -> None:
        self.db.execute(f"DELETE FROM {self.table}")
        self.raw_count = self.total = 0
        self.hidden = False

    def add(self, message: object) -> None:
        if not isinstance(message, dict):
            raise ValueError("Session message must be an object")
        self.raw_count += 1
        provenance = message.get("provenance", "")
        if message.get("role") == "user" and provenance in ("", "session_wakeup"):
            self.hidden = provenance == "session_wakeup"
        if provenance != "wakeup_notification" and self.hidden:
            return
        position = self.total
        self.total += 1
        if message.get("role") not in ("user", "assistant"):
            return
        if message.get("_meta", {}).get("type") == "reasoning_context":
            return
        payload = {"role": message["role"],
                   "content": message_display_text(message.get("display_command", message.get("content", ""))),
                   "timestamp": message.get("timestamp")}
        self.db.execute(f"INSERT INTO {self.table} VALUES (?,?)",
                        (position, json.dumps(payload, ensure_ascii=False)))

    def adopt(self, staged: _Projection) -> None:
        # Swap table names instead of copying another history-sized set of
        # text rows. The previous generation becomes reusable scratch space.
        self.db.execute("ALTER TABLE messages RENAME TO discarded")
        self.db.execute("ALTER TABLE staged RENAME TO messages")
        self.db.execute("ALTER TABLE discarded RENAME TO staged")
        self.db.execute("DELETE FROM staged")
        self.raw_count, self.total, self.hidden = staged.raw_count, staged.total, staged.hidden


def _signature(store: SessionStore) -> str:
    values = []
    for path in (store.legacy_path, store.snapshot_path, store.jsonl_path, store.header_path):
        try:
            stat = path.stat()
            values.append((stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
        except FileNotFoundError:
            values.append(None)
    return json.dumps([_VERSION, *values], separators=(",", ":"))


def _read_document(path: Path, db: sqlite3.Connection, staged: _Projection) -> dict | None:
    db.execute("SAVEPOINT document")
    staged.reset()
    try:
        with path.open(encoding="utf-8") as handle:
            value = _JSONStream(handle).object(staged.reset, staged.add)
    except (OSError, ValueError):
        db.execute("ROLLBACK TO document")
        return None
    finally:
        db.execute("RELEASE document")
    return value


def _rebuild(store: SessionStore, db: sqlite3.Connection) -> int:
    current, staged = _Projection(db, "messages"), _Projection(db, "staged")
    current.reset()
    baseline = store.snapshot_path if store.snapshot_path.exists() else store.legacy_path
    document = _read_document(baseline, db, staged)
    if document and document.get("messages") is True:
        current.adopt(staged)
    if store.jsonl_path.exists():
        with store.jsonl_path.open(encoding="utf-8") as handle:
            reader = _JSONStream(handle, lines=True)
            while reader.peek():
                db.execute("SAVEPOINT record")
                staged.reset()
                try:
                    event = reader.object(staged.reset, staged.add)
                except ValueError:
                    db.execute("ROLLBACK TO record")
                else:
                    if event.get("type") == "replace":
                        # A missing messages field means an empty replacement.
                        current.adopt(staged)
                    elif event.get("type") == "message":
                        index = event.get("index", current.raw_count)
                        message = event.get("message")
                        if isinstance(message, dict) and not (isinstance(index, int) and index < current.raw_count):
                            current.add(message)
                finally:
                    db.execute("RELEASE record")
                if not reader.next_line():
                    break
    # SessionStore applies the header last. Normal headers have no messages,
    # but honoring a legacy one keeps projection semantics compatible.
    header = _read_document(store.header_path, db, staged)
    if header and header.get("messages") is True:
        current.adopt(staged)
    db.execute("DELETE FROM staged")
    return current.total


def _cache_path(store: SessionStore) -> Path:
    cache = Path(os.environ.get("ASTRA_GUI_HISTORY_CACHE", "").strip() or state_path("gui", "history-index"))
    identity = hashlib.sha256(str(store.legacy_path.resolve()).encode()).hexdigest()
    return cache / f"v{_VERSION}-{identity}.sqlite3"


def _create_private(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Cache files contain a presentation copy of private session messages.
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        pass
    else:
        os.close(fd)


def _corrupt(exc: sqlite3.DatabaseError) -> bool:
    # Extended result codes keep the primary SQLite error in their low byte.
    code = getattr(exc, "sqlite_errorcode", None)
    return isinstance(code, int) and code & 0xff in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB)


@contextmanager
def _database(path: Path) -> Iterator[sqlite3.Connection]:
    _create_private(path)
    db = sqlite3.connect(path, timeout=15)
    try:
        db.execute("PRAGMA cache_size=-2048")
        db.execute("PRAGMA temp_store=FILE")
        db.execute("PRAGMA secure_delete=ON")
        db.execute("CREATE TABLE IF NOT EXISTS metadata (id INTEGER PRIMARY KEY, signature TEXT, total INTEGER)")
        db.execute("CREATE TABLE IF NOT EXISTS messages (position INTEGER PRIMARY KEY, payload TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS staged (position INTEGER PRIMARY KEY, payload TEXT NOT NULL)")
        db.commit()
        yield db
    finally:
        db.close()


def discard_history_index(store: SessionStore) -> None:
    """Remove a deleted session's presentation copy, waiting for active readers."""
    path = _cache_path(store)
    if not path.exists():
        return
    cleared = False
    try:
        with _database(path) as db:
            db.execute("BEGIN IMMEDIATE")
            for table in ("messages", "staged", "metadata"):
                db.execute(f"DELETE FROM {table}")
            db.commit()
            # Shrink any pages previously used by an older cached generation too.
            db.execute("VACUUM")
            cleared = True
    except sqlite3.DatabaseError as exc:
        if not _corrupt(exc):
            raise
        # A broken disposable cache cannot be vacuumed; close it and unlink.
    try:
        path.unlink(missing_ok=True)
    except PermissionError:
        # Windows can retain an already-open handle briefly. The cache is now
        # empty and contains no history; a later query still checks the source.
        if not cleared:
            raise


def history_page(store: SessionStore, *, before: int | None = None, limit: int = 200) -> dict:
    """Return a stable page without keeping complete histories in memory."""
    path = _cache_path(store)
    for attempt in range(2):
        try:
            return _read_page(store, path, before=before, limit=limit)
        except sqlite3.DatabaseError as exc:
            if attempt or not _corrupt(exc):
                raise
            # _read_page has already closed its connection, including failures
            # during SELECT/rebuild. Never discard caches for locks/permissions.
            path.unlink(missing_ok=True)
    raise AssertionError("Unreachable history cache retry")


def _read_page(store: SessionStore, path: Path, *, before: int | None, limit: int) -> dict:
    with _database(path) as db:
        for _attempt in range(3):
            signature = _signature(store)
            if not store.exists:
                raise FileNotFoundError("Session does not exist")
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT signature,total FROM metadata WHERE id=1").fetchone()
            if not row or row[0] != signature:
                total = _rebuild(store, db)
                db.execute("INSERT OR REPLACE INTO metadata VALUES (1,?,?)", (signature, total))
            else:
                total = row[1]
            end = max(0, min(total, int(before) if before is not None else total))
            start = max(0, end - min(max(1, limit), 1000))
            messages = [{"position": position, **json.loads(payload)} for position, payload in db.execute(
                "SELECT position,payload FROM messages WHERE position>=? AND position<? ORDER BY position",
                (start, end))]
            if signature != _signature(store):
                db.rollback()
                continue
            db.commit()
            return {"messages": messages, "before": start, "has_more": start > 0, "total": total,
                    "revision": hashlib.sha256(signature.encode()).hexdigest()}
    raise OSError("Session changed while loading history; retry the page")
