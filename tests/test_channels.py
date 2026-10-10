import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from websockets.asyncio.client import connect

from agent.channels.base import ChannelAdapter, ChannelImage, ChannelMessage
from agent.channels.config import ChannelsConfig, OneBotConfig, load_channels_config
from agent.channels.manager import ChannelManager, is_address_in_use
from agent.channels.media import download_image_data_url
from agent.channels.onebot import OneBotAdapter
from agent.channels.router import (
    AgentChannelRouter,
    active_channel_message,
    channel_session_name,
)
from agent.channels.tools import register_channel_tools
from agent.runtime.context import AgentContext
from agent.runtime.tools.policy import ToolPolicy
from agent.runtime.tools.registry import ToolRegistry


class FakeConnection:
    def __init__(self):
        self.sent: list[str] = []

    async def send(self, value: str):
        self.sent.append(value)


def test_channel_config_is_opt_in_and_supports_env_override(tmp_path, monkeypatch):
    path = tmp_path / "channels.json"
    path.write_text(json.dumps({
        "qq": {
            "enabled": False,
            "port": 2299,
            "wake_prefixes": ["!", "/astra "],
        }
    }), encoding="utf-8")

    config = load_channels_config(path)
    assert config.enabled is False
    assert config.onebot.port == 2299
    assert config.onebot.wake_prefixes == ("!", "/astra ")

    monkeypatch.setenv("ASTRA_QQ_ENABLED", "1")
    assert load_channels_config(path).onebot.enabled is True


def test_channel_session_names_are_stable_and_isolated():
    private = ChannelMessage("qq", "123", "123", "hello")
    same_private = ChannelMessage("qq", "123", "123", "again")
    group = ChannelMessage("qq", "123", "456", "hello", is_group=True)
    assert channel_session_name(private) == channel_session_name(same_private)
    assert channel_session_name(private) != channel_session_name(group)
    assert "123" not in channel_session_name(private)


def test_onebot_private_message_routes_and_replies():
    async def scenario():
        seen = []

        async def handler(message, reply_sink=None):
            seen.append(message)
            return "pong"

        adapter = OneBotAdapter(OneBotConfig(), handler)
        connection = FakeConnection()
        await adapter._handle_payload(connection, {
            "post_type": "message",
            "message_type": "private",
            "self_id": 42,
            "user_id": 10001,
            "message_id": 7,
            "message": [{"type": "text", "data": {"text": "ping"}}],
            "sender": {"nickname": "tester"},
        })

        assert len(seen) == 1
        assert seen[0].conversation_id == "10001"
        assert seen[0].text == "ping"
        payload = json.loads(connection.sent[0])
        assert payload["action"] == "send_private_msg"
        assert payload["params"] == {"message": "pong", "user_id": 10001}

    asyncio.run(scenario())


def test_onebot_downloads_image_segments_for_the_agent(monkeypatch):
    async def scenario():
        seen = []

        async def fake_download(url, *, max_bytes):
            assert url == "https://example.com/qq.png"
            assert max_bytes == 12345
            return "data:image/png;base64,aW1hZ2U=", "image/png"

        async def handler(message, reply_sink=None):
            seen.append(message)
            return "seen"

        monkeypatch.setattr(
            "agent.channels.onebot.download_image_data_url",
            fake_download,
        )
        adapter = OneBotAdapter(OneBotConfig(max_image_bytes=12345), handler)
        connection = FakeConnection()
        await adapter._handle_payload(connection, {
            "post_type": "message",
            "message_type": "private",
            "self_id": 42,
            "user_id": 10001,
            "message": [{
                "type": "image",
                "data": {
                    "file": "qq.png",
                    "url": "https://example.com/qq.png",
                },
            }],
        })

        assert seen[0].text == "[image]"
        assert seen[0].images == (
            ChannelImage(
                "data:image/png;base64,aW1hZ2U=",
                "qq.png",
                "https://example.com/qq.png",
            ),
        )

    asyncio.run(scenario())


def test_channel_image_loader_rejects_private_urls():
    with pytest.raises(ValueError, match="public HTTP"):
        asyncio.run(download_image_data_url(
            "http://127.0.0.1/private.png",
            max_bytes=1024,
        ))


def test_onebot_group_requires_mention_or_prefix():
    async def scenario():
        seen = []

        async def handler(message, reply_sink=None):
            seen.append(message)
            return "ok"

        adapter = OneBotAdapter(
            OneBotConfig(group_require_mention=True, wake_prefixes=("/",)),
            handler,
        )
        connection = FakeConnection()
        base = {
            "post_type": "message",
            "message_type": "group",
            "self_id": 42,
            "user_id": 10001,
            "group_id": 20002,
            "sender": {"card": "Alice"},
        }
        await adapter._handle_payload(connection, {**base, "message": "ordinary chat"})
        assert seen == []
        await adapter._handle_payload(connection, {**base, "message": "/ hello"})
        assert seen[-1].text == "hello"
        await adapter._handle_payload(connection, {
            **base,
            "message": [
                {"type": "at", "data": {"qq": "42"}},
                {"type": "text", "data": {"text": " hi"}},
            ],
        })
        assert seen[-1].text == "hi"
        assert len(connection.sent) == 2

    asyncio.run(scenario())


def test_onebot_server_accepts_a_real_reverse_websocket():
    async def scenario():
        async def handler(message, reply_sink=None):
            return f"reply:{message.text}"

        adapter = OneBotAdapter(OneBotConfig(host="127.0.0.1", port=0), handler)
        await adapter.start()
        try:
            port = adapter._server.sockets[0].getsockname()[1]
            async with connect(f"ws://127.0.0.1:{port}") as websocket:
                await websocket.send(json.dumps({
                    "post_type": "message",
                    "message_type": "private",
                    "self_id": 42,
                    "user_id": 10001,
                    "message": "ping",
                }))
                response = json.loads(await asyncio.wait_for(websocket.recv(), timeout=2))
                assert response["action"] == "send_private_msg"
                assert response["params"]["message"] == "reply:ping"
        finally:
            await adapter.stop()

    asyncio.run(scenario())


def test_onebot_sends_reply_phases_before_handler_finishes():
    async def scenario():
        release_second_phase = asyncio.Event()

        async def handler(message, reply_sink=None):
            assert reply_sink is not None
            await reply_sink("before tool")
            await release_second_phase.wait()
            await reply_sink("after tool")
            return ""

        adapter = OneBotAdapter(OneBotConfig(host="127.0.0.1", port=0), handler)
        await adapter.start()
        try:
            port = adapter._server.sockets[0].getsockname()[1]
            async with connect(f"ws://127.0.0.1:{port}") as websocket:
                await websocket.send(json.dumps({
                    "post_type": "message",
                    "message_type": "private",
                    "self_id": 42,
                    "user_id": 10001,
                    "message": "ping",
                }))
                first = json.loads(await asyncio.wait_for(websocket.recv(), timeout=2))
                assert first["params"]["message"] == "before tool"
                release_second_phase.set()
                second = json.loads(await asyncio.wait_for(websocket.recv(), timeout=2))
                assert second["params"]["message"] == "after tool"
        finally:
            await adapter.stop()

    asyncio.run(scenario())


def test_onebot_uploads_a_private_file_and_waits_for_action_result(tmp_path):
    async def scenario():
        async def handler(message, reply_sink=None):
            return "unused"

        adapter = OneBotAdapter(OneBotConfig(host="127.0.0.1", port=0), handler)
        await adapter.start()
        try:
            port = adapter._server.sockets[0].getsockname()[1]
            async with connect(f"ws://127.0.0.1:{port}") as websocket:
                await asyncio.sleep(0)
                message = ChannelMessage("qq", "10001", "10001", "send it")
                task = asyncio.create_task(adapter.send_file(
                    message,
                    str(tmp_path / "report.txt"),
                    "report.txt",
                ))
                request = json.loads(await asyncio.wait_for(websocket.recv(), timeout=2))
                assert request["action"] == "upload_private_file"
                assert request["params"]["user_id"] == 10001
                await websocket.send(json.dumps({
                    "status": "ok",
                    "retcode": 0,
                    "data": {},
                    "echo": request["echo"],
                }))
                result = await asyncio.wait_for(task, timeout=2)
                assert result["status"] == "ok"
        finally:
            await adapter.stop()

    asyncio.run(scenario())


def test_onebot_file_action_accepts_napcat_response_with_empty_echo(tmp_path):
    async def scenario():
        async def handler(message, reply_sink=None):
            return "unused"

        adapter = OneBotAdapter(OneBotConfig(host="127.0.0.1", port=0), handler)
        await adapter.start()
        try:
            port = adapter._server.sockets[0].getsockname()[1]
            async with connect(f"ws://127.0.0.1:{port}") as websocket:
                await asyncio.sleep(0)
                message = ChannelMessage("qq", "10001", "10001", "send it")
                task = asyncio.create_task(adapter.send_file(
                    message,
                    str(tmp_path / "report.txt"),
                    "report.txt",
                ))
                request = json.loads(await asyncio.wait_for(websocket.recv(), timeout=2))
                assert request["echo"].startswith("astra-")
                await websocket.send(json.dumps({
                    "status": "ok",
                    "retcode": 0,
                    "data": {},
                    "echo": "",
                }))
                result = await asyncio.wait_for(task, timeout=2)
                assert result["status"] == "ok"
        finally:
            await adapter.stop()

    asyncio.run(scenario())


def test_onebot_file_action_preserves_failure_with_empty_echo(tmp_path):
    async def scenario():
        async def handler(message, reply_sink=None):
            return "unused"

        adapter = OneBotAdapter(OneBotConfig(host="127.0.0.1", port=0), handler)
        await adapter.start()
        try:
            port = adapter._server.sockets[0].getsockname()[1]
            async with connect(f"ws://127.0.0.1:{port}") as websocket:
                await asyncio.sleep(0)
                message = ChannelMessage("qq", "10001", "10001", "send it")
                task = asyncio.create_task(adapter.send_file(
                    message,
                    str(tmp_path / "report.txt"),
                    "report.txt",
                ))
                await asyncio.wait_for(websocket.recv(), timeout=2)
                await websocket.send(json.dumps({
                    "status": "failed",
                    "retcode": 1400,
                    "message": "file upload failed",
                    "echo": "",
                }))
                with pytest.raises(RuntimeError, match="file upload failed"):
                    await asyncio.wait_for(task, timeout=2)
        finally:
            await adapter.stop()

    asyncio.run(scenario())


class FakeAdapter(ChannelAdapter):
    name = "fake"

    def __init__(self, handler):
        super().__init__(handler)
        self.started = False
        self.stopped = False

    @property
    def running(self):
        return self.started and not self.stopped

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True


def test_channel_manager_owns_adapter_lifecycle():
    async def scenario():
        async def handler(message, reply_sink=None):
            return message.text

        adapter = FakeAdapter(handler)
        manager = ChannelManager(ChannelsConfig(), handler, adapters=[adapter])
        await manager.start()
        assert manager.statuses() == [{"name": "fake", "running": True}]
        await manager.stop()
        assert adapter.stopped is True

    asyncio.run(scenario())


def test_channel_manager_rolls_back_partially_started_adapter():
    class FailingAdapter(FakeAdapter):
        name = "failing"

        async def start(self):
            self.started = True
            raise RuntimeError("startup failed after acquiring resources")

    async def scenario():
        async def handler(message, reply_sink=None):
            return message.text

        adapter = FailingAdapter(handler)
        manager = ChannelManager(ChannelsConfig(), handler, adapters=[adapter])
        try:
            await manager.start()
        except RuntimeError as exc:
            assert str(exc) == "startup failed after acquiring resources"
        else:
            raise AssertionError("manager.start() should propagate adapter failure")
        assert adapter.stopped is True
        assert manager.statuses() == [{"name": "failing", "running": False}]

    asyncio.run(scenario())


class FakeAgent:
    def __init__(self, session_path: Path):
        self.context = AgentContext(system_prompt="system", max_messages=20, max_prompt_tokens=1000)
        self.context.set_session(str(session_path))
        self.llm = object()

    async def reply_stream(self, message):
        self.context.add_user(message.get_text())
        reply = f"{Path(self.context.session_path).stem}:{message.get_text()}"
        self.context.add_assistant(reply)
        yield {"type": "chunk", "content": reply}
        yield {"type": "done", "content": reply}


def test_agent_channel_router_restores_tui_context_and_isolates_chats(tmp_path):
    async def scenario():
        tui_path = tmp_path / "tui.json"
        agent = FakeAgent(tui_path)
        original = agent.context
        router = AgentChannelRouter(agent, asyncio.Lock(), tmp_path)

        first = ChannelMessage("qq", "peer-a", "peer-a", "one")
        second = ChannelMessage("qq", "peer-b", "peer-b", "two")
        again = ChannelMessage("qq", "peer-a", "peer-a", "three")
        await router.handle(first)
        await router.handle(second)
        await router.handle(again)

        assert agent.context is original
        first_context = router._contexts[channel_session_name(first)]
        second_context = router._contexts[channel_session_name(second)]
        assert [item["content"] for item in first_context.messages if item["role"] == "user"] == ["one", "three"]
        assert [item["content"] for item in second_context.messages if item["role"] == "user"] == ["two"]

    asyncio.run(scenario())


def test_agent_channel_router_flushes_at_tool_call_boundary(tmp_path):
    emitted: list[str] = []

    class PhaseAgent(FakeAgent):
        async def reply_stream(self, message):
            self.context.add_user(message.get_text())
            yield {"type": "chunk", "content": "我先检查一下。"}
            yield {
                "type": "tool_calls",
                "calls": [{"id": "call-1", "name": "search", "arguments": {}}],
            }
            assert emitted == ["我先检查一下。"]
            yield {
                "type": "tool_result",
                "id": "call-1",
                "name": "search",
                "output": "found",
            }
            yield {"type": "chunk", "content": "找到了结果。"}
            yield {"type": "done", "content": "找到了结果。"}

    async def scenario():
        agent = PhaseAgent(tmp_path / "tui.json")
        router = AgentChannelRouter(agent, asyncio.Lock(), tmp_path)

        async def reply_sink(text: str):
            emitted.append(text)

        response = await router.handle(
            ChannelMessage("qq", "peer-a", "peer-a", "查一下"),
            reply_sink,
        )
        assert response == ""
        assert emitted == ["我先检查一下。", "找到了结果。"]

    asyncio.run(scenario())


@pytest.mark.parametrize("display_name", ["", "qq.png", "../private.png", "/tmp/../private.png"])
def test_agent_channel_router_passes_images_without_trusting_display_name_as_path(
    tmp_path,
    display_name,
):
    captured = []

    class VisionAgent(FakeAgent):
        async def reply_stream(self, message):
            captured.append(message.to_chat_content())
            yield {"type": "chunk", "content": "看到了"}
            yield {"type": "done", "content": "看到了"}

    async def scenario():
        agent = VisionAgent(tmp_path / "tui.json")
        router = AgentChannelRouter(agent, asyncio.Lock(), tmp_path)
        message = ChannelMessage(
            "qq",
            "peer-a",
            "peer-a",
            "这是什么？",
            images=(ChannelImage("data:image/png;base64,aW1hZ2U=", display_name),),
        )
        await router.handle(message)
        assert captured == [[
            {"type": "text", "text": "这是什么？"},
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,aW1hZ2U="},
            },
        ]]

    asyncio.run(scenario())


def test_channel_send_file_tool_is_scoped_to_trusted_sender_and_root(tmp_path):
    class FileAdapter(FakeAdapter):
        name = "qq"

        def __init__(self, handler):
            super().__init__(handler)
            self.uploads = []

        async def send_file(self, message, path, name):
            self.uploads.append((message.sender_id, path, name))
            return {"status": "ok"}

    async def scenario():
        async def handler(message, reply_sink=None):
            return message.text

        adapter = FileAdapter(handler)
        config = ChannelsConfig(onebot=OneBotConfig(
            send_files_enabled=True,
            file_allow_users=("10001",),
            send_file_roots=(str(tmp_path),),
        ))
        manager = ChannelManager(config, handler, adapters=[adapter])
        registry = ToolRegistry()
        register_channel_tools(registry, lambda: manager)
        source = tmp_path / "report.txt"
        source.write_text("report", encoding="utf-8")
        token = active_channel_message.set(
            ChannelMessage("qq", "10001", "10001", "把报告发给我"),
        )
        try:
            result = await registry.get("channel_send_file").fn(path=str(source))
        finally:
            active_channel_message.reset(token)
        assert result["status"] == "sent"
        assert adapter.uploads == [("10001", str(source.resolve()), "report.txt")]

    asyncio.run(scenario())


def test_channel_send_file_reports_an_unconfirmed_upload_and_the_path_it_could_not_find(tmp_path):
    class SilentAdapter(FakeAdapter):
        name = "qq"

        async def send_file(self, message, path, name):
            raise TimeoutError  # how the adapter's wait for the channel's confirmation ends

    async def scenario():
        async def handler(message, reply_sink=None):
            return message.text

        config = ChannelsConfig(onebot=OneBotConfig(
            send_files_enabled=True,
            file_allow_users=("10001",),
            send_file_roots=(str(tmp_path),),
        ))
        manager = ChannelManager(config, handler, adapters=[SilentAdapter(handler)])
        registry = ToolRegistry(ToolPolicy(mode="permissive"))
        register_channel_tools(registry, lambda: manager)
        source = tmp_path / "report.txt"
        source.write_text("report", encoding="utf-8")
        missing = tmp_path / "gone" / "report.txt"
        token = active_channel_message.set(ChannelMessage("qq", "10001", "10001", "把报告发给我"))
        try:
            return (await registry.execute("channel_send_file", {"path": str(source)}),
                    await registry.execute("channel_send_file", {"path": str(missing)}), missing)
        finally:
            active_channel_message.reset(token)

    unconfirmed, not_found, missing = asyncio.run(scenario())

    # Not the registry's own "[ToolTimeout] ... exceeded 45s", which is neither the time
    # that passed nor a statement about the file.
    assert unconfirmed["code"] == "channel_send_unconfirmed"
    assert "may or may not have reached the user" in unconfirmed["error"]
    assert "exceeded" not in unconfirmed["error"]
    assert unconfirmed["retryable"] is False and "Do not send it again" in unconfirmed["recovery_hint"]
    # The whole path that was asked for, not its first missing directory.
    assert str(missing) in not_found["error"]


def test_channel_send_file_states_its_turn_limit():
    registry = ToolRegistry()
    register_channel_tools(registry, lambda: None)
    tool = registry.get("channel_send_file")
    assert f"At most {tool.max_calls_per_turn} calls per turn" in tool.description


def test_is_address_in_use_detects_common_errnos():
    assert is_address_in_use(OSError(48, "address already in use"))
    assert is_address_in_use(OSError(98, "address already in use"))
    assert is_address_in_use(OSError(10048, "address already in use"))
    assert is_address_in_use(SimpleNamespace(winerror=10048, errno=None))
    assert not is_address_in_use(OSError(13, "permission denied"))


class _PortBusyAdapter(ChannelAdapter):
    name = "qq"

    def __init__(self):
        super().__init__(lambda *args, **kwargs: asyncio.sleep(0, result="ok"))
        self.stopped = False

    async def start(self):
        raise OSError(10048, "address already in use")

    async def stop(self):
        self.stopped = True

    @property
    def running(self):
        return False


def test_channel_manager_warns_for_port_in_use(caplog):
    adapter = _PortBusyAdapter()
    manager = ChannelManager(ChannelsConfig(), adapter.handler, adapters=[adapter])

    with caplog.at_level(logging.WARNING, logger="agent.channels.manager"):
        with pytest.raises(OSError):
            asyncio.run(manager.start())

    assert adapter.stopped is True
    assert any("port already in use" in record.message for record in caplog.records)
    assert "channel failed to start" not in caplog.text
