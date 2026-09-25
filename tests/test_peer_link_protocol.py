"""Two real Astra backends on one computer hand each other a task through the shared mailbox."""

import json
import os
import re
import threading
import time
from http.server import ThreadingHTTPServer

import pytest
from test_backend_task_protocol import (
    _SlowOpenAIHandler,
    _start_question_protocol_backend,
    _stop_protocol_backend,
)
from test_session_lifecycle_protocol import send

from agent.runtime.peer_link import PeerLink


def _tool_call(name: str, arguments: dict, call_id: str) -> tuple[dict, str]:
    return {"content": "", "tool_calls": [{"index": 0, "id": call_id, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments)}}]}, "tool_calls"


@pytest.fixture
def model():
    requests = []

    class Handler(_SlowOpenAIHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            if "messages" not in payload:
                self._json({"total_tokens": 100})
                return
            requests.append(payload)
            messages = payload["messages"]
            last = messages[-1]
            user = str(next(m for m in reversed(messages) if m["role"] == "user")["content"])
            if last["role"] == "tool":
                listed = re.findall(r'"peer_id":\s*"([^"]+:peer_b)"', str(last["content"]))
                if listed:
                    delta, finish = _tool_call("peer_send", {"to": listed[0], "text": "What is 2+2?"}, "send-1")
                else:
                    delta, finish = {"content": "Asked the other session."}, "stop"
            elif "task for you" in user:
                delta, finish = {"content": "4"}, "stop"
            elif "reply on your task" in user:
                delta, finish = {"content": "The other session says 4."}, "stop"
            elif "Ask the other session" in user:
                delta, finish = _tool_call("peer_list", {}, "list-1")
            else:
                delta, finish = {"content": "normal reply"}, "stop"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            chunk = {"id": "fixture", "object": "chat.completion.chunk", "model": "Qwen3.6-35B-A3B",
                     "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            try:
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode())
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_port, requests
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def start(tmp_path, port, name):
    home = tmp_path / name
    home.mkdir()
    return _start_question_protocol_backend(
        home, port, name,
        env_overrides={"ASTRA_CHANNEL_CONFIG": str(home / "no-channels.json"),
                       "ASTRA_CONTEXT_INDEX_EMBEDDING": "off", "ASTRA_TUI_RESTART": "1"},
    )


def test_two_backends_hand_a_task_over_and_back(tmp_path, model):
    port, requests = model
    directory = PeerLink(os.environ["ASTRA_PEER_DB"])
    a, _, seen_a, wait_a = start(tmp_path, port, "peer_a")
    b = None
    try:
        # Backends take the launcher's lease one at a time at startup.
        wait_a(lambda e: e.get("type") == "startup_banner", timeout=30)
        b, _, seen_b, wait_b = start(tmp_path, port, "peer_b")
        wait_b(lambda e: e.get("type") == "startup_banner", timeout=30)
        deadline = time.monotonic() + 15
        while len(directory.peers()) < 2 and time.monotonic() < deadline:
            time.sleep(0.2)
        assert sorted(p["peer_id"].split(":", 1)[1] for p in directory.peers()) == ["peer_a", "peer_b"]

        send(a, {"type": "message", "text": "Ask the other session what 2+2 is"})
        asked = wait_b(lambda e: e.get("type") == "peer_message" and e["direction"] == "in", timeout=20)
        assert (asked["text"], asked["state"]) == ("What is 2+2?", "submitted")
        # B answered in prose without reporting, so its answer closes the task.
        done = wait_b(lambda e: e.get("type") == "peer_message" and e["direction"] == "out", timeout=20)
        assert (done["state"], done["text"]) == ("completed", "4")
        back = wait_a(lambda e: e.get("type") == "peer_message" and e["direction"] == "in", timeout=20)
        assert (back["state"], back["text"]) == ("completed", "4")
        wait_a(lambda e: e.get("type") == "chunk" and e.get("content") == "The other session says 4.", timeout=20)
        wait_a(lambda e: e.get("type") == "done")
        time.sleep(2.5)  # nothing further: the closed task starts no more turns
        # A: peer_list, peer_send, its reply; B: the answer; A: reading the result.
        assert len(requests) == 5

        send(a, {"type": "command", "cmd": "/peers"})
        overview = wait_a(lambda e: e.get("type") == "tool_result" and e.get("name") == "peers")
        assert "This session:" in overview["output"] and "Open tasks:\n- none" in overview["output"]
        send(a, {"type": "command", "cmd": "/peers name 做PPT"})
        renamed = wait_a(lambda e: e.get("type") == "tool_result" and e.get("name") == "peers"
                         and "做PPT" in e.get("output", ""))
        assert renamed["output"] == "This session is now called 做PPT."
        assert any(p["name"] == "做PPT" for p in directory.peers())
        assert not [e for e in seen_a + seen_b if e.get("type") == "tool_approval_request"]
    finally:
        _stop_protocol_backend(a)
        if b is not None:
            _stop_protocol_backend(b)
    time.sleep(0.5)
    assert directory.peers() == []  # closed backends go offline
