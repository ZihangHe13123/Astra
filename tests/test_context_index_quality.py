import sqlite3
from functools import partial
from types import SimpleNamespace

import pytest

from agent.evals.context_index_benchmark import quality
from agent.evals.context_index_benchmark.quality_metrics import QualityObservation, quality_metrics
from agent.evals.context_index_benchmark.quality import load_fixture, replay


def test_gold_metrics_penalize_omitted_evidence_and_unwanted_injection():
    rows = [
        QualityObservation("partial", "holdout", "paraphrase", frozenset({"a", "b"}), ("a",)),
        QualityObservation("miss", "holdout", "paraphrase", frozenset({"c"}), ()),
        QualityObservation("negative", "holdout", "negative", frozenset(), ("noise",)),
        QualityObservation("abstain", "holdout", "negative", frozenset(), ()),
    ]
    scores = quality_metrics(rows)
    assert scores["recall_at_4"] == 0.25
    assert scores["ndcg_at_4"] < 0.5
    assert scores["negative_injection_rate"] == 0.5
    assert scores["displayed_precision"] == 0.5
    assert scores["irrelevant_rows"] == 1


def test_duplicate_results_cannot_inflate_gold_recall():
    row = QualityObservation("one", "development", "lexical", frozenset({"a"}), ("a", "a", "a"))
    assert quality_metrics([row])["recall_at_4"] == 1.0
    assert quality_metrics([])["cases"] == 0
    late = QualityObservation("late", "holdout", "paraphrase", frozenset({"b"}), ("a", "a", "b"))
    scores = quality_metrics([late])
    assert scores["ndcg_at_4"] == 0.5
    assert scores["displayed_precision"] == 0.3333


def test_quality_fixture_has_disjoint_scenarios_and_explicit_negatives():
    scenarios = load_fixture()["scenarios"]
    assert len(scenarios) == 24
    assert {row["split"] for row in scenarios} == {"development", "holdout"}
    assert all(row["content"] and row["lexical"] and row["paraphrase"] and row["negative"] and row["hard_negative"] for row in scenarios)
    assert all(row["content"] not in row["distractors"] for row in scenarios)


def test_quality_replay_exercises_sources_without_live_archives_or_models(monkeypatch):
    from agent.runtime.context_index import embedder
    monkeypatch.setattr(embedder, "get_embedder", lambda: (_ for _ in ()).throw(AssertionError("no model")))
    # Replay builds its own broker with the 200 ms source deadline, which can run out on a busy runner
    # before a source thread starts. This test is about what the sources find, so that broker waits longer.
    monkeypatch.setattr(
        quality, "ContextIndexBroker", partial(quality.ContextIndexBroker, source_deadline_seconds=5.0),
    )
    rows = replay(load_fixture()["scenarios"][:2])
    lexical = [row for row in rows if row.kind == "lexical" and row.case_id.startswith("contact/")]
    negatives = [row for row in rows if row.kind == "negative"]
    assert len(rows) == 16
    assert all(row.displayed and row.displayed[0] == "target" for row in lexical)
    assert all(not row.displayed for row in negatives)


@pytest.mark.parametrize("fail_update", [False, True])
def test_quality_archive_setup_closes_connections_before_cleanup(tmp_path, monkeypatch, fail_update):
    opened = []

    class Connection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if fail_update and sql.startswith("UPDATE memory_records"):
                raise sqlite3.OperationalError("fixture update failed")
            return super().execute(sql, parameters)

    def connect(path):
        db = sqlite3.connect(path, factory=Connection)
        opened.append(db)  # Retain handles so garbage collection cannot mask leaks.
        return db

    monkeypatch.setattr(quality, "sqlite3", SimpleNamespace(connect=connect))
    try:
        if fail_update:
            with pytest.raises(sqlite3.OperationalError, match="fixture update failed"):
                quality._archives(tmp_path, load_fixture()["scenarios"][0], "memory")
        else:
            quality._archives(tmp_path, load_fixture()["scenarios"][0], "memory")
        assert len(opened) == 2
        for db in opened:
            with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
                db.execute("SELECT 1")
    finally:
        for db in opened:
            db.close()
