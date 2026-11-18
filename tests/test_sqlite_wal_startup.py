"""Two desktop backends may open the same installation's stores at startup."""
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest

from agent.runtime.activity_store import ActivityStore
from agent.runtime.agent_team import AgentTeamStore
from agent.runtime.approval_inbox import ApprovalInbox
from agent.runtime.browser_session import BrowserSessionManager
from agent.runtime.event_stream import RuntimeEventStream
from agent.runtime.jobs.store import JobStore
from agent.runtime.learning import LearningStore
from agent.runtime.memory import MemoryStore
from agent.runtime.memory_records import MemoryRecordRepository
from agent.runtime.peer_link import PeerLink
from agent.runtime.sqlite_wal import enable_wal
from agent.runtime.task_store import TaskStore
from agent.runtime.usage import UsageLedger


@pytest.mark.parametrize("store", [LearningStore, MemoryStore, TaskStore, ApprovalInbox, RuntimeEventStream,
                                  MemoryRecordRepository, AgentTeamStore, PeerLink, ActivityStore,
                                  BrowserSessionManager, JobStore, UsageLedger])
def test_concurrent_backend_can_finish_wal_transition(store, tmp_path, monkeypatch):
    path = tmp_path / "jobs" / "jobs.sqlite3" if store is JobStore else tmp_path / "backend.db"
    path.parent.mkdir(exist_ok=True)
    connect = sqlite3.connect
    contended = threading.Event()

    class Observed(sqlite3.Connection):
        def execute(self, sql, parameters=(), /):
            try:
                return super().execute(sql, parameters)
            except sqlite3.OperationalError as exc:
                if (exc.sqlite_errorcode & 0xff) == sqlite3.SQLITE_BUSY:
                    contended.set()
                raise

    def initialize():
        actual = store(tmp_path if store is JobStore else path)
        if isinstance(actual, ActivityStore):
            actual.close()

    with closing(connect(path)) as owner:
        owner.execute("CREATE TABLE existing (message TEXT)")
        owner.execute("INSERT INTO existing VALUES ('preserve')")
        with ThreadPoolExecutor(max_workers=1) as executor:
            monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: connect(*args, **kwargs, factory=Observed))
            pending = executor.submit(initialize)
            try:
                assert contended.wait(4), "the second backend reached the actual SQLite write lock"
            finally:
                owner.commit()
            pending.result(timeout=10)
    with closing(connect(path)) as reopened:
        assert reopened.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert reopened.execute("SELECT message FROM existing").fetchone()[0] == "preserve"


def test_wal_timeout_does_not_change_the_connections_wait_policy(tmp_path):
    path = tmp_path / "locked.db"
    with closing(sqlite3.connect(path)) as owner, closing(sqlite3.connect(path, timeout=0.08)) as waiter:
        owner.execute("CREATE TABLE existing (id)")
        owner.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        with pytest.raises(sqlite3.OperationalError) as caught:
            enable_wal(waiter)
        assert caught.value.sqlite_errorcode == sqlite3.SQLITE_BUSY
        assert time.monotonic() - started >= 0.06
        assert waiter.execute("PRAGMA busy_timeout").fetchone()[0] == 80
        owner.rollback()
        enable_wal(waiter)
        assert waiter.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_wal_does_not_hide_read_only_errors(tmp_path):
    path = tmp_path / "read-only.db"
    with closing(sqlite3.connect(path)) as writer:
        writer.execute("CREATE TABLE original (id)")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0)) as reader:
        with pytest.raises(sqlite3.OperationalError) as caught:
            enable_wal(reader)
        assert caught.value.sqlite_errorcode == sqlite3.SQLITE_READONLY
