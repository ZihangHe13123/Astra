"""Explicit production-sized latency acceptance for Context Index readers."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import gc
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


def test_cold_vector_snapshots_do_not_starve_parallel_archive_readers(tmp_path, monkeypatch):
    """Regression: activity's Python-float expansion exhausted session's SQL deadline."""
    from agent.runtime.context_index import semantic_index, vector_index

    # A ready embedding backend has already imported NumPy. Cold here means
    # empty Astra snapshot caches, not concurrent Python module initialization.
    pytest.importorskip("numpy")
    monkeypatch.setenv("ASTRA_EMBEDDING_BACKEND", "mlx")
    path = tmp_path / "vectors.db"
    archives = {source: tmp_path / f"{source}.db" for source in ("session", "memory")}
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
    print(f"Concurrent cold vector snapshots: rows={counts} dim={dim} p95_ms={p95} missed_deadline={missed}")
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
    session_durations_ms.sort()
    session_p50 = session_durations_ms[len(session_durations_ms) // 2]
    session_p95 = session_durations_ms[math.ceil(len(session_durations_ms) * 0.95) - 1]
    session_available = sum(result.availability == "available" for result in session_results)
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

    durations_ms.sort()
    p50 = durations_ms[len(durations_ms) // 2]
    p95 = durations_ms[math.ceil(len(durations_ms) * 0.95) - 1]
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
    assert session_p95 <= 75.0
    assert session_available == 200
    assert all(not result.error_category for result in session_results), session_errors
    assert all(
        session_results[index].relevance
        for index in range(200)
        if _PERFORMANCE_QUERIES[index % len(_PERFORMANCE_QUERIES)] != _PERFORMANCE_QUERIES[4]
    )
    assert all(
        not session_results[index].relevance
        for index in range(200)
        if _PERFORMANCE_QUERIES[index % len(_PERFORMANCE_QUERIES)] == _PERFORMANCE_QUERIES[4]
    )
    assert p95 <= 150.0
    assert max(len(pack.rendered) for pack in packs) <= 900
    assert session_source.max_rows_observed <= 12
    assert activity_source.max_rows_observed <= 12
    assert broker.last_trace is not None
    assert set(broker.last_trace.source_status) == {"session", "activity", "memory"}
    assert broker.last_trace.source_status["memory"] == "disabled"
    assert statuses == {"session": 200, "activity": 200}
    for turn, pack in enumerate(packs):
        query = _PERFORMANCE_QUERIES[turn % len(_PERFORMANCE_QUERIES)]
        if query in {"这个", _PERFORMANCE_QUERIES[4]}:
            assert not pack.rows  # Empty/stop-word queries do not receive recency filler.
        else:
            assert any(row.source == "session" for row in pack.rows)
        if query == "perfquery001":
            assert any(row.source == "activity" for row in pack.rows)
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
    # _prepare_prompt_for_llm does not contact a provider; this tiny client is
    # only the ContextCompressor dependency required by ReAct construction.
    agent = ReActAgent(
        "perf", SimpleNamespace(), ToolRegistry(), system_prompt="stable", context_index_broker=broker,
        query_profile_enabled=False,
    )
    agent.context.set_session(str(session_db.parent / "current-session.json"))

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
    durations_ms: list[float] = []
    prompts: list[list[dict]] = []
    for turn in range(200):
        started = time.perf_counter_ns()
        prompts.append(asyncio.run(prepare(turn)))
        durations_ms.append((time.perf_counter_ns() - started) / 1_000_000)

    durations_ms.sort()
    p95 = durations_ms[math.ceil(len(durations_ms) * 0.95) - 1]
    print(f"Context Index ReAct prompt-preparation p95={p95:.2f}ms max={max(durations_ms):.2f}ms")
    assert p95 <= 150.0
    for turn, prompt in enumerate(prompts):
        query = _PERFORMANCE_QUERIES[turn % len(_PERFORMANCE_QUERIES)]
        current = next(message for message in reversed(prompt) if message.get("role") == "user")
        has_index = "<context-index" in str(current.get("content", ""))
        assert has_index == (query not in {"这个", _PERFORMANCE_QUERIES[4]})
