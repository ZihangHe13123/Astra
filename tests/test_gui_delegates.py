"""Read-only delegate history and the GUI's deliberately narrow status wire format."""

import json
import os

import pytest

from agent.runtime.session_store import SessionStore
from agent.ui import delegates


def _write(store, *events):
    store.subagent_path.parent.mkdir(parents=True, exist_ok=True)
    store.subagent_path.write_text("".join(json.dumps(event) + "\n" for event in events))


def _record(kind="started", **fields):
    return {"type": kind, "process_id": "worker-1", "task_id": "task-1",
            "goal": "Check a regression", "worker_type": "reader", **fields}


def test_legacy_history_keeps_final_report_without_transcript_and_never_mutates(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / "session-demo.json")
    _write(store,
           _record(recorded_at=100),
           _record("lifecycle", state="active", started_at=100, recorded_at=100.1, limits={"max_turns": 8}),
           _record("assistant", turn=1, content="private text", reasoning="private reasoning",
                   result="not a final report", tool_calls=[{"arguments": "private arguments"}], recorded_at=101),
           _record("tool", turn=1, output="private tool output", tool_name="read_file", recorded_at=102),
           _record("terminal", status="completed", result="The regression is fixed.", turns_used=2,
                   max_turns=8, recorded_at=103.5))
    before = {path: (path.stat().st_mtime_ns, path.read_bytes()) for path in tmp_path.rglob("*") if path.is_file()}
    monkeypatch.setattr(SessionStore, "recover_interrupted", lambda *_args: pytest.fail("history recovered a session"))
    monkeypatch.setattr(SessionStore, "load_subagent_events", lambda *_args: pytest.fail("unbounded sidecar read"))
    [worker] = delegates.delegate_history(store)
    assert worker == {
        "type": "delegate_status", "process_id": "worker-1", "task_id": "task-1",
        "session_id": "session-demo", "goal": "Check a regression", "worker_type": "reader",
        "status": "completed", "started_at": 100, "updated_at": 103.5,
        "completed_at": 103.5, "duration_ms": 3500, "turns_used": 2, "max_turns": 8,
        "result": "The regression is fixed.", "current_tool": "",
    }
    assert "private" not in json.dumps(worker)
    assert before == {path: (path.stat().st_mtime_ns, path.read_bytes()) for path in tmp_path.rglob("*") if path.is_file()}


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled", "timed_out", "partial", "interrupted"])
def test_preserves_explicit_terminal_statuses(tmp_path, status):
    store = SessionStore(tmp_path / "test.json")
    _write(store, _record("terminal", status=status, recorded_at=30, result="Public report", error="Public error"))
    [worker] = delegates.delegate_history(store)
    assert worker["status"] == status
    assert worker["result"] == "Public report"
    assert worker["error"] == "Public error"
    assert worker["completed_at"] == 30
    if status == "partial":
        assert worker["partial"] is True


@pytest.mark.parametrize("status", ["queued", "running", "idle"])
def test_persisted_snapshot_never_implies_a_live_worker(tmp_path, status):
    store = SessionStore(tmp_path / "test.json")
    _write(store, _record("delegate_status", status=status, started_at=20, updated_at=25,
                          result="Public episode report", current_tool="read_file",
                          team_id="team", agent_id="agent", reasoning="private"))
    [worker] = delegates.delegate_history(store)
    assert worker["status"] == "interrupted"
    assert "completed_at" not in worker
    assert worker["duration_ms"] == 5000
    assert worker["current_tool"] == ""
    assert worker["team_id"] == "team" and worker["agent_id"] == "agent"
    assert "reasoning" not in worker
    assert worker.get("result") == ("Public episode report" if status == "idle" else None)


def test_new_episode_does_not_reuse_previous_report(tmp_path):
    store = SessionStore(tmp_path / "test.json")
    _write(store,
           _record("delegate_status", status="idle", result="First episode", updated_at=25),
           _record("delegate_status", status="running", updated_at=30),
           _record("terminal", status="cancelled", recorded_at=31))
    [worker] = delegates.delegate_history(store)
    assert worker["status"] == "cancelled"
    assert "result" not in worker


def test_legacy_active_transition_clears_idle_episode_report(tmp_path):
    store = SessionStore(tmp_path / "test.json")
    _write(store,
           _record("delegate_status", status="idle", result="First episode", updated_at=25),
           _record("lifecycle", state="active", recorded_at=30))
    [worker] = delegates.delegate_history(store)
    assert worker["status"] == "interrupted"
    assert "result" not in worker


def test_terminal_can_recover_start_metadata_when_started_record_is_outside_tail(tmp_path):
    store = SessionStore(tmp_path / "test.json")
    _write(store, _record("terminal", status="completed", recorded_at=30,
                          lifecycle={"started_at": 20, "limits": {"max_turns": 5},
                                     "content": "Not displayable"}))
    [worker] = delegates.delegate_history(store)
    assert worker["started_at"] == 20
    assert worker["max_turns"] == 5
    assert worker["duration_ms"] == 10000
    assert "lifecycle" not in worker


def test_legacy_idle_or_active_is_interrupted_without_invented_report(tmp_path):
    store = SessionStore(tmp_path / "test.json")
    _write(store, _record("lifecycle", state="idle", recorded_at=100, started_at=90, result="not public"))
    [worker] = delegates.delegate_history(store)
    assert worker["status"] == "interrupted"
    assert worker["duration_ms"] == 10000
    assert "result" not in worker


def test_known_lifecycle_terminal_is_preserved_even_without_report(tmp_path):
    store = SessionStore(tmp_path / "test.json")
    _write(store, _record("lifecycle", state="terminal", completion_reason="cancelled", recorded_at=100))
    [worker] = delegates.delegate_history(store)
    assert worker["status"] == "cancelled"
    assert "result" not in worker


def test_sanitizer_strips_unknown_and_nested_fields_and_caps_display_text():
    worker = delegates.sanitize_delegate_event(_record(
        "delegate_status", status="partial", result="r" * 5000, goal="g" * 401,
        error="e" * 1025, current_tool="t" * 129, max_turns=8, turns_used=2,
        started_at=float("nan"), updated_at=float("inf"), completed_at=-1,
        duration_ms=True, lifecycle={"reasoning": "secret"}, arguments={"key": "secret"},
        content="secret", team_id="team", agent_id="agent"))
    for key, length in {"result": 4096, "goal": 400, "error": 1024, "current_tool": 128}.items():
        assert len(worker[key]) == length
        assert worker[f"{key}_truncated"] is True
    for key in ("lifecycle", "reasoning", "arguments", "content", "started_at", "updated_at", "completed_at", "duration_ms"):
        assert key not in worker
    assert worker["partial"] is True
    assert worker["turns_used"] == 2 and worker["max_turns"] == 8
    assert delegates.sanitize_delegate_event(worker) == worker


@pytest.mark.parametrize("event", [None, [], {}, {"process_id": "x", "status": []},
                                         {"process_id": [], "status": "running"},
                                         {"process_id": "x" * 129, "status": "running"},
                                         {"process_id": "x", "status": "mystery"}])
def test_sanitizer_rejects_malformed_identifiers_and_states(event):
    assert delegates.sanitize_delegate_event(event) == {}


def test_malformed_unknown_and_giant_records_do_not_break_history(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / "test.json")
    monkeypatch.setattr(delegates, "_MAX_LINE_BYTES", 1024)
    _write(store,
           _record("assistant", recorded_at=1, content="x" * 3000),
           ["not", "an", "event"],
           _record("team_message", content="private", process_id="unrelated"),
           _record("terminal", recorded_at=3, status="completed", result="OK"))
    with store.subagent_path.open("ab") as file:
        file.write(b"not-json\n\xff\n{\"type\":\"unfinished")
    [worker] = delegates.delegate_history(store)
    assert worker["status"] == "completed" and worker["result"] == "OK"
    assert worker["history_truncated"] is True


def test_tail_is_bounded_and_does_not_parse_partial_first_record(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / "test.json")
    monkeypatch.setattr(delegates, "_MAX_HISTORY_BYTES", 1024)
    _write(store,
           _record(process_id="older", recorded_at=0),
           _record("assistant", content="x" * 5000, recorded_at=1),
           _record("terminal", recorded_at=3, status="failed", result="Final"))
    raw, truncated, _signature = delegates._tail(store.subagent_path)
    assert len(raw) <= 1024 and truncated
    [worker] = delegates.delegate_history(store)
    assert worker["process_id"] == "worker-1"
    assert worker["result"] == "Final" and worker["history_truncated"] is True


def test_only_most_recent_200_workers_but_complete_metadata_for_them(tmp_path):
    store = SessionStore(tmp_path / "test.json")
    events = [_record(process_id=f"p{n}", recorded_at=n) for n in range(205)]
    events += [_record("terminal", process_id="p0", status="completed", result="Newest", recorded_at=300)]
    _write(store, *events)
    history = delegates.delegate_history(store)
    assert len(history) == 200
    assert history[-1]["process_id"] == "p0" and history[-1]["started_at"] == 0
    assert history[-1]["result"] == "Newest"
    assert "p1" not in {item["process_id"] for item in history}
    assert all(item["history_truncated"] for item in history)


def test_cache_reuses_unchanged_file_but_append_and_atomic_replacement_refresh(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / "test.json")
    _write(store, _record(recorded_at=1))
    original_tail = delegates._tail
    reads = []

    def read(path):
        reads.append(path)
        return original_tail(path)

    monkeypatch.setattr(delegates, "_tail", read)
    first = delegates.delegate_history(store)
    first[0]["goal"] = "caller mutation"
    assert delegates.delegate_history(store)[0]["goal"] == "Check a regression"
    assert len(reads) == 1
    store.append_subagent_event(_record("terminal", status="failed", recorded_at=2, result="bad"))
    assert delegates.delegate_history(store)[0]["status"] == "failed"
    assert len(reads) == 2
    previous = store.subagent_path.stat()
    replacement = tmp_path / "replacement"
    replacement.write_bytes(store.subagent_path.read_bytes().replace(b'"bad"', b'"new"'))
    os.utime(replacement, ns=(previous.st_atime_ns, previous.st_mtime_ns))
    os.replace(replacement, store.subagent_path)
    assert delegates.delegate_history(store)[0]["result"] == "new"
    assert len(reads) == 3


def test_same_size_rewrite_with_restored_mtime_refreshes(tmp_path):
    store = SessionStore(tmp_path / "test.json")
    _write(store, _record("terminal", status="completed", result="old"))
    assert delegates.delegate_history(store)[0]["result"] == "old"
    original = store.subagent_path.stat()
    store.subagent_path.write_bytes(store.subagent_path.read_bytes().replace(b'"old"', b'"new"'))
    os.utime(store.subagent_path, ns=(original.st_atime_ns, original.st_mtime_ns))
    assert delegates.delegate_history(store)[0]["result"] == "new"


def test_entirely_giant_last_line_does_not_unbounded_read(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / "test.json")
    monkeypatch.setattr(delegates, "_MAX_HISTORY_BYTES", 1024)
    _write(store, _record("assistant", content="x" * 10000))
    raw, truncated, _signature = delegates._tail(store.subagent_path)
    assert raw == b"" and truncated
    assert delegates.delegate_history(store) == []


def test_missing_sidecar_does_not_create_any_files(tmp_path):
    store = SessionStore(tmp_path / "absent.json")
    assert delegates.delegate_history(store) == []
    assert list(tmp_path.iterdir()) == []


def test_no_result_placeholder_is_not_presented_as_a_final_report(tmp_path):
    store = SessionStore(tmp_path / "test.json")
    event = _record("terminal", status="failed", result="(no result — subagent did not produce output)", error="LLM unavailable")
    assert delegates.sanitize_delegate_event(event)["result"] == ""
    _write(store, event)
    [worker] = delegates.delegate_history(store)
    assert worker["result"] == "" and worker["error"] == "LLM unavailable"
