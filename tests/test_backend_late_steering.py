"""Real backend protocol: a message sent while the final answer streams gets a turn of its own."""

import asyncio
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from agent.core.msg import ContentBlock, Msg
from agent.runtime.message_source import message_source_ref
from agent.runtime.react import ReActAgent
from agent.runtime.session_store import SessionStore
from agent.runtime.tools.registry import ToolDef, ToolRegistry
from test_backend_task_protocol import _start_question_protocol_backend, _stop_protocol_backend

STEERING = "[USER STEERING"


def _user_texts(messages: list[dict]) -> list[str]:
    """The user's own messages in a request, leaving out steering and other transient notes."""
    texts = []
    for message in messages:
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, list):
            content = "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
        if str(content).startswith("[SYSTEM-SUPPLIED TRANSIENT INSTRUCTION]"):
            continue
        # The request shows each message with the time it was sent.
        texts.append(re.sub(r"^<message_time>[^<]*</message_time>\n", "", str(content)))
    return texts


class Provider(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def _json(self, payload: dict):
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _chunk(self, delta: dict, finish: str | None = None):
        chunk = {"id": "late-steering", "object": "chat.completion.chunk", "created": int(time.time()),
                 "model": "Qwen3.6-35B-A3B", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
        self.wfile.flush()

    def do_GET(self):
        self._json({"object": "list", "data": [{"id": "Qwen3.6-35B-A3B"}]})

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
        messages = payload.get("messages") or []
        if not payload.get("stream"):
            self._json({"id": "aux", "object": "chat.completion", "created": int(time.time()), "model": "Qwen3.6-35B-A3B",
                        "choices": [{"index": 0, "message": {"role": "assistant", "content": "{}"}, "finish_reason": "stop"}]})
            return
        server = self.server
        server.requests.append(messages)  # type: ignore[attr-defined]
        last = (_user_texts(messages) or [""])[-1]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
            if "first question" in last:
                # The final answer streams, then the provider holds its end.
                self._chunk({"role": "assistant", "content": "first answer"})
                server.release.wait(10)  # type: ignore[attr-defined]
                self._chunk({}, "stop")
            elif "tool question" in last and not any(m.get("role") == "tool" for m in messages):
                server.tool_step.set()  # type: ignore[attr-defined]
                server.steered.wait(10)  # type: ignore[attr-defined]
                self._chunk({"role": "assistant", "tool_calls": [{"index": 0, "id": "read-note", "type": "function",
                             "function": {"name": "read_file", "arguments": json.dumps({"path": "note.txt"})}}]})
                self._chunk({}, "tool_calls")
            else:
                self._chunk({"role": "assistant", "content": f"answer to {last}"})
                self._chunk({}, "stop")
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


def _start(tmp_path: Path, monkeypatch, name: str):
    monkeypatch.setenv("ASTRA_HOME", str(tmp_path / "state"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "note.txt").write_text("fixture note\n", encoding="utf-8")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    server.requests = []  # type: ignore[attr-defined]
    for event in ("release", "tool_step", "steered"):
        setattr(server, event, threading.Event())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    proc, _, seen, wait = _start_question_protocol_backend(
        tmp_path, server.server_port, name, workdir=workspace,
        env_overrides={"ASTRA_UI_SURFACE": "gui", "ASTRA_GUI_DISABLE_APPSHOT": "1"},
    )

    def send(text: str, sid: str):
        assert proc.stdin is not None
        proc.stdin.write(json.dumps({"type": "message", "text": text, "submission_id": sid}) + "\n")
        proc.stdin.flush()

    def command(cmd: str):
        assert proc.stdin is not None
        proc.stdin.write(json.dumps({"type": "command", "cmd": cmd}) + "\n")
        proc.stdin.flush()

    wait(lambda event: event.get("type") == "gui_ready")
    return server, proc, seen, wait, send, command


def _stop(server, proc):
    server.release.set()
    server.steered.set()
    _stop_protocol_backend(proc)
    server.shutdown()
    server.server_close()


def _turn_requests(server) -> list[list[str]]:
    return [_user_texts(messages) for messages in server.requests]


def test_messages_sent_while_the_final_answer_streams_get_their_own_turns(tmp_path, monkeypatch):
    server, proc, seen, wait, send, _ = _start(tmp_path, monkeypatch, "late-steering")
    try:
        send("first question", "first")
        wait(lambda e: e.get("type") == "chunk" and "first answer" in e.get("content", ""))
        boundary = len(seen)
        send("late one", "late-1")
        send("late two", "late-2")
        for sid in ("late-1", "late-2"):
            wait(lambda e, sid=sid: e.get("type") == "message_accepted" and e.get("submission_id") == sid)
        assert [e["text"] for e in seen[boundary:] if e.get("type") == "steering"] == ["late one", "late two"]
        server.release.set()

        sources = {}
        for sid in ("late-1", "late-2"):
            sources[sid] = wait(lambda e, sid=sid: e.get("type") == "message_source" and e.get("submission_id") == sid)
            wait(lambda e: e.get("type") == "done")
        events = seen[boundary:]
        assert [e["task"]["status"] for e in events if e.get("type") == "task_started"] == ["running", "running"]
        assert [e for e in events if e.get("type") in {"error", "message_rejected"}] == []
        answers = "".join(e.get("content", "") for e in events if e.get("type") == "chunk")
        assert answers.index("answer to late one") < answers.index("answer to late two")

        saved = SessionStore(tmp_path / "sessions" / "late-steering.json").load(readonly=True)["messages"]
        for sid, text in (("late-1", "late one"), ("late-2", "late two")):
            ref = sources[sid]["source_ref"]
            assert message_source_ref(saved[ref["index"]], ref["index"]) == ref
            assert (saved[ref["index"]]["role"], saved[ref["index"]]["content"]) == ("user", text)
            assert saved[ref["index"] + 1]["role"] == "assistant"
            assert saved[ref["index"] + 1]["content"] == f"answer to {text}"

        send("third question", "third")
        wait(lambda e: e.get("type") == "message_source" and e.get("submission_id") == "third")
        wait(lambda e: e.get("type") == "done")
        # Each message was asked once, as a saved user message; nothing came back as steering.
        assert _turn_requests(server) == [
            ["first question"],
            ["first question", "late one"],
            ["first question", "late one", "late two"],
            ["first question", "late one", "late two", "third question"],
        ]
        assert not any(STEERING in json.dumps(messages) for messages in server.requests)
    finally:
        _stop(server, proc)


def test_stopping_the_reply_drops_the_late_message_and_says_so(tmp_path, monkeypatch):
    server, proc, seen, wait, send, command = _start(tmp_path, monkeypatch, "late-steering-cancel")
    try:
        send("first question", "first")
        wait(lambda e: e.get("type") == "chunk" and "first answer" in e.get("content", ""))
        boundary = len(seen)
        send("late one", "late-1")
        wait(lambda e: e.get("type") == "steering")
        command("/cancel")
        rejected = wait(lambda e: e.get("type") == "message_rejected" and e.get("submission_id") == "late-1")
        assert rejected["reason"] and rejected["retryable"] is True
        assert [e["task"]["status"] for e in seen[boundary:] if e.get("type") == "task_status"] == ["cancelled"]
        server.release.set()

        send("third question", "third")
        wait(lambda e: e.get("type") == "message_source" and e.get("submission_id") == "third")
        wait(lambda e: e.get("type") == "done")
        assert len([e for e in seen[boundary:] if e.get("type") == "task_started"]) == 1
        assert _turn_requests(server)[-1][-1] == "third question"
        assert not any("late one" in json.dumps(messages) for messages in server.requests)
    finally:
        _stop(server, proc)


def test_message_sent_during_a_tool_step_still_steers_that_turn(tmp_path, monkeypatch):
    server, proc, seen, wait, send, _ = _start(tmp_path, monkeypatch, "mid-run-steering")
    try:
        send("tool question", "tool")
        assert server.tool_step.wait(10)
        send("mid-run note", "mid")
        wait(lambda e: e.get("type") == "steering" and e.get("text") == "mid-run note")
        server.steered.set()
        wait(lambda e: e.get("type") == "tool_result" and e.get("name") == "read_file")
        wait(lambda e: e.get("type") == "done")
        send("third question", "third")
        wait(lambda e: e.get("type") == "message_source" and e.get("submission_id") == "third")
        wait(lambda e: e.get("type") == "done")

        assert len([e for e in seen if e.get("type") == "task_started"]) == 2
        steered = [STEERING in json.dumps(messages) and "mid-run note" in json.dumps(messages)
                   for messages in server.requests]
        assert steered == [False, True, False]
        assert _turn_requests(server)[-1] == ["tool question", "third question"]
    finally:
        _stop(server, proc)


def test_steering_reaches_only_the_turn_it_was_sent_into(tmp_path):
    prompts: list[str] = []

    class Model:
        async def chat_stream(self, messages, tools):
            prompts.append(json.dumps(messages, ensure_ascii=False))
            if len(prompts) == 1:
                assert agent.queue_steering("for this turn", request_id="turn-a")
                # A channel turn borrows the same agent; its steering is not this turn's.
                assert agent.queue_steering("for another turn", request_id="turn-b")
                yield {"type": "tool_calls", "content": "", "calls": [{"id": "read", "name": "read_result", "arguments": "{}"}]}
            else:
                assert not agent.queue_steering("for this turn", request_id="turn-a")  # a repeat in the same turn
                assert agent.queue_steering("too late", request_id="turn-a")
                yield {"type": "chunk", "content": "Done."}
                yield {"type": "done", "content": "Done."}

    async def read():
        return "result"

    registry = ToolRegistry()
    registry.register(ToolDef("read_result", "Read result", {"type": "object"}, read))
    agent = ReActAgent("test", Model(), registry, max_iterations=3, timing_log_enabled=False)
    agent.context.set_session(str(tmp_path / "session.json"))

    async def turn(text: str, request_id: str):
        async for _ in agent.reply_stream(Msg(content=[ContentBlock.text(text)], metadata={"request_id": request_id})):
            pass

    asyncio.run(turn("work", "turn-a"))
    assert [STEERING in prompt for prompt in prompts] == [False, True]
    assert "for this turn" in prompts[1] and "for another turn" not in prompts[1]
    assert agent.take_undelivered_steering("turn-a") == ["too late"]
    assert agent.take_undelivered_steering("turn-a") == []
    assert agent.take_undelivered_steering("turn-b") == ["for another turn"]
    # The next turn starts with no steering left over, and repeats are accepted again.
    assert agent.queue_steering("for this turn", request_id="turn-a")
    assert agent.take_undelivered_steering("turn-a") == ["for this turn"]
