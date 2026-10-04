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
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, TextIO

from agent.cli.images import message_display_text
from agent.runtime.file_change_time import change_time_token
from agent.runtime.conversation_branches import BranchConflict, ConversationBranches, checked_branch_id
from agent.runtime.paths import state_path
from agent.runtime.session_store import SessionStore

_VERSION = 2
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
        self.db.execute(f"DELETE FROM raw_{self.table}")
        self.db.execute(f"DELETE FROM calls_{self.table}")
        self.raw_count = self.total = 0
        self.hidden = False

    def add(self, message: object) -> None:
        if not isinstance(message, dict):
            raise ValueError("Session message must be an object")
        raw_index = self.raw_count
        self.raw_count += 1
        from agent.runtime.message_source import message_source_ref
        from agent.ui.session_log import MAX_RECORD_BYTES
        reference = message_source_ref(message, raw_index)
        tool_calls = message.get("tool_calls")
        calls = [call["id"] for call in tool_calls
                 if isinstance(call, dict) and isinstance(call.get("id"), str) and 1 <= len(call["id"]) <= 512] if isinstance(tool_calls, list) else []
        if isinstance(message.get("tool_call_id"), str) and 1 <= len(message["tool_call_id"]) <= 512:
            calls.append(message["tool_call_id"])
        # Keep only bounded JSON excerpts in the new inspection projection.
        # The source parser already holds one canonical message; avoid another
        # history-sized raw copy in memory or in the disposable cache.
        excerpt = bytearray()
        raw_bytes = 0
        for chunk in json.JSONEncoder(ensure_ascii=False).iterencode(message):
            for offset in range(0, len(chunk), _CHUNK):
                encoded = chunk[offset:offset + _CHUNK].encode("utf-8")
                raw_bytes += len(encoded)
                if len(excerpt) < MAX_RECORD_BYTES:
                    excerpt.extend(encoded[:MAX_RECORD_BYTES - len(excerpt)])
        self.db.execute(f"INSERT INTO raw_{self.table} VALUES (?,?,?,?,?,?)",
                        (raw_index, reference["digest"], bytes(excerpt),
                         str(message.get("role", ""))[:128], None, raw_bytes))
        self.db.executemany(f"INSERT INTO calls_{self.table} VALUES (?,?)",
                            ((raw_index, call) for call in dict.fromkeys(calls)))
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
        self.db.execute(f"UPDATE raw_{self.table} SET chat_position=? WHERE position=?", (position, raw_index))
        payload = {"role": message["role"],
                   "content": message_display_text(message.get("display_command", message.get("content", ""))),
                   "timestamp": message.get("timestamp"), "source_ref": reference}
        self.db.execute(f"INSERT INTO {self.table} VALUES (?,?)",
                        (position, json.dumps(payload, ensure_ascii=False)))

    def adopt(self, staged: _Projection) -> None:
        # Swap table names instead of copying another history-sized set of
        # text rows. The previous generation becomes reusable scratch space.
        for prefix in ("", "raw_", "calls_"):
            self.db.execute(f"ALTER TABLE {prefix}messages RENAME TO {prefix}discarded")
            self.db.execute(f"ALTER TABLE {prefix}staged RENAME TO {prefix}messages")
            self.db.execute(f"ALTER TABLE {prefix}discarded RENAME TO {prefix}staged")
            self.db.execute(f"DELETE FROM {prefix}staged")
        self.raw_count, self.total, self.hidden = staged.raw_count, staged.total, staged.hidden


def _signature(store: SessionStore) -> str:
    values = []
    for path in (store.legacy_path, store.snapshot_path, store.jsonl_path, store.header_path):
        try:
            stat = path.stat()
            values.append((stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, change_time_token(path, stat)))
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
    staged.reset()
    return current.total


def _cache_path(store: SessionStore, *, version: int = _VERSION) -> Path:
    return _source_cache_path(store.legacy_path, version=version)


def _source_cache_path(source: Path, *, version: int = _VERSION) -> Path:
    cache = Path(os.environ.get("ASTRA_GUI_HISTORY_CACHE", "").strip() or state_path("gui", "history-index"))
    identity = hashlib.sha256(str(source.resolve()).encode()).hexdigest()
    return cache / f"v{version}-{identity}.sqlite3"


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
        for table in ("messages", "staged"):
            db.execute(f"CREATE TABLE IF NOT EXISTS raw_{table} (position INTEGER PRIMARY KEY, digest TEXT NOT NULL, payload BLOB NOT NULL, role TEXT NOT NULL, chat_position INTEGER, raw_bytes INTEGER NOT NULL)")
            db.execute(f"CREATE TABLE IF NOT EXISTS calls_{table} (position INTEGER NOT NULL, call_id TEXT NOT NULL, PRIMARY KEY (position, call_id))")
            db.execute(f"CREATE INDEX IF NOT EXISTS calls_{table}_id ON calls_{table} (call_id)")
            db.execute(f"CREATE INDEX IF NOT EXISTS raw_{table}_digest ON raw_{table} (digest)")
        db.commit()
        yield db
    finally:
        db.close()


def discard_history_index(store: SessionStore, *, related_paths: list[Path] | None = None) -> None:
    """Remove this session's known cache generations, waiting for active readers."""
    # Upgrading the projection leaves the old private presentation copy on
    # disk. Delete only exact identities for schemas we created, never glob
    # across other sessions or infer a source path from cache contents.
    related = store.related_paths() if related_paths is None else related_paths
    heads = ConversationBranches(store.logical_path).root / "heads"
    sources = {store.logical_path, store.legacy_path}
    for path in related:
        # Paths came from SessionStore's symlink-checked ownership inventory.
        # Include orphan heads too: a failed publication can leave their files
        # behind, but must not leave a recoverable private presentation cache.
        if path.parent == heads and (match := re.fullmatch(r"([0-9a-f]{32})(?:\.snapshot|\.header)?\.(?:json|jsonl)", path.name)):
            sources.add(heads / f"{match[1]}.json")
    for source in sources:
        for version in (1, _VERSION):
            _discard_index(_source_cache_path(source, version=version))


def _discard_index(path: Path) -> None:
    if not path.exists():
        return
    cleared = False
    try:
        with _database(path) as db:
            db.execute("BEGIN IMMEDIATE")
            for table in ("messages", "staged", "raw_messages", "raw_staged", "calls_messages", "calls_staged", "metadata"):
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


def check_query_branch(store: SessionStore, branch_id: str | None = None) -> str:
    """Keep a pinned read view on the currently selected logical branch."""
    if branch_id is not None and checked_branch_id(branch_id) != store.branch_id:
        raise BranchConflict("Conversation branch changed; refresh before reading")
    active, _ = ConversationBranches(store.logical_path).resolve()
    if active != store.branch_id:
        raise BranchConflict("Conversation branch changed while reading; refresh before continuing")
    return active


def history_page(store: SessionStore, *, before: int | None = None, limit: int = 200, source_ref: dict | None = None,
                 branch_id: str | None = None) -> dict:
    """Return a stable page without keeping complete histories in memory."""
    check_query_branch(store, branch_id)
    if source_ref is not None:
        from agent.ui.session_log import checked_source_ref
        checked_source_ref(source_ref)
    path = _cache_path(store)
    for attempt in range(2):
        try:
            result = _read_page(store, path, before=before, limit=limit, source_ref=source_ref)
            check_query_branch(store, branch_id)
            return {**result, "branch_id": store.branch_id}
        except sqlite3.DatabaseError as exc:
            if attempt or not _corrupt(exc):
                raise
            # _read_page has already closed its connection, including failures
            # during SELECT/rebuild. Never discard caches for locks/permissions.
            path.unlink(missing_ok=True)
    raise AssertionError("Unreachable history cache retry")


def _read_page(store: SessionStore, path: Path, *, before: int | None, limit: int, source_ref: dict | None = None) -> dict:
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
            target = {}
            if source_ref is not None:
                from agent.ui.session_log import chat_source, resolve_source
                target_status, target_index = resolve_source(db, source_ref)
                chat_position = None
                if target_index is not None:
                    _, chat_position = chat_source(db, target_index)
                    if chat_position is None:
                        target_status = "unmapped"
                    else:
                        before = min(total, chat_position + max(1, limit // 2) + 1)
                target = {"target_status": target_status, "target_position": chat_position}
            end = max(0, min(total, int(before) if before is not None else total))
            start = max(0, end - min(max(1, limit), 1000))
            messages = [{"position": position, **json.loads(payload)} for position, payload in db.execute(
                "SELECT position,payload FROM messages WHERE position>=? AND position<? ORDER BY position",
                (start, end))]
            if source_ref is not None and target["target_status"] != "found":
                messages = []
            if signature != _signature(store):
                db.rollback()
                continue
            db.commit()
            return {"messages": messages, "before": start, "has_more": start > 0, "total": total,
                    "revision": hashlib.sha256(signature.encode()).hexdigest(), **target}
    raise OSError("Session changed while loading history; retry the page")
