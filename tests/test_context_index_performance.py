"""Explicit production-sized latency acceptance for Context Index readers."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import gc
import json
import math
import os
import random
import sqlite3
import struct
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.runtime import activity_sync
from agent.runtime.context_index import workspace as workspace_module
from agent.runtime.context_index.activity_source import ActivityRecommendationReader
from agent.runtime.context_index.broker import ContextIndexBroker
from agent.runtime.context_index.diagnostics import trace_metadata
from agent.runtime.context_index.session_source import SessionRecommendationSource
from agent.runtime.context_index.workspace import WorkspaceIdentity
from agent.runtime.react import ReActAgent
from agent.runtime.tools import activity as activity_tool
from agent.runtime.tools.registry import ToolRegistry

pytestmark = [
    pytest.mark.context_index_performance,
    pytest.mark.skipif(
        os.getenv("ASTRA_RUN_CONTEXT_INDEX_PERF") != "1",
        reason="set ASTRA_RUN_CONTEXT_INDEX_PERF=1 for the production-sized latency gate",
    ),
]

_SEED = 8_312_026
_NOW = datetime(2026, 8, 31, 8, 0, tzinfo=UTC)
_WORKSPACE = WorkspaceIdentity(
    key="fixture-workspace",
    root="",
    label="wspace0",
)
_PERFORMANCE_QUERIES = (
    "deadline",
    "记忆索引",
    "code",
    "这个",
    "definitely-no-match-context-index-token",
    "perfquery001",
)
_BUDGET_ERRORS = frozenset({"deadline", "source_timeout"})


def _expected_prompt_sources(query: str) -> tuple[str, ...]:
    if query in {"这个", _PERFORMANCE_QUERIES[4]}:
        return ()
    return ("session", "activity") if query == "perfquery001" else ("session",)


def test_background_cold_snapshots_do_not_starve_parallel_archive_readers(tmp_path, monkeypatch):
    """Time the entire background cold fill without starving a small 75 ms reader.

    The large archive follows the bounded background recovery path. Its full
    load, decode, publication and cache consumption count toward the unchanged
    150 ms target; no snapshot is preloaded outside the timed section. The
    direct interactive 75 ms bulk-load failure remains separately diagnosed by
    the snapshot probes, and first-request degradation has behavioral coverage.
    """
    from agent.runtime.context_index import semantic_index, vector_index

    # A ready embedding backend has already imported NumPy. Cold here means
    # empty Astra snapshot caches, not concurrent Python module initialization.
    pytest.importorskip("numpy")
    monkeypatch.setenv("ASTRA_EMBEDDING_BACKEND", "mlx")
    path = tmp_path / "vectors.db"
    archives = {source: tmp_path / f"{source}.db" for source in ("session", "memory")}
    for archive in archives.values():
        with sqlite3.connect(archive) as db:
            db.execute("CREATE TABLE archive_fixture (id INTEGER PRIMARY KEY)")
        db.close()
    counts = {"session": 5000, "memory": 32, "activity": 1200}
    dim = 2560
    blob = struct.pack(f"<{dim}f", *([0.5] * dim))
    store = semantic_index.EvidenceVectorStore(path)
    for source, archive in archives.items():
        store._connection.executemany(
            "INSERT INTO context_memory_vectors VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ((source, semantic_index.archive_key(archive), str(index), "revision", semantic_index._LAYOUT,
              float(index), float(index), blob) for index in range(counts[source])),
        )
    store._connection.executemany(
        "INSERT INTO context_index_vectors(summary_id, content_hash, dim, vec) VALUES (?, ?, ?, ?)",
        ((str(index), "revision", dim, blob) for index in range(counts["activity"])),
    )
    store._connection.commit()
    store.close()

    def read(source, barrier):
        barrier.wait()
        started = time.perf_counter()
        try:
            if source == "activity":
                rows, matrix, _revisions = vector_index.read_vector_snapshot(path)
            else:
                if source == "session" and not semantic_index.warm_snapshot(path, source, archives[source]):
                    return math.inf  # A discarded fill is not recovered by an interactive retry.
                rows, matrix = semantic_index.read_snapshot(path, source, archives[source])
        except sqlite3.OperationalError as error:
            if "interrupted" not in str(error):
                raise
            return math.inf  # the reader ran into its own deadline: a read over any bound
        assert len(rows) == counts[source] and matrix.shape == (counts[source], dim)
        return (time.perf_counter() - started) * 1000

    def cold_round(pool):
        with semantic_index._cache_lock:
            semantic_index._cache.clear()
        # A round allocates about as many objects as one generation of the
        # collector holds, so every tenth round would stop for a full
        # collection in the middle of the session read. Collecting between
        # rounds keeps that pause, which is the process's and not the
        # reader's, out of the measured time.
        gc.collect()
        barrier = threading.Barrier(3)
        futures = {source: pool.submit(read, source, barrier) for source in counts}
        return {source: future.result(timeout=5) for source, future in futures.items()}

    with ThreadPoolExecutor(max_workers=3) as pool:
        # The first rounds also pay for what a process does once, such as
        # fresh memory for 100 MB of rows. That is not the cold this test is
        # about, so they are not measured.
        for _ in range(3):
            cold_round(pool)
        rounds = [cold_round(pool) for _ in range(20)]
    # The 95th percentile of 20 rounds: one round may be slow or miss its deadline.
    p95 = {
        source: round(sorted(measured[source] for measured in rounds)[math.ceil(len(rounds) * 0.95) - 1], 2)
        for source in counts
    }
    missed = {source: sum(measured[source] == math.inf for measured in rounds) for source in counts}
    print(f"Concurrent background cold fill and interactive reads: rows={counts} dim={dim} p95_ms={p95} failed_reads_or_fills={missed}")
    assert all(value < 150 for value in p95.values())


def _create_session_fixture(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=OFF;
        CREATE TABLE sessions (
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
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT DEFAULT '',
            tool_name TEXT DEFAULT '',
            timestamp REAL NOT NULL,
            msg_index INTEGER NOT NULL
        );
        CREATE INDEX idx_messages_session ON messages(session_id, msg_index);
        CREATE INDEX idx_messages_timestamp
            ON messages(timestamp DESC, id DESC);
        CREATE INDEX idx_messages_session_timestamp
            ON messages(session_id, timestamp DESC, id DESC);
        CREATE INDEX idx_sessions_workspace_key
            ON sessions(workspace_key);
        CREATE VIRTUAL TABLE messages_fts USING fts5(
            content, content=messages, content_rowid=id, tokenize='trigram'
        );
        """
    )
    randomizer = random.Random(_SEED)
    sessions = []
    messages = []
    now = _NOW.timestamp()
    for session_number in range(5_000):
        session_id = "current-session" if session_number == 0 else f"session-{session_number:05d}"
        sessions.append(
            (
                session_id,
                "fixture workspace continuation",
                now - session_number,
                20,
                _WORKSPACE.key,
                "",
            )
        )
        fixture_terms = []
        if session_number < 11:
            fixture_terms.append("deadline")
        if session_number < 3:
            fixture_terms.extend(("记忆索引", "记忆推荐机制", "报错日志"))
        if session_number < 3_001:
            fixture_terms.append("code")
        if session_number < 776:
            fixture_terms.append("这个")
        for message_index in range(20):
            matching = session_number < 200 and message_index == 0
            if session_number == 0 and message_index == 18:
                content = _PERFORMANCE_QUERIES[4]
            elif matching:
                content = " ".join(
                    (
                        *fixture_terms,
                        f"perfquery{session_number:03d}",
                        f"wspace{session_number % 4}",
                        "result",
                    )
                )
            elif message_index == 0 and fixture_terms:
                content = " ".join(
                    (*fixture_terms, f"wspace{session_number % 4}", "result")
                )
            else:
                content = f"background fixture row {randomizer.randrange(1_000_000)}"
            messages.append(
                (session_id, "user" if message_index % 2 == 0 else "assistant", content, now - session_number - message_index, message_index)
            )
        if len(sessions) == 250:
            connection.executemany(
                "INSERT INTO sessions(id, title, started_at, message_count, workspace_key, workspace_root) VALUES (?, ?, ?, ?, ?, ?)",
                sessions,
            )
            connection.executemany(
                "INSERT INTO messages(session_id, role, content, timestamp, msg_index) VALUES (?, ?, ?, ?, ?)",
                messages,
            )
            sessions.clear()
            messages.clear()
    if sessions:
        connection.executemany(
            "INSERT INTO sessions(id, title, started_at, message_count, workspace_key, workspace_root) VALUES (?, ?, ?, ?, ?, ?)",
            sessions,
        )
        connection.executemany(
            "INSERT INTO messages(session_id, role, content, timestamp, msg_index) VALUES (?, ?, ?, ?, ?)",
            messages,
        )
    connection.execute("INSERT INTO messages_fts(messages_fts) VALUES ('rebuild')")
    connection.commit()
    return connection


def _create_activity_fixture(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=OFF;
        CREATE TABLE activity_events (
            segment_id TEXT NOT NULL,
            event_id INTEGER NOT NULL,
            occurred_at TEXT NOT NULL,
            occurred_at_us INTEGER NOT NULL,
            kind TEXT NOT NULL,
            app_name TEXT NOT NULL DEFAULT '',
            bundle_id TEXT NOT NULL DEFAULT '',
            window_title TEXT NOT NULL DEFAULT '',
            url TEXT NOT NULL DEFAULT '',
            url_search_text TEXT NOT NULL,
            selection_text TEXT NOT NULL DEFAULT '',
            searchable_text TEXT NOT NULL DEFAULT '',
            raw_json TEXT NOT NULL,
            imported_at TEXT NOT NULL,
            PRIMARY KEY(segment_id, event_id)
        );
        CREATE TABLE activity_summaries (
            summary_id TEXT PRIMARY KEY,
            source_path TEXT NOT NULL,
            granularity TEXT NOT NULL,
            period_start TEXT NOT NULL DEFAULT '',
            period_end TEXT NOT NULL DEFAULT '',
            period_end_us INTEGER NOT NULL,
            content TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            source_mtime_ns INTEGER NOT NULL,
            imported_at TEXT NOT NULL
        );
        CREATE TABLE sync_files (
            source_path TEXT PRIMARY KEY,
            source_kind TEXT NOT NULL,
            source_identity TEXT NOT NULL DEFAULT '',
            byte_offset INTEGER NOT NULL DEFAULT 0,
            observed_size INTEGER NOT NULL DEFAULT 0,
            observed_mtime_ns INTEGER NOT NULL DEFAULT 0,
            last_success_at TEXT NOT NULL DEFAULT '',
            last_error TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX idx_activity_events_occurred_at ON activity_events(occurred_at);
        CREATE INDEX idx_activity_events_occurred_at_us ON activity_events(occurred_at_us);
        CREATE INDEX idx_activity_summaries_period_end_us ON activity_summaries(period_end_us);
        CREATE VIRTUAL TABLE activity_events_fts USING fts5(
            app_name, window_title, url_search_text, selection_text, searchable_text,
            content='activity_events', content_rowid='rowid', tokenize='trigram'
        );
        CREATE VIRTUAL TABLE activity_summaries_fts USING fts5(
            content, content='activity_summaries', content_rowid='rowid', tokenize='trigram'
        );
        """
    )
    randomizer = random.Random(_SEED)
    event_rows = []
    summary_rows = []
    for event_number in range(500_000):
        occurred = _NOW - timedelta(seconds=randomizer.randrange(42 * 24 * 60 * 60))
        matching = event_number < 200
        searchable = (
            f"perfquery{event_number:03d} wspace{event_number % 4} activity"
            if matching
            else "background fixture activity"
        )
        event_rows.append(
            (
                f"segment-{event_number // 1000:04d}",
                event_number + 1,
                occurred.isoformat(),
                int(occurred.timestamp() * 1_000_000),
                "window",
                "FixtureApp",
                "org.astra.fixture",
                "fixture-workspace",
                "https://fixture.invalid/",
                "fixture invalid",
                "",
                searchable,
                "{}",
                occurred.isoformat(),
            )
        )
        if len(event_rows) == 2_000:
            connection.executemany(
                """INSERT INTO activity_events(
                    segment_id, event_id, occurred_at, occurred_at_us, kind, app_name, bundle_id,
                    window_title, url, url_search_text, selection_text, searchable_text,
                    raw_json, imported_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                event_rows,
            )
            event_rows.clear()
    if event_rows:
        connection.executemany(
            """INSERT INTO activity_events(
                segment_id, event_id, occurred_at, occurred_at_us, kind, app_name, bundle_id,
                window_title, url, url_search_text, selection_text, searchable_text,
                raw_json, imported_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            event_rows,
        )
    for summary_number in range(10_000):
        occurred = _NOW - timedelta(minutes=summary_number)
        matching = summary_number < 200
        summary_rows.append(
            (
                f"summary-{summary_number:05d}",
                "fixture-source",
                "10min",
                (occurred - timedelta(minutes=10)).isoformat(),
                occurred.isoformat(),
                int(occurred.timestamp() * 1_000_000),
                (f"perfquery{summary_number:03d} wspace{summary_number % 4} summary"
                 if matching else "background fixture summary"),
                f"hash-{summary_number}",
                1,
                occurred.isoformat(),
            )
        )
    connection.executemany(
        """INSERT INTO activity_summaries(
            summary_id, source_path, granularity, period_start, period_end, period_end_us,
            content, content_hash, source_mtime_ns, imported_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        summary_rows,
    )
    connection.execute(
        "INSERT INTO sync_files(source_path, source_kind, last_success_at) VALUES (?, ?, ?)",
        ("fixture-source", "events", _NOW.isoformat()),
    )
    connection.execute("INSERT INTO activity_events_fts(activity_events_fts) VALUES ('rebuild')")
    connection.execute("INSERT INTO activity_summaries_fts(activity_summaries_fts) VALUES ('rebuild')")
    connection.commit()
    return connection


@pytest.fixture(scope="module")
def production_fixture(tmp_path_factory: pytest.TempPathFactory):
    root = tmp_path_factory.mktemp(f"context-index-{_SEED}")
    session_db = root / "sessions.db"
    activity_db = root / "activity-history.sqlite3"
    session_connection = _create_session_fixture(session_db)
    activity_connection = _create_activity_fixture(activity_db)
    try:
        yield session_db, activity_db
    finally:
        session_connection.close()
        activity_connection.close()


def _metadata(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


def test_native_long_chinese_recall_has_evidence_within_source_deadline(production_fixture):
    from agent.runtime.context_index.query import plan_query

    session_db, _ = production_fixture
    reader = SessionRecommendationSource(session_db)
    durations = []
    missed = []
    queries = (
        ("新记忆推荐库，质量如何", "记忆推荐机制"),
        ("目前记忆推荐机制在跑吧", "记忆推荐机制"),
        ("帮我看看这个报错", "报错日志"),
    )
    for round_index in range(20):
        for query, expected in queries:
            plan = plan_query(query, _NOW)
            started = time.perf_counter_ns()
            result = reader.recommend(query, _WORKSPACE, "current-session", frozenset(), _NOW.timestamp(), plan)
            durations.append((time.perf_counter_ns() - started) / 1_000_000)
            if result.error_category == "deadline":
                # A call that ran into the source deadline is a call over the
                # bound: the percentile below allows as few of them as it
                # allows slow calls, and no more.
                missed.append((query, round_index, sqlite3.sqlite_version, durations[-1]))
                continue
            assert not result.error_category, (query, result.error_category, round_index,
                                               sqlite3.sqlite_version, durations[-1])
            assert result.relevance, query
            assert all(expected in item.private_text for item in result.relevance)
    calls = len(durations)
    durations.sort()
    p95 = durations[math.ceil(calls * 0.95) - 1]
    print(f"Native Chinese session recall: nonempty={calls - len(missed)}/{calls} errors={len(missed)} "
          f"p95={p95:.2f}ms max={max(durations):.2f}ms")
    assert p95 <= 75.0
    assert len(missed) <= calls - math.ceil(calls * 0.95), missed


def test_warm_semantic_search_over_5000_vectors_stays_bounded(monkeypatch, production_fixture, tmp_path):
    """Fixed 4096-D vectors measure search/revalidation only, not model quality."""
    pytest.importorskip("numpy")
    from agent.runtime.context_index import embedder, semantic_index
    from agent.runtime.context_index.query import plan_query
    from agent.runtime.context_index.query_embedding import QueryEmbedding, current_embedding
    from agent.runtime.context_index.semantic_index import rebuild_source, read_snapshot
    from agent.runtime.context_index.semantic_reader import SemanticReader

    class Encoder:
        def encode(self, texts):
            return [[1.0, *([0.0] * 4095)]] * len(texts)

    encoder = Encoder()
    monkeypatch.setattr(embedder, "ready_embedder", lambda: encoder)
    session_db, _ = production_fixture
    vectors = tmp_path / "vectors.db"
    report = rebuild_source(session_db, vectors, "session", backend=encoder)
    assert report["encoded"] == 5000
    # Populate the immutable cache before measuring warm search. Loading the
    # entire 4096-D fixture is setup, not one of the interactive queries below;
    # cold-load latency has its own benchmark. Restore the real reader/deadline
    # before all timed rounds instead of spending their 75 ms budget on setup.
    open_readonly = semantic_index.open_readonly
    with monkeypatch.context() as warmup:
        warmup.setattr(semantic_index, "open_readonly",
                      lambda path, **_kwargs: open_readonly(path, deadline_ms=10_000))
        rows, _ = read_snapshot(vectors, "session", session_db)
    assert len(rows) == 5000
    reader = SemanticReader(session_db, vectors)
    plan = plan_query("换一种说法寻找相关历史", _NOW)
    durations = []
    for _ in range(30):
        token = current_embedding.set(QueryEmbedding(plan.query))
        try:
            start = time.perf_counter()
            result = reader.recommend("session", session_db, plan, _WORKSPACE, "current-session", frozenset())
            durations.append((time.perf_counter() - start) * 1000)
            assert result.relevance and not result.error_category
        finally:
            current_embedding.reset(token)
    p95 = sorted(durations)[math.ceil(len(durations) * 0.95) - 1]
    print(f"Semantic search: vectors=5000 dim=4096 nonempty=30/30 p95={p95:.2f}ms")
    assert p95 <= 75.0


def test_production_sized_recommendations_stay_bounded_and_read_only(
    monkeypatch: pytest.MonkeyPatch, production_fixture: tuple[Path, Path]
) -> None:
    session_db, activity_db = production_fixture
    monkeypatch.setattr(activity_tool, "_sync_activity_store", lambda *_args: pytest.fail("must not sync"))
    monkeypatch.setattr(activity_sync, "resolve_source_roots", lambda *_args, **_kwargs: pytest.fail("must not discover"))
    session_source = SessionRecommendationSource(session_db)
    activity_source = ActivityRecommendationReader(activity_db)
    broker = ContextIndexBroker("all", 900, session_source, activity_source)
    database_files = tuple(path for database in (session_db, activity_db) for path in
                           (database, Path(f"{database}-wal"), Path(f"{database}-shm")))
    assert all(path.exists() for path in database_files)
    before = {path: _metadata(path) for path in database_files}

    session_results = []
    session_durations_ms = []
    for turn in range(200):
        started = time.perf_counter_ns()
        session_results.append(
            session_source.recommend(
                _PERFORMANCE_QUERIES[turn % len(_PERFORMANCE_QUERIES)],
                _WORKSPACE,
                "current-session",
                frozenset(),
                _NOW.timestamp(),
            )
        )
        session_durations_ms.append((time.perf_counter_ns() - started) / 1_000_000)
    session_errors = [
        {
            "turn": turn,
            "query": _PERFORMANCE_QUERIES[turn % len(_PERFORMANCE_QUERIES)],
            "duration_ms": round(session_durations_ms[turn], 2),
            "availability": result.availability,
            "error_category": result.error_category,
            "diagnostics": result.diagnostics,
        }
        for turn, result in enumerate(session_results)
        if result.error_category or result.availability != "available"
    ]
    session_samples = [
        {
            "turn": turn, "query": _PERFORMANCE_QUERIES[turn % len(_PERFORMANCE_QUERIES)],
            "raw_ms": raw_ms,
            "effective_ms": max(raw_ms, 75.001) if result.error_category in _BUDGET_ERRORS else raw_ms,
            "availability": result.availability, "error": result.error_category,
            "matched": bool(result.relevance), "diagnostics": result.diagnostics,
            "stages": [{"name": stage.name, "ms": stage.latency_ms, "error": stage.error_category}
                       for stage in result.stages],
        }
        for turn, (raw_ms, result) in enumerate(zip(session_durations_ms, session_results))
    ]
    session_effective_ms = sorted(sample["effective_ms"] for sample in session_samples)
    session_p50 = session_effective_ms[len(session_effective_ms) // 2]
    session_p95 = session_effective_ms[math.ceil(len(session_effective_ms) * 0.95) - 1]
    started = time.perf_counter_ns()
    activity_probe = ActivityRecommendationReader(activity_db, deadline_ms=10_000).recommend(
        "perfquery000", _WORKSPACE, _NOW
    )
    activity_probe_ms = (time.perf_counter_ns() - started) / 1_000_000
    assert activity_probe.availability == "available"

    async def build(turn: int):
        workspace = WorkspaceIdentity(key=_WORKSPACE.key, root="", label=f"wspace{turn % 4}")
        return await broker.build(
            _PERFORMANCE_QUERIES[turn % len(_PERFORMANCE_QUERIES)],
            f"performance-{turn:03d}",
            "current-session",
            workspace,
            _NOW + timedelta(minutes=turn),
            frozenset(),
        )

    warmup_durations_ms = []
    for warmup in range(5):
        started = time.perf_counter_ns()
        asyncio.run(build(-warmup - 1))
        warmup_durations_ms.append((time.perf_counter_ns() - started) / 1_000_000)
    durations_ms = []
    packs = []
    traces = []
    for turn in range(200):
        started = time.perf_counter_ns()
        packs.append(asyncio.run(build(turn)))
        assert broker.last_trace is not None
        traces.append(broker.last_trace)
        durations_ms.append((time.perf_counter_ns() - started) / 1_000_000)

    broker_samples = [
        {
            "turn": turn, "query": _PERFORMANCE_QUERIES[turn % len(_PERFORMANCE_QUERIES)],
            "raw_ms": raw_ms,
            "effective_ms": max(raw_ms, 150.001) if any(error in _BUDGET_ERRORS for error in trace.source_errors.values()) else raw_ms,
            "expected_sources": list(_expected_prompt_sources(_PERFORMANCE_QUERIES[turn % len(_PERFORMANCE_QUERIES)])),
            "displayed_sources": sorted({row.source for row in pack.rows}),
            "trace": trace_metadata(trace),
        }
        for turn, (raw_ms, pack, trace) in enumerate(zip(durations_ms, packs, traces))
    ]
    effective_ms = sorted(sample["effective_ms"] for sample in broker_samples)
    p50 = effective_ms[len(effective_ms) // 2]
    p95 = effective_ms[math.ceil(len(effective_ms) * 0.95) - 1]
    statuses = {
        name: sum(trace.source_status.get(name) == "available" for trace in traces)
        for name in ("session", "activity")
    }
    source_degraded = {
        name: sum(bool(trace.source_errors.get(name)) for trace in traces)
        for name in ("session", "activity")
    }
    displayed_by_source = {
        source: sum(any(row.source == source for row in trace.displayed) for trace in traces)
        for source in ("session", "activity")
    }
    unavailable_turns = [
        {
            "turn": turn,
            "query": _PERFORMANCE_QUERIES[turn % len(_PERFORMANCE_QUERIES)],
            "status": trace.source_status,
            "errors": trace.source_errors,
            "diagnostics": trace.source_diagnostics,
        }
        for turn, trace in enumerate(traces)
        if any(trace.source_status.get(name) != "available" for name in ("session", "activity"))
    ]
    print(
        "Context Index performance "
        f"session-p50={session_p50:.2f}ms session-p95={session_p95:.2f}ms "
        f"session-max={max(session_durations_ms):.2f}ms activity-probe={activity_probe_ms:.2f}ms "
        f"warmup-max={max(warmup_durations_ms):.2f}ms p50={p50:.2f}ms "
        f"p95={p95:.2f}ms max={max(durations_ms):.2f}ms source-available={statuses} "
        f"source-degraded={source_degraded} displayed={displayed_by_source} "
        f"last-errors={broker.last_trace.source_errors if broker.last_trace else {}} "
        f"unavailable-turns={unavailable_turns} session-errors={session_errors}"
    )
    for sample in session_samples:
        print("Context Index direct source sample " + json.dumps(sample, ensure_ascii=False, allow_nan=False))
    for sample in broker_samples:
        print("Context Index broker sample " + json.dumps(sample, ensure_ascii=False, allow_nan=False))
    for sample in session_samples:
        assert not sample["error"] or sample["error"] in _BUDGET_ERRORS, sample
        assert sample["availability"] == "available" or sample["error"] in _BUDGET_ERRORS, sample
        if sample["query"] == _PERFORMANCE_QUERIES[4]:
            assert not sample["matched"], sample
        elif not sample["matched"]:
            assert sample["error"] in _BUDGET_ERRORS, sample
    # The direct lexical reader matches the common-word query too; the broker
    # separately suppresses that query. Do not dilute either coverage population.
    session_expected = [sample for sample in session_samples if sample["query"] != _PERFORMANCE_QUERIES[4]]
    session_expected_ms = sorted(sample["effective_ms"] for sample in session_expected)
    session_expected_p95 = session_expected_ms[math.ceil(len(session_expected_ms) * 0.95) - 1]
    session_coverage = sum(sample["matched"] for sample in session_expected) / len(session_expected)
    assert session_p95 <= 75.0
    assert session_expected_p95 <= 75.0
    assert session_coverage >= 0.95
    assert p95 <= 150.0
    assert max(len(pack.rendered) for pack in packs) <= 900
    assert session_source.max_rows_observed <= 12
    assert activity_source.max_rows_observed <= 12
    assert broker.last_trace is not None
    assert set(broker.last_trace.source_status) == {"session", "activity", "memory"}
    assert broker.last_trace.source_status["memory"] == "disabled"
    for sample in broker_samples:
        errors = sample["trace"]["source_errors"]
        assert set(errors) <= {"session", "activity", "memory"}, sample
        assert all(error in _BUDGET_ERRORS for error in errors.values()), sample
        for source in ("session", "activity"):
            assert sample["trace"]["source_status"][source] == "available" or errors.get(source) in _BUDGET_ERRORS, sample
        if not sample["expected_sources"]:
            assert not sample["displayed_sources"], sample
        for source in sample["expected_sources"]:
            if source not in sample["displayed_sources"]:
                assert errors.get(source) in _BUDGET_ERRORS, sample
    expected = [sample for sample in broker_samples if sample["expected_sources"]]
    expected_ms = sorted(sample["effective_ms"] for sample in expected)
    expected_p95 = expected_ms[math.ceil(len(expected_ms) * 0.95) - 1]
    coverage = sum(bool(sample["displayed_sources"]) for sample in expected) / len(expected)
    assert expected_p95 <= 150.0
    assert coverage >= 0.95
    by_source = {}
    for source in ("session", "activity"):
        required = [sample for sample in broker_samples if source in sample["expected_sources"]]
        source_ms = sorted(max(sample["raw_ms"], 150.001)
                           if sample["trace"]["source_errors"].get(source) in _BUDGET_ERRORS else sample["raw_ms"]
                           for sample in required)
        source_p95 = source_ms[math.ceil(len(source_ms) * 0.95) - 1]
        source_coverage = sum(source in sample["displayed_sources"] for sample in required) / len(required)
        by_source[source] = {"samples": len(required), "p95_ms": source_p95, "coverage": source_coverage}
        assert source_p95 <= 150.0, by_source
        assert source_coverage >= 0.95, by_source
    by_query = {
        query: {
            "direct_matched": sum(sample["query"] == query and sample["matched"] for sample in session_samples),
            "broker_matches": sum(sample["query"] == query and bool(sample["displayed_sources"]) for sample in broker_samples),
            "samples": sum(sample["query"] == query for sample in session_samples),
        }
        for query in _PERFORMANCE_QUERIES
    }
    print("Context Index source coverage " + json.dumps({
        "direct_expected_p95_ms": session_expected_p95, "direct_coverage": session_coverage,
        "broker_expected_p95_ms": expected_p95, "broker_coverage": coverage,
        "by_source": by_source, "by_query": by_query,
    }, ensure_ascii=False, allow_nan=False))
    # Exercise both source openers with a query that actually matches both.
    evidence_pack = asyncio.run(broker.build(
        "perfquery001", "evidence-check", "current-session", _WORKSPACE, _NOW, frozenset()
    ))
    for source in ("session", "activity"):
        handle = next(row.handle for row in evidence_pack.rows if row.source == source)
        opened = broker.open([handle], 1)
        expected_trust = {
            "session": "historical_context",
            "activity": "untrusted_observation",
        }[source]
        assert opened.startswith("<context-evidence>")
        assert f'trust="{expected_trust}"' in opened
        assert "<title>" in opened and "<item>" in opened
    assert {path: _metadata(path) for path in before} == before


def _prompt_observation(query: str, prompt: list[dict], trace: dict | None, raw_ms: float, turn: int) -> dict:
    current = next(message for message in reversed(prompt) if message.get("role") == "user")
    expected_sources = _expected_prompt_sources(query)
    deadline_sources = sorted(
        name for name, error in (trace or {}).get("source_errors", {}).items()
        if name in {"session", "activity", "memory"} and error in {"deadline", "source_timeout"}
    )
    return {
        "turn": turn, "query": query, "raw_ms": raw_ms,
        # A fast fail-open is still an over-budget sample. Keep JSON finite.
        "effective_ms": max(raw_ms, 150.001) if deadline_sources else raw_ms,
        "expected_sources": list(expected_sources), "deadline_sources": deadline_sources,
        "has_index": "<context-index" in str(current.get("content", "")), "trace": trace,
    }


def _assert_prompt_observation(sample: dict) -> None:
    detail = json.dumps(sample, ensure_ascii=False, allow_nan=False)
    trace = sample["trace"]
    assert trace is not None, detail
    assert set(trace["source_errors"]) <= {"session", "activity", "memory"}, detail
    assert all(error in {"deadline", "source_timeout"} for error in trace["source_errors"].values()), detail
    if not sample["expected_sources"]:
        assert not sample["has_index"], detail
    elif not sample["has_index"]:
        # Only the sources known to match this fixture can explain a missing
        # block; an unrelated source timing out cannot excuse a lost match.
        assert set(sample["expected_sources"]) <= set(sample["deadline_sources"]), detail


def _prompt_agent(session_db: Path, broker: ContextIndexBroker) -> ReActAgent:
    # Prompt preparation never contacts a provider; this is the compressor dependency.
    agent = ReActAgent(
        "perf", SimpleNamespace(), ToolRegistry(), system_prompt="stable", context_index_broker=broker,
        query_profile_enabled=False,
    )
    agent.context.set_session(str(session_db.parent / "current-session.json"))
    return agent


def test_react_prompt_source_timeout_preserves_fail_open_contract(
    monkeypatch: pytest.MonkeyPatch, production_fixture: tuple[Path, Path]
) -> None:
    session_db, activity_db = production_fixture
    monkeypatch.chdir(session_db.parent)
    workspace_module.clear_workspace_cache()
    for query, held in (("code", False), ("code", True), ("perfquery001", True)):
        source = SessionRecommendationSource(session_db)
        release, ready = threading.Event(), threading.Event()
        original = source.recommend

        def held_recommend(*args, _original=original, _ready=ready, _release=release, **kwargs):
            result = _original(*args, **kwargs)
            _ready.set()
            assert _release.wait(3), "Source worker was not released after the control"
            return result

        if held:
            monkeypatch.setattr(source, "recommend", held_recommend)
        broker = ContextIndexBroker("all", 900, source, ActivityRecommendationReader(activity_db))
        agent = _prompt_agent(session_db, broker)
        agent.context.add_user(query)
        agent._memory_turn_key = f"controlled:{query}:{held}"

        async def prepare(agent=agent, query=query, broker=broker, release=release) -> dict:
            try:
                started = time.perf_counter_ns()
                prompt, _estimate = await agent._prepare_prompt_for_llm(query, None, current_user_index=0)
                raw_ms = (time.perf_counter_ns() - started) / 1_000_000
                assert broker.last_trace is not None
                return _prompt_observation(query, prompt, trace_metadata(broker.last_trace), raw_ms, 0)
            finally:
                release.set()  # Release before asyncio.run waits for its worker pool.

        sample = asyncio.run(prepare())
        print("Context Index controlled prompt " + json.dumps(sample, ensure_ascii=False, allow_nan=False))
        _assert_prompt_observation(sample)
        if held:
            assert ready.is_set()
            assert sample["trace"]["source_errors"]["session"] == "source_timeout"
            assert sample["effective_ms"] > 150.0
        assert sample["has_index"] == (not held or query == "perfquery001")


@pytest.mark.parametrize("errors", ({}, {"activity": "source_timeout"}, {"session": "database_error"}, {"broker": "broker_error"}))
def test_prompt_missing_index_requires_matching_source_deadline(errors: dict) -> None:
    sample = _prompt_observation("code", [{"role": "user", "content": "code"}], {"source_errors": errors}, 1.0, 0)
    with pytest.raises(AssertionError):
        _assert_prompt_observation(sample)


def test_react_prompt_preparation_p95_includes_real_workspace_resolution(
    monkeypatch: pytest.MonkeyPatch, production_fixture: tuple[Path, Path]
) -> None:
    """Acceptance path: source readers, broker, resolver, and prompt copying."""
    session_db, activity_db = production_fixture
    monkeypatch.chdir(session_db.parent)
    workspace_module.clear_workspace_cache()
    broker = ContextIndexBroker(
        "all", 900, SessionRecommendationSource(session_db), ActivityRecommendationReader(activity_db)
    )
    agent = _prompt_agent(session_db, broker)

    async def prepare(turn: int) -> list[dict]:
        text = _PERFORMANCE_QUERIES[turn % len(_PERFORMANCE_QUERIES)]
        agent.context.add_user(text)
        agent._memory_turn_key = f"react-perf:{turn}"
        prompt, _estimate = await agent._prepare_prompt_for_llm(
            text, None, current_user_index=len(agent.context.messages) - 1
        )
        return prompt

    for warmup in range(5):
        asyncio.run(prepare(-warmup - 1))
    samples: list[dict] = []
    for turn in range(200):
        started = time.perf_counter_ns()
        prompt = asyncio.run(prepare(turn))
        raw_ms = (time.perf_counter_ns() - started) / 1_000_000
        trace = trace_metadata(broker.last_trace) if broker.last_trace is not None else None
        samples.append(_prompt_observation(_PERFORMANCE_QUERIES[turn % len(_PERFORMANCE_QUERIES)], prompt, trace, raw_ms, turn))

    for sample in samples:
        print("Context Index ReAct prompt sample " + json.dumps(sample, ensure_ascii=False, allow_nan=False))
    for sample in samples:
        _assert_prompt_observation(sample)
    expected = [sample for sample in samples if sample["expected_sources"]]
    durations_ms = sorted(sample["effective_ms"] for sample in samples)
    expected_ms = sorted(sample["effective_ms"] for sample in expected)
    p95 = durations_ms[math.ceil(len(durations_ms) * 0.95) - 1]
    expected_p95 = expected_ms[math.ceil(len(expected_ms) * 0.95) - 1]
    coverage = sum(sample["has_index"] for sample in expected) / len(expected)
    by_query = {
        query: {
            "total": sum(sample["query"] == query for sample in samples),
            "with_index": sum(sample["query"] == query and sample["has_index"] for sample in samples),
            "deadline": sum(sample["query"] == query and bool(sample["deadline_sources"]) for sample in samples),
        }
        for query in _PERFORMANCE_QUERIES
    }
    print(f"Context Index ReAct prompt-preparation p95={p95:.2f}ms max={max(durations_ms):.2f}ms "
          f"expected-match-p95={expected_p95:.2f}ms coverage={coverage:.4f} "
          f"by-query={json.dumps(by_query, ensure_ascii=False)}")
    assert p95 <= 150.0
    assert expected_p95 <= 150.0
    assert coverage >= 0.95
