"""Real backend IPC: a reply is read aloud through the speech endpoint and leaves the conversation untouched."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from test_backend_task_protocol import _start_question_protocol_backend, _stop_protocol_backend
from test_speech_service import serve_speech

REPLY = "第一句先说。\n\n```sh\nrm -rf never-spoken\n```\n\n第二句完整地读出来，好吗？"
UNITS = ["第一句先说。", "第二句完整地读出来，好吗？"]


class Model(BaseHTTPRequestHandler):
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
        self.server.payloads.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        pieces = [REPLY[start:start + 7] for start in range(0, len(REPLY), 7)]
        for index, piece in enumerate([*pieces, None]):
            delta = {"content": piece} if piece is not None else {}
            if index == 0:
                delta["role"] = "assistant"
            chunk = {"id": "chatcmpl-voice", "object": "chat.completion.chunk", "created": int(time.time()),
                     "model": "Qwen3.6-35B-A3B",
                     "choices": [{"index": 0, "delta": delta, "finish_reason": "stop" if piece is None else None}]}
            self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def _until(condition, seconds: float = 10) -> bool:
    deadline = time.monotonic() + seconds
    while not condition():
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True


def test_reply_is_read_aloud_without_entering_the_conversation(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRA_HOME", str(tmp_path / "state"))
    model = ThreadingHTTPServer(("127.0.0.1", 0), Model)
    model.payloads = []
    speech = serve_speech()
    for server in (model, speech):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    proc, _, seen, wait = _start_question_protocol_backend(
        tmp_path, model.server_address[1], "voice",
        settings_overrides={"voice": {"enabled": True, "base_url": f"http://127.0.0.1:{speech.server_address[1]}/v1",
                                      "selected": "warm", "voices": {"cool": {"voice": "c"}, "warm": {"voice": "w"}}}},
        env_overrides={"ASTRA_VOICE_PLAYER": "null"},
    )

    def send(event: dict) -> None:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(event, ensure_ascii=False) + "\n")
        proc.stdin.flush()

    def voice(event: dict, **fields) -> bool:
        return event.get("type") == "voice_status" and all(event.get(key) == value for key, value in fields.items())

    def completed(event: dict) -> bool:
        return event.get("type") == "task_status" and event["task"]["status"] == "completed"

    def wait_all(*checks) -> None:
        """Speech and the turn end independently, so their events interleave."""
        pending = list(checks)
        while pending:
            event = wait(lambda event, pending=pending: any(check(event) for check in pending))
            pending = [check for check in pending if not check(event)]

    try:
        wait(lambda event: event.get("type") == "model_info")
        startup = wait(lambda event: voice(event))
        assert (startup["enabled"], startup["state"], startup["voice"], startup["voices"]) == (
            True, "idle", "warm", ["cool", "warm"])

        send({"type": "message", "text": "你好"})
        wait_all(lambda event: voice(event, state="speaking"), lambda event: event.get("type") == "done", completed)
        assert _until(lambda: len(speech.requests) == 2)
        assert [(path, body["input"], body["voice"]) for path, _, body in speech.requests] == [
            ("/v1/audio/speech", unit, "w") for unit in UNITS]

        # Speech that outlives its turn is still the user's to stop.
        held = threading.Event()
        speech.gates[UNITS[0]] = held
        send({"type": "message", "text": "再说一遍"})
        wait_all(lambda event: event.get("type") == "done", completed)
        assert _until(lambda: len(speech.requests) >= 3)
        send({"type": "command", "cmd": "/cancel"})
        stopped = wait(lambda event: event.get("type") == "tool_result" and event.get("name") == "cancel")
        assert (stopped["output"], stopped["error"]) == ("Stopped speaking.", "")
        held.set()
        del speech.gates[UNITS[0]]

        send({"type": "command", "cmd": "/voice use cool"})
        chosen = wait(lambda event: voice(event, voice="cool"))
        assert chosen["message"] == "Voice set to cool."
        send({"type": "command", "cmd": "/voice off"})
        off = wait(lambda event: voice(event, enabled=False))
        assert (off["state"], off["message"]) == ("off", "Voice is off. /voice on reads replies aloud.")
        spoken = len(speech.requests)
        send({"type": "message", "text": "最后一句"})
        wait(lambda event: event.get("type") == "done")
        time.sleep(0.3)
        assert len(speech.requests) == spoken

        # What the model is sent is the conversation alone: three turns, each reply verbatim.
        turns = [message for message in model.payloads[-1]["messages"] if message["role"] != "system"]
        assert [message["role"] for message in turns] == ["user", "assistant", "user", "assistant", "user"]
        assert [message["content"] for message in turns if message["role"] == "assistant"] == [REPLY, REPLY]
        assert "voice" not in json.dumps(turns, ensure_ascii=False).lower()
        assert not [event for event in seen if event.get("type") == "error"]
    finally:
        _stop_protocol_backend(proc)
        for server in (model, speech):
            server.shutdown()
            server.server_close()
    settings = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert (settings["voice"]["enabled"], settings["voice"]["selected"]) == (False, "cool")
