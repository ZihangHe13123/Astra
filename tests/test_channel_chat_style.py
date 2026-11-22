"""WeChat's chat style reaches the model in WeChat conversations only, through a real backend."""

import io
import itertools
import json
import socket
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

from websockets.sync.client import connect

from test_backend_task_protocol import _start_question_protocol_backend, _stop_protocol_backend
from test_session_lifecycle_protocol import send
from test_weixin_backend_protocol import ModelHandler
from weixin_fixture import OWNER, FakeIlink

from agent.channels.weixin import CHAT_STYLE

GUIDANCE = "CHAT_STYLE_FIXTURE_PROJECT_GUIDANCE"
SKILL = "chat-style-fixture-skill"
QQ_TOKEN = "fixture-qq-token"
QQ_OWNER, QQ_CONTACT = "2", "3"
# What ModelHandler answers every request with.
ANSWER = "收到，现在是测试时间。"
# QQ message ids; time.monotonic_ns() repeats within a Windows clock tick.
_QQ_MESSAGE_IDS = itertools.count(1)


def _text(message: dict) -> str:
    content = message.get("content")
    parts = content if isinstance(content, list) else [{"text": content}]
    return "".join(str(part.get("text") or "") for part in parts if isinstance(part, dict))


class SystemRecorder(ModelHandler):
    """Each conversation request's system prompt (its first message), with the last thing the person said."""

    requests: list[tuple[str, str]] = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}"
        messages = json.loads(body).get("messages") or []
        if messages:
            system = _text(messages[0]) if messages[0].get("role") == "system" else ""
            said = next((_text(m) for m in reversed(messages) if m.get("role") == "user"), "")
            type(self).requests.append((system, said))
        self.rfile = io.BytesIO(body)
        self.headers.replace_header("Content-Length", str(len(body)))
        super().do_POST()


def _systems(text: str) -> list[str]:
    """The system prompt of each model request made for the turn that said ``text``."""
    return [system for system, said in SystemRecorder.requests if text in said]


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _qq(port: int, sender: str, text: str, deadline: float = 30) -> str:
    """What Astra answered ``sender`` in QQ, written through NapCat's reverse WebSocket with its token."""
    end = time.monotonic() + deadline
    while True:
        try:
            bridge = connect(f"ws://127.0.0.1:{port}", additional_headers={"Authorization": f"Bearer {QQ_TOKEN}"},
                             open_timeout=2)
            break
        except OSError:
            # The backend opens the QQ port while it starts.
            if time.monotonic() > end:
                raise
            time.sleep(0.1)
    with bridge:
        bridge.send(json.dumps({"post_type": "message", "message_type": "private", "self_id": 42,
                                "user_id": int(sender), "message_id": next(_QQ_MESSAGE_IDS),
                                "message": text}))
        while True:
            action = json.loads(bridge.recv(timeout=max(0.1, end - time.monotonic())))
            if action.get("action") == "send_private_msg" and action["params"].get("user_id") == int(sender):
                return action["params"]["message"]


def _wechat(ilink: FakeIlink, home: Path, text: str, deadline: float = 30) -> None:
    """Write ``text`` in WeChat and wait for the answer, and for WeChat's saved queue to let the message go:
    a backend stopped before that tells the person after its restart that the message was cut off."""
    answered = len(ilink.replies_to(OWNER))
    ilink.inbound.put(ilink.text_message(text, context_token="ctx-owner"))
    end = time.monotonic() + deadline
    while len(ilink.replies_to(OWNER)) == answered and time.monotonic() < end:
        time.sleep(0.05)
    assert ilink.replies_to(OWNER)[answered:] == [ANSWER], f"WeChat's answer to {text}"
    queue = home / "channels" / "weixin" / "sync.json"
    while time.monotonic() < end:
        try:
            if not json.loads(queue.read_text(encoding="utf-8")).get("pending"):
                return
        except (OSError, ValueError):
            pass
        time.sleep(0.05)
    raise AssertionError(f"WeChat's queue still holds {text}")


def test_only_wechat_requests_carry_the_chat_style_next_to_the_project_guidance_and_skills(tmp_path):
    SystemRecorder.requests = []
    model = ThreadingHTTPServer(("127.0.0.1", 0), SystemRecorder)
    threading.Thread(target=model.serve_forever, daemon=True).start()
    ilink = FakeIlink()
    qq_port = _free_port()
    channels = tmp_path / "channels.json"
    channels.write_text(json.dumps({
        "weixin": {"keep_awake": False},
        "qq": {"port": qq_port, "owner_users": [QQ_OWNER], "allow_users": [QQ_CONTACT]},
    }), encoding="utf-8")
    work = tmp_path / "work"
    (work / ".git").mkdir(parents=True)
    (work / "AGENTS.md").write_text(GUIDANCE, encoding="utf-8")
    first, again = tmp_path / "first", tmp_path / "again"
    skill = first / "skills" / SKILL / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(f"---\nname: {SKILL}\ndescription: A fixture skill.\n---\nNothing to do.\n", encoding="utf-8")
    again.mkdir()
    home = tmp_path / "home"
    env = {"ASTRA_HOME": str(home), "ASTRA_CHANNEL_CONFIG": str(channels),
           "ASTRA_WEIXIN_BASE_URL": ilink.base, "ASTRA_WEIXIN_CDN_URL": ilink.cdn,
           "ASTRA_CONTEXT_INDEX_EMBEDDING": "off", "ASTRA_PROJECT_TRUST": "trusted",
           "ASTRA_QQ_ENABLED": "1", "ASTRA_QQ_ACCESS_TOKEN": QQ_TOKEN}

    def guided(system: str) -> bool:
        """The conversation's static guidance: the project's instructions and the skill catalog."""
        return GUIDANCE in system and "<available-skills>" in system and SKILL in system

    try:
        proc, _, _, wait_for = _start_question_protocol_backend(
            first, model.server_port, "chat_style_window", workdir=work, env_overrides=env)
        try:
            wait_for(lambda e: e.get("type") == "model_info", timeout=30)
            send(proc, {"type": "command", "cmd": "/weixin login"})
            wait_for(lambda e: e.get("type") == "weixin_status" and e.get("state") == "online", timeout=30)

            _wechat(ilink, home, "微信里：第一句")
            wechat = _systems("微信里：第一句")
            assert wechat and all(system == wechat[0] for system in wechat)
            wechat_system = wechat[0]
            assert CHAT_STYLE in wechat_system and guided(wechat_system)

            assert _qq(qq_port, QQ_OWNER, "QQ 主人：在吗") == ANSWER
            owner = _systems("QQ 主人：在吗")
            assert owner and all("[微信聊天]" not in system and guided(system) for system in owner)
            # A contact's conversation has none of the owner's guidance, and no chat style either.
            assert _qq(qq_port, QQ_CONTACT, "QQ 联系人：在吗") == ANSWER
            contact = _systems("QQ 联系人：在吗")
            assert contact and all("[微信聊天]" not in system and GUIDANCE not in system for system in contact)

            send(proc, {"type": "message", "text": "窗口里：在吗"})
            wait_for(lambda e: e.get("type") == "done", timeout=30)
            window = _systems("窗口里：在吗")
            assert window and all("[微信聊天]" not in system and guided(system) for system in window)

            # Later turns send the same system prompt: the prefix WeChat's conversation started with.
            _wechat(ilink, home, "微信里：第二句")
            assert set(_systems("微信里：第二句")) == {wechat_system}
        finally:
            _stop_protocol_backend(proc)

        # After a restart the conversation comes back from disk with its guidance, and with the style.
        proc, _, _, wait_for = _start_question_protocol_backend(
            again, model.server_port, "chat_style_window", workdir=work,
            env_overrides={**env, "ASTRA_QQ_ENABLED": "", "AGENT_SETTINGS_PATH": str(first / "settings.json"),
                           "AGENT_SESSION_DIR": str(first / "sessions"), "AGENT_SKILLS_PATH": str(first / "skills")})
        try:
            wait_for(lambda e: e.get("type") == "weixin_status" and e.get("state") == "online", timeout=30)
            _wechat(ilink, home, "微信里：重启以后")
            assert set(_systems("微信里：重启以后")) == {wechat_system}
        finally:
            _stop_protocol_backend(proc)
    finally:
        model.shutdown()
        model.server_close()
        ilink.close()
