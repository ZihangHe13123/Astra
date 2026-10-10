import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

from agent.runtime.hindsight_provider import HindsightMemoryProvider
from agent.runtime.memory import MemoryStore
from agent.runtime.memory_provider import (
    BuiltinMemoryProvider,
    FederatedMemoryProvider,
)
from agent.runtime.react import ReActAgent
from agent.runtime.tools.hindsight import register_hindsight_tools
from agent.runtime.tools.registry import ToolRegistry


class FakeRecallClient:
    def __init__(self, results=(), reflect_text="Synthesized memory answer."):
        self.results = list(results)
        self.reflect_text = reflect_text
        self.calls = []
        self.retain_calls = []
        self.manual_retain_calls = []
        self.reflect_calls = []
        self.closed = False

    async def arecall(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(results=self.results)

    async def aretain_batch(self, **kwargs):
        self.retain_calls.append(kwargs)
        return SimpleNamespace(success=True, item_count=len(kwargs["items"]))

    async def aretain(self, **kwargs):
        self.manual_retain_calls.append(kwargs)
        return SimpleNamespace(success=True, item_count=1)

    async def areflect(self, **kwargs):
        self.reflect_calls.append(kwargs)
        return SimpleNamespace(text=self.reflect_text)

    def get_bank_config(self, bank_id):
        return {"bank_id": bank_id}

    async def aclose(self):
        self.closed = True


def test_hindsight_provider_maps_sdk_results_as_non_authoritative():
    client = FakeRecallClient([
        SimpleNamespace(
            id="obs-1",
            text="User previously preferred concise answers.",
            type="observation",
            entities=["User"],
            context="conversation",
            occurred_start="2026-07-01T00:00:00+00:00",
            mentioned_at=None,
            document_id="session-old",
            metadata={"session_id": "session-old", "source": "hermes"},
            tags=["auto", "user-grounded"],
        )
    ])
    provider = HindsightMemoryProvider(
        base_url="http://127.0.0.1:8888",
        bank_id="main-v2",
        client=client,
    )

    records = asyncio.run(provider.recall("answer preference", limit=3))

    assert len(records) == 1
    record = records[0]
    assert record.record_id == "hs-obs-1"
    assert record.kind == "observation"
    assert record.source_session_id == "session-old"
    assert record.metadata["provider"] == "hindsight"
    assert record.metadata["authoritative"] is False
    assert "non-authoritative" in record.tags
    assert client.calls[0]["bank_id"] == "main-v2"
    assert client.calls[0]["max_tokens"] == 800
    asyncio.run(provider.aclose())
    assert client.closed is True


def test_hindsight_provider_rejects_remote_url_without_explicit_opt_in():
    provider = HindsightMemoryProvider(
        base_url="https://api.example.com",
        bank_id="main-v2",
    )

    health = asyncio.run(provider.health())

    assert health.state == "unavailable"
    assert "local-first policy" in health.detail


def test_federated_provider_preserves_builtin_when_external_fails(tmp_path):
    store = MemoryStore(tmp_path / "memory.db", core_dir=tmp_path / "memory")
    expected = store.add_record(
        kind="preference",
        content="User prefers concise answers",
        source_session_id="session-a",
    )

    class FailingExternal(HindsightMemoryProvider):
        async def recall(self, query, *, kinds=(), limit=8):
            raise TimeoutError("offline")

    external = FailingExternal(
        base_url="http://127.0.0.1:8888",
        bank_id="main-v2",
        client=FakeRecallClient(),
    )
    provider = FederatedMemoryProvider(BuiltinMemoryProvider(store), external)

    records = asyncio.run(provider.recall("concise answers", limit=4))

    assert [record.record_id for record in records] == [expected.record_id]
    assert "TimeoutError" in provider.last_external_error


def test_federated_provider_keeps_builtin_first_and_deduplicates(tmp_path):
    store = MemoryStore(tmp_path / "memory.db", core_dir=tmp_path / "memory")
    builtin = store.add_record(
        kind="user_fact",
        content="Test user lives in Example City",
        source_session_id="session-a",
    )
    client = FakeRecallClient([
        SimpleNamespace(
            id="duplicate",
            text="Test user lives in Example City",
            type="observation",
            occurred_start=None,
            mentioned_at=None,
            document_id="old",
            metadata={},
            tags=[],
        ),
        SimpleNamespace(
            id="extra",
            text="Test user previously discussed opening a library account",
            type="observation",
            occurred_start=None,
            mentioned_at=None,
            document_id="old",
            metadata={},
            tags=[],
        ),
    ])
    external = HindsightMemoryProvider(
        base_url="http://127.0.0.1:8888",
        bank_id="main-v2",
        client=client,
    )
    provider = FederatedMemoryProvider(BuiltinMemoryProvider(store), external)

    records = asyncio.run(provider.recall("Example City account", limit=4))

    assert records[0].record_id == builtin.record_id
    assert [record.content for record in records].count("Test user lives in Example City") == 1
    assert any(record.record_id == "hs-extra" for record in records)


def test_react_federates_builtin_authority_with_optional_hindsight(tmp_path, monkeypatch):
    monkeypatch.setenv("HINDSIGHT_ENABLED", "1")
    monkeypatch.setenv("HINDSIGHT_API_URL", "http://127.0.0.1:8888")
    monkeypatch.setenv("HINDSIGHT_BANK_ID", "main-v2")

    class FakeConfig:
        model = "fake"
        capabilities = frozenset()

    class FakeLLM:
        config = FakeConfig()

    store = MemoryStore(tmp_path / "memory.db", core_dir=tmp_path / "memory")
    tools = ToolRegistry()
    agent = ReActAgent(
        "agent",
        FakeLLM(),  # type: ignore[arg-type]
        tools,
        memory_store=store,
    )

    router = agent.memory_router
    assert router is not None
    assert isinstance(router.provider, FederatedMemoryProvider)
    assert isinstance(router.provider.authority, BuiltinMemoryProvider)
    assert agent.external_memory_provider is router.provider.external
    assert router.recall_timeout == 2.5
    assert {
        "hindsight_retain",
        "hindsight_recall",
        "hindsight_reflect",
    }.issubset(set(tools.tool_names))
    assert router.provider.capabilities.reflect is True
    assert agent.memory_retainer is not None
    assert agent.memory_retainer.provider is router.provider.authority


def test_hindsight_tool_labels_results_as_non_authoritative():
    client = FakeRecallClient([
        SimpleNamespace(
            id="history",
            text="A historical preference",
            type="observation",
            occurred_start=None,
            mentioned_at=None,
            document_id="old",
            metadata={},
            tags=[],
        )
    ])
    provider = HindsightMemoryProvider(
        base_url="http://127.0.0.1:8888",
        bank_id="main-v2",
        client=client,
    )
    tools = ToolRegistry()
    register_hindsight_tools(tools, provider)

    result = asyncio.run(tools.execute(
        "hindsight_recall",
        {"query": "preference", "limit": 2},
    ))

    assert result["error"] == ""
    assert "historical, non-authoritative" in result["output"]
    assert "A historical preference" in result["output"]


def test_hindsight_recall_shows_a_memory_date_only_when_hindsight_has_one():
    def memory(identifier, text, occurred):
        return SimpleNamespace(id=identifier, text=text, type="observation", occurred_start=occurred,
                               mentioned_at=None, document_id="doc", metadata={}, tags=[])

    client = FakeRecallClient([
        memory("dated", "Moved the NAS to the new rack", "2026-03-04T09:30:00Z"),
        memory("undated", "Prefers short summaries", None),
    ])
    provider = HindsightMemoryProvider(base_url="http://127.0.0.1:8888", bank_id="main-v2", client=client)
    tools = ToolRegistry()
    register_hindsight_tools(tools, provider)

    result = asyncio.run(tools.execute("hindsight_recall", {"query": "setup"}))

    dated, undated = result["output"].splitlines()[1:]
    assert dated == "1. [observation] (2026-03-04) Moved the NAS to the new rack"
    assert undated == "2. [observation] Prefers short summaries"


def test_hindsight_provider_keeps_an_undated_memory_undated():
    def memory(identifier, occurred, mentioned):
        return SimpleNamespace(id=identifier, text=f"memory {identifier}", type="observation",
                               occurred_start=occurred, mentioned_at=mentioned, document_id="doc",
                               metadata={}, tags=[])

    client = FakeRecallClient([
        memory("occurred", "2026-03-04T09:30:00Z", "2026-04-01T00:00:00Z"),
        memory("mentioned", None, "2026-05-06T00:00:00+00:00"),
        memory("undated", None, None),
    ])
    provider = HindsightMemoryProvider(base_url="http://127.0.0.1:8888", bank_id="main-v2", client=client)

    records = {record.source_message_id: record for record in asyncio.run(provider.recall("setup"))}

    def dates(record):
        return {record.created_at, record.last_confirmed_at, record.valid_from}

    assert dates(records["occurred"]) == {"2026-03-04T09:30:00Z"}
    assert dates(records["mentioned"]) == {"2026-05-06T00:00:00+00:00"}
    # The time of the recall used to be written here, which made every undated memory look new.
    assert dates(records["undated"]) == {""}
    assert records["undated"].to_dict()["created_at"] == ""


def test_hindsight_recall_keeps_the_date_of_a_memory_dated_during_the_call():
    class JustRecordedClient(FakeRecallClient):
        stamp = ""

        async def arecall(self, **kwargs):
            self.stamp = datetime.now(timezone.utc).isoformat()
            self.results = [SimpleNamespace(
                id="fresh", text="Deployed build 41", type="observation", occurred_start=self.stamp,
                mentioned_at=None, document_id="doc", metadata={}, tags=[],
            )]
            return await super().arecall(**kwargs)

    client = JustRecordedClient()
    provider = HindsightMemoryProvider(base_url="http://127.0.0.1:8888", bank_id="main-v2", client=client)
    tools = ToolRegistry()
    register_hindsight_tools(tools, provider)

    result = asyncio.run(tools.execute("hindsight_recall", {"query": "deploy"}))

    # A date inside the call's own start and end used to be taken for a missing one and dropped.
    assert result["output"].splitlines()[1] == f"1. [observation] ({client.stamp[:10]}) Deployed build 41"


def test_hindsight_failures_are_real_failures_and_a_retry_is_not_served_from_cache():
    class DownClient(FakeRecallClient):
        down = True

        async def arecall(self, **kwargs):
            if self.down:
                raise ConnectionError("connection refused")
            return await super().arecall(**kwargs)

        async def areflect(self, **kwargs):
            raise TimeoutError()

        async def aretain(self, **kwargs):
            raise ConnectionError("connection reset")

    client = DownClient([SimpleNamespace(
        id="m", text="Back again", type="observation", occurred_start=None, mentioned_at=None,
        document_id="doc", metadata={}, tags=[],
    )])
    provider = HindsightMemoryProvider(base_url="http://127.0.0.1:8888", bank_id="main-v2", client=client)
    tools = ToolRegistry()
    register_hindsight_tools(tools, provider)

    def call(name, **arguments):
        return asyncio.run(tools.execute(name, arguments))

    recall = call("hindsight_recall", query="anything")
    assert recall["output"] == "" and recall["code"] == "hindsight_error"
    assert "connection refused" in recall["error"] and "session_search" in recall["recovery_hint"]

    reflect = call("hindsight_reflect", query="anything")
    assert reflect["code"] == "hindsight_timeout" and "did not answer in time" in reflect["error"]

    retain = call("hindsight_retain", content="A fact")
    assert retain["code"] == "hindsight_error"
    assert "may not have been stored" in retain["recovery_hint"]
    blank = call("hindsight_retain", content="   ")
    assert blank["code"] == "invalid_arguments" and "non-empty content" in blank["error"]

    # In a turn, an identical recall after the service is back runs again: a returned
    # error string used to be cached as a result and replayed.
    client.down = True
    agent = ReActAgent("test", SimpleNamespace(), tools)
    turn_cache: dict[str, dict] = {}

    def recall_in_turn(call_id):
        async def scenario():
            event, = await agent._execute_tool_calls([{
                "id": call_id, "name": "hindsight_recall", "arguments": json.dumps({"query": "anything"}),
            }], turn_cache)
            return event
        return asyncio.run(scenario())

    assert recall_in_turn("first")["code"] == "hindsight_error"
    client.down = False
    again = recall_in_turn("second")
    assert not again["error"] and not again.get("cached") and "Back again" in again["output"]


def test_hindsight_descriptions_state_the_per_turn_limit_and_what_tags_do():
    provider = HindsightMemoryProvider(
        base_url="http://127.0.0.1:8888", bank_id="main-v2", client=FakeRecallClient(),
    )
    tools = ToolRegistry()
    register_hindsight_tools(tools, provider)

    for name in ("hindsight_retain", "hindsight_recall", "hindsight_reflect"):
        tool = tools.get(name)
        assert f"At most {tool.max_calls_per_turn} calls per turn" in tool.description
        assert "ends the turn" in tool.description
    properties = tools.get("hindsight_retain").parameters["properties"]
    assert "cannot filter by tag" in properties["tags"]["description"]
    assert properties["context"]["description"] and properties["content"]["description"]


def test_hindsight_registers_native_retain_recall_and_reflect_tools():
    client = FakeRecallClient()
    provider = HindsightMemoryProvider(
        base_url="http://127.0.0.1:8888",
        bank_id="main-v2",
        client=client,
    )
    tools = ToolRegistry()
    register_hindsight_tools(tools, provider)

    assert {
        "hindsight_retain",
        "hindsight_recall",
        "hindsight_reflect",
    }.issubset(set(tools.tool_names))

    retained = asyncio.run(tools.execute(
        "hindsight_retain",
        {"content": "User prefers concise summaries", "context": "preference"},
    ))
    reflected = asyncio.run(tools.execute(
        "hindsight_reflect",
        {"query": "What response style does the user prefer?"},
    ))

    assert retained["error"] == ""
    assert "stored non-authoritative memory" in retained["output"]
    assert client.manual_retain_calls[0]["retain_async"] is False
    assert reflected["error"] == ""
    assert reflected["output"] == "Synthesized memory answer."
    assert client.reflect_calls[0]["bank_id"] == "main-v2"


def test_hindsight_syncs_full_session_incrementally_with_durable_cursor(tmp_path):
    client = FakeRecallClient()
    provider = HindsightMemoryProvider(
        base_url="http://127.0.0.1:8888",
        bank_id="main-v2",
        session_sync=True,
        session_sync_every_n_turns=1,
        session_sync_async=True,
        processing_model="deepseek-v4-flash",
        client=client,
    )
    path = tmp_path / "session-one.json"
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Use api_key=super-secret-value"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,AAAA"},
                    "metadata": {"source_path": "D:/images/example.png"},
                },
            ],
        },
        {
            "role": "assistant",
            "content": "I will inspect it.",
            "reasoning_content": "private chain of thought",
            "tool_calls": [{"name": "read_file", "arguments": {"path": "secret"}}],
        },
        {"role": "tool", "content": "inspection result", "tool_call_id": "call-1"},
        {"role": "assistant", "content": "Inspection complete."},
    ]

    first = asyncio.run(provider.sync_session(path, messages))
    second = asyncio.run(provider.sync_session(path, messages))
    messages.append({"role": "user", "content": "One more detail"})
    messages.append({"role": "assistant", "content": "Noted."})
    third = asyncio.run(provider.sync_session(path, messages))

    assert first["decision"] == "accepted"
    assert first["synced_messages"] == 4
    assert second["decision"] == "current"
    assert third["synced_messages"] == 2
    assert len(client.retain_calls) == 2
    initial = client.retain_calls[0]
    assert initial["retain_async"] is True
    assert initial["document_id"] == "astra-session:session-one"
    assert len(initial["items"]) == 1
    assert initial["items"][0]["update_mode"] == "replace"
    transcript = initial["items"][0]["content"]
    payload = json.loads(transcript)
    assert [message["role"] for message in payload] == ["user", "assistant"]
    assert payload[0]["content"].startswith("User: ")
    assert payload[1]["content"] == "Assistant: Inspection complete."
    assert client.retain_calls[1]["document_id"] == "astra-session:session-one"
    assert client.retain_calls[1]["items"][0]["update_mode"] == "append"
    appended = json.loads(client.retain_calls[1]["items"][0]["content"])
    assert [message["role"] for message in appended] == ["user", "assistant"]
    assert [message["content"] for message in appended] == [
        "User: One more detail",
        "Assistant: Noted.",
    ]
    assert "super-secret-value" not in transcript
    assert "[REDACTED]" in transcript
    assert "private chain of thought" not in transcript
    assert "base64" not in transcript
    assert "example.png" in transcript
    assert "inspection result" not in transcript
    assert "[Tool calls: read_file]" not in transcript

    state_path = tmp_path / ".artifacts" / "session-one.hindsight-sync.json"
    assert state_path.exists()
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["sync_format"] == 3
    assert state["message_count"] == 6
    assert state["processing_model"] == "deepseek-v4-flash"


def test_hindsight_session_sync_interval_can_be_forced(tmp_path):
    client = FakeRecallClient()
    provider = HindsightMemoryProvider(
        base_url="http://127.0.0.1:8888",
        bank_id="main-v2",
        session_sync=True,
        session_sync_every_n_turns=3,
        client=client,
    )
    path = tmp_path / "interval.json"
    messages = [
        {"role": "user", "content": "turn one"},
        {"role": "assistant", "content": "reply one"},
    ]

    deferred = asyncio.run(provider.sync_session(path, messages))
    forced = asyncio.run(provider.sync_session(path, messages, force=True, reason="shutdown"))

    assert deferred["decision"] == "deferred"
    assert forced["decision"] == "accepted"
    assert len(client.retain_calls) == 1


def test_hindsight_session_sync_batches_three_turns_into_one_api_call(tmp_path):
    client = FakeRecallClient()
    provider = HindsightMemoryProvider(
        base_url="http://127.0.0.1:8888",
        bank_id="main-v2",
        session_sync=True,
        client=client,
    )
    path = tmp_path / "three-turn-batch.json"
    messages = []

    for turn in range(1, 4):
        messages.extend([
            {"role": "user", "content": f"turn {turn}"},
            {"role": "assistant", "content": f"reply {turn}"},
        ])
        outcome = asyncio.run(provider.sync_session(path, messages))
        if turn < 3:
            assert outcome["decision"] == "deferred"
            assert outcome["pending_turns"] == turn
            assert client.retain_calls == []

    assert outcome["decision"] == "accepted"
    assert outcome["synced_messages"] == 6
    assert len(client.retain_calls) == 1
    assert len(client.retain_calls[0]["items"]) == 1
    payload = json.loads(client.retain_calls[0]["items"][0]["content"])
    assert [item["content"] for item in payload] == [
        "User: turn 1",
        "Assistant: reply 1",
        "User: turn 2",
        "Assistant: reply 2",
        "User: turn 3",
        "Assistant: reply 3",
    ]


def test_hindsight_sync_skips_abandoned_historical_turn_but_waits_for_trailing_turn(tmp_path):
    client = FakeRecallClient()
    provider = HindsightMemoryProvider(
        base_url="http://127.0.0.1:8888",
        bank_id="main-v2",
        session_sync=True,
        client=client,
    )
    path = tmp_path / "abandoned.json"
    messages = [
        {"role": "user", "content": "abandoned request"},
        {"role": "assistant", "content": "working", "tool_calls": [{"name": "read_file"}]},
        {"role": "tool", "content": "large tool output"},
        {"role": "user", "content": "completed request"},
        {"role": "assistant", "content": "completed answer"},
        {"role": "user", "content": "still running"},
    ]

    outcome = asyncio.run(provider.sync_session(path, messages, force=True))

    assert outcome["decision"] == "accepted"
    assert outcome["message_count"] == 5
    payload = json.loads(client.retain_calls[0]["items"][0]["content"])
    assert [item["content"] for item in payload] == [
        "User: completed request",
        "Assistant: completed answer",
    ]
    assert "large tool output" not in client.retain_calls[0]["items"][0]["content"]


def test_react_session_sync_uses_current_canonical_context(tmp_path, monkeypatch):
    monkeypatch.setenv("HINDSIGHT_ENABLED", "1")
    monkeypatch.setenv("HINDSIGHT_SESSION_SYNC", "1")
    monkeypatch.setenv("HINDSIGHT_SESSION_SYNC_IN_TESTS", "1")
    monkeypatch.setenv("HINDSIGHT_SESSION_SYNC_EVERY_N_TURNS", "1")

    class FakeConfig:
        model = "fake"
        capabilities = frozenset()

    class FakeLLM:
        config = FakeConfig()

    store = MemoryStore(tmp_path / "memory.db", core_dir=tmp_path / "memory")
    agent = ReActAgent(
        "agent",
        FakeLLM(),  # type: ignore[arg-type]
        ToolRegistry(),
        memory_store=store,
    )
    external = agent.external_memory_provider
    assert external is not None
    assert external.session_sync_async is False
    assert external.session_sync_timeout == 180.0
    client = FakeRecallClient()
    external._client = client
    agent.context.set_session(str(tmp_path / "react-session.json"))
    agent.context.add_user("remember the whole session")
    agent.context.add_assistant("acknowledged")

    outcome = asyncio.run(agent.sync_external_session())

    assert outcome["decision"] == "accepted"
    assert outcome["synced_messages"] == 2
    assert len(client.retain_calls) == 1
    assert client.retain_calls[0]["retain_async"] is False
    assert client.retain_calls[0]["items"][0]["metadata"]["session_id"] == "react-session"


def test_react_session_sync_failure_opens_cost_cooldown(tmp_path, monkeypatch):
    monkeypatch.setenv("HINDSIGHT_ENABLED", "1")
    monkeypatch.setenv("HINDSIGHT_SESSION_SYNC", "1")
    monkeypatch.setenv("HINDSIGHT_SESSION_SYNC_IN_TESTS", "1")
    monkeypatch.setenv("HINDSIGHT_SESSION_SYNC_EVERY_N_TURNS", "1")
    monkeypatch.setenv("HINDSIGHT_SESSION_SYNC_FAILURE_COOLDOWN", "300")

    class FakeConfig:
        model = "fake"
        capabilities = frozenset()

    class FakeLLM:
        config = FakeConfig()

    class FailingClient(FakeRecallClient):
        async def aretain_batch(self, **kwargs):
            self.retain_calls.append(kwargs)
            raise TimeoutError()

    store = MemoryStore(tmp_path / "memory.db", core_dir=tmp_path / "memory")
    agent = ReActAgent(
        "agent",
        FakeLLM(),  # type: ignore[arg-type]
        ToolRegistry(),
        memory_store=store,
    )
    external = agent.external_memory_provider
    assert external is not None
    client = FailingClient()
    external._client = client
    agent.context.set_session(str(tmp_path / "react-session.json"))
    agent.context.add_user("remember this")
    agent.context.add_assistant("acknowledged")

    first = asyncio.run(agent.sync_external_session())
    second = asyncio.run(agent.sync_external_session(force=True, reason="shutdown"))

    assert first["decision"] == "failed"
    assert first["retry_after_seconds"] == 300
    assert second["decision"] == "cooldown"
    assert second["retry_after_seconds"] > 0
    assert len(client.retain_calls) == 1


def test_pytest_process_cannot_sync_to_live_hindsight_without_explicit_opt_in(monkeypatch):
    monkeypatch.setenv("HINDSIGHT_ENABLED", "1")
    monkeypatch.setenv("HINDSIGHT_SESSION_SYNC", "1")
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/test_example.py::test_case (call)")
    monkeypatch.delenv("HINDSIGHT_SESSION_SYNC_IN_TESTS", raising=False)

    provider = HindsightMemoryProvider.from_env()

    assert provider is not None
    assert provider.session_sync_enabled is False
