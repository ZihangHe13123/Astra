"""Short SQLite budgets use precise monotonic time, including on Windows3.11."""
import math
import sqlite3
from types import SimpleNamespace

import pytest

from agent.runtime.context_index import semantic_reader, session_source, sqlite_reader
from agent.runtime.context_index.session_source import SessionRecommendationSource
from agent.runtime.context_index.sqlite_reader import ReadBudget, observe_stage, open_readonly
from tests.test_context_index_read_budget import _checkpoint
from tests.test_context_index_semantic import TEXT, archive as archive, recommend
from tests.test_context_index_session_source import NOW, WORKSPACE, recall_db as recall_db


def _clocks(now):
    return SimpleNamespace(perf_counter=lambda: now[0],
                           monotonic=lambda: math.floor(now[0] / .015625) * .015625)


def test_readonly_precise_deadline_keeps_full_75ms_and_expires_at_boundary(tmp_path, monkeypatch):
    now = [.015]
    clocks = _clocks(now)
    # Demonstrate the former Windows clock behavior through a real SQL handler:
    # 63.125ms elapsed, but the coarse clock has already advanced78.125ms.
    with sqlite3.connect(":memory:") as db:
        coarse_deadline = clocks.monotonic() + .075
        db.set_progress_handler(lambda: int(clocks.monotonic() >= coarse_deadline), 1000)
        now[0] = .078125
        with pytest.raises(sqlite3.OperationalError, match="interrupted"):
            _checkpoint(db)

    path = tmp_path / "readonly.db"
    db = sqlite3.connect(path)
    db.close()
    now[0] = .015
    monkeypatch.setattr(sqlite_reader, "time", clocks)
    with open_readonly(path, deadline_ms=75) as db:
        now[0] = .078125
        _checkpoint(db)
        now[0] = .015 + .075 - .000001
        _checkpoint(db)
        now[0] = .015 + .075
        with pytest.raises(sqlite3.OperationalError, match="interrupted"):
            _checkpoint(db)
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        db.execute("SELECT 1")


def test_read_budget_uses_precise_clock_without_extending_deadline(monkeypatch):
    now = [.015]
    monkeypatch.setattr(sqlite_reader, "time", _clocks(now))
    with sqlite3.connect(":memory:") as db:
        budget = ReadBudget(db, 75)
        now[0] = .078125
        with budget.stage("before_cutoff"):
            _checkpoint(db)
        now[0] = budget.deadline
        with pytest.raises(sqlite_reader.ReadDeadlineExceeded), budget.stage("at_cutoff"):
            _checkpoint(db)
    assert budget.deadline == .015 + .075
    assert [stage.error_category for stage in budget.stages] == ["", "deadline"]


def test_stage_elapsed_reports_precise_duration(monkeypatch):
    now = [.015]
    monkeypatch.setattr(sqlite_reader, "time", _clocks(now))
    stages = []
    with observe_stage(stages, "read"):
        now[0] = .078125
    assert stages[0].latency_ms == pytest.approx(63.125)


def test_readonly_cancellation_interrupts_sql_and_closes_handle(tmp_path):
    path = tmp_path / "cancel.db"
    db = sqlite3.connect(path)
    db.close()
    stopped = [False]
    with open_readonly(path, deadline_ms=2000, cancelled=lambda: stopped[0]) as db:
        _checkpoint(db)
        stopped[0] = True
        with pytest.raises(sqlite3.OperationalError, match="interrupted"):
            _checkpoint(db)
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        db.execute("SELECT 1")
    path.unlink()
    assert not path.exists()


@pytest.mark.parametrize("at_deadline", [False, True])
def test_session_handler_keeps_early_tick_budget_and_completed_recency(recall_db, monkeypatch, at_deadline):
    now = [.015]
    monkeypatch.setattr(session_source, "time", _clocks(now))
    monkeypatch.setattr(sqlite_reader, "time", _clocks(now))
    source = SessionRecommendationSource(recall_db, deadline_ms=75)
    original_recency, original_relevance = source._recency_rows, source._relevance_rows

    def recency(connection, *args, **kwargs):
        now[0] = .078125
        _checkpoint(connection)
        return original_recency(connection, *args, **kwargs)

    def relevance(connection, *args, **kwargs):
        now[0] = .015 + .075 if at_deadline else .015 + .075 - .000001
        _checkpoint(connection)
        return original_relevance(connection, *args, **kwargs)

    monkeypatch.setattr(source, "_recency_rows", recency)
    monkeypatch.setattr(source, "_relevance_rows", relevance)
    result = source.recommend("memory index", WORKSPACE, "current", frozenset(), NOW)
    assert result.availability == "available"
    assert result.recency
    assert bool(result.relevance) is not at_deadline
    assert result.error_category == ("deadline" if at_deadline else "")
    assert result.diagnostics == (("relevance_omitted",) if at_deadline else ())


@pytest.mark.parametrize("source", ["session", "memory"])
@pytest.mark.parametrize("elapsed_ms", [47.5, 60])
def test_semantic_validation_loop_respects_precise_60ms(archive, monkeypatch, source, elapsed_ms):
    values = iter([.015, .015 + elapsed_ms / 1000])
    last = [.015]

    def precise():
        last[0] = next(values, last[0])
        return last[0]

    monkeypatch.setattr(semantic_reader, "time", SimpleNamespace(
        perf_counter=precise, monotonic=lambda: math.floor(precise() / .015625) * .015625,
    ))
    result = recommend(archive, source)
    assert not result.error_category
    assert [item.private_text for item in result.relevance] == ([TEXT] if elapsed_ms < 60 else [])
