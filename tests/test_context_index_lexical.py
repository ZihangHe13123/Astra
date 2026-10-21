"""Native lexical recall must recover partial matches without single-term noise."""

import asyncio
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from agent.runtime.context_index.broker import ContextIndexBroker
from agent.runtime.context_index.models import SourceResult
from agent.runtime.context_index.query import plan_query
from agent.runtime.context_index.record_source import RecordSource
from agent.runtime.context_index.session_source import SessionRecommendationSource, content_fingerprint
from agent.runtime.context_index.sqlite_reader import open_readonly, set_read_window
from agent.runtime.context_index.workspace import WorkspaceIdentity
from agent.runtime.memory import MemoryStore
from tests.test_context_index_session_source import _add_session, _create_recall_database


NOW = datetime(2026, 9, 8, 10, tzinfo=UTC)
WORKSPACE = WorkspaceIdentity("astra", "", "astra")


def test_indexed_recall_does_not_visit_every_session_in_the_workspace(tmp_path):
    path = tmp_path / "sessions.db"
    db = _create_recall_database(path)
    db.execute("CREATE INDEX sessions_workspace ON sessions(workspace_key)")
    db.executemany("INSERT INTO sessions(id,title,started_at,workspace_key) VALUES (?, 'History', ?, ?)",
                   [(f"idle-{i}", NOW.timestamp() - 100, WORKSPACE.key) for i in range(5000)])
    ids = _add_session(db, "history", "History", WORKSPACE.key, [
        ("assistant", "记忆推荐机制支持历史检索", NOW.timestamp() - 1),
    ])
    db.commit()
    db.close()
    steps = 0

    def work_budget():
        nonlocal steps
        steps += 1000
        return int(steps >= 25000)

    with open_readonly(path) as reader:
        reader.row_factory = sqlite3.Row
        set_read_window(reader, NOW.timestamp())
        reader.set_progress_handler(work_budget, 1000)
        rows = SessionRecommendationSource(path)._native_relevance_rows(
            reader, "新记忆推荐库，质量如何", WORKSPACE, "current", None, frozenset(), True,
        )
    assert [row["id"] for row in rows] == ids


@pytest.mark.parametrize("exact_id", [False, True])
@pytest.mark.parametrize("query,history", [
    ("新记忆推荐库，质量如何", "记忆推荐支持关键词召回与去重"),
    ("SQLite recovery retry errors", "SQLite transactions need retry on lock timeout"),
])
def test_session_partial_history_survives_current_question_hit(tmp_path, exact_id, query, history):
    path = tmp_path / "sessions.db"
    db = _create_recall_database(path)
    prior = _add_session(db, "history", "History", WORKSPACE.key, [
        ("assistant", history, NOW.timestamp() - 10),
    ])
    current = _add_session(db, "current", "Current", WORKSPACE.key, [
        ("user", query, NOW.timestamp()),
        ("assistant", query + " future answer", NOW.timestamp() + (0 if exact_id else 1)),
    ])
    db.commit()
    db.close()
    plan = plan_query(query, NOW)
    if exact_id:
        plan = replace(plan, message_id=current[0])
    reader = SessionRecommendationSource(path)
    result = reader.recommend(query, WORKSPACE, "current", frozenset({content_fingerprint(query)}), NOW.timestamp(), plan)
    assert not result.error_category
    assert [item.locator.secondary for item in result.relevance] == prior
    assert history in reader.open(result.relevance[0].locator, 2, plan).items[0]


def _read_source(tmp_path, source, query, contents):
    if source == "session":
        path = tmp_path / "sessions.db"
        db = _create_recall_database(path)
        _add_session(db, "history", "History", WORKSPACE.key, [
            ("assistant", content, NOW.timestamp() - len(contents) + i - 1)
            for i, content in enumerate(contents)
        ])
        db.commit()
        db.close()
        plan = plan_query(query, NOW)
        return SessionRecommendationSource(path).recommend(query, WORKSPACE, "current", frozenset(), NOW.timestamp(), plan)
    store = MemoryStore(path=tmp_path / "memory.db", core_dir=tmp_path / "core")
    for content in contents:
        store.add_record(kind="observation", content=content)
    return RecordSource(store.path).recommend(plan_query(query, datetime.now().astimezone()), WORKSPACE, frozenset())


@pytest.mark.parametrize("source", ["session", "memory"])
def test_coverage_before_limit_rejects_newer_single_bigram_noise(tmp_path, source):
    useful = "记忆推荐通过历史会话检索相关约束"
    result = _read_source(tmp_path, source, "新记忆推荐库，质量如何", [
        useful, *[f"Lyra 的身世记忆片段 {i}" for i in range(80)],
    ])
    assert not result.error_category
    assert [item.private_text for item in result.relevance] == [useful]


@pytest.mark.parametrize("source", ["session", "memory"])
def test_chinese_request_words_do_not_hide_one_useful_search_term(tmp_path, source):
    useful = "启动报错需要检查日志文件"
    result = _read_source(tmp_path, source, "帮我看看这个报错", [useful, "这个周末帮我看看菜单"])
    assert not result.error_category
    assert [item.private_text for item in result.relevance] == [useful]


@pytest.mark.parametrize("source", ["session", "memory"])
def test_literal_identifier_does_not_match_sql_like_wildcard(tmp_path, source):
    useful = "error_code identifies the failure"
    result = _read_source(tmp_path, source, "error_code", [useful, "errorXcode is a different identifier"])
    assert [item.private_text for item in result.relevance] == [useful]


@pytest.mark.parametrize("query,useful,noise", [
    ("记忆 推荐", "记忆推荐支持历史检索", "记忆片段来自人物故事"),
    ("go SQLite", "go applications use SQLite", "go applications use Postgres"),
])
def test_session_short_terms_work_with_and_without_an_indexed_term(tmp_path, query, useful, noise):
    result = _read_source(tmp_path, "session", query, [useful, noise])
    assert not result.error_category
    assert [item.private_text for item in result.relevance] == [useful]


def test_native_session_keeps_workspace_pool_when_foreign_hits_rank_higher(tmp_path):
    path = tmp_path / "sessions.db"
    db = _create_recall_database(path)
    prior = _add_session(db, "local", "History", WORKSPACE.key, [
        ("assistant", "SQLite retry handles contention", NOW.timestamp() - 100),
    ])
    _add_session(db, "foreign", "Other project", "elsewhere", [
        ("assistant", f"SQLite recovery retry errors {i}", NOW.timestamp() - 80 + i) for i in range(80)
    ])
    db.commit()
    db.close()
    plan = plan_query("SQLite recovery retry errors", NOW)
    result = SessionRecommendationSource(path).recommend(plan.query, WORKSPACE, "current", frozenset(), NOW.timestamp(), plan)
    assert not result.error_category
    assert result.relevance[0].locator.secondary == prior[0]


def test_broker_injects_chinese_session_evidence_without_unrelated_record(tmp_path):
    class EmptyActivity:
        def recommend(self, *args):
            return SourceResult("absent")

    now = datetime.now().astimezone()
    path = tmp_path / "sessions.db"
    db = _create_recall_database(path)
    useful = "记忆推荐支持关键词召回与去重"
    _add_session(db, "history", "History", WORKSPACE.key, [("assistant", useful, now.timestamp() - 10)])
    _add_session(db, "current", "Current", WORKSPACE.key, [("user", "新记忆推荐库，质量如何", now.timestamp())])
    db.commit()
    db.close()
    store = MemoryStore(path=tmp_path / "memory.db", core_dir=tmp_path / "core")
    store.add_record(kind="observation", content="Lyra 的身世记忆片段")
    # Not a test of the 200 ms source deadline, which can run out on a busy runner before a source thread starts.
    broker = ContextIndexBroker(
        "session", 900, SessionRecommendationSource(path), EmptyActivity(), source_deadline_seconds=5.0,
    )
    pack = asyncio.run(broker.build(
        "新记忆推荐库，质量如何", "request", "current", WORKSPACE, datetime.now().astimezone(),
        frozenset(), memory_path=store.path,
    ))
    assert [item.source for item in pack.rows] == ["session"]
    assert useful in pack.rendered
    assert useful in broker.open([pack.rows[0].handle], 1)


@pytest.mark.parametrize("kind", ["summary", "event"])
def test_activity_short_terms_apply_time_and_import_bounds_to_every_match(tmp_path, monkeypatch, kind):
    from agent.runtime.context_index.activity_source import ActivityRecommendationReader
    from tests.test_context_index_activity_source import NOW_DT, _create_activity_database, _add_summary, _add_event

    monkeypatch.setenv("ASTRA_CONTEXT_INDEX_EMBEDDING", "off")
    path = tmp_path / "activity.db"
    db = _create_activity_database(path)
    for number, (label, seconds) in enumerate((("past", 60), ("future", -60), ("late-import", 30))):
        content = f"记忆索引 {label}"
        if kind == "summary":
            _add_summary(db, label, content, seconds_before_now=seconds)
        else:
            _add_event(db, label, number, seconds_before_now=seconds, searchable_text=content)
    if kind == "summary":
        db.execute("UPDATE activity_summaries SET imported_at = ? WHERE summary_id = 'late-import'",
                   ((NOW_DT + timedelta(hours=1)).isoformat(),))
    else:
        db.execute("UPDATE activity_events SET imported_at = ? WHERE segment_id = 'late-import'",
                   ((NOW_DT + timedelta(hours=1)).isoformat(),))
    db.commit()
    db.close()
    reader = ActivityRecommendationReader(path)
    plan = plan_query("记忆 索引 数据", NOW_DT)
    result = reader.recommend(plan.query, WORKSPACE, NOW_DT, plan)
    assert not result.error_category
    assert [item.locator.primary for item in result.relevance] == ["past"]
