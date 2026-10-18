"""Real backend + scripted model + a local iLink: /weixin login, a WeChat message, the reply, logout."""

import json
import threading
import time
from http.server import ThreadingHTTPServer

from test_backend_task_protocol import _SlowOpenAIHandler, _start_question_protocol_backend, _stop_protocol_backend
from test_session_lifecycle_protocol import send
from weixin_fixture import BOT_TOKEN, OWNER, FakeIlink


class ModelHandler(_SlowOpenAIHandler):
    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
        if "messages" not in payload:
            self._json({"total_tokens": 100})
            return
        answer = "收到，现在是测试时间。"
        if not payload.get("stream"):
            self._json({"id": "fixture", "object": "chat.completion", "model": "Qwen3.6-35B-A3B",
                        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": answer}}]})
            return
        chunk = {"id": "fixture", "object": "chat.completion.chunk", "model": "Qwen3.6-35B-A3B",
                 "choices": [{"index": 0, "delta": {"content": answer}, "finish_reason": "stop"}]}
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode())


def test_weixin_login_message_reply_and_logout_through_the_backend(tmp_path):
    model = ThreadingHTTPServer(("127.0.0.1", 0), ModelHandler)
    threading.Thread(target=model.serve_forever, daemon=True).start()
    ilink = FakeIlink()
    home = tmp_path / "home"
    channels = tmp_path / "channels.json"
    # Keep this machine's sleep settings out of the test; the hold itself has its own test.
    channels.write_text(json.dumps({"weixin": {"keep_awake": False}}), encoding="utf-8")
    workdir = tmp_path / "work"
    workdir.mkdir()
    proc, _, seen, wait_for = _start_question_protocol_backend(
        tmp_path, model.server_port, "weixin_session", workdir=workdir,
        env_overrides={"ASTRA_HOME": str(home), "ASTRA_CHANNEL_CONFIG": str(channels),
                       "ASTRA_WEIXIN_BASE_URL": ilink.base, "ASTRA_WEIXIN_CDN_URL": ilink.cdn,
                       "ASTRA_CONTEXT_INDEX_EMBEDDING": "off"},
    )

    def status(predicate, timeout: float = 20) -> dict:
        return wait_for(lambda e: e.get("type") == "weixin_status" and predicate(e), timeout=timeout)

    try:
        first = status(lambda e: True, timeout=30)
        assert first["state"] == "off" and not first["enabled"] and not first["logged_in"]

        send(proc, {"type": "command", "cmd": "/weixin login"})
        shown = status(lambda e: e.get("login") == "scan")
        assert shown["qr"]["svg"].startswith("data:image/svg+xml") and "█" in shown["qr"]["text"]
        online = status(lambda e: e.get("state") == "online", timeout=30)
        assert online["enabled"] and online["logged_in"] and online["owner"] and "qr" not in online
        saved = json.loads(channels.read_text(encoding="utf-8"))
        assert saved["weixin"] == {"keep_awake": False, "enabled": True}

        ilink.inbound.put(ilink.text_message("现在几点？", context_token="ctx-owner"))
        deadline = time.monotonic() + 30
        while not ilink.replies_to(OWNER) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ilink.replies_to(OWNER) == ["收到，现在是测试时间。"]
        assert ilink.sent[0]["context_token"] == "ctx-owner"
        # The channel turn is its own conversation: nothing of it reaches this window's stream.
        assert not [e for e in seen if e.get("type") == "chunk"]

        send(proc, {"type": "command", "cmd": "/weixin logout"})
        out = status(lambda e: e.get("message") == "已退出微信登录，渠道已停用。")
        assert out["state"] == "off" and not out["logged_in"]
        assert not (home / "channels" / "weixin" / "account.json").exists()
        assert json.loads(channels.read_text(encoding="utf-8"))["weixin"]["enabled"] is False
        assert BOT_TOKEN not in json.dumps(seen, ensure_ascii=False)
    finally:
        _stop_protocol_backend(proc)
        model.shutdown()
        model.server_close()
        ilink.close()
