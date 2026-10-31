"""Background cache filling keeps interactive snapshot deadlines unchanged."""
import asyncio
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
import hashlib
from pathlib import Path
import sqlite3
import struct
import threading
import time
from types import SimpleNamespace

import pytest

from agent.cli.context_index_preferences import ContextIndexPreferences
from agent.runtime.context_index import embedder, semantic_index, semantic_indexer, sqlite_reader
from agent.runtime.context_index.factory import create_context_index_broker
from agent.runtime.instance_lock import InstanceLock
from tests.test_context_index_semantic import NOW, QUERY, TEXT, WORKSPACE, archive as archive
from tests.test_context_index_session_source import _add_session


class OneCycle:
    def is_set(self):
        return False

    def wait(self, _timeout):
        return True


@pytest.fixture(autouse=True)
def isolated_cache(monkeypatch):
    monkeypatch.setattr(semantic_index, "_cache", OrderedDict())
    monkeypatch.setattr(semantic_indexer, "_threads", {})
    monkeypatch.setattr(semantic_indexer, "_stop", OneCycle())


def run_worker(archive, **kwargs):
    thread = semantic_indexer.start_background(archive.session, archive.memory.path, archive.vectors, **kwargs)
    if thread is not None:
        thread.join(10)
        assert not thread.is_alive()
    return thread


@pytest.mark.parametrize("other_writer", [False, True])
def test_background_fills_own_cache_after_interactive_deadline(archive, monkeypatch, other_writer):
    # Enough real rows to invoke SQLite's progress handler after the injected
    # delay. Extra locators have no canonical evidence and must never surface.
    with closing(sqlite3.connect(archive.vectors)) as db, db:
        db.executemany(
            "INSERT INTO context_memory_vectors VALUES (?,?,?,?,?,?,?,?)",
            (("session", semantic_index.archive_key(archive.session), f"extra-{index}", "obsolete",
              semantic_index._LAYOUT, 0, 0, struct.pack("<2f", 1, 0)) for index in range(200)),
        )
    before = hashlib.sha256(archive.vectors.read_bytes()).hexdigest()
    original = semantic_index.open_readonly
    budgets = []
    read_clock = threading.local()

    def now():
        # Only the snapshot worker gets a controlled SQL clock. Lexical reads,
        # asyncio's source deadline and background maintenance keep real time.
        return getattr(read_clock, "now", time.perf_counter())

    monkeypatch.setattr(sqlite_reader, "time", SimpleNamespace(perf_counter=now))

    @contextmanager
    def expired_interactive(path, deadline_ms=75, **kwargs):
        budgets.append(deadline_ms)
        if deadline_ms == 2000 and not other_writer:
            with InstanceLock(archive.vectors.with_suffix(archive.vectors.suffix + ".memory-index.lock")):
                pass
        if deadline_ms == 75:
            read_clock.now = 0.0
        try:
            with original(path, deadline_ms=deadline_ms, **kwargs) as db:
                if deadline_ms == 75:
                    read_clock.now = .09
                yield db
        finally:
            if deadline_ms == 75:
                del read_clock.now

    monkeypatch.setattr(semantic_index, "open_readonly", expired_interactive)
    # This tests recovery from SQLite's 75 ms deadline, not the separate source
    # budget. Leave room for thread scheduling and cold imports on CI hosts.
    monkeypatch.setenv("ASTRA_CONTEXT_INDEX_SOURCE_MS", "2000")
    monkeypatch.delenv("ASTRA_CONTEXT_INDEX_SESSION_MS", raising=False)
    broker = create_context_index_broker(ContextIndexPreferences(mode="session", char_budget=900), Path.cwd())
    assert broker.source_deadline_seconds == 2
    cold = asyncio.run(broker.build(QUERY, "cold", "current", WORKSPACE, NOW, frozenset()))
    assert cold.request_id == "cold"
    assert broker.last_trace is not None
    assert broker.last_trace.source_status["session"] == "available"
    assert broker.last_trace.semantic_errors == {"session": "deadline"}
    assert broker.last_trace.semantic_status == {"session": "error"}
    assert [(stage.name, stage.error_category) for stage in broker.last_trace.semantic_stages["session"]] == [
        ("snapshot", "deadline"),
    ]
    assert not semantic_index._cache
    archive.encoder.calls.clear()
    monkeypatch.setattr(embedder, "get_embedder", lambda: pytest.fail("must not load a model"))
    owner = InstanceLock(archive.vectors.with_suffix(archive.vectors.suffix + ".memory-index.lock"))
    if other_writer:
        owner.acquire()
        monkeypatch.setattr(semantic_indexer, "rebuild_source", lambda *_args, **_kwargs: pytest.fail("writer is owned"))
    else:
        def unchanged(_path, _vectors, _source, **_kwargs):
            # The loader must run after releasing the writer lease.
            return {"pending": 0}
        monkeypatch.setattr(semantic_indexer, "rebuild_source", unchanged)
    try:
        assert run_worker(archive) is not None
    finally:
        owner.release()
    assert sorted(budgets) == [75, 2000, 2000]
    assert not archive.encoder.calls
    assert hashlib.sha256(archive.vectors.read_bytes()).hexdigest() == before
    rows, matrix = semantic_index.read_snapshot(archive.vectors, "session", archive.session)
    assert len(rows) == 201 and not matrix.flags.writeable
    assert budgets == [75, 2000, 2000]  # Real default 75 ms entry is now a cache hit.

    monkeypatch.setattr(semantic_index, "open_readonly", lambda *_args, **_kwargs: pytest.fail("warm reader opened SQL"))
    pack = asyncio.run(broker.build(QUERY, "warm", "current", WORKSPACE, NOW, frozenset()))
    assert pack.rows and TEXT in pack.rendered
    assert len(pack.rows) == 1 and pack.rows[0].source == "session"
    opened = broker.open([pack.rows[0].handle], 0)
    assert TEXT in opened and "extra-" not in opened
    assert broker.last_trace is not None
    assert broker.last_trace.semantic_status == {"session": "available"}
    assert not broker.last_trace.semantic_errors


@pytest.mark.parametrize("guard", ["embedding_off", "mode_off", "stopped", "unready", "missing_vectors", "missing_archives"])
def test_inactive_worker_does_not_fill_or_bootstrap(archive, monkeypatch, guard):
    enabled = lambda: True
    if guard == "embedding_off":
        monkeypatch.setattr(embedder, "_enabled", lambda: False)
    elif guard == "mode_off":
        enabled = lambda: False
    elif guard == "stopped":
        stopped = threading.Event()
        stopped.set()
        monkeypatch.setattr(semantic_indexer, "_stop", stopped)
    elif guard == "unready":
        monkeypatch.setattr(embedder, "ready_embedder", lambda: None)
    elif guard == "missing_archives":
        archive.session = archive.session.with_name("missing-session.db")
        archive.memory = SimpleNamespace(path=archive.memory.path.with_name("missing-memory.db"))
    else:
        archive.vectors.unlink()
    monkeypatch.setattr(embedder, "get_embedder", lambda: pytest.fail("must not load a model"))
    monkeypatch.setattr(semantic_indexer, "rebuild_source", lambda *_args, **_kwargs: pytest.fail("inactive writer"))
    monkeypatch.setattr(semantic_index, "open_readonly", lambda *_args, **_kwargs: pytest.fail("inactive SQL read"))
    run_worker(archive, enabled=enabled)
    assert not semantic_index._cache
    if guard == "missing_vectors":
        assert not archive.vectors.exists()


def test_source_budget_includes_decode_and_does_not_spend_next_source_budget(archive, monkeypatch):
    clock = [0.0]
    decode = semantic_index.matrix_from_blobs
    calls = []
    monkeypatch.setattr(semantic_index, "time", SimpleNamespace(perf_counter=lambda: clock[0]))
    monkeypatch.setattr(semantic_indexer, "rebuild_source", lambda *_args, **_kwargs: {"pending": 0})

    def slow_first_decode(blobs):
        calls.append(True)
        matrix = decode(blobs)
        if len(calls) == 1:
            clock[0] += 2.1  # Memory used its budget; session gets a new one.
        return matrix

    monkeypatch.setattr(semantic_index, "matrix_from_blobs", slow_first_decode)
    run_worker(archive)
    assert len(calls) == 2
    assert {scope[1] for scope in semantic_index._cache} == {"session"}


def test_mode_change_during_fill_prevents_publication_and_next_source(archive, monkeypatch):
    enabled = [True]
    decode = semantic_index.matrix_from_blobs
    calls = []
    monkeypatch.setattr(semantic_indexer, "rebuild_source", lambda *_args, **_kwargs: {"pending": 0})

    def disable_during_decode(blobs):
        calls.append(True)
        matrix = decode(blobs)
        enabled[0] = False
        return matrix

    monkeypatch.setattr(semantic_index, "matrix_from_blobs", disable_during_decode)
    run_worker(archive, enabled=lambda: enabled[0])
    assert len(calls) == 1 and not semantic_index._cache


@pytest.mark.parametrize("phase", ["before", "after_sql", "after_decode"])
def test_cancelled_fill_closes_connection_without_publishing(archive, monkeypatch, phase):
    cancelled = threading.Event()
    connections = []
    original = semantic_index.open_readonly
    decode = semantic_index.matrix_from_blobs

    @contextmanager
    def reader(path, deadline_ms=75, **kwargs):
        assert deadline_ms == 2000 and callable(kwargs["cancelled"])
        with original(path, deadline_ms=deadline_ms, **kwargs) as db:
            connections.append(db)
            yield db
        if phase == "after_sql":
            cancelled.set()

    def decoder(blobs):
        if phase == "after_sql":
            pytest.fail("cancelled fill decoded rows")
        matrix = decode(blobs)
        if phase == "after_decode":
            cancelled.set()
        return matrix

    monkeypatch.setattr(semantic_index, "open_readonly", reader)
    monkeypatch.setattr(semantic_index, "matrix_from_blobs", decoder)
    if phase == "before":
        cancelled.set()
    with pytest.raises(sqlite3.OperationalError, match="interrupted"):
        semantic_index.warm_snapshot(archive.vectors, "session", archive.session, cancelled=cancelled.is_set)
    assert not semantic_index._cache
    assert bool(connections) == (phase != "before")
    for db in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            db.execute("SELECT 1")


@pytest.mark.parametrize("change", ["wal", "replace"])
def test_slow_snapshot_cannot_replace_a_newer_cached_version(archive, monkeypatch, change):
    entered, release = threading.Event(), threading.Event()
    original = semantic_index.matrix_from_blobs
    gate_first = True

    def delayed_decode(blobs):
        nonlocal gate_first
        if gate_first:
            gate_first = False
            entered.set()
            assert release.wait(10)
        return original(blobs)

    writer = sqlite3.connect(archive.vectors)
    if change == "wal":
        writer.execute("PRAGMA journal_mode=WAL")
    monkeypatch.setattr(semantic_index, "matrix_from_blobs", delayed_decode)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(semantic_index.warm_snapshot, archive.vectors, "session", archive.session)
            try:
                assert entered.wait(5)
                if change == "wal":
                    writer.execute("UPDATE context_memory_vectors SET revision='new' WHERE source='session'")
                    writer.commit()
                else:
                    replacement = archive.vectors.with_name("replacement.db")
                    with closing(sqlite3.connect(replacement)) as target, target:
                        writer.backup(target)
                        target.execute("UPDATE context_memory_vectors SET revision='new' WHERE source='session'")
                    writer.close()
                    replacement.replace(archive.vectors)
                new_rows, new_matrix = semantic_index.read_snapshot(archive.vectors, "session", archive.session)
                assert new_rows[0][1] == "new"
            finally:
                release.set()
            assert pending.result(timeout=5) is False
        rows, matrix = semantic_index.read_snapshot(archive.vectors, "session", archive.session)
        assert rows == new_rows and matrix is new_matrix
    finally:
        release.set()
        writer.close()


@pytest.mark.parametrize("change", ["added", "removed"])
def test_snapshot_sees_a_commit_that_leaves_every_file_stat_unchanged(archive, monkeypatch, change):
    def snapshot_ids():
        return {row[0] for row in semantic_index.read_snapshot(archive.vectors, "session", archive.session)[0]}

    def index(statement):
        with closing(sqlite3.connect(archive.session)) as db, db:
            statement(db)
        semantic_index.rebuild_source(archive.session, archive.vectors, "session", backend=archive.encoder)

    index(lambda db: _add_session(db, "first", "第一", WORKSPACE.key, [("user", "第一条记录", NOW.timestamp() - 30)]))
    before = snapshot_ids()
    original, frozen = Path.stat, archive.vectors.stat()
    monkeypatch.setattr(Path, "stat", lambda file, *args, **kwargs:
                        frozen if file == archive.vectors else original(file, *args, **kwargs))
    if change == "added":
        index(lambda db: _add_session(db, "second", "第二", WORKSPACE.key, [("user", "第二条记录", NOW.timestamp() - 20)]))
        assert snapshot_ids() > before
    else:
        index(lambda db: db.execute("DELETE FROM messages WHERE session_id='first'"))
        assert snapshot_ids() < before


def test_legacy_index_without_a_revision_keeps_its_stat_cache(archive, monkeypatch):
    with closing(sqlite3.connect(archive.vectors)) as db, db:
        db.execute("DROP TABLE vector_revision")
    first = semantic_index.read_snapshot(archive.vectors, "session", archive.session)
    monkeypatch.setattr(semantic_index, "open_readonly", lambda *_args, **_kwargs: pytest.fail("reloaded a cached snapshot"))
    assert semantic_index.read_snapshot(archive.vectors, "session", archive.session) is first


def test_rows_survive_a_writer_locking_the_version_recheck(archive, monkeypatch):
    original, reads = semantic_index.read_vector_revision, []

    def locked_on_recheck(path):
        reads.append(path)
        if len(reads) == 2:
            raise sqlite3.OperationalError("database is locked")
        return original(path)

    monkeypatch.setattr(semantic_index, "read_vector_revision", locked_on_recheck)
    rows, _matrix = semantic_index.read_snapshot(archive.vectors, "session", archive.session)
    assert rows and len(reads) == 2
    assert not semantic_index._cache


def test_background_refills_evicted_cache_without_archive_changes(archive, monkeypatch):
    calls = []
    class TwoCycles:
        cycles = 0

        def is_set(self):
            return False

        def wait(self, timeout):
            assert timeout == 30
            self.cycles += 1
            if self.cycles == 1:
                semantic_index._cache.clear()  # Another archive evicted these process-local entries.
                return False
            return True

    monkeypatch.setattr(semantic_indexer, "_stop", TwoCycles())
    def rebuild(_path, _vectors, source, **_kwargs):
        calls.append(source)
        return {"pending": 0}
    monkeypatch.setattr(semantic_indexer, "rebuild_source", rebuild)
    run_worker(archive)
    assert calls == ["memory", "session"]
    assert len(semantic_index._cache) == 2


def test_warm_snapshot_preserves_archive_scoping_and_four_entry_lru(archive, tmp_path):
    store = semantic_index.EvidenceVectorStore(archive.vectors)
    paths = []
    try:
        for index in range(5):
            path = tmp_path / f"archive-{index}.db"
            path.touch()
            paths.append(path)
            store._connection.execute("INSERT INTO context_memory_vectors VALUES (?,?,?,?,?,?,?,?)",
                                      ("session", semantic_index.archive_key(path), f"id-{index}", "revision",
                                       semantic_index._LAYOUT, 0, 0, struct.pack("<2f", 1, 0)))
        store._connection.commit()
    finally:
        store.close()
    for index, path in enumerate(paths):
        assert semantic_index.warm_snapshot(archive.vectors, "session", path)
        rows, matrix = semantic_index.read_snapshot(archive.vectors, "session", path)
        assert rows[0][0] == f"id-{index}" and not matrix.flags.writeable
    assert len(semantic_index._cache) == 4
    assert {scope[2] for scope in semantic_index._cache} == {semantic_index.archive_key(path) for path in paths[1:]}
