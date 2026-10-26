"""The personal thread hands project work to a conversation it asks the desktop to open, and hears back.

Two real backends: the personal thread (as the desktop's background host runs it) and the conversation the
desktop opens in a project for it. The test plays the desktop between them (specs/personal-thread.md D6).
"""

import json
import os
import threading
import time
from http.server import ThreadingHTTPServer

import pytest
from test_backend_task_protocol import _SlowOpenAIHandler, _start_question_protocol_backend, _stop_protocol_backend
from test_session_lifecycle_protocol import send

from agent.runtime.peer_link import PERSONAL_PEER_NAME, TAKEN_OVER, PeerLink


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
                delta, finish = {"content": f"TOOL_SAID {last['content'][:300]}"}, "stop"
            elif "task for you" in user:
                delta, finish = {"content": "两个测试超时了，其余通过。"}, "stop"
            elif "reply on your task" in user:
                delta, finish = {"content": "那边查完了：两个测试超时了。"}, "stop"
            elif "查一下" in user:
                where = "nowhere" if "不存在" in user else "proj"
                delta = {"content": "", "tool_calls": [{"index": 0, "id": f"send-{len(requests)}", "type": "function",
                         "function": {"name": "peer_send", "arguments": json.dumps(
                             {"workspace": where, "text": "查一下为什么测试失败"})}}]}
                finish = "tool_calls"
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


def start(home, port, name, *, personal=False, workdir=None):
    home.mkdir(exist_ok=True)
    env = {"ASTRA_CHANNEL_CONFIG": str(home / "no-channels.json"), "ASTRA_CONTEXT_INDEX_EMBEDDING": "off",
           "ASTRA_TUI_RESTART": "1"}
    if personal:
        env["ASTRA_PERSONAL_THREAD"] = "1"
    return _start_question_protocol_backend(home, port, name, env_overrides=env, workdir=workdir)


def test_the_personal_thread_opens_a_conversation_in_a_project_and_hears_its_result(tmp_path, model):
    port, requests = model
    directory = PeerLink(os.environ["ASTRA_PEER_DB"])
    project = tmp_path / "proj"
    project.mkdir()
    thread, _, seen_thread, wait_thread = start(tmp_path / "thread", port, "personal", personal=True)
    worker = None
    try:
        wait_thread(lambda e: e.get("type") == "startup_banner", timeout=30)
        deadline = time.monotonic() + 15
        while not directory.peers() and time.monotonic() < deadline:
            time.sleep(0.2)
        assert [p["name"] for p in directory.peers()] == [PERSONAL_PEER_NAME]

        # A folder the desktop does not know: the thread is told so, and nothing opens.
        send(thread, {"type": "message", "text": "在不存在的项目里查一下"})
        asked = wait_thread(lambda e: e.get("type") == "open_conversation_request", timeout=20)
        assert asked["where"] == "nowhere"
        send(thread, {"type": "open_conversation_response", "request_id": asked["request_id"],
                      "error": "No project called nowhere. Projects the desktop knows: proj."})
        wait_thread(lambda e: e.get("type") == "chunk" and "No project called nowhere" in e.get("content", ""), timeout=20)
        wait_thread(lambda e: e.get("type") == "done")

        send(thread, {"type": "message", "text": "在 proj 里查一下测试为什么失败"})
        asked = wait_thread(lambda e: e.get("type") == "open_conversation_request" and e["request_id"] != asked["request_id"],
                            timeout=20)
        assert asked["where"] == "proj"
        # The desktop opens a conversation there; its backend joins the mailbox and is handed the task.
        worker, _, seen_worker, wait_worker = start(tmp_path / "worker", port, "session_worker", workdir=project)
        send(thread, {"type": "open_conversation_response", "request_id": asked["request_id"],
                      "session": "session_worker", "workspace": str(project)})
        given = wait_worker(lambda e: e.get("type") == "peer_message" and e["direction"] == "in", timeout=40)
        assert (given["peer"], given["state"], given["text"]) == (PERSONAL_PEER_NAME, "submitted", "查一下为什么测试失败")
        # While it works on it, the window there knows who manages the task.
        managed = wait_worker(lambda e: e.get("type") == "peer_tasks" and e["tasks"], timeout=20)
        assert managed["tasks"][0]["role"] == "assignee" and managed["tasks"][0]["with"] == PERSONAL_PEER_NAME
        done = wait_worker(lambda e: e.get("type") == "peer_message" and e["direction"] == "out", timeout=30)
        assert (done["state"], done["text"]) == ("completed", "两个测试超时了，其余通过。")
        assert wait_worker(lambda e: e.get("type") == "peer_tasks" and not e["tasks"], timeout=20)["tasks"] == []

        # The thread hears the result as a new turn and tells the person.
        back = wait_thread(lambda e: e.get("type") == "peer_message" and e["direction"] == "in", timeout=30)
        assert (back["state"], back["text"]) == ("completed", "两个测试超时了，其余通过。")
        wait_thread(lambda e: e.get("type") == "chunk" and e.get("content") == "那边查完了：两个测试超时了。", timeout=30)
        # Nobody else may open conversations: the worker is told so if it tries (no desktop request leaves it).
        assert not [e for e in seen_worker if e.get("type") == "open_conversation_request"]
        assert not [e for e in seen_thread + seen_worker if e.get("type") == "tool_approval_request"]
    finally:
        _stop_protocol_backend(thread)
        if worker is not None:
            _stop_protocol_backend(worker)


def test_taking_a_task_over_closes_it_for_the_thread(tmp_path):
    path = tmp_path / "peers.db"
    thread, worker = PeerLink(path, site="mac"), PeerLink(path, site="mac")
    thread.join("personal", PERSONAL_PEER_NAME)
    worker.join("session_worker", "查测试")
    task = thread.send("查一下为什么测试失败", to="查测试")
    worker.claim_inbox()
    taken = worker.take_over()
    assert taken == [{"task_id": task["task_id"], "to": PERSONAL_PEER_NAME, "state": "canceled"}]
    [heard] = thread.claim_inbox()
    assert (heard["state"], heard["body"]) == ("canceled", TAKEN_OVER)
    assert thread.tasks() == [] and worker.tasks() == []
    # Closed: the thread can send nothing more on it, and there is nothing left to take over.
    with pytest.raises(ValueError, match="already canceled"):
        thread.send("还有一件事", task_id=task["task_id"])
    assert worker.take_over() == []
    with pytest.raises(ValueError, match="No open task"):
        worker.take_over("mac:t-missing")
