"""A chat's turn while the window is in a private mode (/bar, /minimal, a local mode).

Those modes put the work agent's memory, tools and runtime providers aside and set their own on the agent itself.
A WeChat or QQ turn borrows that agent (AgentChannelRouter), so it has to run with the work agent's own settings
whatever mode the window is in, and the window has to be in its mode as before once the turn has ended.

The real-backend tests ask WeChat once with the window in work mode, open the mode and talk in the window, ask
WeChat again and talk in the window again, then compare what the model was sent each time. WeChat's model runs a
shell command each time; the approval the command asks for shows where it would have run.
"""

import asyncio
import io
import json
import re
import time
from queue import Empty

import pytest

from agent.channels.base import ChannelMessage
from agent.channels.router import AgentChannelRouter
from agent.runtime.bar_mode import BarModeController
from agent.runtime.local_mode import LocalMode
from agent.runtime.memory import MemoryStore
from agent.runtime.minimal_mode import MINIMAL_SYSTEM_PROMPT, MINIMAL_TOOL_NAMES, MinimalModeController
from agent.runtime.react import ReActAgent
from agent.runtime.skills import SkillStore
from agent.runtime.task_store import TaskStore
from agent.runtime.tools.access_scope import CHAT_TOOLS
from agent.runtime.tools.files import register_file_tools
from agent.runtime.tools.registry import ToolRegistry
from agent.sandbox.docker import DockerSandbox
from agent.sandbox.router import SandboxRouter
from test_session_lifecycle_protocol import send
from test_window_commands_during_chat_reply import ChatReplyBackend, RequestRecorder, _text

# A saved memory: the work agent shows it in every request, and no private mode does.
MEMORY = "主人的猫叫小满。"
# One saved while the window is in a mode. The work agent sends memory again only when it has changed, so this
# shows that a chat's turn reads the memory itself, not only what its conversation was sent before.
LATER = "主人周五晚上要去看演出。"
BAR_TURN = {"type": "function", "function": {"name": "bar_turn"}}


def _words(messages: list[dict]) -> list[str]:
    """What the person said in a request, in order. A mode may put its runtime state before their words."""
    return [_text(message).split("</message_time>", 1)[1].strip() for message in messages
            if message.get("role") == "user" and "</message_time>" in _text(message)]


def _after_words(messages: list[dict]) -> list[dict]:
    """What follows the person's latest words in a request: the turn's tool calls and results so far."""
    latest = max(index for index, message in enumerate(messages)
                 if message.get("role") == "user" and "</message_time>" in _text(message))
    return messages[latest + 1:]


def _first_of_turn(messages: list[dict]) -> bool:
    return all(message.get("role") != "tool" for message in _after_words(messages))


def _settings(payload: dict) -> dict:
    """What a request was sent with besides its conversation: the tools by name, the tool choice, sampling."""
    return {**{key: value for key, value in payload.items() if key not in {"messages", "tools"}},
            "tools": sorted(tool["function"]["name"] for tool in payload.get("tools") or [])}


def _system(payload: dict) -> str:
    """The system message. The turn count in the bar's scene state is left out: each window turn adds one."""
    first = payload["messages"][0]
    return re.sub(r"(?m)^shift_turn: \d+$", "shift_turn: …", _text(first)) if first.get("role") == "system" else ""


def _sent(payload: dict) -> str:
    return json.dumps(payload["messages"], ensure_ascii=False)


class ModeRecorder(RequestRecorder):
    """Keeps each conversation request whole. Calls bar_turn when a request forces it, and answers a WeChat
    message ("微信…") with a shell command that does nothing; any other request gets ModelHandler's text. The
    WeChat message that is RequestRecorder.hold waits for RequestRecorder.release first."""

    payloads: list[dict] = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}"
        payload = json.loads(body)
        messages = payload.get("messages") or []
        call = None
        if messages and payload.get("stream"):
            type(self).payloads.append(payload)
            said = _words(messages)
            if payload.get("tool_choice") == BAR_TURN:
                call = ("bar_turn", {"reply": "吧台这边听到了。", "actions": []})
            elif said and said[-1].startswith("微信") and _first_of_turn(messages):
                if said[-1] == RequestRecorder.hold and not RequestRecorder.held.is_set():
                    RequestRecorder.held.set()
                    RequestRecorder.release.wait(60)
                # A no-op, since with YOLO on it runs on this machine without asking.
                call = ("execute_shell", {"command": "true"})
        if call is None:
            self.rfile = io.BytesIO(body)
            super().do_POST()
            return
        name, arguments = call
        base = {"id": "fixture", "object": "chat.completion.chunk", "model": "Qwen3.6-35B-A3B"}
        delta = {"role": "assistant", "tool_calls": [{
            "index": 0, "id": f"call-{name}-{len(type(self).payloads)}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}]}
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for choice in ({"index": 0, "delta": delta, "finish_reason": None},
                       {"index": 0, "delta": {}, "finish_reason": "tool_calls"}):
            self.wfile.write(("data: " + json.dumps({**base, "choices": [choice]}, ensure_ascii=False)
                              + "\n\n").encode())
        self.wfile.write(b"data: [DONE]\n\n")


class ModesBackend(ChatReplyBackend):
    recorder = ModeRecorder

    def __init__(self, tmp_path):
        ModeRecorder.payloads = []
        MemoryStore(tmp_path / "memory.db").add_core("user", MEMORY)
        super().__init__(tmp_path)

    def payload(self, said: str, timeout: float = 30) -> dict:
        """The first request of the turn that answered ``said``."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for payload in list(ModeRecorder.payloads):
                if _words(payload["messages"])[-1:] == [said]:
                    return payload
            time.sleep(0.05)
        raise AssertionError(f"no model request answered {said!r}")

    def request(self, said: str, timeout: float = 30) -> list[dict]:
        return self.payload(said, timeout)["messages"]

    def ask(self, text: str) -> dict:
        """The window says ``text``: what the model was sent for it."""
        self.window(text)
        return self.payload(text)

    def ask_wechat(self, text: str) -> tuple[dict, dict | None]:
        """WeChat says ``text``, and its model runs a shell command that does nothing."""
        return self.answer_wechat(self.write_wechat(text), text)

    def answer_wechat(self, answered: int, text: str) -> tuple[dict, dict | None]:
        """Until WeChat has the reply to ``text``, refusing the approval its shell command asks for. The turn's
        first request, and that approval (None when the command ran without asking)."""
        approval = None
        deadline = time.monotonic() + 30
        while approval is None and not self.replied(answered) and time.monotonic() < deadline:
            try:
                event = self.events.get(timeout=0.05)
            except Empty:
                continue
            self.seen.append(event)
            if event.get("type") == "tool_approval_request":
                approval = event
                send(self.proc, {"type": "tool_approval_response", "request_id": event["request_id"],
                                 "decision": "deny"})
        self.wait_reply(answered)
        return self.payload(text), approval

    def outcome(self, said: str) -> str:
        """What the model was told the shell command of the turn that answered ``said`` did."""
        for payload in ModeRecorder.payloads:
            messages = payload["messages"]
            if _words(messages)[-1:] == [said] and not _first_of_turn(messages):
                return next(_text(message) for message in _after_words(messages) if message.get("role") == "tool")
        raise AssertionError(f"no tool result in the turn that answered {said!r}")

    def yolo(self, command: str = "/yolo status") -> dict:
        """A /yolo command from the window, and the yolo_status it is answered with: the window's YOLO."""
        self.drain()
        send(self.proc, {"type": "command", "cmd": command})
        return self.wait_for(lambda e: e.get("type") == "yolo_status", timeout=20)


@pytest.mark.parametrize(("command", "mode", "tools", "prompt", "work_yolo", "mode_yolo"), [
    # The bar leaves YOLO as it is.
    ("/bar", "bar", ["bar_turn"], "[BAR SCENE STATE", False, False),
    # Minimal starts with YOLO off, and the window turns it on for itself; the work agent's is off.
    ("/minimal", "minimal", sorted(MINIMAL_TOOL_NAMES), MINIMAL_SYSTEM_PROMPT, False, True),
], ids=["bar", "minimal"])
def test_a_wechat_turn_runs_as_in_work_mode_and_the_window_stays_in_its_mode(tmp_path, command, mode, tools,
                                                                             prompt, work_yolo, mode_yolo):
    backend = ModesBackend(tmp_path)
    try:
        if work_yolo:
            assert backend.yolo("/yolo on")["yolo"] is True
        backend.ask("窗口在工作")
        work_chat, work_approval = backend.ask_wechat("微信在窗口工作时")
        opened = backend.command(command)
        assert opened.result["error"] == "" and opened.of("mode_info")[-1]["mode"] == mode
        if mode_yolo:
            assert backend.yolo("/yolo on")["yolo"] is True
        assert backend.yolo()["yolo"] is mode_yolo
        before = backend.ask("窗口进了模式")
        MemoryStore(tmp_path / "memory.db").add_core("user", LATER)
        chat, approval = backend.ask_wechat("微信在窗口的模式里")
        window_yolo = backend.yolo()["yolo"]
        after = backend.ask("窗口还在模式里")

        # The window talks in the mode: its tools and prompt, and no memory.
        assert _settings(before)["tools"] == tools and prompt in _system(before)
        assert MEMORY not in _sent(before)
        # WeChat is answered as in work mode: the same tools, tool choice and sampling, the same system message,
        # the work memory as it is now, and none of the mode's state.
        assert _settings(chat) == _settings(work_chat)
        assert _system(chat) == _system(work_chat)
        assert MEMORY in _sent(work_chat) and LATER in _sent(chat)
        assert "BAR SCENE STATE" not in _sent(chat) and "CURRENT RUNTIME STATE" not in _sent(chat)
        # Its shell command is approved as in work mode, whatever the window's YOLO: on this machine, asked for
        # in WeChat while the work agent's YOLO is off, run at once while it is on.
        for asked in (work_approval, approval):
            if work_yolo:
                assert asked is None
            else:
                assert asked is not None and (asked["kind"], asked["channel"]) == ("host_execution", "weixin")
        assert backend.outcome("微信在窗口的模式里") == backend.outcome("微信在窗口工作时")
        # Then the window is in its mode as before, its YOLO too.
        assert window_yolo is mode_yolo
        assert _settings(after) == _settings(before) and _system(after) == _system(before)
        assert MEMORY not in _sent(after) and LATER not in _sent(after)
    finally:
        backend.close()


def test_yolo_from_the_window_waits_for_a_chat_reply(tmp_path):
    """The YOLO switch is the agent's, and a chat's reply holds the agent with the YOLO it started with. So /yolo
    from the window is refused while the reply is under way, and /yolo status shows the window's own; neither
    the reply nor the window's YOLO changes."""
    backend = ModesBackend(tmp_path)
    try:
        backend.command("/minimal")
        assert backend.yolo("/yolo on")["yolo"] is True
        RequestRecorder.hold = "微信慢慢来"
        answered = backend.write_wechat(RequestRecorder.hold)
        assert RequestRecorder.held.wait(30), "the WeChat reply is under way"

        refused = backend.yolo("/yolo off")
        assert (refused["error"], refused["yolo"]) == (
            "Astra is answering a WeChat message right now. Run /yolo again once that reply has been sent.", True)
        status = backend.yolo()
        assert (status["error"], status["yolo"]) == ("", True)
        RequestRecorder.release.set()
        # The reply goes on with the work agent's YOLO, which is off: its shell command asks.
        _, approval = backend.answer_wechat(answered, RequestRecorder.hold)
        assert approval is not None and (approval["kind"], approval["channel"]) == ("host_execution", "weixin")
        assert backend.yolo()["yolo"] is True
        assert backend.yolo("/yolo off")["yolo"] is False
    finally:
        backend.close()


class _HostSandbox:
    """Where tools run on this machine. Nothing runs here: these turns call no tool."""

    timeout = 30
    workdir = "."


def _work_agent(tmp_path, model) -> ReActAgent:
    """A work agent with file tools, saved memory, skills and a task store, its tools on this machine."""
    registry = ToolRegistry()
    register_file_tools(registry, workdir=str(tmp_path))
    memory = MemoryStore(tmp_path / "memory.db")
    memory.add_core("user", MEMORY)
    agent = ReActAgent("fixture", model, registry, system_prompt="work prompt", memory_store=memory,
                       skill_store=SkillStore(tmp_path / "skills"), task_store=TaskStore(tmp_path / "tasks.db"),
                       timing_log_enabled=False)
    agent.context.set_session(str(tmp_path / "work.json"))
    agent._sandbox = SandboxRouter(_HostSandbox(), local_factory=_HostSandbox,  # type: ignore[arg-type]
                                   docker_factory=lambda: DockerSandbox(workdir=str(tmp_path)))
    return agent


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ASTRA_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("HINDSIGHT_ENABLED", "0")
    return tmp_path


@pytest.mark.parametrize("mode_type", [BarModeController, MinimalModeController])
def test_a_private_mode_lends_back_every_work_setting_it_replaced(isolated, mode_type):
    """work_settings() names every agent setting the mode changed, with the work agent's own value, which is
    what leave() puts back; a chat's turn runs with these."""
    agent = _work_agent(isolated, model=None)
    agent.tools.yolo = True
    mode = mode_type(agent)

    def settings() -> dict:
        public = {name: value for name, value in vars(agent).items()
                  if not name.startswith("_") and name != "context"}
        return {**public, "sandbox_mode": agent.sandbox_mode, "yolo": agent.tools.yolo}

    work = settings()
    assert mode.work_settings() == {}
    assert mode.enter(isolated / "mode.json")
    lent = mode.work_settings()
    changed = {name for name, value in settings().items() if value != work[name]}
    assert changed and changed <= set(lent), changed - set(lent)
    assert lent == {name: work[name] for name in lent}
    assert mode.leave()
    assert settings() == work and mode.work_settings() == {}


def test_a_local_mode_lends_back_what_its_controller_replaced():
    """Public Astra bundles no local mode. An installed one whose controller replaces agent settings names them,
    with the work agent's own values, in its own work_settings(); the host passes them on only while the mode
    is open, and a controller without that method lends nothing."""
    class Controller:
        def __init__(self, lent: dict | None = None):
            self.active, self.session_name = False, ""
            if lent is not None:
                self.work_settings = lambda: dict(lent)

        def enter(self, path): return True
        def switch(self, path): return None
        def leave(self): return True
        def undo_last_reply(self): return False, ""
        def undo_last_exchanges(self, count): return False, ""
        def take_last_request(self): return False, "", ""

    def host(controller: Controller) -> LocalMode:
        return LocalMode(controller, command="/example", label="Example", description="An example local mode.",
                         namespace="example")

    lending = Controller({"tools_enabled": True, "yolo": True})
    mode = host(lending)
    assert mode.work_settings() == {}
    lending.active = True
    assert mode.work_settings() == {"tools_enabled": True, "yolo": True}
    lending.active = False
    assert mode.work_settings() == {}
    silent = Controller()
    silent.active = True
    assert host(silent).work_settings() == {}
    assert LocalMode().work_settings() == {}


def test_chat_turns_borrow_the_work_settings_and_give_minimal_mode_its_own_back(isolated):
    """With the window in Minimal mode and YOLO on there, the owner's WeChat turn runs with the work tools, memory
    and YOLO, its tools on this machine, and a contact's QQ turn gets only the chat tools on top of that. Then the
    window is in Minimal mode as before, its tools in Docker and its YOLO on."""
    seen: list[dict] = []

    class Model:
        async def chat_stream(self, messages, tools, **options):
            seen.append({"tools": {tool["function"]["name"] for tool in tools}, "sandbox": agent.sandbox_mode,
                         "yolo": agent.tools.yolo, "sent": json.dumps(messages, ensure_ascii=False)})
            yield {"type": "done", "content": "好的", "usage": None}

    agent = _work_agent(isolated, Model())
    # As in the backend: the router is set up while the window is in work mode.
    minimal = MinimalModeController(agent)
    router = AgentChannelRouter(agent, asyncio.Lock(), isolated / "sessions", work_settings=minimal.work_settings)
    assert minimal.enter(isolated / "minimal.json")
    agent.tools.yolo = True  # /yolo on in the window; the work agent's YOLO stays off
    conversation = agent.context
    window = {name: getattr(agent, name) for name in minimal.work_settings()}
    assert (window["sandbox_mode"], window["minimal_mode"], window["yolo"]) == ("docker", True, True)

    async def chat() -> None:
        await router.handle(ChannelMessage("weixin", "owner", "owner", "微信你好"))
        await router.handle(ChannelMessage("qq", "contact", "contact", "QQ 你好"))

    asyncio.run(chat())

    wechat, contact = seen
    assert "write_file" in wechat["tools"] and MEMORY in wechat["sent"] and wechat["sandbox"] == "local"
    assert contact["tools"] <= CHAT_TOOLS and MEMORY not in contact["sent"] and contact["sandbox"] == "local"
    assert wechat["yolo"] is False and contact["yolo"] is False
    assert agent.context is conversation and {name: getattr(agent, name) for name in window} == window
    assert minimal.leave() and agent.sandbox_mode == "local" and agent.memory_store is not None
