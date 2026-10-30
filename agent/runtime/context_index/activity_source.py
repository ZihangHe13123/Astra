"""Bounded recommendations from Astra's existing cached Activity archive."""

from __future__ import annotations

import concurrent.futures
import contextvars
from functools import lru_cache
import hashlib
import json
import re
import sqlite3
import threading
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta, tzinfo
from itertools import combinations
from pathlib import Path
from typing import Any

from agent.runtime.activity_store import _bounded_payload, _redact_text, sanitize_url

from . import embedder as _embedder
from .habits import HabitAggregator, _database_identity
from . import vector_index as _vector_index
from .models import (
    EvidenceResult,
    RecommendationCandidate,
    SourceLocator,
    SourceResult,
)
from .sqlite_reader import ReadBudget, observe_stage, open_readonly, set_read_window
from .query import QueryPlan, lexical_match_sql
from .lexical import LexicalQuery
from .selection import fuse_candidates
from .query_embedding import current_embedding
from .workspace import WorkspaceIdentity

_CHANNEL_LIMIT = 12
_EMBED_TOP_K = 6
# 向量段与词法段并行（M4b：208ms 串行事故根治）。池为模块级共享（reader 常驻），
# result 限时即弃：慢向量绝不拖词法交付，超时就当本轮只有词法。
_EMBED_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="ctx-embed")
_VECTOR_WAIT_SECONDS = 0.100
# 定维实验（2026-09-04）：真实语义命中余弦 p50=0.584、min 0.365；词法噪音行集中在低分段。
# 0.25 是保守下界——先不误杀，M3 A/B 若噪音超标再收紧。
_EMBED_MIN_SIMILARITY = 0.25
_EMBED_SLOTS = threading.BoundedSemaphore(2)
_MAX_QUERY_TERMS = 8  # 词对组合数上界 C(8,2)=28，防长 query 撑爆 FTS 查询串
# Recency needs a writer-maintained UTC epoch column.  Read-only legacy caches
# without it omit the optional recency channel rather than claiming lexical ISO
# ordering is correct across arbitrary offsets.
_MAX_DESCRIPTION_CHARS = 240
_MAX_DESCRIPTION_FIELD_CHARS = 180
_MAX_EVIDENCE_WINDOW = 5
_MAX_EVIDENCE_ITEM_CHARS = 500
_FRESHNESS_WINDOW = timedelta(hours=24)
_REQUIRED_COLUMNS = {
    "activity_summaries": {
        "summary_id",
        "granularity",
        "period_start",
        "period_end",
        "content",
    },
    "activity_events": {
        "segment_id",
        "event_id",
        "occurred_at",
        "kind",
        "app_name",
        "window_title",
        "url",
        "url_search_text",
        "selection_text",
        "searchable_text",
    },
}
_REQUIRED_TABLES = {
    "activity_summaries",
    "activity_events",
    "activity_summaries_fts",
    "activity_events_fts",
}


class _IncompatibleSchema(Exception):
    pass


def _bounded_text(value: Any, limit: int) -> str:
    return " ".join(_redact_text(str(value or "")).split())[:limit]


def _normalized_tokens(value: Any) -> str:
    folded = str(value or "").casefold()
    return " ".join(
        "".join(character if character.isalnum() else " " for character in folded).split()
    )


@lru_cache(maxsize=32)
def _workspace_pattern(label: str, root: str) -> re.Pattern[str] | None:
    labels = {_normalized_tokens(label), _normalized_tokens(Path(root).name) if root else ""}
    alternatives = [r"[\W_]+".join(map(re.escape, text.split())) for text in sorted(labels) if text]
    return re.compile(r"(?:" + "|".join(alternatives) + r")(?![^\W_])") if alternatives else None


def _workspace_tier(workspace: WorkspaceIdentity, *values: Any) -> int:
    pattern = _workspace_pattern(workspace.label, workspace.root)
    searchable = " ".join(str(value or "") for value in values).casefold()
    if pattern is not None:
        # Checking the left boundary after a literal match lets the regex
        # engine skip long unrelated spans instead of inspecting each position.
        position = 0
        while match := pattern.search(searchable, position):
            if match.start() == 0 or not searchable[match.start() - 1].isalnum():
                return 1
            position = match.start() + 1
    return 2


def _parse_timestamp(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").strip())
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        return None
    try:
        return parsed.astimezone(UTC)
    except (OSError, OverflowError, ValueError):
        return None


def _timestamp_epoch(value: Any) -> float:
    parsed = _parse_timestamp(value)
    return parsed.timestamp() if parsed is not None else 0.0


def _now_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    return datetime.fromtimestamp(float(value), tz=UTC)


def _local_timestamp(value: Any) -> str:
    parsed = _parse_timestamp(value)
    if parsed is None:
        return ""
    return parsed.astimezone().strftime("%Y-%m-%d %H:%M %Z").strip()


def _identity(kind: str, primary: Any, secondary: Any = "") -> str:
    private_identity = f"{kind}\0{primary}\0{secondary}"
    return hashlib.sha256(private_identity.encode("utf-8")).hexdigest()[:24]


def _topic_key(description: str) -> str:
    normalized = " ".join(description.casefold().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]


def _query_terms(query: str | tuple[str, ...]) -> list[str]:
    if isinstance(query, tuple):
        return list(query)
    return [token for token in re.findall(r"\w+", str(query), flags=re.UNICODE) if token]


def _fts_query(terms: Sequence[str]) -> str:
    # 2-of-N（OR 验收 2026-09-04：单词重叠是 A3 噪音行来源，均分仅 0.5）：
    # 多词 query 要求至少两词共现，词对用 OR 并联、bm25 排序；单词 query 保持单命中。
    quoted = [f'"{term.replace(chr(34), "")}"' for term in terms[:_MAX_QUERY_TERMS]]
    if len(quoted) < 2:
        return " OR ".join(quoted)
    return " OR ".join(
        f"({left} AND {right})" for left, right in combinations(quoted, 2)
    )


def _requires_like_fallback(terms: Sequence[str]) -> bool:
    return any(len(term) < 3 for term in terms)


def _workspace_fts_query(workspace: WorkspaceIdentity) -> str | None:
    terms = [term for term in _query_terms(workspace.label) if len(term) >= 3]
    return _fts_query(terms) if terms else None


def _validate_schema(connection: sqlite3.Connection) -> set[str]:
    tables = {
        str(row["name"])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        )
    }
    if not _REQUIRED_TABLES <= tables:
        raise _IncompatibleSchema
    for table, required in _REQUIRED_COLUMNS.items():
        columns = {
            str(row["name"])
            for row in connection.execute(f"PRAGMA table_info({table})")
        }
        if not required <= columns:
            raise _IncompatibleSchema
    return tables


def _is_deadline(exc: sqlite3.DatabaseError) -> bool:
    return getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_INTERRUPT


def _summary_description(row: sqlite3.Row) -> str:
    timestamp = _local_timestamp(row["period_end"] or row["period_start"])
    summary = _bounded_text(row["content"], _MAX_DESCRIPTION_FIELD_CHARS)
    return _bounded_text(" · ".join(part for part in (timestamp, summary) if part), _MAX_DESCRIPTION_CHARS) or "Cached activity summary"


def _event_description(row: sqlite3.Row) -> str:
    parts = (
        _local_timestamp(row["occurred_at"]),
        _bounded_text(row["app_name"], 60),
        _bounded_text(row["window_title"], 100),
        _bounded_text(sanitize_url(str(row["url"] or "")), 140),
    )
    return _bounded_text(" · ".join(part for part in parts if part), _MAX_DESCRIPTION_CHARS) or "Cached activity event"


def _safe_timestamp(value: Any) -> str:
    parsed = _parse_timestamp(value)
    return parsed.isoformat() if parsed is not None else ""


def _summary_output(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "evidence_type": "summary",
        "source": "computer_history",
        "untrusted_observation": True,
        "granularity": _bounded_text(row["granularity"], 16),
        "period_start": _safe_timestamp(row["period_start"]),
        "period_end": _safe_timestamp(row["period_end"]),
        "snippet": _bounded_text(row["content"], _MAX_EVIDENCE_ITEM_CHARS),
    }


def _event_output(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "evidence_type": "raw_event",
        "source": "computer_history",
        "untrusted_observation": True,
        "occurred_at": _safe_timestamp(row["occurred_at"]),
        "kind": _bounded_text(row["kind"], 40),
        "app_name": _bounded_text(row["app_name"], 80),
        "window_title": _bounded_text(row["window_title"], 160),
        "url": sanitize_url(str(row["url"] or "")),
    }


def _evidence_item(payload: dict[str, Any]) -> str:
    bounded = _bounded_payload(payload, _MAX_EVIDENCE_ITEM_CHARS)
    return json.dumps(bounded, ensure_ascii=False, separators=(",", ":"))


class ActivityRecommendationReader:
    """Read cached Activity recommendations without discovering or refreshing sources."""

    def __init__(
        self,
        database_path: Path,
        *,
        deadline_ms: int = 75,
        local_zone: tzinfo | None = None,
    ):
        self._database_path = Path(database_path)
        self._deadline_ms = deadline_ms
        self._habits = HabitAggregator(local_zone)
        # Diagnostic-only channel bound for production acceptance fixtures.
        self.max_rows_observed = 0
        # 向量矩阵按 reader 生命周期缓存（factory 每进程建一次 reader）。
        self._vector_ids: list[str] | None = None
        self._vector_matrix = None
        self._vector_hashes: dict[str, str] = {}
        self._vector_version: tuple | None = None
        self._vector_lock = threading.Lock()

    def _observe(self, rows: Sequence[tuple[str, sqlite3.Row]]) -> list[tuple[str, sqlite3.Row]]:
        self.max_rows_observed = max(self.max_rows_observed, len(rows))
        return list(rows)

    def _vector_snapshot(self) -> tuple[list[str], object, dict[str, str]]:
        # Stat only a fixed pair of files per query. Include WAL commits and atomic
        # DB replacement; a previously absent index must also become discoverable.
        with self._vector_lock:
            path = _vector_index.default_vectors_db_path()
            def signature(file: Path) -> tuple | None:
                try:
                    stat = file.stat()
                except FileNotFoundError:
                    return None
                return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)

            main = signature(path)
            revision = _vector_index.read_vector_revision(path) if main is not None else None
            version = (str(path.resolve()), main, signature(Path(str(path) + "-wal")), revision)
            if version != self._vector_version or (main is not None and revision is None):
                ids, matrix, hashes = (
                    _vector_index.read_vector_snapshot(path)
                    if main is not None else ([], None, {})
                )
                self._vector_ids, self._vector_matrix, self._vector_hashes = ids, matrix, hashes
                self._vector_version = version
            return self._vector_ids or [], self._vector_matrix, self._vector_hashes

    def _embed_hits(self, query: str) -> list[tuple[str, float, str]]:
        """Encode/search an immutable snapshot; return the encoded content revision."""
        ids, matrix, hashes = self._vector_snapshot()
        if not ids:
            return []
        embedder = _embedder.ready_embedder()
        if embedder is None:
            return []
        shared = current_embedding.get()
        vector = shared.get() if shared is not None else None
        encoded = [vector] if vector is not None else (None if shared is not None else embedder.encode([query]))
        if not encoded:
            return []
        hits = _vector_index.vector_search(encoded[0], matrix, ids, k=_EMBED_TOP_K)
        return [(cid, score, hashes[cid]) for cid, score in hits if score >= _EMBED_MIN_SIMILARITY]

    def _fetch_vector_rows(
        self,
        connection: sqlite3.Connection,
        hits: list[tuple[str, float, str]],
    ) -> list[tuple[str, sqlite3.Row]]:
        if not hits:
            return []
        placeholders = ",".join("?" for _ in hits)
        fetched = connection.execute(
            f"""SELECT s.*, s.rowid AS context_rowid,
                       context_activity_summary_tier(s.content) AS workspace_tier
                FROM activity_summaries AS s
                WHERE s.summary_id IN ({placeholders})
                  AND context_activity_timestamp(s.period_end) BETWEEN context_index_after() AND context_index_before()
                  AND context_activity_timestamp(s.imported_at) <= context_index_cutoff()""",
            [cid for cid, _score, _revision in hits],
        ).fetchall()
        by_id: dict[str, dict] = {str(row["summary_id"]): dict(row) for row in fetched}
        out: list[tuple[str, sqlite3.Row]] = []
        for cid, score, revision in hits:
            row = by_id.get(cid)
            if row is None or row.get("content_hash") != revision:
                continue  # Changed or removed summaries cannot inherit an old vector score.
            row["native_query_rank"] = -score  # 对齐 bm25 语义：越小越相关
            out.append(("summary", row))  # type: ignore[arg-type]
        return out

    def recommend(
        self,
        query: str,
        workspace: WorkspaceIdentity,
        now: float | datetime,
        plan: QueryPlan | None = None,
    ) -> SourceResult:
        if plan is not None and not plan.should_recall:
            return SourceResult("disabled")
        # Preserve native literal identifiers (e.g. error_code or a-b); parsing
        # the already prepared terms again changes matches and scan cost.
        # No pair index here: two-character words would force a full scan.
        lexical = LexicalQuery.from_text(query, word_split=False) if plan is not None else None
        lexical_query = lexical.terms if lexical is not None else query
        budget = None
        try:
            with open_readonly(self._database_path, self._deadline_ms) as connection:
                budget = ReadBudget(connection, self._deadline_ms)
                if plan is not None:
                    set_read_window(connection, plan.cutoff, plan.time_start, plan.time_end)
                tables = _validate_schema(connection)
                event_columns = {
                    str(row["name"])
                    for row in connection.execute("PRAGMA table_info(activity_events)")
                }
                has_event_epoch = "occurred_at_us" in event_columns
                summary_columns = {
                    str(row["name"])
                    for row in connection.execute("PRAGMA table_info(activity_summaries)")
                }
                has_summary_epoch = "period_end_us" in summary_columns
                connection.create_function(
                    "context_activity_timestamp",
                    1,
                    _timestamp_epoch,
                    deterministic=True,
                )
                connection.create_function(
                    "context_activity_summary_tier",
                    1,
                    lambda content: _workspace_tier(workspace, content),
                    deterministic=True,
                )
                connection.create_function(
                    "context_activity_event_tier",
                    4,
                    lambda app, title, url, searchable: _workspace_tier(
                        workspace, app, title, url, searchable
                    ),
                    deterministic=True,
                )
                cache_state = self._cache_state(connection, tables, now)
                # A legacy recency query may encounter a slow or malformed
                # cache.  Relevance and habit recommendations were already
                # read through bounded channels, so never erase them merely
                # because the optional recency supplement degrades.
                channel_errors: list[str] = []
                habit: RecommendationCandidate | None = None
                if has_event_epoch and (plan is None or plan.intent == "preference"):
                    try:
                        with budget.stage("habit", 0.2):
                            habit = self._habits.candidate(
                                connection,
                                workspace,
                                now,
                                self._habit_data_version(connection),
                                as_of=plan.cutoff if plan is not None else None,
                            )
                    except sqlite3.DatabaseError as exc:
                        channel_errors.append("deadline" if _is_deadline(exc) else "database_error")
                elif not has_event_epoch:
                    channel_errors.append("habit_omitted")
                if habit is not None:
                    habit = replace(habit, cache_state=cache_state)

                # 并行窗口从最早的词法查询前打开：encode/检索与词法三通道同时跑。
                embed_job = None
                if _embedder._enabled() and query.strip() and _EMBED_SLOTS.acquire(blocking=False):
                    try:
                        embed_job = _EMBED_POOL.submit(contextvars.copy_context().run, self._embed_hits, query)
                        embed_job.add_done_callback(lambda _done: _EMBED_SLOTS.release())
                    except RuntimeError:
                        _EMBED_SLOTS.release()
                        embed_job = None  # 进程退出竞态：静默走纯词法
                try:
                    with budget.stage("summary", 0.75):
                        summary_relevance = self._observe(
                            self._summary_relevance(connection, lexical_query, workspace, lexical)
                        )
                except sqlite3.DatabaseError as exc:
                    summary_relevance = []
                    channel_errors.append("deadline" if _is_deadline(exc) else "database_error")
                try:
                    with budget.stage("event", 0.8 if plan is None or plan.include_recent else 1.0):
                        event_relevance = self._observe(
                            self._event_relevance(connection, lexical_query, workspace, lexical)
                        )
                except sqlite3.DatabaseError as exc:
                    event_relevance = []
                    channel_errors.append("deadline" if _is_deadline(exc) else "database_error")
                if has_summary_epoch and (plan is None or plan.include_recent):
                    try:
                        with budget.stage("summary_recent", 0.5):
                            summary_recency = self._observe(self._summary_recency(connection))
                    except sqlite3.DatabaseError as exc:
                        summary_recency = []
                        channel_errors.append("deadline" if _is_deadline(exc) else "database_error")
                else:
                    summary_recency = []
                    if not has_summary_epoch:
                        channel_errors.append("recency_omitted")
                if has_event_epoch and (plan is None or plan.include_recent):
                    try:
                        with budget.stage("event_recent", 1.0):
                            event_recency = self._observe(self._event_recency(connection))
                    except sqlite3.DatabaseError as exc:
                        event_recency = []
                        channel_errors.append("deadline" if _is_deadline(exc) else "database_error")
                else:
                    event_recency = []
                    if not has_event_epoch:
                        channel_errors.append("recency_omitted")
                embedding_relevance: list[tuple[str, sqlite3.Row]] = []
                if embed_job is not None:
                    try:
                        with budget.pause_for_compute(), observe_stage(budget.stages, "vector_wait"):
                            hits = embed_job.result(timeout=_VECTOR_WAIT_SECONDS)
                        if hits:
                            with budget.stage("vector_validate"):
                                embedding_relevance = self._observe(self._fetch_vector_rows(connection, hits))
                    except Exception:
                        # 向量段超时/任何异常只降级为纯词法，绝不拖垮整个 activity 源。
                        embedding_relevance = []
                # Preserve independent rankings before deduplication. A double
                # hit receives both ranks rather than inheriting a cosine score
                # as though it were a BM25 score.
                channels = (
                    ("activity-vector", embedding_relevance, ()),
                    ("activity-summary", summary_relevance, ()),
                    ("activity-event", (), event_relevance),
                )
                ranked = []
                for name, summaries, events in channels:
                    items = self._merge_candidates(summaries, events, workspace, cache_state)
                    for rank, item in enumerate(items, 1):
                        ranked.append(replace(item, channel_ranks=((name, rank),)))
                relevance = fuse_candidates(tuple(ranked))[:_CHANNEL_LIMIT]
                recency = self._merge_candidates(
                    summary_recency,
                    event_recency,
                    workspace,
                    cache_state,
                )
                return SourceResult(
                    stages=tuple(budget.stages) if budget is not None else (),
                    availability="available",
                    relevance=relevance,
                    recency=recency,
                    habit=habit,
                    cache_state=cache_state,
                    error_category=(
                        "deadline" if "deadline" in channel_errors
                        else ("recency_omitted" if "recency_omitted" in channel_errors
                              else (channel_errors[0] if channel_errors else ""))
                    ),
                    diagnostics=tuple(
                        diagnostic for diagnostic in ("recency_omitted", "habit_omitted")
                        if diagnostic in channel_errors
                    ),
                )
        except FileNotFoundError:
            return SourceResult(stages=tuple(budget.stages) if budget is not None else (), availability="absent")
        except _IncompatibleSchema:
            return SourceResult(
                stages=tuple(budget.stages) if budget is not None else (),
                availability="error",
                error_category="incompatible_schema",
            )
        except sqlite3.DatabaseError as exc:
            return SourceResult(
                stages=tuple(budget.stages) if budget is not None else (),
                availability="error",
                error_category="deadline" if _is_deadline(exc) else "database_error",
            )

    @staticmethod
    def _habit_data_version(
        connection: sqlite3.Connection,
    ) -> tuple[int, tuple[tuple[int, ...], ...], str]:
        row = connection.execute("PRAGMA data_version").fetchone()
        data_version = int(row[0]) if row is not None else 0
        database_row = next(
            (item for item in connection.execute("PRAGMA database_list") if item[1] == "main"),
            None,
        )
        filename = str(database_row[2] or "") if database_row is not None else ""
        source_version: list[tuple[int, ...]] = []
        for path in (Path(filename), Path(f"{filename}-wal")) if filename else ():
            try:
                stat = path.stat()
            except OSError:
                source_version.append((0, 0, 0, 0, 0))
            else:
                source_version.append(
                    (
                        stat.st_dev,
                        stat.st_ino,
                        stat.st_size,
                        stat.st_mtime_ns,
                        stat.st_ctime_ns,
                    )
                )
        signal_rows: list[tuple[Any, ...]] = []
        for order in ("ASC", "DESC"):
            signal_rows.extend(
                tuple(item)
                for item in connection.execute(
                    f"""SELECT rowid, segment_id, event_id, occurred_at, kind,
                               app_name, window_title, url, url_search_text,
                               searchable_text
                        FROM activity_events
                        ORDER BY rowid {order} LIMIT 32"""
                )
            )
        content_signal = hashlib.sha256(
            json.dumps(
                signal_rows,
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        return data_version, tuple(source_version), content_signal

    @staticmethod
    def _cache_state(
        connection: sqlite3.Connection,
        tables: set[str],
        now: float | datetime,
    ) -> str:
        if "sync_files" not in tables:
            return "stale-cache"
        successful = [
            parsed
            for row in connection.execute(
                "SELECT last_success_at FROM sync_files WHERE TRIM(last_success_at) <> ''"
            )
            if (parsed := _parse_timestamp(row["last_success_at"])) is not None
        ]
        if not successful:
            return "stale-cache"
        return (
            "stale-cache"
            if _now_datetime(now) - max(successful) > _FRESHNESS_WINDOW
            else "cached"
        )

    @staticmethod
    def _native_relevance(
        connection: sqlite3.Connection, lexical: LexicalQuery, kind: str,
    ) -> list[tuple[str, sqlite3.Row]]:
        """Linear posting-list recall, with coverage and workspace before LIMIT."""
        if not lexical.terms:
            return []
        if kind == "summary":
            table, content, timestamp = "activity_summaries", "s.content", "s.period_end"
            tier = "context_activity_summary_tier(s.content)"
        else:
            table, timestamp = "activity_events", "s.occurred_at"
            content = "(s.app_name || ' ' || s.window_title || ' ' || s.url_search_text || ' ' || s.selection_text || ' ' || s.searchable_text)"
            tier = "context_activity_event_tier(s.app_name, s.window_title, s.url_search_text, s.searchable_text)"
        masks, params = lexical.indexed_masks_sql(table + "_fts")
        if masks:
            from_clause = f"query_masks AS q JOIN {table} AS s ON s.rowid = q.id"
            mask, term_params = "q.query_match_mask", ()
        else:
            from_clause = f"{table} AS s"
            mask, term_params = lexical.mask_sql(content)
            long_terms = tuple(term for term in lexical.terms if len(term) >= 3)
            anchor_query = lexical.anchor_fts_query()
            if anchor_query or len(lexical.terms) - len(long_terms) < lexical.minimum:
                # Short terms still scan literally, within an indexed posting list:
                # the required target's, or, when fewer terms are short than the
                # required coverage, the long terms' (every valid row contains one;
                # as in the session source, this prefilter loses no hits).
                from_clause += f" JOIN {table}_fts(?) AS f ON f.rowid = s.rowid"
                term_params = (*term_params, anchor_query or " OR ".join(f'"{term}"' for term in long_terms))
        anchors, anchor_params = lexical.bind_anchor_sql(connection, content), ()
        # Term coverage is cheap integer work; the time window is a Python call per row.
        # Filter on coverage first, so a broad query parses timestamps only for rows
        # that match enough of its terms, not for every row of a large posting list.
        rows = connection.execute(
            "WITH " + (masks + ", " if masks else "") + "matched AS MATERIALIZED ("
            f"SELECT s.rowid AS id, ({mask}) AS query_match_mask FROM {from_clause} "
            f"WHERE ({anchors})), "
            "scored AS MATERIALIZED ("
            f"SELECT q.id, -({lexical.coverage_sql('q.query_match_mask')}) AS native_query_rank "
            f"FROM matched AS q JOIN {table} AS s ON s.rowid = q.id "
            f"WHERE ({lexical.count_sql('q.query_match_mask')}) >= ? "
            f"AND context_activity_timestamp({timestamp}) BETWEEN context_index_after() AND context_index_before() "
            "AND context_activity_timestamp(s.imported_at) <= context_index_cutoff()), "
            "selected AS MATERIALIZED ("
            f"SELECT q.*, {tier} AS workspace_tier FROM scored AS q JOIN {table} AS s ON s.rowid = q.id "
            f"ORDER BY workspace_tier, native_query_rank, context_activity_timestamp({timestamp}) DESC, s.rowid DESC LIMIT ?) "
            f"SELECT s.*, s.rowid AS context_rowid, q.native_query_rank, q.workspace_tier FROM selected AS q "
            f"JOIN {table} AS s ON s.rowid = q.id ORDER BY q.workspace_tier, q.native_query_rank, "
            f"context_activity_timestamp({timestamp}) DESC, s.rowid DESC",
            (*params, *term_params, *anchor_params, lexical.minimum, _CHANNEL_LIMIT),
        ).fetchall()
        return [(kind, row) for row in rows]

    @staticmethod
    def _summary_relevance(
        connection: sqlite3.Connection, query: str | tuple[str, ...], workspace: WorkspaceIdentity,
        lexical: LexicalQuery | None = None,
    ) -> list[tuple[str, sqlite3.Row]]:
        if lexical is not None:
            return ActivityRecommendationReader._native_relevance(connection, lexical, "summary")
        terms = _query_terms(query)
        if not terms:
            return []
        anchors, anchor_params = lexical.anchor_sql("s.content") if lexical else ("1", ())
        anchor_query = lexical.anchor_fts_query() if lexical else ""
        if _requires_like_fallback(terms):
            fts_join = "JOIN activity_summaries_fts ON activity_summaries_fts.rowid = s.rowid" if anchor_query else ""
            fts_guard = "AND activity_summaries_fts MATCH ?" if anchor_query else ""
            fts_params = (anchor_query,) if anchor_query else ()
            score, like_params, minimum = lexical_match_sql("s.content", tuple(terms[:_MAX_QUERY_TERMS]))
            rows = connection.execute(
                f"""SELECT s.*, s.rowid AS context_rowid, -({score}) AS native_query_rank,
                           context_activity_summary_tier(s.content) AS workspace_tier
                    FROM activity_summaries AS s {fts_join}
                    WHERE native_query_rank <= ? {fts_guard}
                      AND context_activity_timestamp(s.period_end) BETWEEN context_index_after() AND context_index_before()
                      AND context_activity_timestamp(s.imported_at) <= context_index_cutoff()
                      AND ({anchors})
                    ORDER BY workspace_tier ASC, native_query_rank ASC,
                             context_activity_timestamp(s.period_end) DESC,
                             s.rowid DESC
                    LIMIT ?""",
                (*like_params, -minimum, *fts_params, *anchor_params, _CHANNEL_LIMIT),
            ).fetchall()
        else:
            base_query = _fts_query(terms)
            if anchor_query:
                base_query = f"({base_query}) AND ({anchor_query})"
            queries = [base_query]
            workspace_query = _workspace_fts_query(workspace)
            if workspace_query:
                queries.append(f"({base_query}) AND ({workspace_query})")
            rows_by_id: dict[int, sqlite3.Row] = {}
            for candidate_query in queries:
                for row in connection.execute(
                    f"""SELECT s.*, s.rowid AS context_rowid,
                              bm25(activity_summaries_fts) AS native_query_rank,
                              context_activity_summary_tier(s.content) AS workspace_tier
                       FROM activity_summaries_fts
                       JOIN activity_summaries AS s ON s.rowid = activity_summaries_fts.rowid
                       WHERE activity_summaries_fts MATCH ?
                         AND context_activity_timestamp(s.period_end) BETWEEN context_index_after() AND context_index_before()
                         AND context_activity_timestamp(s.imported_at) <= context_index_cutoff()
                         AND ({anchors})
                       ORDER BY native_query_rank ASC
                       LIMIT ?""",
                    (candidate_query, *anchor_params, _CHANNEL_LIMIT),
                ):
                    rows_by_id[int(row["context_rowid"])] = row
            rows = list(rows_by_id.values())
        rows.sort(
            key=lambda row: (
                int(row["workspace_tier"]),
                float(row["native_query_rank"]),
                -_timestamp_epoch(row["period_end"]),
                -int(row["context_rowid"]),
            )
        )
        return [("summary", row) for row in rows]

    @staticmethod
    def _event_relevance(
        connection: sqlite3.Connection, query: str | tuple[str, ...], workspace: WorkspaceIdentity,
        lexical: LexicalQuery | None = None,
    ) -> list[tuple[str, sqlite3.Row]]:
        if lexical is not None:
            return ActivityRecommendationReader._native_relevance(connection, lexical, "event")
        terms = _query_terms(query)
        if not terms:
            return []
        searchable = (
            "e.app_name || ' ' || e.window_title || ' ' || e.url_search_text || "
            "' ' || e.selection_text || ' ' || e.searchable_text"
        )
        anchors, anchor_params = lexical.anchor_sql(f"({searchable})") if lexical else ("1", ())
        anchor_query = lexical.anchor_fts_query() if lexical else ""
        if _requires_like_fallback(terms):
            fts_join = "JOIN activity_events_fts ON activity_events_fts.rowid = e.rowid" if anchor_query else ""
            fts_guard = "AND activity_events_fts MATCH ?" if anchor_query else ""
            fts_params = (anchor_query,) if anchor_query else ()
            score, like_params, minimum = lexical_match_sql(f"({searchable})", tuple(terms[:_MAX_QUERY_TERMS]))
            rows = connection.execute(
                f"""SELECT e.*, e.rowid AS context_rowid, -({score}) AS native_query_rank,
                           context_activity_event_tier(
                               e.app_name, e.window_title, e.url_search_text, e.searchable_text
                           ) AS workspace_tier
                    FROM activity_events AS e {fts_join}
                    WHERE native_query_rank <= ? {fts_guard}
                      AND context_activity_timestamp(e.occurred_at) BETWEEN context_index_after() AND context_index_before()
                      AND context_activity_timestamp(e.imported_at) <= context_index_cutoff()
                      AND ({anchors})
                    ORDER BY workspace_tier ASC, native_query_rank ASC,
                             context_activity_timestamp(e.occurred_at) DESC,
                             e.event_id DESC, e.rowid DESC
                    LIMIT ?""",
                (*like_params, -minimum, *fts_params, *anchor_params, _CHANNEL_LIMIT),
            ).fetchall()
        else:
            base_query = _fts_query(terms)
            if anchor_query:
                base_query = f"({base_query}) AND ({anchor_query})"
            queries = [base_query]
            workspace_query = _workspace_fts_query(workspace)
            if workspace_query:
                queries.append(f"({base_query}) AND ({workspace_query})")
            rows_by_id: dict[int, sqlite3.Row] = {}
            for candidate_query in queries:
                for row in connection.execute(
                    f"""SELECT e.*, e.rowid AS context_rowid,
                              bm25(activity_events_fts) AS native_query_rank,
                              context_activity_event_tier(
                                  e.app_name, e.window_title, e.url_search_text, e.searchable_text
                              ) AS workspace_tier
                       FROM activity_events_fts
                       JOIN activity_events AS e ON e.rowid = activity_events_fts.rowid
                       WHERE activity_events_fts MATCH ?
                         AND context_activity_timestamp(e.occurred_at) BETWEEN context_index_after() AND context_index_before()
                         AND context_activity_timestamp(e.imported_at) <= context_index_cutoff()
                         AND ({anchors})
                       ORDER BY native_query_rank ASC
                       LIMIT ?""",
                    (candidate_query, *anchor_params, _CHANNEL_LIMIT),
                ):
                    rows_by_id[int(row["context_rowid"])] = row
            rows = list(rows_by_id.values())
        rows.sort(
            key=lambda row: (
                int(row["workspace_tier"]),
                float(row["native_query_rank"]),
                -_timestamp_epoch(row["occurred_at"]),
                -int(row["event_id"]),
                -int(row["context_rowid"]),
            )
        )
        return [("event", row) for row in rows]

    @staticmethod
    def _summary_recency(connection: sqlite3.Connection) -> list[tuple[str, sqlite3.Row]]:
        rows = connection.execute(
            """SELECT s.*, s.rowid AS context_rowid, 0.0 AS native_query_rank,
                      context_activity_summary_tier(s.content) AS workspace_tier
               FROM activity_summaries AS s
               WHERE s.period_end_us BETWEEN context_index_after() * 1000000 AND context_index_before() * 1000000
                 AND context_activity_timestamp(s.imported_at) <= context_index_cutoff()
               ORDER BY s.period_end_us DESC, s.rowid DESC
               LIMIT ?""",
            (_CHANNEL_LIMIT,),
        ).fetchall()
        return [("summary", row) for row in rows]

    @staticmethod
    def _event_recency(connection: sqlite3.Connection) -> list[tuple[str, sqlite3.Row]]:
        rows = connection.execute(
            """SELECT e.*, 0.0 AS native_query_rank,
                      context_activity_event_tier(
                              e.app_name, e.window_title, e.url_search_text, e.searchable_text
                          ) AS workspace_tier
               FROM activity_events AS e
               WHERE e.occurred_at_us BETWEEN context_index_after() * 1000000 AND context_index_before() * 1000000
                 AND context_activity_timestamp(e.imported_at) <= context_index_cutoff()
               ORDER BY e.occurred_at_us DESC, e.rowid DESC
               LIMIT ?""",
            (_CHANNEL_LIMIT,),
        ).fetchall()
        return [("event", row) for row in rows]

    @staticmethod
    def _merge_candidates(
        summaries: Sequence[tuple[str, sqlite3.Row]],
        events: Sequence[tuple[str, sqlite3.Row]],
        workspace: WorkspaceIdentity,
        cache_state: str,
    ) -> tuple[RecommendationCandidate, ...]:
        candidates: list[RecommendationCandidate] = []
        seen: set[str] = set()
        for kind, row in (*summaries, *events):
            if kind == "summary":
                description = _summary_description(row)
                searchable = str(row["content"])
                primary = str(row["summary_id"])
                secondary = 0
                timestamp = _timestamp_epoch(row["period_end"] or row["period_start"])
                locator = SourceLocator("activity_summary", primary)
            else:
                description = _event_description(row)
                searchable = " ".join(str(row[field] or "") for field in (
                    "app_name", "window_title", "url_search_text", "selection_text", "searchable_text",
                ))
                primary = str(row["segment_id"])
                secondary = int(row["event_id"])
                timestamp = _timestamp_epoch(row["occurred_at"])
                locator = SourceLocator("activity_event", primary, secondary)
            identity = _identity(kind, primary, secondary)
            if identity in seen:
                continue  # 向量+词法双命中：留排前者（调用方以 embedding 优先传入）
            seen.add(identity)
            tier = int(row["workspace_tier"])
            candidates.append(
                RecommendationCandidate(
                    source="activity",
                    identity=identity,
                    description=description,
                    timestamp=timestamp,
                    workspace_tier=tier,
                    native_query_rank=float(row["native_query_rank"]),
                    topic_key=_topic_key(description),
                    trust_label="untrusted_observation",
                    locator=locator,
                    # Ranking checks evidence, not the truncated UI preview.
                    # This redacted, bounded value stays request-private.
                    private_text=_redact_text(searchable)[:6000],
                    available_at=_timestamp_epoch(row["imported_at"]),
                    revision=str(row["content_hash"]) if kind == "summary" else _identity("event", description, str(row["imported_at"])),
                    project_label=workspace.label if tier < 2 else "",
                    cache_state=cache_state,
                )
            )
            if len(candidates) >= _CHANNEL_LIMIT:
                break
        return tuple(candidates)

    def open(self, locator: SourceLocator, window: int, plan: QueryPlan | None = None) -> EvidenceResult:
        if locator.kind == "habit":
            empty_habit = EvidenceResult(
                source="habit",
                trust_label="inferred_pattern",
                title="Inferred Activity habit unavailable",
                items=(),
            )
            database_identity = str(self._database_path.resolve())
            try:
                with open_readonly(self._database_path, self._deadline_ms) as connection:
                    _validate_schema(connection)
                    database_identity = _database_identity(connection)
                    data_version = self._habit_data_version(connection)
            except (FileNotFoundError, _IncompatibleSchema, sqlite3.DatabaseError):
                self._habits.invalidate_database(database_identity)
                return empty_habit
            return self._habits.open(
                locator,
                database_identity=database_identity,
                data_version=data_version,
            )
        empty = EvidenceResult(
            source="activity",
            trust_label="untrusted_observation",
            title="Cached Activity evidence unavailable",
            items=(),
        )
        if locator.kind == "activity_summary":
            if not locator.primary or locator.secondary != 0:
                return empty
        elif locator.kind == "activity_event":
            if not locator.primary or locator.secondary <= 0:
                return empty
        else:
            return empty
        bounded_window = max(0, min(int(window), _MAX_EVIDENCE_WINDOW))
        try:
            with open_readonly(self._database_path, self._deadline_ms) as connection:
                _validate_schema(connection)
                connection.create_function("context_activity_timestamp", 1, _timestamp_epoch, deterministic=True)
                if plan is not None:
                    set_read_window(connection, plan.cutoff, plan.time_start, plan.time_end)
                if locator.kind == "activity_summary":
                    row = connection.execute(
                        """SELECT granularity, period_start, period_end, content
                           FROM activity_summaries WHERE summary_id = ?
                           AND context_activity_timestamp(period_end) BETWEEN context_index_after() AND context_index_before()
                           AND context_activity_timestamp(imported_at) <= context_index_cutoff() LIMIT 1""",
                        (locator.primary,),
                    ).fetchone()
                    if row is None:
                        return empty
                    return EvidenceResult(
                        source="activity",
                        trust_label="untrusted_observation",
                        title="Cached Activity summary",
                        items=(_evidence_item(_summary_output(row)),),
                    )

                target = connection.execute(
                    """SELECT occurred_at, kind, app_name, window_title, url
                       FROM activity_events
                       WHERE segment_id = ? AND event_id = ?
                         AND context_activity_timestamp(occurred_at) BETWEEN context_index_after() AND context_index_before()
                         AND context_activity_timestamp(imported_at) <= context_index_cutoff() LIMIT 1""",
                    (locator.primary, locator.secondary),
                ).fetchone()
                if target is None:
                    return empty
                before = connection.execute(
                    """SELECT occurred_at, kind, app_name, window_title, url
                       FROM activity_events
                       WHERE segment_id = ? AND event_id < ?
                         AND context_activity_timestamp(occurred_at) BETWEEN context_index_after() AND context_index_before()
                         AND context_activity_timestamp(imported_at) <= context_index_cutoff()
                       ORDER BY event_id DESC, rowid DESC LIMIT ?""",
                    (locator.primary, locator.secondary, bounded_window),
                ).fetchall()
                after = connection.execute(
                    """SELECT occurred_at, kind, app_name, window_title, url
                       FROM activity_events
                       WHERE segment_id = ? AND event_id > ?
                         AND context_activity_timestamp(occurred_at) BETWEEN context_index_after() AND context_index_before()
                         AND context_activity_timestamp(imported_at) <= context_index_cutoff()
                       ORDER BY event_id ASC, rowid ASC LIMIT ?""",
                    (locator.primary, locator.secondary, bounded_window),
                ).fetchall()
                context = [*reversed(before), *after]
                return EvidenceResult(
                    source="activity",
                    trust_label="untrusted_observation",
                    title="Cached Activity event",
                    items=tuple(
                        _evidence_item(_event_output(row))
                        for row in (target, *context)
                    ),
                )
        except (FileNotFoundError, _IncompatibleSchema, sqlite3.DatabaseError):
            return empty
