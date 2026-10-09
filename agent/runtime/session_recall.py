#!/usr/bin/env python3
"""
Session Recall — Long-term conversation search for the local agent.

Usage (from agent tools):
    from agent.runtime.session_recall import SessionRecall
    sr = SessionRecall()
    sr.init_db()
    sr.log_message(session_id, "user", "hello")
    sr.log_message(session_id, "assistant", "hi back")
    results = sr.search("hello", limit=5)
    sessions = sr.browse(limit=10)
"""

from agent.runtime.paths import state_dir

import json
import logging
import os
import re
import sqlite3
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

DB_DIR = state_dir()
DB_PATH = DB_DIR / "sessions.db"
SESSION_RECALL_DB_ENV = "ASTRA_SESSION_RECALL_DB"
logger = logging.getLogger(__name__)

_FTS_TRIGGERS_SQL = """
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content)
    VALUES('delete', old.id, old.content);
END;
CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content)
    VALUES('delete', old.id, old.content);
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;
"""
_DROP_FTS_TRIGGERS_SQL = """
DROP TRIGGER IF EXISTS messages_ai;
DROP TRIGGER IF EXISTS messages_ad;
DROP TRIGGER IF EXISTS messages_au;
"""


def _session_id() -> str:
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%d_%H%M%S")
    suffix = uuid.uuid4().hex[:6]
    return f"{stamp}_{suffix}"


def _fmt_ts(ts: Optional[float]) -> str:
    if ts is None:
        return "unknown"
    try:
        dt = datetime.fromtimestamp(ts)
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(ts)


# A scroll returns its anchor message whole up to this bound, so one long
# message can be read; its neighbours and all other views are short excerpts.
SCROLL_ANCHOR_CHARS = 20_000
_SCROLL_NEIGHBOUR_CHARS = 500
_WINDOW_CHARS = 200
_LIKE_SNIPPET_CHARS = 200
_LIKE_TOKEN = re.compile(r'"([^"]*)"|(\S+)')


def _mark_cut(item: Dict[str, Any], shown: str, full: Optional[str]) -> Dict[str, Any]:
    """Flag an item whose text is only part of the stored message."""
    full = full or ""
    if shown != full:
        item["truncated"] = True
        item["content_chars"] = len(full)
    return item


class SearchResults(List[Dict[str, Any]]):
    """Search rows plus how the query was matched.

    ``matching`` is "fts" for the full-text index and "substring" when the
    query fell back to LIKE; ``note`` then says why and what that changes.
    """

    matching = "fts"
    note = ""


def _like_alternatives(query: str) -> List[tuple[List[str], List[str]]]:
    """Read an FTS-style query as OR-alternatives of (required, excluded) substrings."""
    alternatives: List[tuple[List[str], List[str]]] = [([], [])]
    negate = False
    for phrase, word in _LIKE_TOKEN.findall(query):
        if word == "OR":
            alternatives.append(([], []))
            negate = False
        elif word == "NOT":
            negate = True
        elif word != "AND":
            term = (phrase or word).replace("*", "").strip('"').strip()
            if term:
                alternatives[-1][1 if negate else 0].append(term)
            negate = False
    # An alternative with nothing required would match almost every message.
    return [item for item in alternatives if item[0]]


def configured_session_recall_path() -> Path:
    configured = os.getenv(SESSION_RECALL_DB_ENV, "").strip()
    return Path(configured).expanduser() if configured else Path(DB_PATH).expanduser()


class SessionRecall:
    """Persistent session history with FTS5 search."""

    def __init__(self, db_path: Union[str, Path, None] = None):
        self._db_path = (
            Path(db_path).expanduser()
            if db_path is not None
            else configured_session_recall_path()
        )
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None
        self._fts_stale = False

    # ── connection ────────────────────────────────────────────

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(
                str(self._db_path), timeout=5.0, check_same_thread=False
            )
            self._conn.row_factory = sqlite3.Row
            try:
                self._conn.execute("PRAGMA busy_timeout=5000")
                try:
                    self._conn.execute("PRAGMA journal_mode=WAL")
                except sqlite3.OperationalError as exc:
                    # WAL can be unavailable on locked or WSL-mounted
                    # databases. Recall is best-effort, so continue with the
                    # default journal mode instead of failing every request.
                    logger.warning(
                        "Session Recall WAL unavailable; falling back to default journal mode: %s",
                        exc,
                    )
                self._conn.execute("PRAGMA foreign_keys=ON")
            except sqlite3.Error:
                self._conn.close()
                self._conn = None
                raise
        return self._conn

    def close(self):
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ── schema ────────────────────────────────────────────────

    def init_db(self):
        """Create tables and FTS5 index if they don't exist."""
        try:
            self._init_db_once()
        except sqlite3.OperationalError:
            # A transient disk/lock failure during restart should not make
            # recall permanently unavailable for the process.
            self.close()
            time.sleep(0.1)
            self._init_db_once()

    def _init_db_once(self):
        conn = self._get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                title TEXT DEFAULT '',
                started_at REAL NOT NULL,
                ended_at REAL,
                message_count INTEGER DEFAULT 0,
                personality TEXT DEFAULT '',
                source_session_key TEXT,
                workspace_key TEXT,
                workspace_root TEXT
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL REFERENCES sessions(id),
                role TEXT NOT NULL CHECK(role IN ('user','assistant','tool','system')),
                content TEXT DEFAULT '',
                tool_name TEXT DEFAULT '',
                timestamp REAL NOT NULL,
                msg_index INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_messages_session
                ON messages(session_id, msg_index);

            CREATE TABLE IF NOT EXISTS recall_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)
        stale = conn.execute(
            "SELECT 1 FROM recall_meta WHERE key='fts_stale' AND value='1'"
        ).fetchone() is not None
        fts_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages_fts'"
        ).fetchone() is not None
        if stale:
            try:
                self._rebuild_fts(conn)
            except sqlite3.DatabaseError as exc:
                logger.warning("Session Recall FTS rebuild failed; search remains degraded: %s", exc)
                self._detach_fts(conn, exc)
        elif not fts_exists:
            conn.executescript("""
                CREATE VIRTUAL TABLE messages_fts USING fts5(
                    content, content=messages, content_rowid=id, tokenize='trigram'
                );
            """ + _FTS_TRIGGERS_SQL)
        else:
            conn.executescript(_FTS_TRIGGERS_SQL)
        # Migrate legacy unicode61 FTS index to trigram for CJK support.
        fts_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='messages_fts'"
        ).fetchone()
        if fts_sql and "unicode61" in str(fts_sql[0]):
            self._migrate_fts_trigram(conn)
        session_columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(sessions)").fetchall()
        }
        if "source_session_key" not in session_columns:
            conn.execute("ALTER TABLE sessions ADD COLUMN source_session_key TEXT")
        if "workspace_key" not in session_columns:
            conn.execute("ALTER TABLE sessions ADD COLUMN workspace_key TEXT")
        if "workspace_root" not in session_columns:
            conn.execute("ALTER TABLE sessions ADD COLUMN workspace_root TEXT")
        conn.executescript("""
            CREATE INDEX IF NOT EXISTS idx_messages_timestamp
                ON messages(timestamp DESC, id DESC);
            CREATE INDEX IF NOT EXISTS idx_messages_session_timestamp
                ON messages(session_id, timestamp DESC, id DESC);
            CREATE INDEX IF NOT EXISTS idx_sessions_workspace_key
                ON sessions(workspace_key);
        """)
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_source_key "
            "ON sessions(source_session_key) WHERE source_session_key IS NOT NULL"
        )
        conn.commit()

    def _migrate_fts_trigram(self, conn: sqlite3.Connection) -> None:
        """Rebuild the FTS index with the trigram tokenizer for CJK support."""
        self._rebuild_fts(conn)

    @staticmethod
    def _is_fts_write_failure(exc: sqlite3.DatabaseError) -> bool:
        text = str(exc).lower()
        return any(marker in text for marker in (
            "fts", "malformed", "corrupt", "database disk image is malformed",
            "vtable", "shadow table",
        ))

    def _detach_fts(self, conn: sqlite3.Connection, exc: BaseException) -> None:
        """Atomically mark derived search stale and detach its write triggers."""
        conn.rollback()
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO recall_meta(key, value) VALUES('fts_stale', '1') "
                "ON CONFLICT(key) DO UPDATE SET value='1'"
            )
            for trigger in ("messages_ai", "messages_ad", "messages_au"):
                conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        self._fts_stale = True
        logger.error(
            "Session Recall FTS disabled after write failure; canonical messages remain writable: %s",
            exc,
        )

    def _rebuild_fts(self, conn: sqlite3.Connection) -> None:
        """Rebuild all derived rows before atomically restoring sync triggers."""
        conn.executescript(
            "BEGIN IMMEDIATE;"
            + _DROP_FTS_TRIGGERS_SQL
            + """
            DROP TABLE IF EXISTS messages_fts;
            CREATE VIRTUAL TABLE messages_fts USING fts5(
                content, content=messages, content_rowid=id, tokenize='trigram'
            );
            INSERT INTO messages_fts(messages_fts) VALUES('rebuild');
            DELETE FROM recall_meta WHERE key='fts_stale';
            """
            + _FTS_TRIGGERS_SQL
            + "COMMIT;"
        )
        self._fts_stale = False

    # ── session lifecycle ─────────────────────────────────────

    def create_session(
        self,
        title: str = "",
        personality: str = "",
        *,
        workspace_key: str = "",
        workspace_root: str = "",
    ) -> str:
        """Start a new session, return its id."""
        sid = _session_id()
        now = time.time()
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO sessions "
            "(id, title, started_at, personality, workspace_key, workspace_root) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (sid, title, now, personality, workspace_key, workspace_root),
        )
        conn.commit()
        return sid

    def get_or_create_session(
        self,
        source_session_key: str,
        *,
        title: str = "",
        personality: str = "",
        workspace_key: str = "",
        workspace_root: str = "",
    ) -> str:
        """Resolve one durable Recall session for an application session.

        Reopening a previously closed application session clears ``ended_at``
        instead of creating a duplicate row or appending to a closed record.
        """
        key = str(source_session_key or "").strip()
        if not key:
            raise ValueError("source_session_key must be non-empty")
        conn = self._get_conn()
        row = conn.execute(
            "SELECT id FROM sessions WHERE source_session_key = ?",
            (key,),
        ).fetchone()
        if row is not None:
            sid = str(row["id"])
            conn.execute(
                "UPDATE sessions SET ended_at = NULL, "
                "title = CASE WHEN ? <> '' THEN ? ELSE title END, "
                "personality = CASE WHEN ? <> '' THEN ? ELSE personality END, "
                "workspace_key = CASE WHEN ? <> '' THEN ? ELSE workspace_key END, "
                "workspace_root = CASE WHEN ? <> '' THEN ? ELSE workspace_root END "
                "WHERE id = ?",
                (
                    title,
                    title,
                    personality,
                    personality,
                    workspace_key,
                    workspace_key,
                    workspace_root,
                    workspace_root,
                    sid,
                ),
            )
            conn.commit()
            return sid

        sid = _session_id()
        try:
            conn.execute(
                "INSERT INTO sessions "
                "(id, title, started_at, personality, source_session_key, workspace_key, workspace_root) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (sid, title, time.time(), personality, key, workspace_key, workspace_root),
            )
            conn.commit()
            return sid
        except sqlite3.IntegrityError:
            # Another writer may have created the same durable mapping.
            conn.rollback()
            row = conn.execute(
                "SELECT id FROM sessions WHERE source_session_key = ?",
                (key,),
            ).fetchone()
            if row is None:
                raise
            return str(row["id"])

    def close_session(self, session_id: str):
        conn = self._get_conn()
        conn.execute(
            "UPDATE sessions SET ended_at = ?, message_count = ("
            "SELECT COUNT(*) FROM messages WHERE session_id = ?"
            ") WHERE id = ?",
            (time.time(), session_id, session_id),
        )
        conn.commit()

    # ── logging ───────────────────────────────────────────────

    def log_message(
        self,
        session_id: str,
        role: str,
        content: str,
        tool_name: str = "",
    ) -> int:
        """Write one message to the DB. Returns the message id."""
        conn = self._get_conn()
        # get next msg_index
        row = conn.execute(
            "SELECT COALESCE(MAX(msg_index), 0) + 1 FROM messages WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        next_idx = row[0]
        now = time.time()
        try:
            conn.execute(
                "INSERT INTO messages (session_id, role, content, tool_name, timestamp, msg_index) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, role, content, tool_name, now, next_idx),
            )
            conn.execute(
                "UPDATE sessions SET message_count = message_count + 1 WHERE id = ?",
                (session_id,),
            )
            conn.commit()
        except sqlite3.DatabaseError as exc:
            if not self._is_fts_write_failure(exc):
                conn.rollback()
                raise
            self._detach_fts(conn, exc)
            conn.execute(
                "INSERT INTO messages (session_id, role, content, tool_name, timestamp, msg_index) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, role, content, tool_name, now, next_idx),
            )
            conn.execute(
                "UPDATE sessions SET message_count = message_count + 1 WHERE id = ?",
                (session_id,),
            )
            conn.commit()
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    # ── search ────────────────────────────────────────────────

    @staticmethod
    def _source_filter_sql(source_type: str) -> str:
        """Return a fixed SQL predicate for one Recall source category."""
        if source_type == "":
            return "1 = 1"
        if source_type == "hermes":
            return "s.source_session_key LIKE 'hermes:%'"
        if source_type == "api":
            return "substr(s.source_session_key, 1, 12) = 'session_api_'"
        if source_type == "astra":
            return (
                "(s.source_session_key IS NULL OR "
                "(s.source_session_key NOT LIKE 'hermes:%' AND "
                "substr(s.source_session_key, 1, 12) <> 'session_api_'))"
            )
        raise ValueError("source_type must be one of: astra, hermes, api")

    def search(
        self,
        query: str,
        limit: int = 5,
        window: int = 3,
        sort: Optional[str] = None,
        source_type: str = "",
    ) -> List[Dict[str, Any]]:
        """FTS5 full-text search across all session messages.

        Returns list of dicts, each with:
          message_id, session_id, role, content snippet, timestamp,
          session_title, and a window_context list.
        """
        if not query or not query.strip():
            return []
        source_filter = self._source_filter_sql(source_type)

        def like(reason: str) -> List[Dict[str, Any]]:
            return self._search_like(
                query.strip(), limit, source_type=source_type, window=window, sort=sort, reason=reason,
            )

        if self._fts_stale:
            return like("the full-text index is unavailable")

        # Trigram tokenizer requires >= 3 chars per *search term* for MATCH.
        # Strip FTS5 boolean operators (OR / NOT / AND) and quoted phrases
        # before checking lengths so operators don't trigger LIKE fallback.
        clean = query.strip()
        _no_quotes = re.sub(r'"[^"]*"', ' ', clean)
        _no_ops = re.sub(r'\b(?:OR|NOT|AND)\b', '', _no_quotes)
        _no_ops = _no_ops.replace('*', '')
        actual_terms = _no_ops.split()
        if any(len(t) < 3 for t in actual_terms):
            return like("a query term is shorter than 3 characters, which the full-text index cannot match")
        # Strip FTS5 prefix operators that trigram doesn't support.
        query = query.replace("*", "")

        conn = self._get_conn()

        order_sql = "ORDER BY rank"
        if sort == "newest":
            order_sql = "ORDER BY m.timestamp DESC, rank"
        elif sort == "oldest":
            order_sql = "ORDER BY m.timestamp ASC, rank"

        sql = f"""
            SELECT
                m.id, m.session_id, m.role,
                snippet(messages_fts, 0, '>>>', '<<<', '...', 40) AS snippet,
                m.content, m.timestamp,
                s.title AS session_title,
                s.started_at
            FROM messages_fts
            JOIN messages m ON m.rowid = messages_fts.rowid
            JOIN sessions s ON s.id = m.session_id
            WHERE messages_fts MATCH ? AND {source_filter}
            {order_sql}
            LIMIT ?
        """
        try:
            rows = conn.execute(sql, (query, limit)).fetchall()
        except sqlite3.DatabaseError:
            return like("the full-text index rejected the query syntax")

        results = SearchResults()
        for row in rows:
            snippet = row["snippet"] or (row["content"][:120] if row["content"] else "")
            r = _mark_cut({
                "message_id": row["id"],
                "session_id": row["session_id"],
                "role": row["role"],
                "snippet": snippet,
                "timestamp": _fmt_ts(row["timestamp"]),
                "session_title": row["session_title"],
            }, snippet.replace(">>>", "").replace("<<<", ""), row["content"])
            r["window_context"] = self._get_window(
                row["session_id"], row["id"], window
            )
            results.append(r)

        return results

    def _search_like(
        self,
        query: str,
        limit: int,
        *,
        source_type: str = "",
        window: int = 3,
        sort: Optional[str] = None,
        reason: str = "",
    ) -> List[Dict[str, Any]]:
        """Substring fallback for queries the trigram index cannot answer.

        Terms are AND-ed, ``OR`` separates alternatives and ``NOT`` excludes.
        When no message holds every term of a plain multi-word query, messages
        holding any one of them are returned instead.
        """
        conn = self._get_conn()
        source_filter = self._source_filter_sql(source_type)
        alternatives = _like_alternatives(query)
        order = "ASC" if sort == "oldest" else "DESC"

        def _run(choices: List[tuple[List[str], List[str]]]) -> list:
            where = " OR ".join(
                "(" + " AND ".join(
                    ["m.content LIKE ?"] * len(required) + ["m.content NOT LIKE ?"] * len(excluded)
                ) + ")"
                for required, excluded in choices
            )
            params = [f"%{term}%" for required, excluded in choices for term in (*required, *excluded)]
            return conn.execute(
                "SELECT m.id, m.session_id, m.role, m.content AS snippet, "
                "m.content, m.timestamp, s.title AS session_title, s.started_at "
                "FROM messages m JOIN sessions s ON s.id = m.session_id "
                f"WHERE ({where}) AND {source_filter} "
                "AND m.role IN ('user','assistant') "
                f"ORDER BY m.timestamp {order} LIMIT ?",
                (*params, limit),
            ).fetchall()

        rows = _run(alternatives) if alternatives else []
        any_term = False
        if not rows and len(alternatives) == 1 and len(alternatives[0][0]) > 1:
            required, excluded = alternatives[0]
            rows = _run([([term], excluded) for term in required])
            any_term = bool(rows)

        results = SearchResults()
        results.matching = "substring"
        results.note = (
            f"Substring matching was used because {reason or 'the full-text index could not be used'}. "
            "Every term must appear as a substring of the message, OR separates alternatives and NOT excludes; "
            "relevance ranking does not apply, so results are newest first unless sort is 'oldest'."
            + (" No message contained all the terms, so messages containing any one of them are listed."
               if any_term else "")
        )
        terms = [term.lower() for required, _excluded in alternatives for term in required]
        for row in rows:
            content = row["content"] or ""
            # Show the neighbourhood of a matched term, not an unrelated opening.
            lowered = content.lower()
            hit = min((position for position in map(lowered.find, terms) if position >= 0), default=0)
            begin = max(0, hit - _LIKE_SNIPPET_CHARS // 4)
            snippet = content[begin:begin + _LIKE_SNIPPET_CHARS]
            item = _mark_cut({
                "message_id": row["id"],
                "session_id": row["session_id"],
                "role": row["role"],
                "snippet": snippet,
                "timestamp": _fmt_ts(row["timestamp"]),
                "session_title": row["session_title"],
            }, snippet, content)
            item["window_context"] = self._get_window(row["session_id"], row["id"], window)
            results.append(item)
        return results

    # ── browse ────────────────────────────────────────────────

    def browse(
        self,
        limit: int = 10,
        *,
        source_type: str = "",
    ) -> List[Dict[str, Any]]:
        """List recent sessions with previews."""
        conn = self._get_conn()
        source_filter = self._source_filter_sql(source_type)
        rows = conn.execute(
            "SELECT s.id, s.title, s.started_at, s.ended_at, s.message_count, "
            "  (SELECT content FROM messages WHERE session_id = s.id "
            "   AND role = 'user' ORDER BY msg_index ASC LIMIT 1) AS first_msg, "
            "  (SELECT MIN(id) FROM messages WHERE session_id = s.id) AS first_id, "
            "  (SELECT MAX(id) FROM messages WHERE session_id = s.id) AS last_id "
            f"FROM sessions s WHERE {source_filter} "
            "ORDER BY s.started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()

        results = []
        for row in rows:
            preview = (row["first_msg"] or "")[:120]
            results.append({
                "session_id": row["id"],
                "title": row["title"],
                "started": _fmt_ts(row["started_at"]),
                "ended": _fmt_ts(row["ended_at"]),
                "messages": row["message_count"],
                # What a scroll needs to open this session from either end.
                "first_message_id": row["first_id"],
                "last_message_id": row["last_id"],
                "preview": preview,
            })
        return results

    # ── scroll ────────────────────────────────────────────────

    def scroll(self, session_id: str, around_msg_id: int, window: int = 5) -> Dict[str, Any]:
        """Scroll within a session around a specific message id.

        Returns dict with window, messages_before, messages_after. The anchor
        message is returned whole up to SCROLL_ANCHOR_CHARS; its neighbours are
        cut short and marked. An empty window means the anchor is not in that
        session; ``anchor_session_id`` then names the session that holds it.
        """
        conn = self._get_conn()
        # check anchor exists
        anchor = conn.execute(
            "SELECT 1 FROM messages WHERE id = ? AND session_id = ? LIMIT 1",
            (around_msg_id, session_id),
        ).fetchone()
        if not anchor:
            elsewhere = conn.execute(
                "SELECT session_id FROM messages WHERE id = ? LIMIT 1", (around_msg_id,),
            ).fetchone()
            return {
                "window": [], "messages_before": 0, "messages_after": 0,
                "anchor_session_id": elsewhere["session_id"] if elsewhere else "",
            }

        before = conn.execute(
            "SELECT id, role, content, tool_name, timestamp FROM messages "
            "WHERE session_id = ? AND id <= ? ORDER BY id DESC LIMIT ?",
            (session_id, around_msg_id, window + 1),
        ).fetchall()
        after = conn.execute(
            "SELECT id, role, content, tool_name, timestamp FROM messages "
            "WHERE session_id = ? AND id > ? ORDER BY id ASC LIMIT ?",
            (session_id, around_msg_id, window),
        ).fetchall()

        combined = list(reversed(before)) + list(after)
        msgs = []
        for m in combined:
            limit = SCROLL_ANCHOR_CHARS if m["id"] == around_msg_id else _SCROLL_NEIGHBOUR_CHARS
            shown = (m["content"] or "")[:limit]
            msgs.append(_mark_cut({
                "id": m["id"],
                "role": m["role"],
                "content": shown,
                "tool_name": m["tool_name"],
                "timestamp": _fmt_ts(m["timestamp"]),
            }, shown, m["content"]))

        msgs_before = max(0, len(before) - 1)
        msgs_after = len(after)
        return {
            "window": msgs,
            "messages_before": msgs_before,
            "messages_after": msgs_after,
            "has_more_before": msgs_before >= window,
            "has_more_after": msgs_after >= window,
        }

    # ── internal ──────────────────────────────────────────────

    def _get_window(
        self, session_id: str, around_msg_id: int, window: int = 3
    ) -> List[Dict[str, Any]]:
        """Get ±window messages around a given message id."""
        conn = self._get_conn()

        before = conn.execute(
            "SELECT id, role, content FROM messages "
            "WHERE session_id = ? AND id <= ? "
            "ORDER BY id DESC LIMIT ?",
            (session_id, around_msg_id, window + 1),
        ).fetchall()
        after = conn.execute(
            "SELECT id, role, content FROM messages "
            "WHERE session_id = ? AND id > ? "
            "ORDER BY id ASC LIMIT ?",
            (session_id, around_msg_id, window),
        ).fetchall()

        combined = list(reversed(before)) + list(after)
        return [
            _mark_cut({
                "id": m["id"],
                "role": m["role"],
                "content": (m["content"] or "")[:_WINDOW_CHARS],
            }, (m["content"] or "")[:_WINDOW_CHARS], m["content"])
            for m in combined
        ]


def _test() -> None:
    """Exercise the archive without reading or changing an installation's data."""
    with tempfile.TemporaryDirectory(prefix="astra-session-recall-test-") as temporary:
        sr = SessionRecall(Path(temporary) / "sessions.db")
        try:
            sr.init_db()
            sid = sr.create_session(title="self-test")
            sr.log_message(sid, "user", "A synthetic message about archive search.")
            sr.log_message(sid, "assistant", "This temporary archive is only a demonstration.")
            sr.close_session(sid)
            print("Session created:", sid)
            print("Browse:", json.dumps(sr.browse(), indent=2, ensure_ascii=False))
            print("Search 'synthetic':", json.dumps(sr.search("synthetic"), indent=2, ensure_ascii=False))
        finally:
            sr.close()


if __name__ == "__main__":
    _test()
