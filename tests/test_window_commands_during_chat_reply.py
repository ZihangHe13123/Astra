"""Window commands while a WeChat reply is under way.

A chat's turn borrows the agent's conversation (agent.context) and client while it answers. A command from the
window meanwhile must change the window's own conversation, or wait for the reply when it needs the agent
itself; it must never reach the chat's. Each test holds a WeChat reply in flight at the model, runs window
commands, then reads the next window request and the next WeChat request to see where the change landed.
"""

import io
import json
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from queue import Empty

from test_backend_task_protocol import _start_question_protocol_backend, _stop_protocol_backend
from test_session_lifecycle_protocol import send
from test_weixin_backend_protocol import ModelHandler
from weixin_fixture import OWNER, FakeIlink

WINDOW = "chat_reply_window"
HELD = "微信慢慢想"
TYPING_STOP = 2  # iLink's sendtyping status that ends "typing…"


def _text(message: dict) -> str:
    content = message.get("content")
    parts = content if isinstance(content, list) else [{"text": content}]
    return "".join(str(part.get("text") or "") for part in parts if isinstance(part, dict))


def _said(message: dict) -> str:
    """What the person wrote, without the time the backend puts before it."""
    return _text(message).split("</message_time>")[-1].strip()


def _person(messages: list[dict]) -> list[str]:
    """What the person said in a request's conversation, in order (the runtime's own context blocks left out)."""
    return [_said(message) for message in messages
            if message.get("role") == "user" and not _text(message).lstrip().startswith("[SYSTEM-SUPPLIED")]


def _persona(messages: list[dict]) -> str:
    system = "".join(_text(message) for message in messages if message.get("role") == "system")
    marker = "agent-persona:id="
    return system[system.index(marker) + len(marker):].split(";")[0] if marker in system else ""


class RequestRecorder(ModelHandler):
    """Records each conversation request. The first whose person says ``hold`` waits for ``release``."""

    requests: list[list[dict]] = []
    hold = ""
    held = threading.Event()
    release = threading.Event()

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}"
        messages = json.loads(body).get("messages") or []
        if messages:
            type(self).requests.append(messages)
            said = _person(messages)
            if type(self).hold and said and said[-1] == type(self).hold and not type(self).held.is_set():
                type(self).held.set()
                type(self).release.wait(60)
        self.rfile = io.BytesIO(body)
        self.headers.replace_header("Content-Length", str(len(body)))
        super().do_POST()


class Answer:
    def __init__(self, events: list[dict], name: str):
        self.events = events
        self.result = next((event for event in reversed(events)
                            if event.get("type") == "tool_result" and event.get("name") == name), {})

    def of(self, kind: str) -> list[dict]:
        return [event for event in self.events if event.get("type") == kind]


class ChatReplyBackend:
    """A real backend on the persona "work" with WeChat on a local iLink and a recording model."""

    def __init__(self, tmp_path):
        RequestRecorder.requests, RequestRecorder.hold = [], ""
        RequestRecorder.held, RequestRecorder.release = threading.Event(), threading.Event()
        self.tmp = tmp_path
        self.model = ThreadingHTTPServer(("127.0.0.1", 0), RequestRecorder)
        threading.Thread(target=self.model.serve_forever, daemon=True).start()
        self.ilink = FakeIlink()
        channels = tmp_path / "channels.json"
        channels.write_text(json.dumps({"weixin": {"keep_awake": False}}), encoding="utf-8")
        (tmp_path / "work").mkdir()
        # Public Astra ships Lyra and her WeChat version only. These stand in for personas without a WeChat
        # version of their own: "work" for the window, "balanced" for /persona and /reload.
        personas = tmp_path / "persona.local.json"
        personas.write_text(json.dumps({"schema": 1, "profiles": [
            {"name": name, "description": f"Example {name} persona", "persona": f"Example {name} identity",
             "version": 1, "persona_layer": "identity", "active_mode": "work"} for name in ("work", "balanced")]}),
            encoding="utf-8")
        self.proc, self.events, self.seen, self.wait_for = _start_question_protocol_backend(
            tmp_path, self.model.server_port, WINDOW, workdir=tmp_path / "work",
            settings_overrides={"selected_persona": "work"},
            env_overrides={"ASTRA_HOME": str(tmp_path / "home"), "ASTRA_CHANNEL_CONFIG": str(channels),
                           "ASTRA_WEIXIN_BASE_URL": self.ilink.base, "ASTRA_WEIXIN_CDN_URL": self.ilink.cdn,
                           "ASTRA_CONTEXT_INDEX_EMBEDDING": "off",
                           # A local mode, whose own command (/fixture) opens a mode around the conversation.
                           "ASTRA_LOCAL_MODE_FILE": str(Path(__file__).parent / "fixtures/local_mode.py"),
                           "ASTRA_PERSONA_FILE": str(personas)})
        self.info = self.wait_for(lambda e: e.get("type") == "model_info", timeout=30)
        send(self.proc, {"type": "command", "cmd": "/weixin login"})
        self.wait_for(lambda e: e.get("type") == "weixin_status" and e.get("state") == "online", timeout=30)

    def close(self) -> None:
        RequestRecorder.release.set()
        _stop_protocol_backend(self.proc)
        self.model.shutdown()
        self.model.server_close()
        self.ilink.close()

    def request(self, said: str, timeout: float = 30) -> list[dict]:
        """The latest request in which the person's last words were ``said``."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for messages in reversed(RequestRecorder.requests):
                person = _person(messages)
                if person and person[-1] == said:
                    return messages
            time.sleep(0.05)
        raise AssertionError(f"no model request answered {said!r}")

    def drain(self) -> int:
        """Keep what the backend has sent so far, so the next wait reads only what comes after."""
        while True:
            try:
                self.seen.append(self.events.get_nowait())
            except Empty:
                return len(self.seen)

    def window(self, text: str) -> list[dict]:
        self.drain()
        send(self.proc, {"type": "message", "text": text})
        self.wait_for(lambda e: e.get("type") == "done", timeout=30)
        return self.request(text)

    def write_wechat(self, text: str) -> int:
        answered = len(self.ilink.replies_to(OWNER))
        self.ilink.inbound.put(self.ilink.text_message(text, context_token="ctx-owner"))
        return answered

    def wait_reply(self, answered: int) -> None:
        """Until WeChat has the reply and its turn has ended: "typing…" stops after the reply is sent, once the
        turn has given the agent back."""
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            timeline = list(self.ilink.timeline)
            sent = [index for index, (kind, _) in enumerate(timeline) if kind == "text"]
            if (len(self.ilink.replies_to(OWNER)) > answered and sent
                    and ("typing", TYPING_STOP) in timeline[sent[-1]:]):
                return
            time.sleep(0.05)
        raise AssertionError("WeChat was answered and its turn ended")

    def wechat(self, text: str) -> list[dict]:
        self.wait_reply(self.write_wechat(text))
        return self.request(text)

    def hold(self, text: str = HELD) -> int:
        """Write to WeChat and keep its reply at the model until release()."""
        RequestRecorder.hold = text
        answered = self.write_wechat(text)
        assert RequestRecorder.held.wait(30), "the WeChat reply is under way"
        return answered

    def release(self, answered: int) -> None:
        RequestRecorder.release.set()
        self.wait_reply(answered)

    def command(self, command: str, until=None, timeout: float = 20) -> Answer:
        """Run a window command and keep the events up to its done (or ``until``)."""
        start = self.drain()
        send(self.proc, {"type": "command", "cmd": command})
        self.wait_for(until or (lambda e: e.get("type") == "done"), timeout=timeout)
        return Answer(self.seen[start:], command.split()[0][1:])

    def session_file(self, name: str) -> dict:
        return json.loads((self.tmp / "sessions" / f"{name}.header.json").read_text(encoding="utf-8"))

    def wechat_session(self) -> str:
        names = {path.name.split(".")[0] for path in (self.tmp / "sessions").glob("channel_weixin_*")}
        assert len(names) == 1, names
        return names.pop()


def test_persona_and_think_from_the_window_during_a_wechat_reply_change_the_window(tmp_path):
    """/persona and /think change the window's conversation in place, so they apply at once."""
    backend = ChatReplyBackend(tmp_path)
    try:
        window_display = backend.info["show_reasoning"]
        assert _persona(backend.window("窗口第一句")) == "work"
        assert _persona(backend.wechat("微信第一句")) == "work"
        answered = backend.hold()

        switched = backend.command("/persona balanced")
        assert switched.result["output"].startswith("Persona switched to: balanced")
        toggled = backend.command("/think")
        assert toggled.result["output"] == f"Thinking display: {'OFF' if window_display else 'ON'}"
        # What the window is told about its conversation agrees.
        assert toggled.of("model_info")[-1]["show_reasoning"] is (not window_display)
        listing = backend.command("/persona")
        assert "   * balanced — " in listing.result["output"]
        backend.release(answered)

        assert _persona(backend.request(HELD)) == "work"
        assert _persona(backend.window("窗口之后")) == "balanced"
        assert _persona(backend.wechat("微信之后")) == "work"
        assert backend.session_file(WINDOW)["show_reasoning"] is (not window_display)
        # A chat's conversation never shows reasoning; the window's /think does not reach it.
        assert backend.session_file(backend.wechat_session())["show_reasoning"] is False
    finally:
        backend.close()


def test_undo_and_retry_from_the_window_during_a_wechat_reply_edit_the_window(tmp_path):
    """/undo and /retry edit the window's history; the request /retry takes back is sent after the reply."""
    backend = ChatReplyBackend(tmp_path)
    try:
        backend.window("窗口一")
        backend.window("窗口二")
        backend.wechat("微信一")
        answered = backend.hold()

        undone = backend.command("/undo 1")
        assert undone.result["output"] == "Removed 1 exchange (2 messages)."
        shown = undone.of("history")[-1]
        assert shown["session_id"] == WINDOW
        assert [item["content"] for item in shown["messages"] if item["role"] == "user"] == ["窗口一"]
        retried = backend.command("/retry", until=lambda e: e.get("type") == "tool_result" and e.get("name") == "retry")
        assert retried.result["output"] == 'Retrying the last request: "窗口一"'
        backend.release(answered)
        # The request taken back is sent again in the window once WeChat has its reply.
        backend.wait_for(lambda e: e.get("type") == "done", timeout=30)

        assert _person(backend.request("窗口一")) == ["窗口一"]
        assert _person(backend.window("窗口之后")) == ["窗口一", "窗口之后"]
        assert _person(backend.wechat("微信之后")) == ["微信一", HELD, "微信之后"]
    finally:
        backend.close()


def test_commands_that_need_the_agent_wait_for_a_wechat_reply(tmp_path):
    """/reset, switching sessions, the private modes, /compress, /reload and applying guidance act through the
    agent, which holds the chat's conversation while it answers. They are refused at once with the reason, and
    both conversations stay as they were; the same command works once the reply has been sent."""
    backend = ChatReplyBackend(tmp_path)
    try:
        backend.window("窗口第一句")
        backend.wechat("微信第一句")
        # What /reload would pick up for the persona.
        settings = tmp_path / "settings.json"
        settings.write_text(json.dumps({**json.loads(settings.read_text(encoding="utf-8")),
                                        "selected_persona": "balanced"}), encoding="utf-8")
        answered = backend.hold()

        for command in ("/reset", "/session other_window", "/bar", "/minimal", "/fixture", "/sip", "/compress",
                        "/reload", "/reload persona", "/guidance refresh --now", "/skills"):
            head = command.split()[0]
            # Had they run, /reset and /session would show the conversation they went to (/reset sends no done).
            answer = backend.command(command, until=lambda e: e.get("type") in {"done", "session_info"})
            assert answer.result.get("error") == (
                f"Astra is answering a WeChat message right now. Run {head} again once that reply has been sent."
            ), (command, answer.result)
            assert answer.result.get("code") == "busy", command
            if head == "/reset":
                # The terminal clears its transcript as it sends /reset: it gets the conversation back.
                shown = answer.of("history")[-1]
                assert shown["session_id"] == WINDOW
                assert [item["content"] for item in shown["messages"] if item["role"] == "user"] == ["窗口第一句"]
        backend.release(answered)

        assert _person(backend.window("窗口之后")) == ["窗口第一句", "窗口之后"]
        after = backend.wechat("微信之后")
        assert _person(after) == ["微信第一句", HELD, "微信之后"] and _persona(after) == "work"
        sessions = sorted({path.name.split(".")[0] for path in (tmp_path / "sessions").glob("*.json*")})
        assert sessions == sorted([WINDOW, backend.wechat_session()])

        # Once the reply has been sent, /reset clears the window's conversation and only that.
        backend.command("/reset", until=lambda e: e.get("type") == "session_info")
        assert _person(backend.window("窗口重来")) == ["窗口重来"]
        assert _person(backend.wechat("微信还在")) == ["微信第一句", HELD, "微信之后", "微信还在"]
    finally:
        backend.close()


def test_what_the_window_keeps_for_its_conversation_during_a_wechat_reply_stays_the_window_s(tmp_path):
    """A goal, working memory, a wakeup, a handoff and the session list belong to the window's conversation,
    also when they are set while a WeChat reply has borrowed the agent."""
    backend = ChatReplyBackend(tmp_path)
    try:
        backend.window("窗口第一句")
        backend.wechat("微信第一句")
        # The window joins the local mailbox of Astra sessions once it is idle.
        deadline = time.monotonic() + 20
        while backend.command("/peers").result["error"] and time.monotonic() < deadline:
            time.sleep(0.2)
        answered = backend.hold()

        goal = backend.command("/goal 窗口的目标")
        assert goal.of("goal_status")[-1]["goal"]["session_id"] == WINDOW
        assert backend.command("/goal pause").result["output"].startswith("Goal paused")
        memory = backend.command("/memory working assumptions 只属于窗口的假设")
        assert memory.result["error"] == "" and memory.of("working_memory")[-1]["session_id"] == WINDOW
        wakeup = backend.command("/wakeup after 600 看看窗口", until=lambda e: e.get("type") == "wakeup_status")
        assert wakeup.of("wakeup_status")[-1]["plan"]["session"] == WINDOW
        backend.command("/wakeup cancel", until=lambda e: e.get("type") == "wakeup_status")
        handoff = backend.command("/handoff --raw")
        saved = tmp_path / "work" / handoff.result["output"].removeprefix("Saved redacted session handoff to ")
        assert saved.name.startswith(f"{WINDOW}_")
        conversation = saved.read_text(encoding="utf-8").split("## Conversation Summary")[1].split("\n## ")[0]
        assert "窗口第一句" in conversation and "微信" not in conversation
        listing = backend.command("/session")
        assert f"  * {WINDOW} " in listing.result["output"]
        deleting = backend.command(f"/session delete {WINDOW}")
        assert deleting.result["error"] == "Switch to another session before deleting the active session."
        assert backend.command("/peers").result["error"] == ""
        backend.release(answered)
        # The goal's first round, started by /goal, runs in the window after the reply.
        backend.wait_for(lambda e: e.get("type") == "done", timeout=30)

        window_request = json.dumps(backend.window("窗口之后"), ensure_ascii=False)
        assert "只属于窗口的假设" in window_request and "窗口的目标" in window_request
        wechat_request = json.dumps(backend.wechat("微信之后"), ensure_ascii=False)
        assert "只属于窗口的假设" not in wechat_request and "窗口的目标" not in wechat_request
        assert (tmp_path / "sessions" / f"{WINDOW}.header.json").exists()
    finally:
        backend.close()


def test_a_window_message_during_a_wechat_reply_is_recorded_for_the_window(tmp_path):
    """The window's turn waits for the reply, but it is recorded for the window's conversation at once."""
    backend = ChatReplyBackend(tmp_path)
    try:
        backend.window("窗口第一句")
        backend.wechat("微信第一句")
        answered = backend.hold()
        backend.drain()
        send(backend.proc, {"type": "message", "text": "窗口插话"})
        started = backend.wait_for(lambda e: e.get("type") == "task_started", timeout=20)["task"]
        assert started["session_id"] == WINDOW
        backend.release(answered)
        backend.wait_for(lambda e: e.get("type") == "done", timeout=30)

        assert _person(backend.request("窗口插话")) == ["窗口第一句", "窗口插话"]
        send(backend.proc, {"type": "command", "cmd": f"/tasks {started['id']}"})
        detail = backend.wait_for(lambda e: e.get("type") == "tool_result" and e.get("name") == "tasks", timeout=20)
        assert f"Session: {WINDOW}\n" in detail["output"]
    finally:
        backend.close()
