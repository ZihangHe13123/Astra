"""Bounded, read-only Session Recall recommendations for Context Index."""

import hashlib
import re
import sqlite3
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .models import (
    EvidenceResult,
    RecommendationCandidate,
    SourceLocator,
    SourceResult,
)
from .sqlite_reader import observe_stage, open_readonly, set_read_window
from .lexical import LexicalQuery
from .query import QueryPlan
from .workspace import WorkspaceIdentity

_CHANNEL_LIMIT = 12
_POOL_BASE_LIMIT = _CHANNEL_LIMIT
_POOL_HEADROOM_LIMIT = 64
_MAX_ACTIVE_FINGERPRINTS = 512
_MAX_DESCRIPTION_CHARS = 240
_MAX_EVIDENCE_CONTENT_CHARS = 500
_MAX_EVIDENCE_WINDOW = 5
_REQUIRED_COLUMNS = {
    # workspace columns were introduced after the original Session Recall
    # archive.  Recommendation reads must keep the original archive usable;
    # legacy rows are deliberately treated as global fallback rather than
    # performing a writer-side migration.
    "sessions": {"id", "title", "started_at"},
    "messages": {"id", "session_id", "role", "content", "timestamp", "msg_index"},
}
_FTS_OPERATORS = {"and", "or", "not"}
_SQL_SHAPED_RE = re.compile(
    r"\b(?:insert\s+into\b|update\s+\S+\s+set\b|delete\s+from\b|"
    r"(?:create|alter|drop)\s+(?:table|index|view|trigger|database)\b|pragma\b)",
    flags=re.IGNORECASE,
)
_UPPERCASE_SELECT_FROM_RE = re.compile(r"\bSELECT\b.+?\bFROM\b", re.DOTALL)
_SELECT_FROM_RE = re.compile(
    r"\bselect\b(?P<select_list>.*?)\bfrom\b(?P<tail>.*)",
    flags=re.IGNORECASE | re.DOTALL,
)
_SELECT_LIST_EVIDENCE_RE = re.compile(
    r"[,*`\"]|[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]*"
)
_SQL_TAIL_TOKEN_RE = re.compile(
    r"\b(?:where|join|group\s+by|order\s+by|having|limit|union)\b",
    flags=re.IGNORECASE,
)
_SQL_STATEMENT_TERMINATOR_RE = re.compile(r";")
_EXCEPTION_SHAPED_RE = re.compile(
    r"traceback\s*\(most recent call last\)|"
    r"\b[A-Za-z_][\w.]*(?:Error|Exception):(?=\s|$)|"
    r"\bFile\s+[\"'][^\"']+[\"'],\s+line\s+\d+|"
    r"\bat\s+\S+\s+\([^)]+:\d+(?::\d+)?\)",
    flags=re.IGNORECASE,
)
_POSIX_ABSOLUTE_PATH_RE = re.compile(
    r"(?<![\w:/])/(?!/)(?:[^\s/,:;()\[\]{}]+/)*[^\s/,:;()\[\]{}]+"
)
_WINDOWS_DRIVE_PATH_RE = re.compile(
    r"(?<![\w])[A-Za-z]:[\\/](?:[^\\/\s,:;()\[\]{}]+[\\/])*"
    r"[^\\/\s,:;()\[\]{}]+"
)
_WINDOWS_UNC_PATH_RE = re.compile(
    r"(?<!\\)\\\\(?:[^\\\s,:;()\[\]{}]+\\)+[^\\\s,:;()\[\]{}]+"
)


class _IncompatibleSchema(Exception):
    pass


@dataclass(frozen=True)
class _PreparedSessionRow:
    id: int
    session_id: str
    role: str
    content: str
    timestamp: float
    title: str
    workspace_key: str
    preview: str
    native_query_rank: float
    workspace_tier: int


def content_fingerprint(text: str) -> str:
    """Return a stable fingerprint after case and whitespace normalization."""
    normalized = " ".join(str(text).casefold().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _multimodal_text(content: object) -> str:
    if not isinstance(content, list):
        return str(content or "")
    text_parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") not in {"text", "input_text", "output_text"}:
            continue
        text = block.get("text")
        if text is not None:
            text_parts.append(str(text))
    return " ".join(text_parts)


def active_context_fingerprints(messages: Sequence[dict]) -> frozenset[str]:
    """Fingerprint visible conversational text without reading image payloads."""
    return frozenset(
        content_fingerprint(_multimodal_text(message.get("content")))
        for message in messages
        if message.get("role") in {"user", "assistant"}
    )


def _normalized_tokens(value: object) -> str:
    folded = str(value or "").casefold()
    characters = [
        character if character.isalnum() else " "
        for character in folded
    ]
    return " ".join("".join(characters).split())


def _workspace_tier(
    workspace: WorkspaceIdentity,
    stored_workspace_key: object,
    title: object,
    preview: object,
    content: object,
) -> int:
    if workspace.key and str(stored_workspace_key or "").casefold() == workspace.key.casefold():
        return 0
    label = _normalized_tokens(workspace.label)
    searchable = _normalized_tokens(f"{title or ''} {preview or ''} {content or ''}")
    if label and f" {label} " in f" {searchable} ":
        return 1
    return 2


def _candidate_pool_limit(active_count: int) -> int:
    return _POOL_BASE_LIMIT + min(
        max(0, int(active_count)) + 1,
        _POOL_HEADROOM_LIMIT,
    )


def _eligible_rows(
    rows: Sequence[sqlite3.Row],
    workspace: WorkspaceIdentity,
    current_session_id: str,
    current_message_id: int | None,
    active_fingerprints: frozenset[str],
    *,
    relevance: bool,
) -> list[_PreparedSessionRow]:
    unique: dict[tuple[str, int], _PreparedSessionRow] = {}
    for row in rows:
        identity = (str(row["session_id"]), int(row["id"]))
        if identity in unique:
            continue
        if current_message_id is not None and identity == (
            current_session_id,
            current_message_id,
        ):
            continue
        content = str(row["content"] or "")
        if content_fingerprint(content) in active_fingerprints:
            continue
        tier = _workspace_tier(
            workspace,
            row["workspace_key"],
            row["title"],
            row["preview"],
            content,
        )
        unique[identity] = _PreparedSessionRow(
            id=identity[1],
            session_id=identity[0],
            role=str(row["role"]),
            content=content,
            timestamp=float(row["timestamp"]),
            title=str(row["title"] or ""),
            workspace_key=str(row["workspace_key"] or ""),
            preview=str(row["preview"] or ""),
            native_query_rank=float(row["native_query_rank"]),
            workspace_tier=tier,
        )

    def ordering(row: _PreparedSessionRow) -> tuple[object, ...]:
        private_identity = _identity(row.session_id, row.id)
        if relevance:
            return (
                row.workspace_tier,
                row.native_query_rank,
                -row.timestamp,
                private_identity,
            )
        return (row.workspace_tier, -row.timestamp, private_identity)

    return sorted(unique.values(), key=ordering)[:_CHANNEL_LIMIT]


def _bounded_text(value: object, limit: int) -> str:
    normalized = " ".join(str(value or "").split())
    return normalized[:limit]


def _is_sql_shaped(value: str) -> bool:
    """Detect SQL-shaped summaries without treating prose punctuation as SQL."""
    if _UPPERCASE_SELECT_FROM_RE.search(value):
        return True
    select_from = _SELECT_FROM_RE.search(value)
    if select_from:
        select_list = select_from.group("select_list")
        tail = select_from.group("tail")
        if _SELECT_LIST_EVIDENCE_RE.search(select_list):
            return True
        if _SQL_TAIL_TOKEN_RE.search(tail) or _SQL_STATEMENT_TERMINATOR_RE.search(tail):
            return True
    return _SQL_SHAPED_RE.search(value) is not None


def _public_summary(value: object, limit: int) -> str:
    normalized = " ".join(str(value or "").split())
    if _is_sql_shaped(normalized) or _EXCEPTION_SHAPED_RE.search(normalized):
        return ""
    without_paths = _WINDOWS_UNC_PATH_RE.sub("[path]", normalized)
    without_paths = _WINDOWS_DRIVE_PATH_RE.sub("[path]", without_paths)
    without_paths = _POSIX_ABSOLUTE_PATH_RE.sub("[path]", without_paths)
    return _bounded_text(without_paths, limit)


def _description(title: object, content: object) -> str:
    safe_title = _public_summary(title, 100)
    safe_content = _public_summary(content, 180)
    if safe_title and safe_content:
        return _bounded_text(f"{safe_title}: {safe_content}", _MAX_DESCRIPTION_CHARS)
    return safe_title or safe_content or "Historical session"


def _topic_key(title: object, content: object) -> str:
    topic = _bounded_text(title, 120) or _bounded_text(content, 120)
    return hashlib.sha256(topic.casefold().encode("utf-8")).hexdigest()[:24]


def _identity(session_id: object, message_id: object) -> str:
    private_identity = f"{session_id}\0{message_id}"
    return hashlib.sha256(private_identity.encode("utf-8")).hexdigest()[:24]


def session_revision(session_id: object, message_id: int, role: object, content: object, timestamp: float) -> str:
    """Bind semantic evidence to the exact canonical message version."""
    value = repr((str(session_id), int(message_id), str(role), str(content), float(timestamp)))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _plain_query_terms(query: str) -> list[str]:
    return [
        token
        for token in re.findall(r"\w+", str(query), flags=re.UNICODE)
        if token.casefold() not in _FTS_OPERATORS
    ]


def _fts_query(query: str) -> str | None:
    terms = _plain_query_terms(query)
    if not terms or any(len(term) < 3 for term in terms):
        return None
    return " ".join(f'"{term}"' for term in terms)


def _query_boundary(alias: str) -> str:
    # Exact replay identity also excludes later messages sharing a timestamp.
    return (
        " AND (context_index_query_id() = 0 "
        f"OR {alias}.session_id != (SELECT session_id FROM messages WHERE id = context_index_query_id()) "
        f"OR {alias}.msg_index < (SELECT msg_index FROM messages WHERE id = context_index_query_id())) "
    )


def _validate_schema(connection: sqlite3.Connection) -> set[str]:
    tables = {
        str(row["name"])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        )
    }
    if not {"sessions", "messages", "messages_fts"} <= tables:
        raise _IncompatibleSchema
    session_columns: set[str] = set()
    for table, required in _REQUIRED_COLUMNS.items():
        columns = {
            str(row["name"])
            for row in connection.execute(f"PRAGMA table_info({table})")
        }
        if not required <= columns:
            raise _IncompatibleSchema
        if table == "sessions":
            session_columns = columns
    return session_columns


def _is_deadline(exc: sqlite3.DatabaseError) -> bool:
    return getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_INTERRUPT


class SessionRecommendationSource:
    """Read Session Recall through a separate, non-mutating recommendation path."""

    def __init__(self, database_path: Path, *, deadline_ms: int = 75):
        self._database_path = Path(database_path)
        self._deadline_ms = deadline_ms
        # Diagnostic-only channel bound for production acceptance fixtures.
        self.max_rows_observed = 0

    def _observe(
        self, rows: Sequence[_PreparedSessionRow]
    ) -> list[_PreparedSessionRow]:
        self.max_rows_observed = max(self.max_rows_observed, len(rows))
        return list(rows)

    def recommend(
        self,
        query: str,
        workspace: WorkspaceIdentity,
        current_session_id: str,
        active_fingerprints: frozenset[str],
        now: float,
        plan: QueryPlan | None = None,
    ) -> SourceResult:
        if plan is not None and not plan.should_recall:
            return SourceResult("disabled")
        stages = []
        try:
            with open_readonly(self._database_path, self._deadline_ms) as connection:
                if plan is not None:
                    set_read_window(connection, plan.cutoff, plan.time_start, plan.time_end, message_id=plan.message_id)
                deadline = time.perf_counter() + self._deadline_ms / 1000
                connection.set_progress_handler(lambda: int(time.perf_counter() >= deadline), 1000)
                session_columns = _validate_schema(connection)
                resolved_session_id = self._resolve_current_session_id(
                    connection,
                    current_session_id,
                    has_source_session_key="source_session_key" in session_columns,
                )
                current_query_fingerprint = content_fingerprint(plan.original if plan is not None else query)
                current_message_id = self._current_message_id(
                    connection,
                    resolved_session_id or "",
                    current_query_fingerprint,
                )
                if plan is not None and plan.message_id is not None:
                    match = connection.execute("SELECT id FROM messages WHERE id = ? AND session_id = ?", (plan.message_id, resolved_session_id or "")).fetchone()
                    if match is not None:
                        current_message_id = int(match["id"])
                bounded_active_fingerprints = frozenset(
                        fingerprint
                        for fingerprint in active_fingerprints
                        # The current query is excluded only by its exact inferred row id;
                        # otherwise older identical history would disappear with it.
                        if fingerprint != current_query_fingerprint
                )
                if len(bounded_active_fingerprints) > _MAX_ACTIVE_FINGERPRINTS:
                    bounded_active_fingerprints = frozenset(
                        sorted(bounded_active_fingerprints)[:_MAX_ACTIVE_FINGERPRINTS]
                    )
                channel_errors: dict[str, str] = {}
                channel_successes = 0

                # Prepare the bounded fallback before query-dependent scans.
                # A relevance interruption can arrive after the total deadline
                # (for example after descheduling), leaving no safe time to
                # fetch recency afterward. Completed candidates remain usable.
                try:
                    with observe_stage(stages, "recency"):
                        recency_rows = self._observe(
                            self._recency_rows(
                                connection,
                                workspace,
                                resolved_session_id or "",
                                current_message_id,
                                bounded_active_fingerprints,
                                has_workspace_key="workspace_key" in session_columns,
                            ) if plan is None or plan.include_recent else ()
                        )
                        recency = self._candidates(recency_rows, workspace)
                    channel_successes += 1
                except sqlite3.DatabaseError as exc:
                    recency = ()
                    channel_errors["recency_omitted"] = (
                        "deadline" if _is_deadline(exc) else "database_error"
                    )

                try:
                    with observe_stage(stages, "relevance"):
                        relevance_reader = self._native_relevance_rows if plan is not None else self._relevance_rows
                        relevance_rows = self._observe(
                            _eligible_rows(
                                relevance_reader(
                                    connection,
                                    query,
                                    workspace,
                                    resolved_session_id or "",
                                    current_message_id,
                                    bounded_active_fingerprints,
                                    has_workspace_key="workspace_key" in session_columns,
                                ),
                                workspace,
                                resolved_session_id or "",
                                current_message_id,
                                bounded_active_fingerprints,
                                relevance=True,
                            )
                        )
                        relevance = self._candidates(relevance_rows, workspace)
                    channel_successes += 1
                except sqlite3.DatabaseError as exc:
                    relevance = ()
                    channel_errors["relevance_omitted"] = (
                        "deadline" if _is_deadline(exc) else "database_error"
                    )

                return SourceResult(
                    stages=tuple(stages),
                    availability="available" if channel_successes else "error",
                    relevance=relevance,
                    recency=recency,
                    error_category=(
                        "deadline"
                        if "deadline" in channel_errors.values()
                        else next(iter(channel_errors.values()), "")
                    ),
                    diagnostics=tuple(
                        name
                        for name in ("relevance_omitted", "recency_omitted")
                        if name in channel_errors
                    ),
                )
        except FileNotFoundError:
            return SourceResult(stages=tuple(stages), availability="absent")
        except _IncompatibleSchema:
            return SourceResult(
                stages=tuple(stages),
                availability="error",
                error_category="incompatible_schema",
            )
        except sqlite3.DatabaseError as exc:
            return SourceResult(
                stages=tuple(stages),
                availability="error",
                error_category="deadline" if _is_deadline(exc) else "database_error",
            )

    @staticmethod
    def _resolve_current_session_id(
        connection: sqlite3.Connection,
        current_session_key: str,
        *,
        has_source_session_key: bool,
    ) -> str | None:
        key = str(current_session_key or "")
        if not key:
            return None
        row = None
        if has_source_session_key:
            row = connection.execute(
                "SELECT id FROM sessions WHERE source_session_key = ? LIMIT 1",
                (key,),
            ).fetchone()
        if row is None:
            row = connection.execute(
                "SELECT id FROM sessions WHERE id = ? LIMIT 1",
                (key,),
            ).fetchone()
        return None if row is None else str(row["id"])

    @staticmethod
    def _current_message_id(
        connection: sqlite3.Connection,
        current_session_id: str,
        current_query_fingerprint: str,
    ) -> int | None:
        row = connection.execute(
            "SELECT id, content FROM messages "
            "WHERE session_id = ? AND role = 'user' AND timestamp <= context_index_cutoff() "
            "ORDER BY msg_index DESC, id DESC LIMIT 1",
            (current_session_id,),
        ).fetchone()
        if row is None:
            return None
        if content_fingerprint(str(row["content"] or "")) != current_query_fingerprint:
            return None
        return int(row["id"])

    @staticmethod
    def _base_select(native_rank: str, *, has_workspace_key: bool) -> str:
        workspace_key = "s.workspace_key" if has_workspace_key else "NULL"
        return f"""
            SELECT
                m.id,
                m.session_id,
                m.role,
                m.content,
                m.timestamp,
                s.title,
                {workspace_key} AS workspace_key,
                COALESCE((
                    SELECT first_message.content
                    FROM messages AS first_message
                    WHERE first_message.session_id = m.session_id
                      AND first_message.role = 'user'
                      AND +first_message.timestamp <= context_index_cutoff()
                    ORDER BY first_message.msg_index ASC, first_message.id ASC
                    LIMIT 1
                ), '') AS preview,
                {native_rank} AS native_query_rank
            FROM {{from_clause}}
            JOIN sessions AS s ON s.id = m.session_id
        """

    @staticmethod
    def _pool_guards(current_session_id: str = "") -> tuple[str, tuple[str, ...]]:
        """Shared candidate-pool guards, applied before any caller filters.

        Blank or whitespace-only rows carry no recall value and must not
        consume recommendation slots. Callers that surface rows purely by
        recency additionally exclude the live session: "recent" is never a
        reason to recommend the present context back into it. Relevance pools
        keep live-session rows so a real query match can still recover
        history that slid out of the visible window; the active-context
        fingerprints stay as the second line of defense there.
        """
        guards = ("AND length(trim(coalesce(m.content, ''))) > 1 "
                  "AND m.timestamp >= context_index_after() "
                  "AND m.timestamp <= context_index_before() " + _query_boundary("m"))
        params: tuple[str, ...] = ()
        if current_session_id:
            guards += "AND m.session_id != ? "
            params = (current_session_id,)
        return guards, params

    def _relevance_rows(
        self,
        connection: sqlite3.Connection,
        query: str,
        workspace: WorkspaceIdentity,
        current_session_id: str,
        current_message_id: int | None,
        active_fingerprints: frozenset[str],
        has_workspace_key: bool,
    ) -> list[sqlite3.Row]:
        terms = _plain_query_terms(query)
        if not terms:
            return []
        fts_query = _fts_query(query)
        if fts_query is None:
            return self._like_rows(
                connection,
                terms,
                workspace,
                current_session_id,
                current_message_id,
                active_fingerprints,
                has_workspace_key,
            )
        pool_limit = _candidate_pool_limit(len(active_fingerprints))
        fts_from = (
            "messages_fts "
            "JOIN messages AS m ON m.rowid = messages_fts.rowid "
            "JOIN sessions AS s ON s.id = m.session_id"
        )

        guards, _ = self._pool_guards()

        pools: list[tuple[str, tuple[object, ...]]] = []

        if has_workspace_key and workspace.key:
            pools.append(("AND workspace_key = ?", (workspace.key,)))

        inferred_ids = self._inferred_session_ids(
            connection,
            workspace,
            has_workspace_key=has_workspace_key,
        )
        if inferred_ids:
            placeholders = ", ".join("?" for _ in inferred_ids)
            pools.append((f"AND session_id IN ({placeholders})", inferred_ids))

        workspace_fts_query = _fts_query(workspace.label)
        if workspace_fts_query is not None:
            content_workspace_predicate = ""
            if has_workspace_key:
                content_workspace_predicate = (
                    "AND (workspace_key IS NULL OR workspace_key = '') "
                )
            pools.append((
                "AND id IN (SELECT rowid FROM messages_fts(?)) " + content_workspace_predicate,
                (workspace_fts_query,),
            ))

        pools.append(("", ()))
        # Compute each user-query hit/rank once and reuse it across workspace
        # and global pools. Broad terms previously repeated the same MATCH,
        # BM25 and canonical-row reads for every pool.
        workspace_column = "s.workspace_key" if has_workspace_key else "NULL"
        pool_sql = " UNION ".join(
            "SELECT * FROM (SELECT id, native_query_rank FROM hits "
            f"WHERE 1 {where} ORDER BY native_query_rank ASC, timestamp DESC, id DESC LIMIT ?)"
            for where, _ in pools
        )
        pool_params = tuple(value for _, params in pools for value in (*params, pool_limit))
        rows = connection.execute(
            "WITH hits AS MATERIALIZED ("
            f"SELECT m.id, m.session_id, m.timestamp, {workspace_column} AS workspace_key, "
            f"bm25(messages_fts) AS native_query_rank FROM {fts_from} "
            f"WHERE messages_fts MATCH ? {guards} AND m.role IN ('user', 'assistant')) " + pool_sql,
            (fts_query, *pool_params),
        ).fetchall()
        del current_session_id, current_message_id
        return self._enrich_seed_rows(
            connection,
            tuple((int(row["id"]), float(row["native_query_rank"])) for row in rows),
            has_workspace_key=has_workspace_key,
        )

    def _native_relevance_rows(
        self,
        connection: sqlite3.Connection,
        query: str,
        workspace: WorkspaceIdentity,
        current_session_id: str,
        current_message_id: int | None,
        active_fingerprints: frozenset[str],
        has_workspace_key: bool,
    ) -> list[sqlite3.Row]:
        lexical = LexicalQuery.from_text(query)
        terms = lexical.terms
        if not terms:
            return []
        mask, term_params = lexical.mask_sql("m.content")
        anchors, anchor_params = lexical.bind_anchor_sql(connection, "m.content"), ()
        minimum = lexical.minimum
        pool_limit = _candidate_pool_limit(len(active_fingerprints))
        guards, _ = self._pool_guards()
        # A self-hit must not consume a pool slot or suppress partial matches.
        guards += " AND m.id != ? "
        from_clause = "messages AS m"
        fts_guard = ""
        fts_params: tuple[str, ...] = ()
        long_terms = tuple(term for term in terms if len(term) >= 3)
        anchor_query = lexical.anchor_fts_query()
        if anchor_query or len(terms) - len(long_terms) < minimum:
            # Fewer short terms than the required coverage means every valid
            # row must contain an indexed term: this prefilter loses no hits.
            from_clause += " JOIN messages_fts ON messages_fts.rowid = m.id"
            fts_guard = " AND messages_fts MATCH ? "
            # A required indexed target is more selective than common CJK
            # grams, and already implies a query-term match.
            fts_params = (anchor_query or " OR ".join(f'"{term}"' for term in long_terms),)

        pools = [""]
        pool_params: list[object] = [pool_limit]
        if has_workspace_key and workspace.key:
            # Probe only matching sessions. Building every id in a large
            # workspace can cost more than the indexed text retrieval itself.
            pools.append("AND EXISTS (SELECT 1 FROM sessions AS s "
                         "WHERE s.id = covered.session_id AND s.workspace_key = ?)")
            pool_params.extend((workspace.key, pool_limit))
        inferred_ids = self._inferred_session_ids(connection, workspace, has_workspace_key=has_workspace_key)
        if inferred_ids:
            placeholders = ", ".join("?" for _ in inferred_ids)
            pools.append(f"AND session_id IN ({placeholders})")
            pool_params.extend((*inferred_ids, pool_limit))
        indexed_masks, indexed_params = lexical.indexed_masks_sql("messages_fts")
        if indexed_masks:
            from_clause = "query_masks AS q JOIN messages AS m ON m.id = q.id"
            mask, term_params = "q.query_match_mask", ()
            fts_guard, fts_params = "", ()
        elif not fts_params:
            start, before, cutoff = connection.execute(
                "SELECT context_index_after(), context_index_before(), context_index_cutoff()"
            ).fetchone()
            if start == 0 and before == cutoff:
                # Unindexed short terms must inspect the full history. A
                # timestamp-index walk adds one table lookup per row without
                # pruning it; sequential reads are much cheaper on long archives.
                # Explicit date windows keep the selective timestamp index.
                from_clause += " NOT INDEXED"
        # Text is matched once per indexed hit. Short scans discard nonmatches
        # before materialization; coverage uses cheap integer masks afterwards.
        scan_guard = "1" if fts_params or indexed_masks else "query_match_mask != 0"
        pool_sql = " UNION ".join(
            "SELECT * FROM (SELECT id, native_query_rank FROM covered "
            f"WHERE 1 {where} ORDER BY native_query_rank ASC, timestamp DESC, id DESC LIMIT ?)"
            for where in pools
        )
        rows = connection.execute(
            "WITH " + (indexed_masks + ", " if indexed_masks else "") + "matched AS MATERIALIZED ("
            f"SELECT m.id, m.session_id, m.timestamp, ({mask}) AS query_match_mask FROM {from_clause} "
            f"WHERE {scan_guard} "
            f"AND m.role IN ('user', 'assistant') {fts_guard}{guards} AND ({anchors})), "
            "covered AS MATERIALIZED ("
            f"SELECT *, -({lexical.coverage_sql('query_match_mask')}) AS native_query_rank FROM matched "
            f"WHERE ({lexical.count_sql('query_match_mask')}) >= ?) " + pool_sql,
            (*indexed_params, *term_params, *fts_params, current_message_id or 0, *anchor_params, minimum, *pool_params),
        ).fetchall()
        return self._enrich_seed_rows(
            connection,
            tuple((int(row["id"]), float(row["native_query_rank"])) for row in rows),
            has_workspace_key=has_workspace_key,
        )

    def _like_rows(
        self,
        connection: sqlite3.Connection,
        terms: list[str],
        workspace: WorkspaceIdentity,
        current_session_id: str,
        current_message_id: int | None,
        active_fingerprints: frozenset[str],
        has_workspace_key: bool,
    ) -> list[sqlite3.Row]:
        pool_limit = _candidate_pool_limit(len(active_fingerprints))
        predicates = " AND ".join("m.content LIKE ?" for _ in terms)
        guards, _ = self._pool_guards()
        del current_session_id, current_message_id

        inferred_ids = self._inferred_session_ids(
            connection,
            workspace,
            has_workspace_key=has_workspace_key,
        )

        def run(joined_predicates: str) -> list[tuple[int, float]]:
            seeds: list[tuple[int, float]] = []
            content_params = tuple(f"%{term}%" for term in terms)

            def read_pool(where: str, params: tuple[object, ...]) -> list[tuple[int, float]]:
                rows = connection.execute(
                    "SELECT m.id, 0.0 AS native_query_rank FROM messages AS m "
                    f"WHERE ({joined_predicates}) {guards}{where} "
                    "AND m.role IN ('user', 'assistant') "
                    "ORDER BY m.timestamp DESC, m.id DESC LIMIT ?",
                    (*content_params, *params, pool_limit),
                ).fetchall()
                return [(int(row["id"]), 0.0) for row in rows]

            global_seeds = read_pool("", ())
            if len(global_seeds) < pool_limit:
                # This is the complete LIKE result from messages itself.
                # Workspace pools can only repeat these rows; final ranking
                # still applies workspace tiers and active-context exclusions.
                # Do not infer completeness from an FTS index or a full pool.
                return global_seeds

            if has_workspace_key and workspace.key:
                seeds.extend(read_pool(
                    "AND m.session_id IN ("
                    "SELECT s.id FROM sessions AS s WHERE s.workspace_key = ?)",
                    (workspace.key,),
                ))
            if inferred_ids:
                placeholders = ", ".join("?" for _ in inferred_ids)
                seeds.extend(read_pool(f"AND m.session_id IN ({placeholders})", inferred_ids))
            seeds.extend(global_seeds)
            return seeds

        seeds = run(predicates)
        if not seeds and len(terms) > 1:
            seeds = run(" OR ".join("m.content LIKE ?" for _ in terms))
        unique_seeds: dict[int, float] = {}
        for message_id, native_query_rank in seeds:
            unique_seeds.setdefault(message_id, native_query_rank)
        return self._enrich_seed_rows(
            connection,
            tuple(unique_seeds.items()),
            has_workspace_key=has_workspace_key,
        )

    def _recency_rows(
        self,
        connection: sqlite3.Connection,
        workspace: WorkspaceIdentity,
        current_session_id: str,
        current_message_id: int | None,
        active_fingerprints: frozenset[str],
        has_workspace_key: bool,
    ) -> list[_PreparedSessionRow]:
        pool_limit = _candidate_pool_limit(len(active_fingerprints))
        seeds: list[tuple[int, float]] = []
        guards, guard_params = self._pool_guards(current_session_id)
        if has_workspace_key and workspace.key:
            # A bounded timestamp prefix handles dense, recently active
            # workspaces without sorting their full history. LIMIT is inside
            # the subquery so unrelated/empty rows cannot expand this scan.
            probe_limit = max(256, pool_limit * 8)
            workspace_rows = connection.execute(
                "SELECT m.id, 0.0 AS native_query_rank FROM ("
                "SELECT id, session_id, role, content, timestamp, msg_index FROM messages "
                "WHERE timestamp >= context_index_after() AND timestamp <= context_index_before() "
                "ORDER BY timestamp DESC, id DESC LIMIT ?) AS m "
                "JOIN sessions AS s ON s.id = m.session_id "
                "WHERE m.role IN ('user', 'assistant') "
                f"{guards}AND s.workspace_key = ? "
                "ORDER BY m.timestamp DESC, m.id DESC LIMIT ?",
                (probe_limit, *guard_params, workspace.key, pool_limit),
            ).fetchall()
            if len(workspace_rows) < pool_limit:
                # Sparse/old workspaces must seek through their session index,
                # never scan every newer message from unrelated workspaces.
                # CROSS JOIN retains the workspace-first loop order.
                workspace_rows = connection.execute(
                    "SELECT m.id, 0.0 AS native_query_rank FROM sessions AS s "
                    "CROSS JOIN messages AS m ON m.session_id = s.id "
                    "WHERE m.role IN ('user', 'assistant') "
                    f"{guards}AND s.workspace_key = ? "
                    "ORDER BY m.timestamp DESC, m.id DESC LIMIT ?",
                    (*guard_params, workspace.key, pool_limit),
                ).fetchall()
            seeds.extend((int(row["id"]), float(row["native_query_rank"])) for row in workspace_rows)
        seeds.extend(
            (int(row["id"]), float(row["native_query_rank"]))
            for row in connection.execute(
                "SELECT m.id, 0.0 AS native_query_rank "
                "FROM messages AS m "
                "WHERE m.role IN ('user', 'assistant') "
                f"{guards}"
                "ORDER BY m.timestamp DESC, m.id DESC LIMIT ?",
                (*guard_params, pool_limit),
            )
        )
        inferred_ids = self._inferred_session_ids(
            connection,
            workspace,
            has_workspace_key=has_workspace_key,
        )
        if inferred_ids:
            placeholders = ", ".join("?" for _ in inferred_ids)
            seeds.extend(
                (int(row["id"]), float(row["native_query_rank"]))
                for row in connection.execute(
                    "SELECT m.id, 0.0 AS native_query_rank FROM messages AS m "
                    "WHERE m.role IN ('user', 'assistant') "
                    f"AND m.session_id IN ({placeholders}) "
                    f"{guards}"
                    "ORDER BY m.timestamp DESC, m.id DESC LIMIT ?",
                    (*inferred_ids, *guard_params, pool_limit),
                )
            )
        unique_seeds: dict[int, float] = {}
        for message_id, native_query_rank in seeds:
            unique_seeds.setdefault(message_id, native_query_rank)
        rows = self._enrich_seed_rows(
            connection,
            tuple(unique_seeds.items()),
            has_workspace_key=has_workspace_key,
        )
        return _eligible_rows(
            rows,
            workspace,
            current_session_id,
            current_message_id,
            active_fingerprints,
            relevance=False,
        )

    @staticmethod
    def _inferred_session_ids(
        connection: sqlite3.Connection,
        workspace: WorkspaceIdentity,
        *,
        has_workspace_key: bool,
    ) -> tuple[str, ...]:
        normalized_label = _normalized_tokens(workspace.label)
        if not normalized_label:
            return ()
        if has_workspace_key and connection.execute(
            "SELECT 1 FROM sessions WHERE workspace_key IS NULL OR workspace_key = '' LIMIT 1"
        ).fetchone() is None:
            # Fully attributed archives need no legacy workspace inference.
            return ()
        label_pattern = "%" + "%".join(normalized_label.split()) + "%"
        workspace_predicate = (
            "(workspace_key IS NULL OR workspace_key = '') AND "
            if has_workspace_key
            else ""
        )
        session_ids = [
            str(row["id"])
            for row in connection.execute(
                "SELECT id FROM sessions "
                f"WHERE {workspace_predicate}lower(title) LIKE ? AND started_at <= context_index_cutoff() "
                "ORDER BY started_at DESC, id DESC LIMIT 64",
                (label_pattern,),
            )
        ]
        fts_workspace_query = _fts_query(workspace.label)
        if fts_workspace_query is not None:
            content_sql = (
                "SELECT DISTINCT m.session_id "
                "FROM messages_fts "
                "JOIN messages AS m ON m.rowid = messages_fts.rowid "
                "JOIN sessions AS s ON s.id = m.session_id "
                "WHERE messages_fts MATCH ? AND m.timestamp <= context_index_cutoff() "
                + (
                    "AND (s.workspace_key IS NULL OR s.workspace_key = '') "
                    if has_workspace_key
                    else ""
                )
                + "ORDER BY m.timestamp DESC, m.id DESC LIMIT 64"
            )
            content_params: tuple[object, ...] = (fts_workspace_query,)
        else:
            content_sql = (
                "SELECT DISTINCT m.session_id FROM messages AS m "
                "JOIN sessions AS s ON s.id = m.session_id "
                "WHERE m.content LIKE ? AND m.timestamp <= context_index_cutoff() "
                + (
                    "AND (s.workspace_key IS NULL OR s.workspace_key = '') "
                    if has_workspace_key
                    else ""
                )
                + "ORDER BY m.timestamp DESC, m.id DESC LIMIT 64"
            )
            content_params = (f"%{workspace.label}%",)
        session_ids.extend(
            str(row["session_id"])
            for row in connection.execute(content_sql, content_params)
        )
        return tuple(dict.fromkeys(session_ids))

    @staticmethod
    def _enrich_seed_rows(
        connection: sqlite3.Connection,
        seeds: tuple[tuple[int, float], ...],
        *,
        has_workspace_key: bool,
    ) -> list[sqlite3.Row]:
        if not seeds:
            return []
        values = ", ".join("(?, ?)" for _ in seeds)
        workspace_key = "s.workspace_key" if has_workspace_key else "NULL"
        sql = f"""
            WITH seed(id, native_query_rank) AS (VALUES {values})
            SELECT
                m.id, m.session_id, m.role, m.content, m.timestamp, s.title,
                {workspace_key} AS workspace_key,
                COALESCE((
                    SELECT first_message.content
                    FROM messages AS first_message
                    WHERE first_message.session_id = m.session_id
                      AND first_message.role = 'user'
                      AND +first_message.timestamp <= context_index_cutoff()
                    ORDER BY first_message.msg_index ASC, first_message.id ASC
                    LIMIT 1
                ), '') AS preview,
                seed.native_query_rank
            FROM seed
            JOIN messages AS m ON m.id = seed.id
            JOIN sessions AS s ON s.id = m.session_id
        """
        params = tuple(value for seed in seeds for value in seed)
        return connection.execute(sql, params).fetchall()

    @staticmethod
    def _candidates(
        rows: Sequence[_PreparedSessionRow],
        workspace: WorkspaceIdentity,
    ) -> tuple[RecommendationCandidate, ...]:
        candidates: list[RecommendationCandidate] = []
        for row in rows:
            content = row.content
            tier = row.workspace_tier
            revision = session_revision(row.session_id, row.id, row.role, content, row.timestamp)
            candidates.append(
                RecommendationCandidate(
                    source="session",
                    identity=_identity(row.session_id, row.id),
                    description=_description(row.title, content),
                    timestamp=row.timestamp,
                    workspace_tier=tier,
                    native_query_rank=row.native_query_rank,
                    topic_key=_topic_key(row.title, content),
                    trust_label="historical_context",
                    locator=SourceLocator(
                        "session_message",
                        row.session_id,
                        row.id,
                        revision=revision,
                    ),
                    private_text=content,
                    revision=revision,
                    project_label=workspace.label if tier < 2 else "",
                )
            )
            if len(candidates) >= _CHANNEL_LIMIT:
                break
        return tuple(candidates)

    def open(self, locator: SourceLocator, window: int, plan: QueryPlan | None = None) -> EvidenceResult:
        empty = EvidenceResult(
            source="session",
            trust_label="historical_context",
            title="Session evidence unavailable",
            items=(),
        )
        if locator.kind != "session_message":
            return empty
        bounded_window = max(0, min(int(window), _MAX_EVIDENCE_WINDOW))
        try:
            with open_readonly(self._database_path, self._deadline_ms) as connection:
                if plan is not None:
                    set_read_window(connection, plan.cutoff, plan.time_start, plan.time_end, message_id=plan.message_id)
                _validate_schema(connection)
                anchor = connection.execute(
                    "SELECT m.*, s.title "
                    "FROM messages AS m "
                    "JOIN sessions AS s ON s.id = m.session_id "
                    "WHERE m.session_id = ? AND m.id = ? AND m.timestamp BETWEEN context_index_after() AND context_index_before() "
                    + _query_boundary("m") + "LIMIT 1",
                    (locator.primary, locator.secondary),
                ).fetchone()
                if anchor is None:
                    return empty
                if locator.revision and locator.revision != session_revision(
                    anchor["session_id"], anchor["id"], anchor["role"], anchor["content"], anchor["timestamp"]
                ):
                    return empty
                before = connection.execute(
                    "SELECT role, content FROM messages "
                    "WHERE session_id = ? AND msg_index < ? "
                    "AND timestamp BETWEEN context_index_after() AND context_index_before() " + _query_boundary("messages") +
                    "ORDER BY msg_index DESC LIMIT ?",
                    (locator.primary, anchor["msg_index"], bounded_window),
                ).fetchall()
                after = connection.execute(
                    "SELECT role, content FROM messages "
                    "WHERE session_id = ? AND msg_index > ? "
                    "AND timestamp BETWEEN context_index_after() AND context_index_before() " + _query_boundary("messages") +
                    "ORDER BY msg_index ASC LIMIT ?",
                    (locator.primary, anchor["msg_index"], bounded_window),
                ).fetchall()
                rows = list(reversed(before)) + [anchor] + list(after)
                return EvidenceResult(
                    source="session",
                    trust_label="historical_context",
                    title=_bounded_text(anchor["title"], 200) or "Historical session",
                    items=tuple(
                        f"{row['role']}: {str(row['content'] or '')[:_MAX_EVIDENCE_CONTENT_CHARS]}"
                        for row in rows
                    ),
                )
        except (FileNotFoundError, _IncompatibleSchema, sqlite3.DatabaseError):
            return empty
