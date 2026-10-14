import json

from test_backend_task_protocol import _start_question_protocol_backend, _stop_protocol_backend

from agent.cli.session_lifecycle import RESTART_EXIT_CODE
from agent.runtime.session_store import SessionStore


def send(proc, value):
    proc.stdin.write(json.dumps(value) + "\n")
    proc.stdin.flush()


def start(tmp_path, *, sessions_dir=None):
    return _start_question_protocol_backend(
        tmp_path, 9, "lifecycle_session", env_overrides={
            "ASTRA_TUI_RESTART": "1",
            "ASTRA_CHANNEL_CONFIG": str(tmp_path / "no-channels.json"),
            "ASTRA_CONTEXT_INDEX_EMBEDDING": "off",
            **({"AGENT_SESSION_DIR": str(sessions_dir)} if sessions_dir is not None else {}),
        },
    )


def test_real_backend_exits_only_after_ready_receipt_and_saves_session(tmp_path):
    proc, _, _, wait_for = start(tmp_path)
    try:
        wait_for(lambda e: e.get("type") == "startup_banner", timeout=30)
        send(proc, {"type": "command", "cmd": "/restart"})
        ready = wait_for(lambda e: e.get("type") == "restart_ready")
        assert ready["session"] == "lifecycle_session"
        assert ready["replayable"] is False
        send(proc, {"type": "restart_ack", "request_id": "stale"})
        send(proc, {"type": "command", "cmd": "/session other"})
        rejected = wait_for(lambda e: e.get("type") == "restart_status" and "changing" in e.get("message", ""))
        assert rejected["state"] == "awaiting_ack"
        assert proc.poll() is None
        send(proc, {"type": "restart_ack", "request_id": ready["request_id"]})
        assert proc.wait(timeout=15) == RESTART_EXIT_CODE, proc.stderr.read()
        store = SessionStore(tmp_path / "sessions/lifecycle_session.json")
        assert store.exists
        assert store.lifecycle_events()[-1]["type"] == "session_ended"
        assert store.recover_interrupted() is None
        assert not SessionStore(tmp_path / "sessions/other.json").exists
    finally:
        _stop_protocol_backend(proc)


def test_late_save_cannot_reopen_a_closed_lifecycle(tmp_path):
    store = SessionStore(tmp_path / "session.json")
    data = {"messages": [{"role": "user", "content": "hello"}]}
    store.save(data)
    store.record_end("shutdown")
    data["messages"].append({"role": "assistant", "content": "saved late"})
    store.save(data, append_from=1)
    assert [e["type"] for e in store.lifecycle_events()] == ["session_started", "session_ended"]
    assert store.load()["messages"] == data["messages"]
    store.record_end("shutdown")
    store.begin()
    store.save(data, append_from=2)
    events = store.lifecycle_events()
    assert [e["type"] for e in events] == ["session_started", "session_ended", "session_started"]
    assert events[0]["run_id"] != events[2]["run_id"]


def test_real_backend_cancel_restores_command_admission(tmp_path):
    proc, _, _, wait_for = start(tmp_path)
    try:
        wait_for(lambda e: e.get("type") == "startup_banner", timeout=30)
        send(proc, {"type": "command", "cmd": "/restart"})
        ready = wait_for(lambda e: e.get("type") == "restart_ready")
        send(proc, {"type": "command", "cmd": "/restart cancel"})
        wait_for(lambda e: e.get("type") == "restart_status" and e.get("state") == "cancelled")
        send(proc, {"type": "restart_ack", "request_id": ready["request_id"]})
        send(proc, {"type": "command", "cmd": "/tasks"})
        wait_for(lambda e: e.get("type") == "tool_result" and e.get("name") == "tasks")
        assert proc.poll() is None
    finally:
        _stop_protocol_backend(proc)


def test_real_backend_rejected_branch_rename_keeps_command_loop_alive(tmp_path):
    from agent.runtime.conversation_branches import ConversationBranches
    from agent.runtime.message_source import message_source_ref

    # Publish fixture data before a real backend can index it. This test covers
    # rename rejection, not concurrent external writes to the branch manifest.
    path = tmp_path / "prepared-sessions" / "branched.json"
    messages = [{"role": "user", "content": "question"},
                {"role": "assistant", "content": "original answer"}]
    SessionStore(path).save({"messages": messages})
    branches = ConversationBranches(path)
    ticket = branches.prepare_retry(message_source_ref(messages[-1], 1), branch_id="main", expected_revision=0)
    alternate = ticket["seed"]
    alternate["messages"].append({"role": "assistant", "content": "alternate answer"})
    SessionStore(path, branch_id=ticket["candidate_branch_id"]).save(alternate)
    branches.finish_candidate(ticket["candidate_branch_id"])
    before = {file: file.read_bytes() for file in SessionStore(path).related_paths() if file.is_file()}

    proc, _, _, wait_for = start(tmp_path, sessions_dir=path.parent)
    try:
        wait_for(lambda event: event.get("type") == "startup_banner", timeout=30)
        send(proc, {"type": "command", "cmd": "/session rename branched renamed"})
        rejected = wait_for(lambda event: event.get("type") == "tool_result" and event.get("name") == "session")
        assert "reply versions" in rejected["error"] and not rejected["output"]
        wait_for(lambda event: event.get("type") == "done")
        send(proc, {"type": "command", "cmd": "/tasks"})
        wait_for(lambda event: event.get("type") == "tool_result" and event.get("name") == "tasks")
        assert proc.poll() is None
        assert {file: file.read_bytes() for file in before} == before
        assert not SessionStore(path.with_name("renamed.json")).exists
    finally:
        _stop_protocol_backend(proc)
