"""Guidance commands through a real backend process and HTTP model adapter."""

import copy
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from test_backend_task_protocol import _start_question_protocol_backend, _stop_protocol_backend


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def reply(self, value):
        content = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        self.reply({"data": [{"id": "Qwen3.6-35B-A3B"}]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if "messages" not in body:
            self.reply({"data": []})
            return
        self.server.requests.append(copy.deepcopy(body))
        index = len(self.server.requests)
        if self.server.hold:
            self.server.started.set()
            self.server.release.wait(10)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for chunk in (
            {"choices": [{"index": 0, "delta": {"content": f"ACK-{index}"}, "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ):
            self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def test_backend_preserves_prefix_and_applies_only_at_an_idle_boundary(tmp_path):
    work = tmp_path / "project"
    (work / ".git").mkdir(parents=True)
    guidance = work / "AGENTS.md"
    guidance.write_text("BACKEND_GUIDANCE_V1", encoding="utf-8")
    state = tmp_path / "state"
    state.mkdir()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.requests = []
    server.hold = False
    server.started = threading.Event()
    server.release = threading.Event()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    proc, _events, seen, wait = _start_question_protocol_backend(
        state, server.server_port, "guidance", workdir=work,
        env_overrides={"ASTRA_HOME": str(state / "home"), "ASTRA_PROJECT_TRUST": "trusted"},
    )

    def send(message):
        proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()

    def turn(text, index):
        send({"type": "message", "text": text})
        wait(lambda event: event.get("type") == "chunk" and event.get("content") == f"ACK-{index}")
        wait(lambda event: event.get("type") == "done")

    try:
        wait(lambda event: event.get("type") == "startup_banner")
        turn("first request", 1)
        first = copy.deepcopy(server.requests[0])
        assert "BACKEND_GUIDANCE_V1" in first["messages"][0]["content"]
        send({"type": "command", "cmd": '/jobs add fixture --after 60 --reminder --prompt "fixture reminder"'})
        job = wait(lambda event: event.get("type") == "tool_result" and event.get("name") == "jobs")
        assert not job["error"]
        saved_job = json.loads(job["output"])
        assert saved_job["spec"]["workdir"] == str(work.resolve())
        wait(lambda event: event.get("type") == "done")
        guidance.write_text("BACKEND_GUIDANCE_V2", encoding="utf-8")
        send({"type": "command", "cmd": '/skills create --template pending-skill "Pending skill description"'})
        saved = wait(lambda event: event.get("type") == "tool_result" and event.get("name") == "skills")
        assert not saved["error"] and "new conversation" in saved["output"]
        wait(lambda event: event.get("type") == "done")
        turn("second request", 2)
        second = server.requests[1]
        assert second["messages"][:len(first["messages"])] == first["messages"]
        assert second["tools"] == first["tools"]
        assert "pending-skill" not in second["messages"][0]["content"]
        send({"type": "command", "cmd": "/guidance"})
        status = wait(lambda event: event.get("type") == "tool_result" and event.get("name") == "guidance")
        assert "project, skills" in status["output"]
        wait(lambda event: event.get("type") == "done")
        send({"type": "command", "cmd": "/guidance refresh --now"})
        applied = wait(lambda event: event.get("type") == "tool_result" and event.get("name") == "guidance")
        assert not applied["error"] and "Applied saved guidance" in applied["output"]
        wait(lambda event: event.get("type") == "done")
        turn("third request", 3)
        assert "BACKEND_GUIDANCE_V2" in server.requests[2]["messages"][0]["content"]
        assert "pending-skill" in server.requests[2]["messages"][0]["content"]

        server.hold = True
        send({"type": "message", "text": "held request"})
        wait(lambda event: event.get("type") == "task_started")
        assert server.started.wait(3)
        seen_before = len(seen)
        send({"type": "command", "cmd": "/jobs add --help"})
        job_help = wait(lambda event: event.get("type") == "tool_result" and event.get("name") == "jobs")
        assert not job_help["error"] and "--request" in job_help["output"]
        send({"type": "command", "cmd": '/skills create --template blocked-skill "Blocked" --now'})
        blocked = wait(lambda event: event.get("type") == "tool_result" and event.get("name") == "skills")
        assert "active task" in blocked["error"]
        assert not (state / "skills" / "user" / "blocked-skill").exists()
        send({"type": "command", "cmd": "/guidance refresh --now"})
        blocked = wait(lambda event: event.get("type") == "tool_result" and event.get("name") == "guidance")
        assert "active task" in blocked["error"]
        assert not any(event.get("type") == "done" for event in seen[seen_before:])
        server.hold = False
        server.release.set()
        wait(lambda event: event.get("type") == "chunk" and event.get("content") == "ACK-4")
        wait(lambda event: event.get("type") == "done")
    finally:
        server.release.set()
        _stop_protocol_backend(proc)
        server.shutdown()
        server.server_close()
