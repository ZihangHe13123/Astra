"""Semantic retrieval contracts; fixed vectors test wiring, not model accuracy."""
import asyncio
from dataclasses import replace
from datetime import UTC, datetime
import math
from pathlib import Path
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

from agent.runtime.context_index import embedder
from agent.runtime.context_index.factory import create_context_index_broker
from agent.runtime.context_index.query import plan_query
from agent.runtime.context_index.query_embedding import QueryEmbedding, current_embedding
from agent.runtime.context_index.semantic_index import rebuild_source, read_snapshot
from agent.runtime.context_index.semantic_reader import SemanticReader
from agent.runtime.context_index.session_source import SessionRecommendationSource
from agent.runtime.context_index.record_source import RecordSource
from agent.runtime.context_index.workspace import WorkspaceIdentity
from agent.runtime.memory import MemoryStore
from tests.test_context_index_session_source import _create_recall_database, _add_session

NOW = datetime(2026, 9, 9, 10, tzinfo=UTC)
WORKSPACE = WorkspaceIdentity("repo", "", "repo")
TEXT = "下班后发邮件即可，尽量别打电话。"
QUERY = "我的联络习惯如何"


class Encoder:
    def __init__(self):
        self.calls = []

    def encode(self, texts):
        self.calls.append(tuple(texts))
        return [(0.0, 1.0) if "咖啡" in text else (1.0, 0.0) for text in texts]


@pytest.fixture
def archive(tmp_path, monkeypatch):
    session = tmp_path / "研究 repo" / "session # %.db"
    session.parent.mkdir()
    db = _create_recall_database(session)
    _add_session(db, "old", "旧会话", WORKSPACE.key, [("user", TEXT, NOW.timestamp() - 60)])
    db.commit()
    db.close()
    memory = MemoryStore(tmp_path / "memory.db", core_dir=tmp_path / "core")
    record = memory.add_record(kind="preference", content=TEXT)
    with sqlite3.connect(memory.path) as db:
        for column in ("created_at", "last_confirmed_at", "valid_from"):
            db.execute(f"UPDATE memory_records SET {column}=?", ("2026-09-09T09:00:00+00:00",))
    vectors = tmp_path / "vectors.db"
    encoder = Encoder()
    monkeypatch.setenv("ASTRA_EMBEDDING_BACKEND", "llamacpp")
    monkeypatch.setenv("ASTRA_CONTEXT_INDEX_EMBEDDING", "on")
    monkeypatch.setenv("ASTRA_CONTEXT_INDEX_VECTORS_DB", str(vectors))
    monkeypatch.setenv("ASTRA_CONTEXT_INDEX_SESSIONS_DB", str(session))
    monkeypatch.setenv("ASTRA_CONTEXT_INDEX_ACTIVITY_DB", str(tmp_path / "absent.db"))
    monkeypatch.setattr(embedder, "get_embedder", lambda: encoder)
    monkeypatch.setattr(embedder, "ready_embedder", lambda: encoder)
    for source, path in (("session", session), ("memory", memory.path)):
        rebuild_source(path, vectors, source, backend=encoder)
    encoder.calls.clear()
    return SimpleNamespace(session=session, memory=memory, record=record, vectors=vectors, encoder=encoder)


def recommend(archive, source, query=QUERY, **kwargs):
    plan = kwargs.pop("plan", plan_query(query, NOW))
    token = current_embedding.set(QueryEmbedding(plan.query))
    try:
        reader = SemanticReader(archive.session, archive.vectors)
        path = archive.session if source == "session" else archive.memory.path
        return reader.recommend(source, path, plan, WORKSPACE, kwargs.pop("session_id", "current"),
                                kwargs.pop("active", frozenset()))
    finally:
        current_embedding.reset(token)


def test_production_broker_finds_paraphrases_and_shares_one_query_encoding(archive, monkeypatch):
    # Verify production wiring with fixed vectors, not cold-host latency.
    # Dedicated timeout/performance tests retain their explicit short budgets.
    monkeypatch.setenv("ASTRA_CONTEXT_INDEX_SOURCE_MS", "2000")
    assert not SessionRecommendationSource(archive.session).recommend(QUERY, WORKSPACE, "current",
        frozenset(), NOW.timestamp(), plan_query(QUERY, NOW)).relevance
    assert not RecordSource(archive.memory.path).recommend(plan_query(QUERY, NOW), WORKSPACE, frozenset()).relevance
    broker = create_context_index_broker(SimpleNamespace(mode="session", char_budget=900), Path.cwd())
    pack = asyncio.run(broker.build(QUERY, "one", "current", WORKSPACE, NOW, frozenset(),
                                   memory_path=archive.memory.path))
    assert pack.rows and TEXT in pack.rendered, repr(broker.last_trace)
    assert len(archive.encoder.calls) == 1
    assert broker.last_trace.semantic_status == {"session": "available", "memory": "available"}
    assert broker.last_trace.source_status["activity"] == "disabled"
    assert "Semantic session: available" in broker.format_last_trace()


@pytest.mark.parametrize("source", ["session", "memory"])
def test_semantic_handles_reject_changed_canonical_versions(archive, source):
    result = recommend(archive, source)
    assert len(result.relevance) == 1
    item = result.relevance[0]
    reader = SessionRecommendationSource(archive.session) if source == "session" else RecordSource(archive.memory.path)
    assert reader.open(item.locator, 0).items
    with sqlite3.connect(archive.session if source == "session" else archive.memory.path) as db:
        db.execute("UPDATE messages SET content='已经改为白天打电话'" if source == "session"
                   else "UPDATE memory_records SET content='已经改为白天打电话'")
    assert reader.open(item.locator, 0).items == ()
    assert recommend(archive, source).relevance == ()


@pytest.mark.parametrize("source", ["session", "memory"])
def test_index_updates_changed_rows_without_reencoding_unchanged_rows(archive, source):
    path = archive.session if source == "session" else archive.memory.path
    assert rebuild_source(path, archive.vectors, source, backend=archive.encoder)["encoded"] == 0
    assert not archive.encoder.calls
    with sqlite3.connect(path) as db:
        db.execute("UPDATE messages SET content='午后喜欢喝咖啡'" if source == "session"
                   else "UPDATE memory_records SET content='午后喜欢喝咖啡'")
    assert rebuild_source(path, archive.vectors, source, backend=archive.encoder)["encoded"] == 1
    assert recommend(archive, source).relevance == ()
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM messages" if source == "session" else "UPDATE memory_records SET status='forgotten'")
    assert rebuild_source(path, archive.vectors, source, backend=archive.encoder)["removed"] == 1


@pytest.mark.parametrize("mutation", [
    "status='superseded'", "status='expired'", "status='forgotten'",
    """metadata_json='{"pinned":true}'""",
    """metadata_json='{"conflict_state":"pending"}'""",
    "valid_until='2026-09-09T08:00:00+00:00'",
    "valid_from='2026-09-10T00:00:00+00:00'",
])
def test_semantic_record_lifecycle_is_authoritative_even_after_reindex(archive, mutation):
    with sqlite3.connect(archive.memory.path) as db:
        db.execute("UPDATE memory_records SET " + mutation)
    rebuild_source(archive.memory.path, archive.vectors, "memory", backend=archive.encoder)
    assert not recommend(archive, "memory").relevance


def test_window_active_context_and_named_target_filters_apply_to_vectors(archive):
    assert not recommend(archive, "session", "NUS 课程讲师是谁").relevance
    assert not recommend(archive, "memory", "昨天我的联络习惯如何").relevance
    from agent.runtime.context_index.session_source import content_fingerprint
    assert not recommend(archive, "session", active=frozenset({content_fingerprint(TEXT)})).relevance
    assert not recommend(archive, "memory", "谢谢").relevance
    assert not recommend(archive, "session", "2+2").relevance


@pytest.mark.parametrize("source", ["session", "memory"])
def test_full_semantic_selection_reuses_immutable_snapshot(archive, monkeypatch, source):
    import numpy as np
    from agent.runtime.context_index import semantic_reader

    path = archive.session if source == "session" else archive.memory.path
    rows, matrix = read_snapshot(archive.vectors, source, path)
    original = semantic_reader.vector_search
    seen = []

    def search(query, selected, ids, *, k):
        seen.append(True)
        assert ids == [row[0] for row in rows]
        assert np.shares_memory(selected, matrix)
        assert not selected.flags.writeable
        return original(query, selected, ids, k=k)

    monkeypatch.setattr(semantic_reader, "vector_search", search)
    assert [item.private_text for item in recommend(archive, source).relevance] == [TEXT]
    assert seen == [True]


@pytest.mark.parametrize("target", [0.749995, 0.750005])
def test_semantic_threshold_uses_accurate_wide_vector_cosines(archive, monkeypatch, tmp_path, target):
    import numpy as np

    vector = np.full(4096, .001, dtype=np.float32)
    vector[0] = 1
    first = target * math.hypot(*map(float, vector))
    other = math.sqrt((1 - first * first) / 2)
    query = np.zeros(4096, dtype=np.float32)
    query[:3] = [first, other, -other]
    reference = math.fsum(float(a) * float(b) for a, b in zip(vector, query)) / (
        math.hypot(*map(float, vector)) * math.hypot(*map(float, query)))

    class WideEncoder:
        def encode(self, texts):
            return [query.tolist() if text == QUERY else vector.tolist() for text in texts]

    encoder = WideEncoder()
    archive.vectors = tmp_path / "wide-vectors.db"
    monkeypatch.setattr(embedder, "ready_embedder", lambda: encoder)
    monkeypatch.setenv("ASTRA_CONTEXT_INDEX_MEMORY_MIN_SIMILARITY", "0.75")
    rebuild_source(archive.session, archive.vectors, "session", backend=encoder)
    result = recommend(archive, "session")
    assert not result.error_category
    assert [item.private_text for item in result.relevance] == ([TEXT] if reference >= .75 else [])


@pytest.mark.parametrize("filter_kind", ["window", "available_at"])
def test_partial_semantic_selection_keeps_vector_ids_and_time_scope(archive, monkeypatch, filter_kind):
    from agent.runtime.context_index import semantic_reader

    recent = "新近留下的联络偏好"
    older = "很久以前的联络偏好"
    future = "尚未发生的联络偏好"
    with sqlite3.connect(archive.session) as db:
        db.executemany(
            "INSERT INTO messages(session_id, role, content, timestamp, msg_index) VALUES ('old','user',?,?,?)",
            [(recent, NOW.timestamp() - 30, 2), (older, NOW.timestamp() - 3600, 3),
             (future, NOW.timestamp() + 30, 4)],
        )
        ids_by_text = dict(db.execute("SELECT content, CAST(id AS TEXT) FROM messages"))
    rebuild_source(archive.session, archive.vectors, "session", backend=archive.encoder)
    plan = plan_query(QUERY, NOW)
    if filter_kind == "window":
        plan = replace(plan, time_start=NOW.timestamp() - 90, time_end=NOW.timestamp() - 45)
        expected_texts = {TEXT}
    else:
        with sqlite3.connect(archive.vectors) as db:
            db.execute("UPDATE context_memory_vectors SET available_at=? WHERE source='session' AND item_id=?",
                       (NOW.timestamp() + 1, ids_by_text[recent]))
        expected_texts = {TEXT, older}
    expected_ids = {ids_by_text[text] for text in expected_texts}
    original = semantic_reader.vector_search
    seen = []

    def search(query, matrix, ids, *, k):
        seen.append(True)
        assert set(ids) == expected_ids and matrix.shape[0] == len(expected_ids)
        return original(query, matrix, ids, k=k)

    monkeypatch.setattr(semantic_reader, "vector_search", search)
    result = recommend(archive, "session", plan=plan)
    assert not result.error_category
    assert {item.private_text for item in result.relevance} == expected_texts
    assert seen == [True]


def test_session_self_and_same_timestamp_future_messages_are_excluded(archive):
    with sqlite3.connect(archive.session) as db:
        db.execute("INSERT INTO messages(session_id, role, content, timestamp, msg_index) VALUES ('old','user',?,?,2)",
                   (QUERY, NOW.timestamp() - 30))
        query_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.execute("INSERT INTO messages(session_id, role, content, timestamp, msg_index) VALUES ('old','assistant',?,?,3)",
                   ("未来才出现的答案", NOW.timestamp() - 30))
    rebuild_source(archive.session, archive.vectors, "session", backend=archive.encoder)
    plan = replace(plan_query(QUERY, NOW), message_id=query_id)
    result = recommend(archive, "session", plan=plan, session_id="old")
    assert [item.private_text for item in result.relevance] == [TEXT]


def test_archives_and_models_cannot_share_vectors_accidentally(archive, tmp_path, monkeypatch):
    copied = tmp_path / "another.db"
    with sqlite3.connect(archive.session) as original, sqlite3.connect(copied) as target:
        original.backup(target)
    reader = SemanticReader(copied, archive.vectors)
    token = current_embedding.set(QueryEmbedding(QUERY))
    try:
        result = reader.recommend("session", copied, plan_query(QUERY, NOW), WORKSPACE, "current", frozenset())
        assert result.availability == "absent"
    finally:
        current_embedding.reset(token)
    monkeypatch.setenv("ASTRA_EMBEDDING_MODEL", "changed-model")
    assert recommend(archive, "session").availability == "error"


def test_query_reads_do_not_migrate_or_write_vector_database(archive, monkeypatch):
    before = archive.vectors.read_bytes()
    monkeypatch.setattr(embedder, "get_embedder", lambda: pytest.fail("query must not load a model"))
    assert recommend(archive, "session").relevance
    assert archive.vectors.read_bytes() == before


def test_invalid_batch_is_atomic_and_corrupt_snapshot_falls_back(archive):
    class BadEncoder:
        def encode(self, texts):
            return [[float("nan")]] * len(texts)
    before = archive.vectors.read_bytes()
    with pytest.raises(ValueError, match="Invalid semantic vector"):
        rebuild_source(archive.session, archive.vectors, "session", backend=BadEncoder(), force=True)
    assert archive.vectors.read_bytes() == before
    with sqlite3.connect(archive.vectors) as db:
        db.execute("UPDATE context_memory_vectors SET vec=x'01'")
    assert recommend(archive, "session").availability == "error"


def test_cold_backend_preserves_lexical_results_and_never_loads(archive, monkeypatch):
    monkeypatch.setattr(embedder, "ready_embedder", lambda: None)
    monkeypatch.setattr(embedder, "get_embedder", lambda: pytest.fail("no query-time model load"))
    broker = create_context_index_broker(SimpleNamespace(mode="session", char_budget=900), Path.cwd())
    pack = asyncio.run(broker.build("下班后发邮件", "cold", "current", WORKSPACE, NOW, frozenset()))
    assert pack.rows and TEXT in pack.rendered


def test_slow_semantics_cannot_discard_completed_lexical_results(archive, monkeypatch):
    release = threading.Event()
    class SlowEncoder:
        def encode(self, texts):
            release.wait(5)
            return [[1.0, 0.0]]
    monkeypatch.setattr(embedder, "ready_embedder", lambda: SlowEncoder())
    broker = create_context_index_broker(SimpleNamespace(mode="session", char_budget=900), Path.cwd())
    broker.source_deadline_seconds = 0.08
    query = "下班后发邮件"
    # This contract starts with completed lexical work. Keep the real archive
    # lookup outside the semantic timeout race, including cold Windows disk I/O.
    lexical = broker.session_source.recommend(
        query, WORKSPACE, "current", frozenset(), NOW.timestamp(), plan_query(query, NOW),
    )
    assert lexical.availability == "available" and lexical.relevance
    monkeypatch.setattr(broker.session_source, "recommend", lambda *_args: lexical)

    async def run():
        start = time.monotonic()
        try:
            pack = await broker.build(query, "slow", "current", WORKSPACE, NOW, frozenset())
            assert time.monotonic() - start < 0.3
            assert pack.rows and TEXT in pack.rendered
            assert broker.last_trace.source_status["session"] == "available"
            assert broker.last_trace.semantic_status["session"] == "timeout"
        finally:
            release.set()
    asyncio.run(run())


def test_embedding_count_mismatch_does_not_mark_pending_records_indexed(archive):
    class EmptyEncoder:
        def encode(self, texts):
            return []
    with pytest.raises(ValueError, match="count mismatch"):
        rebuild_source(archive.session, archive.vectors, "session", backend=EmptyEncoder(), force=True)
    metadata, _matrix = read_snapshot(archive.vectors, "session", archive.session)
    assert len(metadata) == 1


def test_dimension_change_does_not_mix_incompatible_vectors(archive):
    class ChangedEncoder:
        def encode(self, texts):
            return [[1.0, 0.0, 0.0]] * len(texts)
    with pytest.raises(ValueError, match="dimension changed"):
        rebuild_source(archive.session, archive.vectors, "session", backend=ChangedEncoder(), force=True)
    assert recommend(archive, "session").relevance


def test_lexical_and_vector_votes_share_only_the_same_canonical_revision(archive):
    from agent.runtime.context_index.selection import fuse_candidates
    query = "下班后发邮件"
    lexical = SessionRecommendationSource(archive.session).recommend(
        query, WORKSPACE, "current", frozenset(), NOW.timestamp(), plan_query(query, NOW),
    ).relevance[0]
    lexical = replace(lexical, channel_ranks=(("session-lexical", 1),))
    vector = recommend(archive, "session", query).relevance[0]
    merged = fuse_candidates((lexical, vector))[0]
    assert merged.locator.revision == vector.locator.revision
    assert len(merged.channel_ranks) == 2
    changed = replace(vector, revision="new-version")
    assert len(fuse_candidates((lexical, changed))[0].channel_ranks) == 1


def test_shared_encoding_waits_for_both_sources_within_broker_budget(archive, monkeypatch):
    # This checks shared waiting/encoding, including SQL readback on slow hosts.
    # Production latency remains enforced by the separate performance gate.
    monkeypatch.setenv("ASTRA_CONTEXT_INDEX_SOURCE_MS", "2000")
    class SlowEncoder(Encoder):
        def encode(self, texts):
            time.sleep(0.09)
            return super().encode(texts)
    encoder = SlowEncoder()
    monkeypatch.setattr(embedder, "ready_embedder", lambda: encoder)
    broker = create_context_index_broker(SimpleNamespace(mode="session", char_budget=900), Path.cwd())
    results = {}
    original = broker.semantic_reader.recommend

    def capture(source, *args):
        result = original(source, *args)
        results[source] = result
        return result

    monkeypatch.setattr(broker.semantic_reader, "recommend", capture)
    pack = asyncio.run(broker.build(QUERY, "shared-slow", "current", WORKSPACE, NOW, frozenset(),
                                   memory_path=archive.memory.path))
    assert pack.rows
    assert all(results[source].relevance for source in ("session", "memory"))
    assert len(encoder.calls) == 1


def test_activity_and_other_sources_share_query_encoding(archive):
    from agent.runtime.context_index.activity_source import ActivityRecommendationReader
    from agent.runtime.context_index.vector_index import VectorStore
    store = VectorStore(archive.vectors)
    store.upsert("activity-one", "version", [1.0, 0.0])
    store.close()
    activity = ActivityRecommendationReader(archive.session)
    token = current_embedding.set(QueryEmbedding(QUERY))
    try:
        assert current_embedding.get().get() == (1.0, 0.0)
        assert activity._embed_hits(QUERY)
        assert len(archive.encoder.calls) == 1
    finally:
        current_embedding.reset(token)


def test_legacy_activity_index_is_not_migrated_by_semantic_reads(archive):
    with sqlite3.connect(archive.vectors) as db:
        db.execute("DROP TABLE context_memory_vectors")
    before = archive.vectors.read_bytes()
    assert recommend(archive, "session").availability == "absent"
    assert not archive.encoder.calls
    assert archive.vectors.read_bytes() == before


def test_deleted_top_vectors_do_not_consume_semantic_result_quota(archive):
    with sqlite3.connect(archive.session) as db:
        for index in range(80):
            db.execute("INSERT INTO messages(session_id,role,content,timestamp,msg_index) VALUES('old','assistant',?,?,?)",
                       ("后来删除的无效线索" + str(index), NOW.timestamp() - 30, index + 2))
    rebuild_source(archive.session, archive.vectors, "session", backend=archive.encoder)
    with sqlite3.connect(archive.session) as db:
        db.execute("DELETE FROM messages WHERE msg_index >= 2")
    assert [item.private_text for item in recommend(archive, "session").relevance] == [TEXT]


def test_background_worker_is_bounded_and_does_not_bootstrap_models(archive, monkeypatch):
    from agent.runtime.context_index import semantic_indexer
    stopped = threading.Event()
    completed = threading.Event()
    calls = []
    monkeypatch.setattr(semantic_indexer, "_stop", stopped)
    monkeypatch.setattr(semantic_indexer, "_threads", {})
    monkeypatch.setattr(embedder, "get_embedder", lambda: pytest.fail("worker must use ready backend"))

    def rebuild(path, vectors, source, **kwargs):
        calls.append((source, kwargs["max_encode"]))
        if source == "session":
            completed.set()
        return {"pending": 0}

    monkeypatch.setattr(semantic_indexer, "rebuild_source", rebuild)
    assert semantic_indexer.start_background(archive.session, archive.memory.path, archive.vectors.with_name("missing.db")) is None
    thread = semantic_indexer.start_background(archive.session, archive.memory.path, archive.vectors)
    try:
        assert thread is not None and completed.wait(2)
        assert semantic_indexer.start_background(archive.session, archive.memory.path, archive.vectors) is thread
        assert calls == [("memory", 16), ("session", 16)]
    finally:
        stopped.set()
        if thread is not None:
            thread.join(2)


def test_explicit_enable_starts_maintenance_but_off_boot_does_not(archive, monkeypatch):
    from agent.runtime.context_index import semantic_indexer
    calls = []
    monkeypatch.setattr(embedder, "start_warm", lambda: calls.append("warm"))
    monkeypatch.setattr(embedder, "pause_shared_runtime", lambda: calls.append("pause"))
    monkeypatch.setattr(semantic_indexer, "start_background", lambda *args, **kwargs: calls.append(kwargs["enabled"]))
    broker = create_context_index_broker(SimpleNamespace(mode="off", char_budget=900), Path.cwd())
    broker.start_background(archive.memory.path)
    assert calls == []
    broker.set_mode("session")
    assert calls[0] == "warm" and calls[1]() is True
    broker.set_mode("all")
    assert len(calls) == 2
    broker.set_mode("off")
    assert calls[1]() is False
    assert calls[-1] == "pause"


def test_background_maintenance_defers_to_an_existing_archive_owner(archive, monkeypatch):
    from agent.runtime.context_index import semantic_indexer
    from agent.runtime.instance_lock import InstanceLock

    class StopAfterCycle:
        def is_set(self):
            return False

        def wait(self, _):
            return True

    monkeypatch.setattr(semantic_indexer, "_stop", StopAfterCycle())
    monkeypatch.setattr(semantic_indexer, "_threads", {})
    monkeypatch.setattr(semantic_indexer, "rebuild_source", lambda *args, **kwargs: pytest.fail("archive is already owned"))
    owner = InstanceLock(archive.vectors.with_suffix(archive.vectors.suffix + ".memory-index.lock"))
    with owner:
        thread = semantic_indexer.start_background(archive.session, archive.memory.path, archive.vectors)
        assert thread is not None
        thread.join(2)
        assert not thread.is_alive()


def test_bounded_rebuild_reports_remaining_work_and_continues(archive):
    with sqlite3.connect(archive.session) as db:
        for index in range(3):
            db.execute("INSERT INTO messages(session_id,role,content,timestamp,msg_index) VALUES('old','assistant',?,?,?)",
                       ("待编码的旧记录" + str(index), NOW.timestamp() - 20, index + 2))
    first = rebuild_source(archive.session, archive.vectors, "session", backend=archive.encoder, max_encode=1)
    assert first["encoded"] == 1 and first["pending"] == 2
    second = rebuild_source(archive.session, archive.vectors, "session", backend=archive.encoder, max_encode=2)
    assert second["encoded"] == 2 and second["pending"] == 0


def test_model_encoding_is_nonblocking_while_background_batch_is_busy():
    started, release = threading.Event(), threading.Event()
    class Backend:
        def encode(self, texts):
            started.set()
            release.wait(2)
            return [[1.0, 0.0]]
    model = embedder.Embedder(Backend())
    thread = threading.Thread(target=lambda: model.encode(["background"]))
    thread.start()
    try:
        assert started.wait(1)
        start = time.monotonic()
        assert model.encode(["query"]) is None
        assert time.monotonic() - start < 0.05
    finally:
        release.set()
        thread.join(2)


@pytest.mark.parametrize("stage,target", [("snapshot", "read_snapshot"), ("search", "vector_search"), ("validate", "_sessions")])
def test_semantic_failure_reports_stage_and_category_without_exception_text(archive, monkeypatch, stage, target):
    from agent.runtime.context_index import semantic_reader

    # This verifies failure classification, not the production 200 ms SLA.
    # Leave time for the injected fault to run on a loaded Windows CI worker.
    monkeypatch.setenv("ASTRA_CONTEXT_INDEX_SOURCE_MS", "2000")

    def interrupted(*_args, **_kwargs):
        raise sqlite3.OperationalError("interrupted private SQL /archive/path")

    owner = SemanticReader if target == "_sessions" else semantic_reader
    monkeypatch.setattr(owner, target, staticmethod(interrupted) if target == "_sessions" else interrupted)
    result = recommend(archive, "session")
    assert result.error_category == "deadline"
    assert result.stages[-1].name == stage
    assert result.stages[-1].error_category == "deadline"
    assert "private" not in repr(result.stages)

    broker = create_context_index_broker(SimpleNamespace(mode="session", char_budget=900), Path.cwd())
    asyncio.run(broker.build(QUERY, "diagnostic", "current", WORKSPACE, NOW, frozenset()))
    assert broker.last_trace.semantic_errors["session"] == "deadline"
    assert broker.last_trace.semantic_stages["session"][-1].name == stage


@pytest.mark.parametrize("source", ["session", "memory"])
def test_mixed_language_soft_terms_can_retrieve_chinese_evidence(archive, source):
    # Fixed vectors isolate filtering; they do not measure semantic accuracy.
    assert recommend(archive, source, "查一下之前的 email 联络习惯").relevance
    assert not recommend(archive, source, "查一下之前的 `email` 联络习惯").relevance
    assert not recommend(archive, source, "查一下之前的 mail_config 配置").relevance
    assert not recommend(archive, source, "查一下 NUS 的联络习惯").relevance


def test_only_redacted_text_reaches_the_encoder(tmp_path):
    # Items are redacted when they are encoded, not when the archive is listed.
    session = tmp_path / "sessions.db"
    db = _create_recall_database(session)
    _add_session(db, "secret", "部署", WORKSPACE.key,
                 [("user", '部署时用 {"api_key": "sk-live-0123456789abcdef"} 这个配置', NOW.timestamp())])
    db.commit()
    db.close()
    encoder = Encoder()
    assert rebuild_source(session, tmp_path / "vectors.db", "session", backend=encoder)["encoded"] == 1
    sent = "\n".join(text for call in encoder.calls for text in call)
    assert "sk-live-0123456789abcdef" not in sent
    assert "<redacted>" in sent


def test_semantic_memory_keeps_a_project_memory_to_its_project(archive):
    def scope_to(workspace_key):
        with sqlite3.connect(archive.memory.path) as db:
            db.execute("UPDATE memory_records SET metadata_json = json_set(metadata_json, '$.scope', 'project', "
                       "'$.workspace_key', ?)", (workspace_key,))
        rebuild_source(archive.memory.path, archive.vectors, "memory", backend=archive.encoder)

    scope_to("another-repo")
    assert recommend(archive, "memory").relevance == ()
    scope_to(WORKSPACE.key)
    assert [item.identity for item in recommend(archive, "memory").relevance] == [archive.record.record_id]
