"""Exercise conversation commands through the real JSON backend, with a local LLM."""

from __future__ import annotations

import json
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from agent.cli import backend
from agent.runtime.context import AgentContext
from agent.runtime.session_store import SessionStore
from agent.runtime.task_store import TaskStore
from test_backend_task_protocol import (
    ThreadingHTTPServer,
    _ApprovalOpenAIHandler,
    _SlowOpenAIHandler,
    _start_question_protocol_backend,
    _stop_protocol_backend,
)


@pytest.fixture
def protocol(tmp_path, request, monkeypatch):
    if getattr(request, "param", "") != "public":
        monkeypatch.setenv("ASTRA_LOCAL_MODE_FILE", str(Path(__file__).parent / "fixtures/local_mode.py"))
    if getattr(request, "param", "") == "no-task-db":
        (tmp_path / "tasks.db").mkdir()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ApprovalOpenAIHandler)
    server.call_number = 1  # Plain replies only, never execute a tool.
    threading.Thread(target=server.serve_forever, daemon=True).start()
    proc = None
    try:
        proc, _, seen, wait_for = _start_question_protocol_backend(tmp_path, server.server_port, "work")

        def send(payload):
            proc.stdin.write(json.dumps(payload) + "\n")
            proc.stdin.flush()

        # Cold imports/startup may exceed the ordinary command-event budget on
        # busy Windows runners. Keep startup bounded and clean up even on failure.
        wait_for(lambda e: e.get("type") == "model_info", timeout=30)
        yield SimpleNamespace(send=send, wait=wait_for, seen=seen, server=server)
    finally:
        try:
            if proc is not None:
                _stop_protocol_backend(proc)
        finally:
            server.shutdown()
            server.server_close()


@pytest.mark.parametrize("failure_phase", ["spawn", "ready"])
def test_protocol_setup_failure_cleans_up_owned_resources(tmp_path, monkeypatch, failure_phase):
    cleaned = []
    proc = object()
    server = SimpleNamespace(
        server_port=12345,
        serve_forever=lambda: None,
        shutdown=lambda: cleaned.append("server_shutdown"),
        server_close=lambda: cleaned.append("server_close"),
    )

    def failed_ready(*_args, **_kwargs):
        raise AssertionError("fixture startup failed")

    def start(*_args, **_kwargs):
        if failure_phase == "spawn":
            raise AssertionError("fixture startup failed")
        return proc, None, [], failed_ready

    def stop(actual):
        assert actual is proc
        cleaned.append("backend_stop")

    monkeypatch.setattr(f"{__name__}.ThreadingHTTPServer", lambda *_args: server)
    monkeypatch.setattr(f"{__name__}._start_question_protocol_backend", start)
    monkeypatch.setattr(f"{__name__}._stop_protocol_backend", stop)
    setup = protocol.__wrapped__(tmp_path, SimpleNamespace(param="public"), monkeypatch)
    with pytest.raises(AssertionError, match="fixture startup failed"):
        next(setup)
    assert cleaned == (["backend_stop"] if failure_phase == "ready" else []) + ["server_shutdown", "server_close"]


def _command(protocol, command):
    protocol.send({"type": "command", "cmd": command})
    protocol.wait(lambda e: e.get("type") == "done")


def _completed_turn(protocol):
    start = protocol.wait(lambda e: e.get("type") == "task_started")
    protocol.wait(lambda e: e.get("type") == "done")
    protocol.wait(lambda e: e.get("type") == "task_status" and e["task"]["id"] == start["task"]["id"])
    protocol.wait(lambda e: e.get("type") == "startup_banner")
    return start["task"]["id"]


def test_fixture_turns_and_retry_stay_out_of_shared_tasks(protocol, tmp_path):
    _command(protocol, "/fixture private_test")
    protocol.send({"type": "message", "text": "PRIVATE_LOCAL_SENTINEL"})
    _completed_turn(protocol)
    store = TaskStore(tmp_path / "tasks.db")
    assert store.list_tasks() == []

    for command in ("/retry", "/fixture retry"):
        protocol.send({"type": "command", "cmd": command})
        _completed_turn(protocol)
        assert store.list_tasks() == []
    fixture = SessionStore(tmp_path / "sessions/fixture/private_test.json").load()
    assert [m["content"] for m in fixture["messages"] if m["role"] == "user"] == ["PRIVATE_LOCAL_SENTINEL"]

    _command(protocol, "/fixture leave")
    protocol.send({"type": "message", "text": "public work request"})
    task_id = _completed_turn(protocol)
    assert store.get_task(task_id)["input_text"] == "public work request"
    assert len(store.list_tasks()) == 1


def test_fixture_continue_ignores_old_durable_checkpoints(protocol, tmp_path):
    _command(protocol, "/fixture private_test")
    store = TaskStore(tmp_path / "tasks.db")
    old = store.start_run("legacy-fixture", "LEGACY_TASK_TEXT", session_id="private_test")
    store.checkpoint(old["id"], {"resume_kind": "iteration_budget", "phase": "after_llm"})
    store.finish_run(old["id"], "interrupted")
    before = store.get_task(old["id"])

    protocol.send({"type": "message", "text": "continue"})
    _completed_turn(protocol)

    assert store.get_task(old["id"]) == before
    assert len(store.list_tasks()) == 1
    fixture = SessionStore(tmp_path / "sessions/fixture/private_test.json").load()
    assert fixture["messages"][0]["content"] == "continue"


@pytest.mark.parametrize("command", ["/retry", "/fixture retry"])
def test_retry_has_one_continuous_start_to_done_lifecycle(protocol, command):
    if command.startswith("/fixture"):
        _command(protocol, "/fixture new")
    protocol.send({"type": "message", "text": "short request"})
    _completed_turn(protocol)
    boundary = len(protocol.seen)

    protocol.send({"type": "command", "cmd": command})
    _completed_turn(protocol)

    turn = protocol.seen[boundary:]
    types = [event["type"] for event in turn]
    assert types.index("task_started") < types.index("done")
    assert types.count("task_started") == types.count("done") == 1


@pytest.mark.parametrize("command", ["/retry", "/fixture retry"])
def test_empty_retry_finishes_without_starting_a_turn(protocol, command):
    if command.startswith("/fixture"):
        _command(protocol, "/fixture new")
    boundary = len(protocol.seen)
    _command(protocol, command)
    turn = protocol.seen[boundary:]
    assert any(e.get("error") == "No request to retry." for e in turn)
    assert not any(e["type"] == "task_started" for e in turn)


def test_fixture_stream_is_cancellable_without_durable_task(protocol, tmp_path):
    _command(protocol, "/fixture new")
    protocol.server.RequestHandlerClass = _SlowOpenAIHandler
    protocol.send({"type": "message", "text": "PRIVATE_CANCEL_SENTINEL"})
    start = protocol.wait(lambda e: e.get("type") == "task_started")
    protocol.wait(lambda e: e.get("type") == "chunk")
    protocol.send({"type": "command", "cmd": "/cancel"})
    terminal = protocol.wait(lambda e: e.get("type") == "task_status" and e["task"]["status"] == "cancelled")
    assert terminal["task"]["id"] == start["task"]["id"]
    assert TaskStore(tmp_path / "tasks.db").list_tasks() == []


@pytest.mark.parametrize("protocol", ["no-task-db"], indirect=True)
def test_active_turn_can_cancel_when_task_database_is_unavailable(protocol):
    protocol.server.RequestHandlerClass = _SlowOpenAIHandler
    protocol.send({"type": "message", "text": "cancellable without persistence"})
    start = protocol.wait(lambda e: e.get("type") == "task_started")
    protocol.wait(lambda e: e.get("type") == "chunk")
    protocol.send({"type": "command", "cmd": "/cancel"})
    terminal = protocol.wait(lambda e: e.get("type") == "task_status" and e["task"]["status"] == "cancelled")
    assert terminal["task"]["id"] == start["task"]["id"]


@pytest.mark.parametrize("command", ["/retry", "/fixture retry"])
def test_busy_retry_rejection_does_not_end_the_running_turn(protocol, command):
    if command.startswith("/fixture"):
        _command(protocol, "/fixture new")
    protocol.server.RequestHandlerClass = _SlowOpenAIHandler
    protocol.send({"type": "message", "text": "still running"})
    protocol.wait(lambda e: e.get("type") == "chunk")
    boundary = len(protocol.seen)
    protocol.send({"type": "command", "cmd": command})
    protocol.wait(lambda e: e.get("type") == "tool_result" and "Finish or cancel" in e.get("error", ""))
    protocol.send({"type": "command", "cmd": "/cancel"})
    protocol.wait(lambda e: e.get("type") == "task_status" and e["task"]["status"] == "cancelled")
    assert [e["type"] for e in protocol.seen[boundary:]].count("done") == 1


def test_failed_fixture_leave_reports_error_and_keeps_backend_usable(protocol, tmp_path):
    _command(protocol, "/fixture private_test")
    protocol.send({"type": "message", "text": "keep this draft"})
    _completed_turn(protocol)
    # Fail the leave's save in place by occupying the header's temporary name.
    # The session directory cannot be moved aside: the open session holds its
    # writer lease inside it, and Windows refuses to rename a directory while
    # a file beneath it is open.
    blocker = tmp_path / "sessions/fixture/private_test.header.tmp"
    blocker.mkdir()
    try:
        boundary = len(protocol.seen)
        _command(protocol, "/fixture leave")
        turn = protocol.seen[boundary:]
        assert any(e.get("type") == "mode_info" and e.get("mode") == "local" for e in turn)
        assert any(e.get("type") == "tool_result" and e.get("error") for e in turn)
    finally:
        blocker.rmdir()
    _command(protocol, "/fixture leave")
    assert protocol.seen[-1]["type"] == "done"


def test_fixture_context_is_not_exported_to_work_handoff(monkeypatch):
    context = AgentContext("fixture")
    context.add_user("PRIVATE_HANDOFF_SENTINEL")
    agent = SimpleNamespace(local_mode=True, context=context, llm=SimpleNamespace(config=SimpleNamespace(model="test")))
    saved = []
    monkeypatch.setattr(backend, "save_handoff", lambda text, **kwargs: saved.append(text))

    assert backend._save_handoff(agent, None) is None
    assert saved == []


@pytest.mark.parametrize("protocol", ["public"], indirect=True)
def test_public_backend_has_no_local_mode_command(protocol):
    info = protocol.wait(lambda e: e.get("type") == "local_mode_info")
    assert info["definition"] is None and info["sessions"] == []
    _command(protocol, "/fixture")
    assert not any(e.get("type") == "mode_info" and e.get("mode") == "local" for e in protocol.seen)
