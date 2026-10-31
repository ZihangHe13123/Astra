"""Regression coverage for native posting masks, phase isolation and diagnostics."""
import json
import sqlite3
from types import SimpleNamespace

import pytest

from agent.runtime.context_index import sqlite_reader
from agent.runtime.context_index.activity_source import ActivityRecommendationReader, _normalized_tokens, _workspace_tier
from agent.runtime.context_index.diagnostics import trace_metadata
from agent.runtime.context_index.lexical import LexicalQuery
from agent.runtime.context_index.models import RetrievalStage
from agent.runtime.context_index.query import plan_query
from agent.runtime.context_index.sqlite_reader import ReadBudget
from agent.runtime.context_index.workspace import WorkspaceIdentity
from tests.test_context_index_activity_source import (
    NOW_DT, WORKSPACE, _add_event, _add_summary, _create_activity_database,
)
from tests.test_context_index_inspection import broker_with_rows, build


def _checkpoint(db):
    db.execute("WITH RECURSIVE ticks(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM ticks WHERE n<1000) SELECT sum(n) FROM ticks").fetchone()


def test_phase_timeout_leaves_reserved_time_but_never_renews_total_budget(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(sqlite_reader, "time", SimpleNamespace(perf_counter=lambda: clock[0]))
    with sqlite3.connect(":memory:") as db:
        budget = ReadBudget(db, 75)
        with pytest.raises(sqlite3.OperationalError, match="interrupted"), budget.stage("summary", 0.6):
            clock[0] = 0.046
            _checkpoint(db)
        with budget.stage("event"):
            clock[0] = 0.060
            _checkpoint(db)
        with pytest.raises(sqlite3.OperationalError, match="interrupted"), budget.stage("event_recent"):
            clock[0] = 0.076
            _checkpoint(db)
    assert [item.error_category for item in budget.stages] == ["deadline", "", "deadline"]
    assert budget.deadline == 0.075


def test_compute_wait_keeps_only_unspent_sql_budget(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(sqlite_reader, "time", SimpleNamespace(perf_counter=lambda: clock[0]))
    with sqlite3.connect(":memory:") as db:
        budget = ReadBudget(db, 75)
        clock[0] = 0.060
        with budget.pause_for_compute():
            clock[0] += 0.100
        with budget.stage("vector_validate"):
            clock[0] += 0.010
            _checkpoint(db)
        with pytest.raises(sqlite3.OperationalError, match="interrupted"), budget.stage("validate"):
            clock[0] += 0.006
            _checkpoint(db)
    assert budget.deadline == pytest.approx(0.175)


def test_activity_summary_timeout_does_not_starve_events(tmp_path, monkeypatch):
    path = tmp_path / "activity.db"
    db = _create_activity_database(path)
    _add_event(db, "segment", 7, searchable_text="memory retrieval evidence", seconds_before_now=20)
    db.commit()
    db.close()
    clock = [0.0]
    monkeypatch.setattr(sqlite_reader, "time", SimpleNamespace(perf_counter=lambda: clock[0]))
    monkeypatch.setenv("ASTRA_CONTEXT_INDEX_EMBEDDING", "off")
    reader = ActivityRecommendationReader(path)

    def slow_summary(connection, *_args):
        clock[0] = 0.057
        _checkpoint(connection)
        pytest.fail("summary must stop at its share of the read budget")

    monkeypatch.setattr(reader, "_summary_relevance", slow_summary)
    plan = plan_query("memory retrieval", NOW_DT)
    result = reader.recommend(plan.query, WORKSPACE, NOW_DT, plan)
    assert result.error_category == "deadline"
    assert any(item.locator.primary == "segment" for item in result.relevance)
    assert [(s.name, s.error_category) for s in result.stages] == [("summary", "deadline"), ("event", "")]


@pytest.mark.usefixtures("unhurried_context_readers")
@pytest.mark.parametrize("kind", ["summary", "event"])
def test_activity_ranks_independent_coverage_before_limit_and_excludes_future(tmp_path, kind, monkeypatch):
    path = tmp_path / "activity.db"
    db = _create_activity_database(path)
    query = "数据库连接池故障重试超时"
    useful = "数据库设置已检查，重试超时需要记录。"
    noise = "连接池故障出现在虚构故事里。"
    for i, text in enumerate([useful, *[noise] * 80, query]):
        # The strongest exact match is future evidence and cannot consume quota.
        age = -60 if i == 81 else 100 - i
        if kind == "summary":
            _add_summary(db, str(i), text, seconds_before_now=age)
        else:
            _add_event(db, str(i), i, searchable_text=text, seconds_before_now=age)
    db.commit()
    db.close()
    monkeypatch.setenv("ASTRA_CONTEXT_INDEX_EMBEDDING", "off")
    plan = plan_query(query, NOW_DT)
    result = ActivityRecommendationReader(path).recommend(plan.query, WORKSPACE, NOW_DT, plan)
    assert not result.error_category
    assert result.relevance[0].locator.primary == "0"
    assert all(item.locator.primary != "81" for item in result.relevance)


@pytest.mark.parametrize("text", ["NUS 课程", "sinus 课程", "NUS2 课程", "_NUS_", "NUS", "a-b", "İnus", "ſnus", "NUS\n课程", "zz NUS zz"])
def test_bound_anchor_predicate_preserves_sqlite_ascii_boundaries(text):
    lexical = LexicalQuery.from_text("NUS a-b 课程")
    with sqlite3.connect(":memory:") as db:
        old, params = lexical.anchor_sql("content")
        new = lexical.bind_anchor_sql(db, "content")
        expected = db.execute(f"SELECT ({old}) FROM (SELECT ? AS content)", (*params, text)).fetchone()[0]
        actual = db.execute(f"SELECT ({new}) FROM (SELECT ? AS content)", (text,)).fetchone()[0]
    assert actual == expected


@pytest.mark.parametrize("label,text", [
    ("astra-master", "hello ASTRA_master world"), ("astra", "astra2"),
    ("研究 repo", "研究 / repo"), ("a_b", "a---b"), ("Straße", "STRASSE"),
    ("astra", "界astra界"), ("astra", "_astra_"), ("repo", "other repository"),
])
def test_fast_workspace_match_preserves_alphanumeric_normalization(label, text):
    workspace = WorkspaceIdentity("key", "", label)
    expected = 1 if f" {_normalized_tokens(label)} " in f" {_normalized_tokens(text)} " else 2
    assert _workspace_tier(workspace, text) == expected


def test_inspection_keeps_phase_errors_separate_and_content_free():
    broker = broker_with_rows()
    build(broker)
    trace = broker.last_trace
    trace.semantic_errors = {"session": "deadline", "private path": "secret"}
    trace.source_stages = {"activity": (RetrievalStage("summary", 40, "deadline"), RetrievalStage("event", 5))}
    trace.semantic_stages = {"session": (
        RetrievalStage("snapshot", 75, "deadline"), RetrievalStage("private query", 1, "secret"),
        RetrievalStage("validate", float("nan"), "secret"),
    )}
    metadata = trace_metadata(trace)
    text = json.dumps(metadata, allow_nan=False)
    assert "secret" not in text and "private" not in text
    assert metadata["semantic_errors"] == {"session": "deadline"}
    assert metadata["source_stages"]["activity"][1] == {"stage": "event", "latency_ms": 5, "error": ""}
    assert metadata["semantic_stages"]["session"][0]["stage"] == "snapshot"
    detail = json.loads(broker.inspect())
    assert '"snapshot"' in json.dumps(detail)


@pytest.mark.parametrize("query", ["2+2", "２＋２", "3 * 4?", "0.5×2"])
def test_simple_arithmetic_does_not_scan_archives_or_inject_numeric_collisions(query):
    broker = broker_with_rows()
    pack = build(broker, query)
    assert not pack.rows
    assert broker.last_trace.decision == "not-needed"
    assert broker.last_trace.source_stages == {}


@pytest.mark.parametrize("query", ["之前2+2怎么修的", "2026-09-09", "1/2", "memory 2+2", "2+2 报错"])
def test_arithmetic_gate_keeps_dates_versions_and_substantive_questions(query):
    assert plan_query(query, NOW_DT).should_recall
