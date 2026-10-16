"""Real backend IPC: /memory consolidation is a setting answered with a status, even during a reply."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from test_backend_task_protocol import _start_question_protocol_backend, _stop_protocol_backend


class HeldModel(BaseHTTPRequestHandler):
    """Streams one piece, then waits until the test lets the reply finish."""

    def log_message(self, *args):
        pass

    def do_GET(self):
        body = json.dumps({"object": "list", "data": [{"id": "Qwen3.6-35B-A3B"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for index, piece in enumerate(["好的，", None]):
            if piece is None:
                self.server.release.wait(10)
            delta = {"content": piece} if piece is not None else {}
            if index == 0:
                delta["role"] = "assistant"
            chunk = {"id": "chatcmpl-held", "object": "chat.completion.chunk", "created": int(time.time()),
                     "model": "Qwen3.6-35B-A3B",
                     "choices": [{"index": 0, "delta": delta, "finish_reason": "stop" if piece is None else None}]}
            self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def test_the_mode_is_switched_during_a_reply_without_entering_the_conversation(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRA_HOME", str(tmp_path / "state"))
    model = ThreadingHTTPServer(("127.0.0.1", 0), HeldModel)
    model.release = threading.Event()
    threading.Thread(target=model.serve_forever, daemon=True).start()
    proc, _, seen, wait = _start_question_protocol_backend(
        tmp_path, model.server_address[1], "consolidation", settings_overrides={"memory_consolidation": True})
    settings = tmp_path / "settings.json"

    def send(event: dict) -> None:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(event, ensure_ascii=False) + "\n")
        proc.stdin.flush()

    def status(event: dict) -> bool:
        return event.get("type") == "memory_consolidation_status"

    try:
        # A setting saved as on before the three modes reads as review.
        startup = wait(status)
        assert (startup["mode"], startup["pending"], startup["proposals"]) == ("review", 0, []) and "message" not in startup

        send({"type": "message", "text": "你好"})
        wait(lambda event: event.get("type") == "chunk")
        start = len(seen)
        send({"type": "command", "cmd": "/memory consolidation auto"})
        switched = wait(status)
        assert switched["mode"] == "auto" and switched["message"].startswith("Memory consolidation: auto")
        assert json.loads(settings.read_text(encoding="utf-8"))["memory_consolidation"] == "auto"
        refused = (send({"type": "command", "cmd": "/memory consolidation always"}), wait(status))[1]
        assert refused["error"].startswith("Usage: /memory consolidation") and refused["mode"] == "auto"
        # Answered while the reply runs: no conversation message, and the reply is not ended early.
        assert not [event for event in seen[start:] if event.get("type") in {"tool_result", "done"}]
        model.release.set()
        wait(lambda event: event.get("type") == "done")

        # Another window saved a different mode: this backend reports the saved one.
        data = json.loads(settings.read_text(encoding="utf-8"))
        settings.write_text(json.dumps({**data, "memory_consolidation": "off"}), encoding="utf-8")
        send({"type": "command", "cmd": "/memory consolidation status"})
        assert wait(status)["mode"] == "off"
        assert not [event for event in seen if event.get("type") == "error"]
    finally:
        model.release.set()
        _stop_protocol_backend(proc)
        model.shutdown()
        model.server_close()


def test_waiting_proposals_are_listed_and_accepting_one_updates_the_list(tmp_path, monkeypatch):
    from agent.runtime.memory import MemoryStore

    monkeypatch.setenv("ASTRA_HOME", str(tmp_path / "state"))
    store = MemoryStore(tmp_path / "memory.db", core_dir=tmp_path / "state" / "core")
    first, second = store.record_store.add_proposals([
        ({"action": "remember", "kind": "preference", "scope": "global", "summary": "前端项目用 pnpm",
          "content": "前端项目用 pnpm。"}, [{"session": "chat-1", "message_id": 1, "date": "2026-10-16", "text": "用 pnpm"}]),
        ({"action": "remember", "kind": "user_fact", "scope": "global", "summary": "车位在 B2 层 47 号",
          "content": "车位在 B2 层 47 号。"}, [{"session": "chat-1", "message_id": 2, "date": "2026-10-16", "text": "车位换了"}]),
    ], model="scripted")
    model = ThreadingHTTPServer(("127.0.0.1", 0), HeldModel)
    model.release = threading.Event()
    model.release.set()
    threading.Thread(target=model.serve_forever, daemon=True).start()
    proc, _, _, wait = _start_question_protocol_backend(tmp_path, model.server_address[1], "proposals")

    def status(event: dict) -> bool:
        return event.get("type") == "memory_consolidation_status"

    try:
        startup = wait(status)
        assert startup["pending"] == 2
        assert startup["proposals"] == [{"id": first, "action": "remember", "summary": "前端项目用 pnpm"},
                                        {"id": second, "action": "remember", "summary": "车位在 B2 层 47 号"}]
        assert proc.stdin is not None
        proc.stdin.write(json.dumps({"type": "command", "cmd": f"/memory accept {first}"}) + "\n")
        proc.stdin.flush()
        accepted = wait(lambda event: event.get("type") == "tool_result" and event.get("name") == "memory")
        assert accepted["output"].startswith("Saved #") and not accepted["error"]
        after = wait(status)
        assert after["pending"] == 1 and [item["id"] for item in after["proposals"]] == [second]
    finally:
        _stop_protocol_backend(proc)
        model.shutdown()
        model.server_close()
