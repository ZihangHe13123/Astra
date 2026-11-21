"""Real backend + scripted model + a local iLink: /weixin login, a WeChat message, the reply, logout."""

import io
import json
import threading
import time
from http.server import ThreadingHTTPServer

import pytest

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


@pytest.mark.parametrize("written", ["50 MB", "--5"])
def test_a_wechat_setting_that_is_not_a_number_is_reported_and_the_backend_keeps_working(tmp_path, written):
    model = ThreadingHTTPServer(("127.0.0.1", 0), ModelHandler)
    threading.Thread(target=model.serve_forever, daemon=True).start()
    channels = tmp_path / "channels.json"
    channels.write_text(json.dumps({"weixin": {"keep_awake": False, "max_receive_file_bytes": written}}),
                        encoding="utf-8")
    workdir = tmp_path / "work"
    workdir.mkdir()
    proc, _, _, wait_for = _start_question_protocol_backend(
        tmp_path, model.server_port, "weixin_settings", workdir=workdir,
        env_overrides={"ASTRA_HOME": str(tmp_path / "home"), "ASTRA_CHANNEL_CONFIG": str(channels),
                       "ASTRA_CONTEXT_INDEX_EMBEDDING": "off"},
    )
    try:
        first = wait_for(lambda e: e.get("type") == "weixin_status", timeout=30)
        assert first["state"] == "off"
        assert first["error"].startswith(f'weixin.max_receive_file_bytes 应为整数，现在是 "{written}"')
        send(proc, {"type": "command", "cmd": "/weixin status"})
        again = wait_for(lambda e: e.get("type") == "weixin_status" and e is not first, timeout=20)
        assert again["error"] == first["error"] and proc.poll() is None
    finally:
        _stop_protocol_backend(proc)
        model.shutdown()
        model.server_close()


def test_a_running_channel_follows_saved_settings_and_stops_when_switched_off(tmp_path):
    """Settings saved in channels.json while WeChat is online apply to the next message, and switching the
    channel off there stops it: no restart, and no command needed in the backend that has the channel."""
    from test_weixin_channel import _file, _on_cdn

    model = ThreadingHTTPServer(("127.0.0.1", 0), ModelHandler)
    threading.Thread(target=model.serve_forever, daemon=True).start()
    ilink = FakeIlink()
    inbox = tmp_path / "inbox"
    channels = tmp_path / "channels.json"
    settings = {"keep_awake": False, "receive_files_dir": str(inbox), "max_receive_file_bytes": 2048}
    channels.write_text(json.dumps({"weixin": settings}), encoding="utf-8")
    workdir = tmp_path / "work"
    workdir.mkdir()
    proc, _, _, wait_for = _start_question_protocol_backend(
        tmp_path, model.server_port, "weixin_limits", workdir=workdir,
        env_overrides={"ASTRA_HOME": str(tmp_path / "home"), "ASTRA_CHANNEL_CONFIG": str(channels),
                       "ASTRA_WEIXIN_BASE_URL": ilink.base, "ASTRA_WEIXIN_CDN_URL": ilink.cdn,
                       "ASTRA_CONTEXT_INDEX_EMBEDDING": "off"},
    )

    def status(predicate, timeout: float = 20) -> dict:
        return wait_for(lambda e: e.get("type") == "weixin_status" and predicate(e), timeout=timeout)

    def save(**changes) -> None:
        saved = json.loads(channels.read_text(encoding="utf-8"))
        saved["weixin"].update(changes)
        channels.write_text(json.dumps(saved), encoding="utf-8")

    def receive(param: str) -> None:
        _on_cdn(ilink, param, param.encode() * 3)
        answered = len(ilink.replies_to(OWNER))
        ilink.inbound.put({**ilink.text_message(""), "item_list": [_file(param, f"{param}.txt")]})
        deadline = time.monotonic() + 30
        while len(ilink.replies_to(OWNER)) == answered and time.monotonic() < deadline:
            time.sleep(0.05)
        assert len(ilink.replies_to(OWNER)) > answered

    def saved_files() -> list[str]:
        return sorted(path.name for path in inbox.rglob("*") if path.is_file())

    try:
        status(lambda e: True, timeout=30)
        send(proc, {"type": "command", "cmd": "/weixin login"})
        status(lambda e: e.get("state") == "online", timeout=30)
        receive("first")
        assert saved_files() == ["first.txt"]

        # What status says is what the channel does: the saved setting applies to the next file.
        save(max_receive_file_bytes="50 MB")
        send(proc, {"type": "command", "cmd": "/weixin status"})
        warned = status(lambda e: "max_receive_file_bytes" in e.get("error", ""))
        assert warned["error"] == ('weixin.max_receive_file_bytes 应为整数，现在是 "50 MB"；'
                                   "收到的文件先不下载，只收文件名；写成字节数（如 52428800）后生效。")
        receive("second")
        assert saved_files() == ["first.txt"]
        save(max_receive_file_bytes=2048)
        send(proc, {"type": "command", "cmd": "/weixin status"})
        status(lambda e: "error" not in e and e.get("state") == "online")
        receive("third")
        assert saved_files() == ["first.txt", "third.txt"]

        # Switched off in the file: the channel stops taking messages, and the window hears it.
        save(enabled=False)
        off = status(lambda e: e.get("state") == "off")
        assert off["enabled"] is False
        polls, answered = len(ilink.polls), len(ilink.replies_to(OWNER))
        ilink.inbound.put(ilink.text_message("还在吗"))
        time.sleep(1.5)
        assert (len(ilink.polls), len(ilink.replies_to(OWNER))) == (polls, answered)
        assert ilink.media_requests == ["first", "third"]
    finally:
        _stop_protocol_backend(proc)
        model.shutdown()
        model.server_close()
        ilink.close()


def test_a_change_made_in_a_waiting_backend_reaches_the_one_that_has_wechat(tmp_path):
    """Two backends, one receiving WeChat and one waiting (the desktop's channel host and a window, say). A
    setting saved, or /weixin off run, in the waiting one applies in the one that receives."""
    from test_weixin_channel import _file, _on_cdn

    model = ThreadingHTTPServer(("127.0.0.1", 0), ModelHandler)
    threading.Thread(target=model.serve_forever, daemon=True).start()
    ilink = FakeIlink()
    inbox = tmp_path / "inbox"
    channels = tmp_path / "channels.json"
    channels.write_text(json.dumps({"weixin": {"keep_awake": False, "receive_files_dir": str(inbox)}}),
                        encoding="utf-8")
    shared = {"ASTRA_HOME": str(tmp_path / "home"), "ASTRA_CHANNEL_CONFIG": str(channels),
              "ASTRA_WEIXIN_BASE_URL": ilink.base, "ASTRA_WEIXIN_CDN_URL": ilink.cdn,
              "ASTRA_CONTEXT_INDEX_EMBEDDING": "off"}
    backends = []

    def start(name: str):
        root = tmp_path / name
        root.mkdir()
        (root / "work").mkdir()
        proc, _, _, wait_for = _start_question_protocol_backend(
            root, model.server_port, f"weixin_{name}", workdir=root / "work", env_overrides=shared)
        backends.append(proc)
        return proc, lambda predicate, timeout=20: wait_for(
            lambda e: e.get("type") == "weixin_status" and predicate(e), timeout=timeout)

    try:
        receiving, receiving_status = start("receiving")
        receiving_status(lambda e: True, timeout=30)
        send(receiving, {"type": "command", "cmd": "/weixin login"})
        receiving_status(lambda e: e.get("state") == "online", timeout=30)
        waiting, waiting_status = start("waiting")
        waiting_status(lambda e: e.get("state") == "standby", timeout=30)

        saved = json.loads(channels.read_text(encoding="utf-8"))
        saved["weixin"]["max_receive_file_bytes"] = 0
        channels.write_text(json.dumps(saved), encoding="utf-8")
        _on_cdn(ilink, "note", b"notes")
        ilink.inbound.put({**ilink.text_message(""), "item_list": [_file("note", "note.md")]})
        deadline = time.monotonic() + 30
        while not ilink.replies_to(OWNER) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ilink.replies_to(OWNER) and ilink.media_requests == [] and not inbox.exists()

        # The waiting backend does not say it has stopped WeChat: the receiving one stops when it reads the change.
        send(waiting, {"type": "command", "cmd": "/weixin off"})
        waiting_status(lambda e: e.get("message") == "微信渠道已停用；如果另一个 Astra 后台正在收微信，它会在几秒内停下。")
        receiving_status(lambda e: e.get("state") == "off")
        polls = len(ilink.polls)
        time.sleep(1.5)
        assert len(ilink.polls) == polls
    finally:
        for proc in backends:
            _stop_protocol_backend(proc)
        model.shutdown()
        model.server_close()
        ilink.close()


def test_an_unreadable_channels_json_is_reported_and_never_written_over(tmp_path):
    model = ThreadingHTTPServer(("127.0.0.1", 0), ModelHandler)
    threading.Thread(target=model.serve_forever, daemon=True).start()
    ilink = FakeIlink()
    channels = tmp_path / "channels.json"
    channels.write_text(json.dumps({"weixin": {"keep_awake": False, "max_receive_file_bytes": 0}}), encoding="utf-8")
    workdir = tmp_path / "work"
    workdir.mkdir()
    proc, _, _, wait_for = _start_question_protocol_backend(
        tmp_path, model.server_port, "weixin_unreadable", workdir=workdir,
        env_overrides={"ASTRA_HOME": str(tmp_path / "home"), "ASTRA_CHANNEL_CONFIG": str(channels),
                       "ASTRA_WEIXIN_BASE_URL": ilink.base, "ASTRA_WEIXIN_CDN_URL": ilink.cdn,
                       "ASTRA_CONTEXT_INDEX_EMBEDDING": "off"},
    )

    def status(predicate, timeout: float = 20) -> dict:
        return wait_for(lambda e: e.get("type") == "weixin_status" and predicate(e), timeout=timeout)

    try:
        status(lambda e: True, timeout=30)
        send(proc, {"type": "command", "cmd": "/weixin login"})
        status(lambda e: e.get("state") == "online", timeout=30)
        good = channels.read_text(encoding="utf-8")
        broken = good.rstrip().removesuffix("}") + ",}\n"
        channels.write_text(broken, encoding="utf-8")
        # Said without being asked, and shown as the running channel has it: on, not the defaults.
        unreadable = status(lambda e: "不是合法的 JSON" in e.get("error", ""))
        assert unreadable["error"] == "channels.json 不是合法的 JSON：改好之前，微信沿用原来的设置，这里也不能改设置。"
        assert unreadable["enabled"] is True and unreadable["state"] == "online"
        send(proc, {"type": "command", "cmd": "/weixin on"})
        refused = status(lambda e: e.get("error", "").startswith("channels.json 不是合法的 JSON，设置没有改动"))
        assert refused["state"] == "online"
        assert channels.read_text(encoding="utf-8") == broken
        channels.write_text(good, encoding="utf-8")
        status(lambda e: "error" not in e and e.get("state") == "online")
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


def test_a_wechat_turn_asks_for_approval_in_wechat_and_follows_the_answer(tmp_path):
    from test_backend_task_protocol import _ApprovalOpenAIHandler

    target = tmp_path / "outside-workspace.txt"
    model = ThreadingHTTPServer(("127.0.0.1", 0), _ApprovalOpenAIHandler)
    model.target_path, model.target_content, model.call_number = target, "approved in WeChat", 0  # type: ignore[attr-defined]
    threading.Thread(target=model.serve_forever, daemon=True).start()
    ilink = FakeIlink()
    channels = tmp_path / "channels.json"
    channels.write_text(json.dumps({"weixin": {"keep_awake": False}}), encoding="utf-8")
    workdir = tmp_path / "work"
    workdir.mkdir()
    proc, _, seen, wait_for = _start_question_protocol_backend(
        tmp_path, model.server_port, "weixin_approval_session", workdir=workdir,
        env_overrides={"ASTRA_HOME": str(tmp_path / "home"), "ASTRA_CHANNEL_CONFIG": str(channels),
                       "ASTRA_WEIXIN_BASE_URL": ilink.base, "ASTRA_WEIXIN_CDN_URL": ilink.cdn,
                       "ASTRA_CONTEXT_INDEX_EMBEDDING": "off"},
    )

    def replies(count: int) -> list[str]:
        deadline = time.monotonic() + 30
        while len(ilink.replies_to(OWNER)) < count and time.monotonic() < deadline:
            time.sleep(0.05)
        assert len(ilink.replies_to(OWNER)) >= count, ilink.replies_to(OWNER)
        return ilink.replies_to(OWNER)

    try:
        wait_for(lambda e: e.get("type") == "weixin_status", timeout=30)
        send(proc, {"type": "command", "cmd": "/weixin login"})
        wait_for(lambda e: e.get("type") == "weixin_status" and e.get("state") == "online", timeout=30)

        ilink.inbound.put(ilink.text_message("帮我写个文件", context_token="ctx-ask"))
        prompt = replies(1)[0]
        assert prompt.startswith("需要你批准") and str(target) in prompt and "回复“同意”执行一次" in prompt
        request = wait_for(lambda e: e.get("type") == "tool_approval_request")
        assert request["surface"] == "channel" and request["channel"] == "weixin"
        assert not target.exists()

        ilink.inbound.put(ilink.text_message("同意", context_token="ctx-answer"))
        assert replies(2)[1] == "saved"
        assert target.read_text(encoding="utf-8") == "approved in WeChat"
        resolved = wait_for(lambda e: e.get("type") == "approval_resolved")
        assert resolved["request_id"] == request["request_id"] and resolved["decision"] == "once"
        # The answer settled the approval rather than starting a turn of its own, and the reply used its token.
        assert model.call_number == 2  # type: ignore[attr-defined]
        assert ilink.sent[-1]["context_token"] == "ctx-answer"

        model.call_number, model.target_content = 0, "must not be written"  # type: ignore[attr-defined]
        ilink.inbound.put(ilink.text_message("再写一次", context_token="ctx-again"))
        assert replies(3)[2].startswith("需要你批准")
        ilink.inbound.put(ilink.text_message("拒绝", context_token="ctx-no"))
        replies(4)
        denied = wait_for(lambda e: e.get("type") == "approval_resolved" and e.get("decision") == "deny")
        assert denied["request_id"] != request["request_id"]
        assert target.read_text(encoding="utf-8") == "approved in WeChat"
    finally:
        _stop_protocol_backend(proc)
        model.shutdown()
        model.server_close()
        ilink.close()


def test_new_in_wechat_starts_a_new_conversation(tmp_path):
    from agent.channels.router import NEW_CONVERSATION_REPLY

    model = ThreadingHTTPServer(("127.0.0.1", 0), ModelHandler)
    threading.Thread(target=model.serve_forever, daemon=True).start()
    ilink = FakeIlink()
    home = tmp_path / "home"
    channels = tmp_path / "channels.json"
    channels.write_text(json.dumps({"weixin": {"keep_awake": False}}), encoding="utf-8")
    workdir = tmp_path / "work"
    workdir.mkdir()
    proc, _, _, wait_for = _start_question_protocol_backend(
        tmp_path, model.server_port, "weixin_new_session", workdir=workdir,
        env_overrides={"ASTRA_HOME": str(home), "ASTRA_CHANNEL_CONFIG": str(channels),
                       "ASTRA_WEIXIN_BASE_URL": ilink.base, "ASTRA_WEIXIN_CDN_URL": ilink.cdn,
                       "ASTRA_CONTEXT_INDEX_EMBEDDING": "off"},
    )

    def say(text: str) -> str:
        answered = len(ilink.replies_to(OWNER))
        ilink.inbound.put(ilink.text_message(text))
        deadline = time.monotonic() + 30
        while len(ilink.replies_to(OWNER)) == answered and time.monotonic() < deadline:
            time.sleep(0.05)
        return ilink.replies_to(OWNER)[-1]

    try:
        wait_for(lambda e: e.get("type") == "weixin_status", timeout=30)
        send(proc, {"type": "command", "cmd": "/weixin login"})
        wait_for(lambda e: e.get("type") == "weixin_status" and e.get("state") == "online", timeout=30)
        assert say("第一句") == "收到，现在是测试时间。"
        assert say("/new") == NEW_CONVERSATION_REPLY
        assert say("新的开始") == "收到，现在是测试时间。"
        conversations = sorted({path.name.split(".")[0] for path in (tmp_path / "sessions").glob("channel_weixin_*")})
        assert len(conversations) == 2 and conversations[1].startswith(f"{conversations[0]}_")
        saved = json.loads((home / "channels" / "conversations.json").read_text(encoding="utf-8"))
        assert saved == {conversations[0]: conversations[1]}
    finally:
        _stop_protocol_backend(proc)
        model.shutdown()
        model.server_close()
        ilink.close()


def test_the_desktop_channel_host_backend_takes_wechat_from_an_older_backend(tmp_path):
    model = ThreadingHTTPServer(("127.0.0.1", 0), ModelHandler)
    threading.Thread(target=model.serve_forever, daemon=True).start()
    ilink = FakeIlink()
    home = tmp_path / "home"
    channels = tmp_path / "channels.json"
    channels.write_text(json.dumps({"weixin": {"keep_awake": False}}), encoding="utf-8")
    shared = {"ASTRA_HOME": str(home), "ASTRA_CHANNEL_CONFIG": str(channels),
              "ASTRA_WEIXIN_BASE_URL": ilink.base, "ASTRA_WEIXIN_CDN_URL": ilink.cdn,
              "ASTRA_CONTEXT_INDEX_EMBEDDING": "off"}
    backends = []

    def start(name: str, **extra: str):
        root = tmp_path / name
        (root / "work").mkdir(parents=True)
        proc, _, _, wait_for = _start_question_protocol_backend(
            root, model.server_port, f"{name}_session", workdir=root / "work", env_overrides={**shared, **extra})
        backends.append(proc)
        return proc, wait_for

    try:
        terminal, terminal_events = start("terminal")
        terminal_events(lambda e: e.get("type") == "weixin_status", timeout=30)
        send(terminal, {"type": "command", "cmd": "/weixin login"})
        terminal_events(lambda e: e.get("type") == "weixin_status" and e.get("state") == "online", timeout=30)

        host, host_events = start("host", ASTRA_CHANNEL_HOST="1")
        host_events(lambda e: e.get("type") == "weixin_status" and e.get("state") == "online", timeout=30)
        terminal_events(lambda e: e.get("type") == "weixin_status" and e.get("state") == "standby", timeout=30)

        ilink.inbound.put(ilink.text_message("现在谁在？"))
        deadline = time.monotonic() + 30
        while not ilink.replies_to(OWNER) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ilink.replies_to(OWNER) == ["收到，现在是测试时间。"]
        assert any((tmp_path / "host" / "sessions").glob("channel_weixin_*"))
        assert not list((tmp_path / "terminal" / "sessions").glob("channel_weixin_*")), "the older backend answered nothing"
    finally:
        for proc in backends:
            _stop_protocol_backend(proc)
        model.shutdown()
        model.server_close()
        ilink.close()
