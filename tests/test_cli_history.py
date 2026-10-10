from agent.cli.history import history_tool_results
from agent.runtime.react import ReActAgent
from agent.runtime.task_store import TaskStore


def _event(call_id, name, *, error="", duration=18):
    return {"type": "tool_result", "id": call_id, "name": name, "output": "saved output", "tool_output": error or "saved output", "error": error, "duration_ms": duration}


def _message(event):
    return {"role": "tool", "tool_call_id": event["id"], "content": ReActAgent._tool_result_context(event)}


def test_history_restores_saved_results_and_timing_only_for_current_conversation(tmp_path):
    store = TaskStore(tmp_path / "tasks.db")
    events = [_event("read", "read_file"), _event("open", "context_open", error="missing context", duration=0)]
    for session, records in (("current", events), ("other", [_event("open", "private_other_session")])):
        run = store.start_run(session, "test", session_id=session)
        for event in records:
            claim = store.claim_tool(run["id"], event["name"], {}, "read")
            store.finish_step(claim.step_id, status="failed" if event["error"] else "completed", output=event, error=event["error"])
        unfinished = store.claim_tool(run["id"], "unfinished_tool", {}, "read")
        assert unfinished.action == "execute"

    # Simulate restarting the store, including records from multiple sessions.
    saved = TaskStore(tmp_path / "tasks.db").recent_tool_results("current")
    assert saved == events
    assert store.recent_tool_results("current", limit=1) == events[-1:]
    assert history_tool_results([_message(event) for event in events], saved) == [
        {"name": "read_file", "call_id": "read", "output": "saved output", "error": "", "duration_ms": 18},
        {"name": "context_open", "call_id": "open", "output": "saved output", "error": "missing context", "duration_ms": 0},
    ]
    assert history_tool_results([], saved) == []
    assert history_tool_results([_message(events[-1])], saved) == [
        {"name": "context_open", "call_id": "open", "output": "saved output", "error": "missing context", "duration_ms": 0},
    ]


def test_history_without_task_database_preserves_known_outcomes_without_inventing_timing():
    events = [_event("a", "read_file"), _event("b", "context_open", error="missing context")]
    restored = history_tool_results([_message(event) for event in events], [])
    assert restored == [
        {"name": "read_file", "call_id": "a", "output": "saved output", "error": ""},
        {"name": "context_open", "call_id": "b", "output": "", "error": "missing context"},
    ]
    assert history_tool_results([{"role": "tool", "content": "unknown legacy outcome"}], []) == []


def test_history_without_task_database_leaves_out_the_notices_written_for_the_model():
    replayed = {**_event("a", "git_status"), "cached": True, "_repeat_limit_reached": True}
    partial = {**_event("b", "read_file"), "partial": True}

    restored = history_tool_results([_message(replayed), _message(partial)], [])

    assert restored == [
        {"name": "git_status", "call_id": "a", "output": "saved output", "error": ""},
        {"name": "read_file", "call_id": "b", "output": "saved output", "error": ""},
    ]


def test_history_without_task_database_restores_runs_that_did_not_succeed():
    failed = {**_event("a", "execute_python"), "execution": {"status": "completed", "exit_code": 1}}
    stopped = {**_event("b", "execute_python"), "execution": {"status": "timed_out", "exit_code": -1}}
    started = {**_event("c", "execute_shell"), "execution": {"status": "running", "exit_code": None}}

    restored = history_tool_results([_message(failed), _message(stopped), _message(started)], [])

    assert [(item["name"], item["call_id"], item["output"], item["error"]) for item in restored] == [
        ("execute_python", "a", "saved output", ""),
        ("execute_python", "b", "saved output", ""),
        ("execute_shell", "c", "saved output", ""),
    ]


def test_history_retains_result_artifacts_but_omits_internal_args():
    event = _event("a", "read_file") | {"artifact_path": "/tmp/saved.txt", "output_truncated": True, "args": {"internal": True}}
    restored = history_tool_results([_message(event)], [event])[0]
    assert restored["artifact_path"] == "/tmp/saved.txt"
    assert restored["output_truncated"] is True
    assert "args" not in restored


def test_history_repeated_call_ids_keep_their_own_results_and_are_bounded():
    first = _event("same", "read_file", duration=5)
    second = _event("same", "read_file", duration=9)
    messages = [_message(first), _message(second)]
    assert [r["duration_ms"] for r in history_tool_results(messages, [first, second])] == [5, 9]
    assert [r["duration_ms"] for r in history_tool_results(messages, [first, second], limit=1)] == [9]


def test_history_last_tool_follows_parallel_completion_order():
    slow = _event("first", "read_file", duration=90)
    fast = _event("second", "context_open", duration=18)
    restored = history_tool_results([_message(slow), _message(fast)], [fast, slow])
    assert [result["name"] for result in restored] == ["context_open", "read_file"]
