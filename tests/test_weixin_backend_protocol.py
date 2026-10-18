"""Real backend + scripted model + a local iLink: /weixin login, a WeChat message, the reply, logout."""

import io
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


class RecordingModelHandler(ModelHandler):
    models: list[str] = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) or b"{}"
        payload = json.loads(body)
        if "messages" in payload:
            type(self).models.append(str(payload.get("model")))
        self.rfile = io.BytesIO(body)
        self.headers.replace_header("Content-Length", str(len(body)))
        super().do_POST()


def _two_models(path, port: int) -> None:
    lines = ["version: 1", "models:"]
    for name in ("Qwen3.6-35B-A3B", "Chat-Small"):
        lines += [f"  {name}:", "    provider: openai-compatible", f"    base_url: http://127.0.0.1:{port}/v1",
                  "    api_key_env: ''", "    context_limit: 131072", "    capabilities: [tools, streaming]"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_weixin_answers_with_its_own_model_and_follows_the_default_again(tmp_path):
    RecordingModelHandler.models = []
    model = ThreadingHTTPServer(("127.0.0.1", 0), RecordingModelHandler)
    threading.Thread(target=model.serve_forever, daemon=True).start()
    ilink = FakeIlink()
    home = tmp_path / "home"
    channels = tmp_path / "channels.json"
    channels.write_text(json.dumps({"weixin": {"keep_awake": False}}), encoding="utf-8")
    models = tmp_path / "two-models.yaml"
    _two_models(models, model.server_port)
    workdir = tmp_path / "work"
    workdir.mkdir()
    proc, _, seen, wait_for = _start_question_protocol_backend(
        tmp_path, model.server_port, "weixin_model_session", workdir=workdir,
        env_overrides={"ASTRA_HOME": str(home), "ASTRA_CHANNEL_CONFIG": str(channels),
                       "ASTRA_WEIXIN_BASE_URL": ilink.base, "ASTRA_WEIXIN_CDN_URL": ilink.cdn,
                       "ASTRA_CONTEXT_INDEX_EMBEDDING": "off", "AGENT_MODELS_FILE": str(models)},
    )

    def status(predicate, timeout: float = 20) -> dict:
        return wait_for(lambda e: e.get("type") == "weixin_status" and predicate(e), timeout=timeout)

    def ask(text: str) -> list[str]:
        before, answered = len(RecordingModelHandler.models), len(ilink.replies_to(OWNER))
        ilink.inbound.put(ilink.text_message(text, context_token="ctx-owner"))
        deadline = time.monotonic() + 30
        while len(ilink.replies_to(OWNER)) == answered and time.monotonic() < deadline:
            time.sleep(0.05)
        assert len(ilink.replies_to(OWNER)) == answered + 1
        return RecordingModelHandler.models[before:]

    try:
        assert status(lambda e: True, timeout=30)["model"] == ""
        send(proc, {"type": "command", "cmd": "/weixin model"})
        assert status(lambda e: bool(e.get("message")))["message"] == "微信回复用的模型：跟随默认（Qwen3.6-35B-A3B）"
        send(proc, {"type": "command", "cmd": "/weixin login"})
        status(lambda e: e.get("state") == "online", timeout=30)

        send(proc, {"type": "command", "cmd": "/weixin model no-such-model"})
        assert "没有这个模型" in status(lambda e: bool(e.get("error")))["error"]
        assert "model" not in json.loads(channels.read_text(encoding="utf-8"))["weixin"]

        send(proc, {"type": "command", "cmd": "/weixin model Chat-Small"})
        chosen = status(lambda e: bool(e.get("model")))
        assert chosen["model_label"] == "Chat-Small"
        assert json.loads(channels.read_text(encoding="utf-8"))["weixin"]["model"] == chosen["model"]
        assert set(ask("换了模型吗")) == {"Chat-Small"}

        # The window keeps its own model: a turn here goes to the default one.
        before = len(RecordingModelHandler.models)
        send(proc, {"type": "message", "text": "窗口这边"})
        wait_for(lambda e: e.get("type") == "done", timeout=30)
        assert set(RecordingModelHandler.models[before:]) == {"Qwen3.6-35B-A3B"}

        send(proc, {"type": "command", "cmd": "/weixin model default"})
        followed = status(lambda e: e.get("message", "").startswith("微信改为跟随默认模型"))
        assert followed["model"] == "" and followed["model_label"] == ""
        assert followed["default_model"] == "Qwen3.6-35B-A3B"
        assert set(ask("现在呢")) == {"Qwen3.6-35B-A3B"}

        # Another window chooses a new default: WeChat follows it, this window keeps its model until it switches.
        settings = tmp_path / "settings.json"
        settings.write_text(json.dumps({**json.loads(settings.read_text(encoding="utf-8")),
                                        "selected_model": chosen["model"]}), encoding="utf-8")
        send(proc, {"type": "command", "cmd": "/weixin status"})
        assert status(lambda e: e.get("default_model") == "Chat-Small")["model"] == ""
        assert set(ask("默认换了")) == {"Chat-Small"}
    finally:
        _stop_protocol_backend(proc)
        model.shutdown()
        model.server_close()
        ilink.close()
