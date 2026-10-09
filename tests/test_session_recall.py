import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from agent.runtime import session_recall as recall_module
from agent.runtime.tools import session_recall as session_recall_tool
from agent.runtime.tools.registry import ToolRegistry
from agent.runtime.session_recall import SessionRecall


def test_default_path_reads_environment_when_instance_is_constructed(
    monkeypatch, tmp_path
):
    configured = tmp_path / "configured" / "sessions.db"
    monkeypatch.setenv("ASTRA_SESSION_RECALL_DB", str(configured))

    recall = SessionRecall()

    assert recall._db_path == configured


def test_explicit_path_wins_over_environment(monkeypatch, tmp_path):
    configured = tmp_path / "configured.db"
    explicit = tmp_path / "explicit.db"
    monkeypatch.setenv("ASTRA_SESSION_RECALL_DB", str(configured))

    recall = SessionRecall(explicit)

    assert recall._db_path == explicit


def test_blank_environment_uses_current_module_default(monkeypatch, tmp_path):
    fallback = tmp_path / "fallback.db"
    monkeypatch.setenv("ASTRA_SESSION_RECALL_DB", "   ")
    monkeypatch.setattr(recall_module, "DB_PATH", fallback)

    recall = SessionRecall()

    assert recall._db_path == fallback


def test_pytest_default_session_recall_path_is_per_test_tmp_path(tmp_path):
    assert SessionRecall()._db_path == tmp_path / "session-recall.db"


def test_pytest_default_context_index_reader_path_is_per_test_tmp_path(tmp_path):
    assert os.environ["ASTRA_CONTEXT_INDEX_SESSIONS_DB"] == str(tmp_path / "session-recall.db")


def test_pytest_subprocess_overrides_ambient_context_reader_path(tmp_path):
    ambient_path = tmp_path / "ambient-reader.db"
    env = os.environ.copy()
    env["ASTRA_CONTEXT_INDEX_SESSIONS_DB"] = str(ambient_path)
    env["ASTRA_SESSION_RECALL_DB"] = str(tmp_path / "ambient-writer.db")

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/test_context_index_runtime.py::test_pytest_isolation_binds_context_index_factory_to_test_archive",
            "-q",
        ],
        cwd=Path(__file__).parents[1],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert not ambient_path.exists()


def _index_columns(connection: sqlite3.Connection, index_name: str) -> tuple[str, ...]:
    return tuple(str(row[2]) for row in connection.execute(f"PRAGMA index_info({index_name})"))


def test_recall_writer_creates_context_index_query_indexes(tmp_path):
    recall = SessionRecall(tmp_path / "indexed.db")
    recall.init_db()
    connection = recall._get_conn()

    assert _index_columns(connection, "idx_messages_timestamp") == ("timestamp", "id")
    assert _index_columns(connection, "idx_messages_session_timestamp") == (
        "session_id", "timestamp", "id",
    )
    assert _index_columns(connection, "idx_sessions_workspace_key") == ("workspace_key",)
    recall.close()


def test_recall_writer_adds_query_indexes_to_legacy_archive_idempotently(tmp_path):
    path = tmp_path / "legacy-indexes.db"
    first = SessionRecall(path)
    first.init_db()
    first.close()

    with sqlite3.connect(path) as connection:
        connection.execute("DROP INDEX idx_messages_timestamp")
        connection.execute("DROP INDEX idx_messages_session_timestamp")
        connection.execute("DROP INDEX idx_sessions_workspace_key")

    for _ in range(2):
        reopened = SessionRecall(path)
        reopened.init_db()
        reopened.close()

    with sqlite3.connect(path) as connection:
        assert _index_columns(connection, "idx_messages_timestamp") == ("timestamp", "id")
        assert _index_columns(connection, "idx_messages_session_timestamp") == (
            "session_id", "timestamp", "id",
        )
        assert _index_columns(connection, "idx_sessions_workspace_key") == ("workspace_key",)


def test_recall_session_mapping_survives_restart_and_reopens(tmp_path):
    db_path = tmp_path / "sessions.db"
    first = SessionRecall(db_path)
    first.init_db()
    sid = first.get_or_create_session(
        "session_alpha",
        title="session_alpha",
        personality="work",
    )
    first.log_message(sid, "user", "first")
    first.close_session(sid)
    first.close()

    second = SessionRecall(db_path)
    second.init_db()
    reopened = second.get_or_create_session(
        "session_alpha",
        title="session_alpha",
        personality="work",
    )
    second.log_message(reopened, "assistant", "second")

    assert reopened == sid
    conn = sqlite3.connect(db_path)
    count, ended_at = conn.execute(
        "SELECT message_count, ended_at FROM sessions WHERE id = ?",
        (sid,),
    ).fetchone()
    assert count == 2
    assert ended_at is None
    assert conn.execute(
        "SELECT COUNT(*) FROM sessions WHERE source_session_key = ?",
        ("session_alpha",),
    ).fetchone()[0] == 1
    conn.close()
    second.close()


def test_recall_schema_migrates_existing_database(tmp_path):
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE sessions ("
        "id TEXT PRIMARY KEY, title TEXT DEFAULT '', started_at REAL NOT NULL, "
        "ended_at REAL, message_count INTEGER DEFAULT 0, personality TEXT DEFAULT '')"
    )
    conn.commit()
    conn.close()

    recall = SessionRecall(db_path)
    recall.init_db()
    columns = {
        row[1]
        for row in recall._get_conn().execute("PRAGMA table_info(sessions)")
    }
    assert {"source_session_key", "workspace_key", "workspace_root"} <= columns
    recall.close()


def test_recall_reopen_updates_only_nonempty_workspace_values(tmp_path):
    recall = SessionRecall(tmp_path / "workspace.db")
    recall.init_db()
    sid = recall.get_or_create_session(
        "session_alpha",
        workspace_key="first-key",
        workspace_root="/first/root",
    )

    reopened = recall.get_or_create_session(
        "session_alpha",
        workspace_key="second-key",
    )

    assert reopened == sid
    assert tuple(recall._get_conn().execute(
        "SELECT workspace_key, workspace_root FROM sessions WHERE id = ?", (sid,)
    ).fetchone()) == ("second-key", "/first/root")


def test_recall_keeps_canonical_writes_when_fts_trigger_fails(tmp_path):
    db_path = tmp_path / "fts-fail-open.db"
    recall = SessionRecall(db_path)
    recall.init_db()
    sid = recall.create_session("fts recovery")
    conn = recall._get_conn()
    conn.executescript("""
        DROP TRIGGER messages_ai;
        CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
            INSERT INTO messages_fts_broken(rowid, content)
            VALUES (new.id, new.content);
        END;
    """)

    message_id = recall.log_message(sid, "user", "canonical survives broken search index")

    assert conn.execute("SELECT content FROM messages WHERE id=?", (message_id,)).fetchone()[0].startswith("canonical")
    assert conn.execute("SELECT value FROM recall_meta WHERE key='fts_stale'").fetchone()[0] == "1"
    assert conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'messages_a%'"
    ).fetchone()[0] == 0
    assert recall.search("canonical")[0]["message_id"] == message_id
    recall.close()

    reopened = SessionRecall(db_path)
    reopened.init_db()
    assert reopened._fts_stale is False
    assert reopened._get_conn().execute(
        "SELECT 1 FROM recall_meta WHERE key='fts_stale'"
    ).fetchone() is None
    assert reopened.search("canonical")[0]["message_id"] == message_id
    reopened.close()



def test_init_db_retries_once_after_operational_error(tmp_path, monkeypatch):
    recall = SessionRecall(tmp_path / "retry.db")
    calls = []
    original = recall._init_db_once

    def flaky_once():
        calls.append("init")
        if len(calls) == 1:
            raise sqlite3.OperationalError("disk I/O error")
        original()

    monkeypatch.setattr(recall, "_init_db_once", flaky_once)
    monkeypatch.setattr(recall_module.time, "sleep", lambda _seconds: None)

    recall.init_db()
    assert calls == ["init", "init"]
    assert recall._get_conn().execute("SELECT 1").fetchone()[0] == 1
    recall.close()


class _WalFailOnceConnection:
    def __init__(self, real: sqlite3.Connection):
        self._real = real
        self._wal_failed = False

    def __getattr__(self, name):
        return getattr(self._real, name)

    def __setattr__(self, name, value):
        if name in {"_real", "_wal_failed"}:
            object.__setattr__(self, name, value)
        else:
            setattr(self._real, name, value)

    def execute(self, sql, *args):
        if "JOURNAL_MODE=WAL" in str(sql).upper() and not self._wal_failed:
            self._wal_failed = True
            raise sqlite3.OperationalError("disk I/O error")
        return self._real.execute(sql, *args)


def test_wal_failure_falls_back_without_breaking_init(tmp_path, monkeypatch):
    wrapped = []
    real_connect = recall_module.sqlite3.connect

    def flaky_connect(*args, **kwargs):
        connection = _WalFailOnceConnection(real_connect(*args, **kwargs))
        wrapped.append(connection)
        return connection

    monkeypatch.setattr(recall_module.sqlite3, "connect", flaky_connect)

    recall = SessionRecall(tmp_path / "wal-fallback.db")
    recall.init_db()
    recall.create_session("wal fallback")
    assert wrapped[0]._wal_failed is True
    assert recall.search("wal fallback") is not None
    recall.close()


def test_source_filter_applies_to_fts_like_and_unfiltered_search(tmp_path):
    recall = SessionRecall(tmp_path / "sources.db")
    recall.init_db()
    session_ids = {
        "astra": recall.get_or_create_session("session_native", title="native"),
        "hermes": recall.get_or_create_session("hermes:source", title="hermes"),
        "api": recall.get_or_create_session("session_api_sister", title="api"),
    }
    astra_wildcard_decoy = recall.get_or_create_session(
        "sessionXapiYnative",
        title="native wildcard decoy",
    )
    for sid in [*session_ids.values(), astra_wildcard_decoy]:
        recall.log_message(sid, "user", "sharedneedle hi")

    assert {
        result["session_id"] for result in recall.search("sharedneedle", limit=10)
    } == {*session_ids.values(), astra_wildcard_decoy}
    for source_type, expected_id in session_ids.items():
        expected_ids = (
            {expected_id, astra_wildcard_decoy}
            if source_type == "astra"
            else {expected_id}
        )
        assert {
            result["session_id"]
            for result in recall.search(
                "sharedneedle",
                limit=10,
                source_type=source_type,
            )
        } == expected_ids
        assert {
            result["session_id"]
            for result in recall.search("hi", limit=10, source_type=source_type)
        } == expected_ids

    with pytest.raises(ValueError, match="source_type"):
        recall.search("sharedneedle", source_type="unknown")
    recall.close()


def test_source_filter_applies_before_browse_limit_and_includes_legacy_astra(tmp_path):
    recall = SessionRecall(tmp_path / "browse-sources.db")
    recall.init_db()
    api_id = recall.get_or_create_session("session_api_sister", title="api")
    legacy_astra_id = recall.create_session("legacy native")
    hermes_id = recall.get_or_create_session("hermes:source", title="hermes")
    conn = recall._get_conn()
    conn.execute("UPDATE sessions SET started_at = 1 WHERE id = ?", (api_id,))
    conn.execute("UPDATE sessions SET started_at = 2 WHERE id = ?", (legacy_astra_id,))
    conn.execute("UPDATE sessions SET started_at = 3 WHERE id = ?", (hermes_id,))
    conn.commit()

    assert recall.browse(limit=1, source_type="api")[0]["session_id"] == api_id
    assert recall.browse(limit=1, source_type="astra")[0]["session_id"] == legacy_astra_id
    assert recall.browse(limit=1, source_type="hermes")[0]["session_id"] == hermes_id
    recall.close()


def test_session_search_tool_exposes_and_forwards_source_filter(monkeypatch):
    calls = []

    class FakeRecall:
        def search(self, query, *, limit, window, sort, source_type):
            calls.append((query, limit, window, sort, source_type))
            return []

    monkeypatch.setattr(session_recall_tool, "_SR", FakeRecall())
    payload = session_recall_tool._search(query="needle", source_type="api")
    registry = ToolRegistry()
    session_recall_tool.register_session_recall_tools(registry)
    source_schema = registry.get("session_search").parameters["properties"]["source_type"]

    assert calls == [("needle", 3, 3, None, "api")]
    assert '"source_type": "api"' in payload
    assert source_schema["enum"] == ["", "astra", "hermes", "api", "learning"]


def _search_tool(monkeypatch, tmp_path):
    """The real session_search tool over a real archive in a temporary directory."""
    recall = SessionRecall(tmp_path / "tool.db")
    recall.init_db()
    monkeypatch.setattr(session_recall_tool, "_SR", recall)
    registry = ToolRegistry()
    session_recall_tool.register_session_recall_tools(registry)

    def call(**arguments):
        result = asyncio.run(registry.execute("session_search", {"source_type": "astra", **arguments}))
        return result, (json.loads(result["output"]) if result["output"] else {})

    return recall, call


def test_cut_messages_are_marked_and_a_scroll_returns_its_anchor_whole(monkeypatch, tmp_path):
    recall, call = _search_tool(monkeypatch, tmp_path)
    sid = recall.get_or_create_session("session_long", title="long")
    first = recall.log_message(sid, "user", "short opening question")
    long_text = "a" * 1500 + " needleword " + "b" * 1488
    long_id = recall.log_message(sid, "assistant", long_text)
    neighbour = recall.log_message(sid, "user", "c" * 700)

    result, found = call(query="needleword")
    row = found["results"][0]
    assert row["message_id"] == long_id
    assert row["truncated"] is True and row["content_chars"] == len(long_text)
    assert result["partial"] is True and "around_message_id" in found["partial"]
    cut_context = [item for item in row["window_context"] if item["id"] in {long_id, neighbour}]
    assert [(item["truncated"], item["content_chars"]) for item in cut_context] == [(True, 3000), (True, 700)]

    result, scrolled = call(session_id=sid, around_message_id=long_id)
    by_id = {item["id"]: item for item in scrolled["window"]}
    assert by_id[long_id]["content"] == long_text and "truncated" not in by_id[long_id]
    assert len(by_id[neighbour]["content"]) == 500
    assert by_id[neighbour]["truncated"] is True and by_id[neighbour]["content_chars"] == 700
    assert "truncated" not in by_id[first]
    assert result["partial"] is True and "around_message_id" in scrolled["partial"]

    # The cut neighbour becomes readable by anchoring on it; nothing short is called partial.
    _, reread = call(session_id=sid, around_message_id=neighbour, scroll_window=1)
    assert {item["id"]: item["content"] for item in reread["window"]}[neighbour] == "c" * 700
    other = recall.get_or_create_session("session_short", title="short")
    only = recall.log_message(other, "user", "nothing long here")
    result, short = call(session_id=other, around_message_id=only)
    assert "partial" not in short and not result.get("partial")


def test_a_browsed_session_can_be_opened_with_its_first_or_last_message_id(monkeypatch, tmp_path):
    recall, call = _search_tool(monkeypatch, tmp_path)
    sid = recall.get_or_create_session("session_browse", title="browse")
    ids = [recall.log_message(sid, "user" if index % 2 == 0 else "assistant", f"turn {index}") for index in range(4)]

    _, browsed = call()
    row = next(item for item in browsed["sessions"] if item["session_id"] == sid)
    assert (row["first_message_id"], row["last_message_id"]) == (ids[0], ids[-1])

    _, opened = call(session_id=sid, around_message_id=row["first_message_id"])
    assert [item["content"] for item in opened["window"]] == [f"turn {index}" for index in range(4)]
    _, tail = call(session_id=sid, around_message_id=row["last_message_id"], scroll_window=1)
    assert [item["id"] for item in tail["window"]] == ids[-2:]


def test_scroll_with_an_anchor_outside_the_session_fails_and_names_its_session(monkeypatch, tmp_path):
    recall, call = _search_tool(monkeypatch, tmp_path)
    one = recall.get_or_create_session("session_one", title="one")
    two = recall.get_or_create_session("session_two", title="two")
    recall.log_message(one, "user", "in the first session")
    foreign = recall.log_message(two, "user", "in the second session")

    result, _ = call(session_id=one, around_message_id=foreign)
    assert result["code"] == "message_not_found"
    assert f"not found in session {one}" in result["error"] and two in result["error"]
    assert "same search result" in result["recovery_hint"]

    missing, _ = call(session_id=one, around_message_id=foreign + 1000)
    assert missing["code"] == "message_not_found" and two not in missing["error"]


def test_short_terms_match_as_substrings_and_keep_not_or_sort_and_window(monkeypatch, tmp_path):
    recall, call = _search_tool(monkeypatch, tmp_path)
    sid = recall.get_or_create_session("session_cjk", title="cjk")
    rollback = recall.log_message(sid, "user", "部署 之后 需要 回滚 方案")
    shipped = recall.log_message(sid, "assistant", "部署 已经 上线")
    weather = recall.log_message(sid, "user", "今天 天气 不错")
    conn = recall._get_conn()
    for offset, message_id in enumerate((rollback, shipped, weather)):
        conn.execute("UPDATE messages SET timestamp = ? WHERE id = ?", (1_000 + offset, message_id))
    conn.commit()

    def ids(**arguments):
        return [row["message_id"] for row in call(**arguments)[1]["results"]]

    # An excluded term stays excluded; it used to be required, which returned only the rollback message.
    assert ids(query="部署 NOT 回滚") == [shipped]
    assert set(ids(query="回滚 OR 天气")) == {rollback, weather}
    assert ids(query="部署") == [shipped, rollback]
    assert ids(query="部署", sort="oldest") == [rollback, shipped]

    _, found = call(query="部署 NOT 回滚", window=1)
    assert found["matching"] == "substring"
    assert "shorter than 3 characters" in found["matching_note"] and "relevance" in found["matching_note"]
    assert [item["id"] for item in found["results"][0]["window_context"]] == [rollback, shipped, weather]

    _, either = call(query="回滚 天气")
    assert {row["message_id"] for row in either["results"]} == {rollback, weather}
    assert "any one of them" in either["matching_note"]

    recall.log_message(sid, "user", "a fulltext indexed sentence")
    _, indexed = call(query="fulltext")
    assert indexed["total"] == 1 and "matching" not in indexed
