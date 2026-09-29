"""Request metrics remain read-only, bounded and isolated by exact session identity."""
import hashlib
import json
import os
from pathlib import Path

import pytest

from agent.runtime.query_profiler import QueryProfiler
from agent.ui import queries


def record(path: Path, *, request="request", stamp=10, **extra):
    return {"session_key": hashlib.sha256(os.path.normcase(str(path.resolve())).encode()).hexdigest(),
            "session_id": path.stem, "request_id": request, "step": 1, "timestamp": stamp,
            "total_ms": 1200, "request_to_first_text_ms": 400,
            "usage": {"prompt_tokens": 500, "prompt_cache_hit_tokens": 200}, **extra}


def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRA_HOME", str(tmp_path))
    monkeypatch.setattr(queries.sessions, "SESSION_DIR", tmp_path / "sessions")
    return tmp_path / "query-profile.jsonl", queries._session_path("same")


def test_request_timeline_filters_same_named_modes_and_never_leaks_unknown_fields(tmp_path, monkeypatch):
    log, work = setup(tmp_path, monkeypatch)
    bar_session = queries._session_path("same", "bar")
    log.write_text("\n".join(json.dumps(r) for r in [
        record(work, request="work", prompt="private text", token="secret"),
        record(bar_session, request="bar"),
        {"session_id": "same", "request_id": "legacy", "timestamp": 8},
    ]) + "\n")
    before = log.read_bytes(), log.stat().st_mtime_ns
    result = queries.query({"method": "request_timeline", "params": {"name": "same", "mode": "work"}})
    assert [r["request_id"] for r in result["records"]] == ["work"]
    assert "private text" not in json.dumps(result)
    assert "secret" not in json.dumps(result)
    assert result["unscoped_records"] == 1
    assert before == (log.read_bytes(), log.stat().st_mtime_ns)


def test_request_timeline_missing_is_not_zero_usage_and_does_not_create_files(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch)
    result = queries.query({"method": "request_timeline", "params": {"name": "same"}})
    assert result["records"] == []
    assert result["available"] is False
    assert list(tmp_path.iterdir()) == []


def test_request_timeline_survives_partial_tail_and_paginates_latest_records(tmp_path, monkeypatch):
    log, path = setup(tmp_path, monkeypatch)
    log.write_text("\n".join(json.dumps(record(path, request=str(i), stamp=i)) for i in range(7)) + '\n{"partial":')
    result = queries.query({"method": "request_timeline", "params": {"name": "same", "limit": 3}})
    assert [r["request_id"] for r in result["records"]] == ["6", "5", "4"]
    assert result["has_more"] is True


@pytest.mark.parametrize("params", [{"name": "../escape"}, {"name": "same", "mode": "bad"},
                                         {"name": "same", "limit": -1}])
def test_request_timeline_validates_boundaries(tmp_path, monkeypatch, params):
    setup(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        queries.query({"method": "request_timeline", "params": params})


def test_profiler_persists_opaque_exact_session_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRA_HOME", str(tmp_path / "state"))
    path = tmp_path / "sessions" / "bar" / "same.json"
    profiler = QueryProfiler(enabled=True, session_id="same", session_path=path,
                             request_id="r", step=1, model="fixture", root=tmp_path)
    profiler.finish()
    text = (tmp_path / "state" / "query-profile.jsonl").read_text()
    assert json.loads(text)["session_key"] == hashlib.sha256(os.path.normcase(str(path.resolve())).encode()).hexdigest()
    assert str(path) not in text


def test_missing_usage_remains_missing_through_actual_profiler_and_query(tmp_path, monkeypatch):
    from agent.runtime.query_profiler import PromptCacheTracker
    from agent.runtime.llm import _usage_dict
    from types import SimpleNamespace
    log, path = setup(tmp_path, monkeypatch)
    for i, usage in enumerate([None, {"prompt_tokens": 0}, _usage_dict(SimpleNamespace(prompt_tokens=9, completion_tokens=2, total_tokens=11))]):
        profiler = QueryProfiler(enabled=True, session_id="same", session_path=path,
                                 request_id=f"r{i}", step=1, model="fixture", root=tmp_path)
        profiler.finish(usage)
    result = queries.query({"method": "request_timeline", "params": {"name": "same"}})
    by_id = {r["request_id"]: r for r in result["records"]}
    assert by_id["r0"]["usage"] == {}
    assert by_id["r1"]["usage"] == {"prompt_tokens": 0}
    assert by_id["r2"]["usage"] == {"prompt_tokens": 9, "completion_tokens": 2}
    tracker = PromptCacheTracker()
    tracker.observe(model="m", session_id="s", fingerprint={}, usage={"prompt_cache_hit_tokens": 5000}, now=1)
    missing = tracker.observe(model="m", session_id="s", fingerprint={}, usage=None, now=2)
    assert missing["status"] == "unknown"
    assert missing.get("cache_read_tokens") is None
