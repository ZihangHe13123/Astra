import asyncio
import json

from agent.channels.base import ChannelMessage
from agent.channels.config import OneBotConfig
from agent.channels.onebot import OneBotAdapter
from agent.channels.router import AgentChannelRouter
from agent.runtime.memory import MemoryStore
from agent.runtime.react import ReActAgent
from agent.runtime.skills import SkillStore
from agent.runtime.tools.registry import ToolRegistry
from agent.runtime.tools.files import register_file_tools
from agent.runtime.tools.memory import register_memory_tools
from agent.runtime.tools import session_recall as recall_tool
from agent.runtime.session_recall import SessionRecall


def test_qq_admission_and_owner_authority_are_explicit():
    async def run():
        seen = []
        class Connection:
            async def send(self, payload):
                pass
        async def handler(message, sink=None):
            seen.append(message)
            return "ok"
        payload = {"post_type": "message", "message_type": "private", "self_id": 42, "user_id": 1,
                   "message": "go", "local_tools": True}
        await OneBotAdapter(OneBotConfig(enabled=True), handler)._handle_payload(Connection(), payload)
        assert seen == []
        adapter = OneBotAdapter(OneBotConfig(allow_users=("1",), owner_users=("2",)), handler)
        await adapter._handle_payload(Connection(), payload)
        await adapter._handle_payload(Connection(), {**payload, "user_id": 2})
        await adapter._handle_payload(Connection(), {**payload, "user_id": 2, "message_type": "group", "group_id": 3, "message": "/ hi"})
        assert [message.local_tools for message in seen] == [False, True, False]
    asyncio.run(run())


def test_chat_scope_blocks_tools_and_automatic_owner_context_and_restores_owner(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ASTRA_HOME", str(tmp_path / "state"))
    (tmp_path / "AGENTS.md").write_text("OWNER_PROJECT_MARKER")
    registry = ToolRegistry()
    register_file_tools(registry, workdir=str(tmp_path))
    store = MemoryStore(tmp_path / "memory.db")
    record, _ = store.remember(kind="user_fact", summary="OWNER_MEMORY_MARKER")
    recall = SessionRecall(tmp_path / "recall.db")
    recall.init_db()
    sid = recall.get_or_create_session("owner")
    recall.log_message(sid, "user", "OWNER_HISTORY_MARKER")
    monkeypatch.setattr(recall_tool, "_SR", recall)
    recall_tool.register_session_recall_tools(registry)
    denied = []
    class Model:
        async def chat_stream(self, messages, tools):
            text = json.dumps(messages)
            for marker in ("OWNER_PROMPT_MARKER", "OWNER_MEMORY_MARKER", "OWNER_HISTORY_MARKER", "OWNER_PROJECT_MARKER", "OWNER_RUNTIME_MARKER"):
                assert marker not in text
            for name, args in [("write_file", {"path": str(tmp_path / "bad.txt"), "content": "bad"}),
                               ("memory", {"action": "read", "memory_id": record.record_id}),
                               ("session_search", {"query": "OWNER_HISTORY_MARKER"})]:
                # A nested task bypassing schemas must still meet the caller ceiling.
                denied.append(await asyncio.create_task(registry.execute(name, args)))
            yield {"type": "done", "content": "chat", "usage": None}
    agent = ReActAgent("fixture", Model(), registry, system_prompt="OWNER_PROMPT_MARKER",
                       skill_store=SkillStore(tmp_path / "skills"), timing_log_enabled=False, memory_store=store)
    register_memory_tools(registry, store, session_id=lambda: agent.context.session_scope)
    agent.context.set_session(str(tmp_path / "owner.json"))
    agent.runtime_context_provider = lambda: "OWNER_RUNTIME_MARKER"
    router = AgentChannelRouter(agent, asyncio.Lock(), tmp_path / "sessions")
    async def run():
        for sender in ("A", "B", "A"):
            await router.handle(ChannelMessage("qq", sender, sender, "hello"))
        await router.handle(ChannelMessage("qq", "A", "A", "/new"))
        await router.handle(ChannelMessage("qq", "A", "A", "after reset"))
        assert agent.memory_store is store and agent.context.system_prompt == "OWNER_PROMPT_MARKER"
        result = await registry.execute("memory", {"action": "read", "memory_id": record.record_id})
        assert "OWNER_MEMORY_MARKER" in result["output"]
    asyncio.run(run())
    assert all(result["code"] == "caller_scope_denied" for result in denied)
    assert not (tmp_path / "bad.txt").exists()


def test_no_echo_matches_only_the_unique_request_on_its_connection(monkeypatch):
    from websockets.asyncio.client import connect
    monkeypatch.delenv("ASTRA_QQ_ACCESS_TOKEN", raising=False)
    async def run():
        async def unused(*args):
            return ""
        adapter = OneBotAdapter(OneBotConfig(host="127.0.0.1", port=0), unused)
        await adapter.start()
        try:
            url = f"ws://127.0.0.1:{adapter._server.sockets[0].getsockname()[1]}"
            async with connect(url) as a:
                await a.ping()
                while len(adapter._connections) < 1:
                    await asyncio.sleep(0)
                server_a = next(iter(adapter._connections))
                async with connect(url) as b:
                    await b.ping()
                    while len(adapter._connections) < 2:
                        await asyncio.sleep(0)
                    server_b = next(c for c in adapter._connections if c is not server_a)
                    first = asyncio.create_task(adapter._call_action(server_a, "fixture", {"for": "A"}))
                    second = asyncio.create_task(adapter._call_action(server_b, "fixture", {"for": "B"}))
                    req_a = json.loads(await asyncio.wait_for(a.recv(), 2))
                    await asyncio.wait_for(b.recv(), 2)
                    await b.send(json.dumps({"status": "ok", "retcode": 0, "data": "B"}))
                    assert (await asyncio.wait_for(second, 2))["data"] == "B"
                    assert not first.done()
                    await a.send(json.dumps({"status": "ok", "retcode": 0, "echo": req_a["echo"], "data": "A"}))
                    assert (await asyncio.wait_for(first, 2))["data"] == "A"
        finally:
            await adapter.stop()
    asyncio.run(run())


def test_ambiguous_no_echo_is_unknown_for_all_candidates_and_never_replayed(monkeypatch):
    from websockets.asyncio.client import connect
    monkeypatch.delenv("ASTRA_QQ_ACCESS_TOKEN", raising=False)
    async def run():
        async def unused(*args):
            return ""
        adapter = OneBotAdapter(OneBotConfig(host="127.0.0.1", port=0), unused)
        await adapter.start()
        try:
            url = f"ws://127.0.0.1:{adapter._server.sockets[0].getsockname()[1]}"
            async with connect(url) as client:
                while not adapter._connections:
                    await asyncio.sleep(0)
                server = next(iter(adapter._connections))
                actions = [asyncio.create_task(adapter._call_action(server, "fixture", {"id": i})) for i in (1, 2)]
                requests = [json.loads(await asyncio.wait_for(client.recv(), 2)) for _ in actions]
                assert len({request["echo"] for request in requests}) == 2
                await client.send(json.dumps({"status": "ok", "retcode": 0}))
                results = await asyncio.wait_for(asyncio.gather(*actions, return_exceptions=True), 2)
                assert all(isinstance(result, RuntimeError) and "outcome unknown" in str(result) for result in results)
                assert not adapter._pending_actions
                try:
                    await asyncio.wait_for(client.recv(), .02)
                except TimeoutError:
                    pass
                else:
                    raise AssertionError("an unknown action was replayed")
        finally:
            await adapter.stop()
    asyncio.run(run())
