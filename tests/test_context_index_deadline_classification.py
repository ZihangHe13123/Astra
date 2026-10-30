import sqlite3

import pytest

from agent.runtime.context_index import sqlite_reader
from agent.runtime.context_index.activity_source import _is_deadline as activity_deadline
from agent.runtime.context_index.session_source import _is_deadline as session_deadline


def test_preexpired_stage_has_the_same_category_as_an_actual_sqlite_interrupt(monkeypatch):
    clock = [1.0]
    monkeypatch.setattr(sqlite_reader.time, "perf_counter", lambda: clock[0])
    connection = sqlite3.connect(":memory:")
    try:
        budget = sqlite_reader.ReadBudget(connection, 10)
        clock[0] = 2.0
        with pytest.raises(sqlite3.DatabaseError) as caught:
            with budget.stage("event"):
                pytest.fail("expired stage executed")
        assert activity_deadline(caught.value)
        assert session_deadline(caught.value)
        assert budget.stages[0].error_category == "deadline"
    finally:
        connection.close()
