import asyncio
import json
import sys
from types import MethodType, SimpleNamespace

import pytest

from agent.cli import api_server
from agent.cli import sessions as session_helpers
from agent.core.msg import ContentBlock, Msg
from agent.runtime.context import AgentContext
from agent.runtime.llm import LLMClient, LLMConfig
from agent.runtime.react import ReActAgent
from agent.runtime.session_store import SessionStore


class FakeAgent:
    def __init__(self):
        self.context = AgentContext(system_prompt="system", max_prompt_tokens=4096)
        self.context.set_stable_system_suffix("stable")
        self.context.set_tools_token_cost(17)
        self.llm = object()
        self.tools = SimpleNamespace(
            hooks=None,
            tool_names=["memory", "read_file"],
            get=lambda _name: SimpleNamespace(expose_by_default=True),
        )
        self.history_sizes: list[int] = []

    async def reply_stream(self, _message):
        self.history_sizes.append(len(self.context.messages))
        self.context.add_user("request")
        response = f"history={self.history_sizes[-1]}"
        self.context.add_assistant(response)
        yield {"type": "chunk", "content": response}


class FailingAgent(FakeAgent):
    async def reply_stream(self, _message):
        self.context.add_user("request before failure")
        raise RuntimeError("synthetic failure")
        yield  # pragma: no cover - keeps this an async generator


class ErrorEventAgent(FakeAgent):
    async def reply_stream(self, _message):
        self.context.add_user("request before error event")
        yield {"type": "error", "message": "synthetic event error"}


class MemoryAwareAgent(FakeAgent):
    def __init__(self):
        super().__init__()
        self.memory_store = object()
        self.memory_router = object()
        self.memory_retainer = object()
        self.tool_allowlist = None
        self.tools.tool_names = ["memory", "session_search", "read_file"]
        self.tools.get = lambda name: SimpleNamespace(
            expose_by_default=name != "session_search"
        )
        self.observed_memory_state = None

    async def reply_stream(self, _message):
        self.observed_memory_state = (
            self.memory_store,
            self.memory_router,
            self.memory_retainer,
            self.tool_allowlist,
        )
        yield {"type": "chunk", "content": "ok"}


class PausingAgent(FakeAgent):
    def __init__(self):
        super().__init__()
        self.cleanup_state = None

    async def reply_stream(self, _message):
        self.context.add_user("request before pause")
        try:
            yield {"type": "chunk", "content": "partial"}
            await asyncio.Event().wait()
        finally:
            self.cleanup_state = (
                self.context.session_path,
                api_server._agent_lock.locked(),
            )


class FakeRecall:
    def __init__(self):
        self.messages: list[tuple[str, str, str]] = []
        self.sessions: list[tuple[str, str, str, str, str]] = []

    def get_or_create_session(
        self,
        key,
        *,
        title="",
        personality="",
        workspace_key="",
        workspace_root="",
    ):
        assert key == title
        self.sessions.append((key, title, personality, workspace_key, workspace_root))
        return f"recall-{key}"

    def log_message(self, session_id, role, content):
        self.messages.append((session_id, role, content))


class FakeRequest:
    def __init__(self, body: dict, headers: dict[str, str] | None = None):
        self._body = body
        self.headers = headers or {}

    async def json(self):
        return self._body


@pytest.fixture
def api_runtime(monkeypatch, tmp_path):
    monkeypatch.delenv("ASTRA_API_KEY", raising=False)
    monkeypatch.setattr(session_helpers, "SESSION_DIR", tmp_path / "sessions")
    agent = FakeAgent()
    monkeypatch.setattr(api_server, "_agent", agent)
    monkeypatch.setattr(api_server, "_session_recall", FakeRecall())
    sessions = getattr(api_server, "_sessions", None)
    if sessions is None:
        sessions = {}
        monkeypatch.setattr(api_server, "_sessions", sessions, raising=False)
    sessions.clear()
    yield agent, tmp_path / "sessions"
    sessions.clear()


async def _events(text: str, session_id: str | None):
    return [event async for event in api_server._agent_events(text, session_id)]


def _run(coroutine):
    return asyncio.run(coroutine)


def test_api_session_path_uses_stable_prefixed_name(monkeypatch, tmp_path):
    monkeypatch.setattr(session_helpers, "SESSION_DIR", tmp_path)

    path = session_helpers.api_session_path("sister")

    assert path == tmp_path / "session_api_sister.json"
    assert session_helpers.api_session_path("Sister") == path
    with pytest.raises(session_helpers.SessionNameError, match="64"):
        session_helpers.api_session_path("a" * 65)
    with pytest.raises(session_helpers.SessionNameError):
        session_helpers.validate_api_session_id("İ")
    with pytest.raises(session_helpers.SessionNameError, match="UTF-8"):
        session_helpers.validate_api_session_id("𐐀" * 64)


def test_request_session_id_supports_body_header_and_body_precedence():
    assert api_server._request_session_id({}, {}) is None
    assert api_server._request_session_id({}, {"X-Astra-Session-Id": "header"}) == "header"
    assert api_server._request_session_id(
        {"session_id": "Body"},
        {"X-Astra-Session-Id": "header"},
    ) == "body"


def test_non_streaming_response_echoes_canonical_explicit_session(api_runtime):
    response = _run(api_server.chat_completions(FakeRequest({
        "messages": [{"role": "user", "content": "hello"}],
        "session_id": "Sister",
    })))

    assert response.headers["X-Astra-Session-Id"] == "sister"
    assert json.loads(response.body)["session_id"] == "sister"


def test_response_header_percent_encodes_unicode_session_id(api_runtime):
    response = _run(api_server.chat_completions(FakeRequest({
        "messages": [{"role": "user", "content": "hello"}],
        "session_id": "示例会话",
    })))

    assert response.headers["X-Astra-Session-Id"] == "%E7%A4%BA%E4%BE%8B%E4%BC%9A%E8%AF%9D"
    assert json.loads(response.body)["session_id"] == "示例会话"


def test_streaming_response_echoes_explicit_session_and_stateless_omits_it(
    api_runtime,
):
    streaming = _run(api_server.chat_completions(FakeRequest({
        "messages": [{"role": "user", "content": "hello"}],
        "session_id": "sister",
        "stream": True,
    })))
    stateless = _run(api_server.chat_completions(FakeRequest({
        "messages": [{"role": "user", "content": "hello"}],
    })))

    assert streaming.headers["X-Astra-Session-Id"] == "sister"
    assert "X-Astra-Session-Id" not in stateless.headers
    assert "session_id" not in json.loads(stateless.body)


def test_requests_without_session_id_are_stateless_and_create_no_files(api_runtime):
    agent, session_root = api_runtime

    _run(_events("first", None))
    _run(_events("second", None))

    assert agent.history_sizes == [0, 0]
    assert agent.context.session_path == ""
    assert api_server._sessions == {}
    assert not session_root.exists()


def test_explicit_session_continues_and_uses_prefixed_storage(api_runtime):
    agent, session_root = api_runtime

    _run(_events("first", "sister"))
    _run(_events("second", "sister"))

    assert agent.history_sizes == [0, 2]
    assert SessionStore(session_root / "session_api_sister.json").exists
    assert "session_api_sister" in session_helpers.list_sessions()
    assert len(api_server._sessions["sister"].messages) == 4


def test_explicit_session_reloads_after_cache_reset(api_runtime, monkeypatch):
    _agent, session_root = api_runtime
    _run(_events("first", "sister"))
    api_server._sessions.clear()
    restarted = FakeAgent()
    monkeypatch.setattr(api_server, "_agent", restarted)

    _run(_events("after restart", "sister"))

    assert restarted.history_sizes == [2]
    assert len(SessionStore(session_root / "session_api_sister.json").load()["messages"]) == 4


def test_two_explicit_sessions_do_not_share_history(api_runtime):
    agent, _session_root = api_runtime

    _run(_events("a1", "alpha"))
    _run(_events("b1", "beta"))
    _run(_events("a2", "alpha"))

    assert agent.history_sizes == [0, 0, 2]
    assert api_server._sessions["alpha"] is not api_server._sessions["beta"]
    assert len(api_server._sessions["alpha"].messages) == 4
    assert len(api_server._sessions["beta"].messages) == 2


def test_streaming_and_non_streaming_share_persistent_execution(api_runtime):
    _agent, session_root = api_runtime

    response = _run(api_server._run_agent("first", "shared"))

    async def collect_stream():
        return [
            chunk
            async for chunk in api_server._stream_response(
            "chatcmpl-test",
            1,
            "astra",
            "second",
            "shared",
        )
        ]

    chunks = _run(collect_stream())

    assert response == "history=0"
    assert chunks[-1] == "data: [DONE]\n\n"
    assert len(SessionStore(session_root / "session_api_shared.json").load()["messages"]) == 4


@pytest.mark.parametrize("invalid", ["../escape", "a/b", "", None, 123, ["sister"]])
def test_invalid_session_id_returns_400_without_running_agent(api_runtime, invalid):
    agent, session_root = api_runtime
    request = FakeRequest({
        "messages": [{"role": "user", "content": "hello"}],
        "session_id": invalid,
    })

    response = _run(api_server.chat_completions(request))

    assert response.status_code == 400
    assert json.loads(response.body)["error"]["type"] == "invalid_request_error"
    assert agent.history_sizes == []
    assert not session_root.exists()


def test_memory_session_uses_active_context_path(api_runtime):
    agent, session_root = api_runtime
    agent.context.set_session(str(session_root / "session_api_sister.json"))

    assert api_server._current_memory_session() == "session_api_sister"


def test_agent_failure_saves_named_session_and_restores_parked_context(
    api_runtime,
    monkeypatch,
):
    _agent, session_root = api_runtime
    failing = FailingAgent()
    parked = failing.context
    monkeypatch.setattr(api_server, "_agent", failing)

    with pytest.raises(RuntimeError, match="synthetic failure"):
        _run(_events("hello", "sister"))

    assert failing.context is parked
    assert len(
        SessionStore(session_root / "session_api_sister.json").load()["messages"]
    ) == 1


def test_error_event_restores_context_before_non_streaming_return(
    api_runtime,
    monkeypatch,
):
    _agent, session_root = api_runtime
    failing = ErrorEventAgent()
    parked = failing.context
    monkeypatch.setattr(api_server, "_agent", failing)

    async def run_and_inspect():
        # A turn that ends on an error with no answer is a failure, not the error as answer text.
        with pytest.raises(api_server.TurnFailed, match="synthetic event error") as failed:
            await api_server._run_agent("hello", "sister")
        return failed.value, failing.context is parked, api_server._agent_lock.locked()

    failure, restored, locked = _run(run_and_inspect())
    assert failure.status == 502
    assert restored is True
    assert locked is False
    assert len(
        SessionStore(session_root / "session_api_sister.json").load()["messages"]
    ) == 1


def test_stateless_turn_detaches_session_memory_and_restores_agent(
    api_runtime,
    monkeypatch,
):
    _agent, _session_root = api_runtime
    agent = MemoryAwareAgent()
    original = (agent.memory_store, agent.memory_router, agent.memory_retainer)
    monkeypatch.setattr(api_server, "_agent", agent)

    _run(_events("hello", None))

    memory_store, memory_router, memory_retainer, allowlist = agent.observed_memory_state
    assert (memory_store, memory_router, memory_retainer) == (None, None, None)
    assert "memory" not in allowlist
    assert "read_file" in allowlist
    assert "session_search" not in allowlist
    assert (agent.memory_store, agent.memory_router, agent.memory_retainer) == original
    assert agent.tool_allowlist is None


def test_react_reply_stream_closes_owned_loop_before_return():
    agent = object.__new__(ReActAgent)
    agent.context = AgentContext(system_prompt="system", max_prompt_tokens=4096)
    cleanup_seen = []

    async def inner_loop(_self, _msg, emit_events=True):
        assert emit_events is True
        try:
            yield {"type": "chunk", "content": "partial"}
            await asyncio.Event().wait()
        finally:
            cleanup_seen.append("closed")

    agent._run_react_loop = MethodType(inner_loop, agent)
    msg = Msg(
        sender="user",
        role="user",
        content=[ContentBlock.text("hello")],
    )

    async def close_after_first_chunk():
        stream = agent.reply_stream(msg)
        assert (await anext(stream))["content"] == "partial"
        await stream.aclose()
        return list(cleanup_seen)

    assert _run(close_after_first_chunk()) == ["closed"]


def test_react_loop_closes_active_loop_before_vision_state_reset():
    agent = object.__new__(ReActAgent)
    agent.context = AgentContext(system_prompt="system", max_prompt_tokens=4096)
    agent._active_vision_request_id = None
    agent.vision_preprocessor = SimpleNamespace(release_request=lambda *_args: None)
    cleanup_state = []

    async def active_loop(_self, _msg, _emit_events=True):
        try:
            yield {"type": "chunk", "content": "partial"}
            await asyncio.Event().wait()
        finally:
            cleanup_state.append(_self._active_vision_request_id is not None)

    agent._run_react_loop_active = MethodType(active_loop, agent)
    msg = Msg(
        sender="user",
        role="user",
        content=[ContentBlock.text("hello")],
    )

    async def close_after_first_chunk():
        stream = agent._run_react_loop(msg)
        await anext(stream)
        await stream.aclose()
        return agent._active_vision_request_id

    assert _run(close_after_first_chunk()) is None
    assert cleanup_state == [True]


def test_llm_client_closes_provider_stream_before_return():
    cleanup_seen = []

    class PausingProvider:
        async def chat_stream(self, _messages, _tools):
            try:
                yield {"type": "chunk", "content": "partial"}
                await asyncio.Event().wait()
            finally:
                cleanup_seen.append("closed")

    client = LLMClient(LLMConfig(), provider=PausingProvider())

    async def close_after_first_chunk():
        stream = client.chat_stream([], [])
        await anext(stream)
        await stream.aclose()
        return list(cleanup_seen)

    assert _run(close_after_first_chunk()) == ["closed"]


def test_named_session_is_logged_to_session_recall(api_runtime, monkeypatch):
    recall = FakeRecall()
    monkeypatch.setattr(api_server, "_session_recall", recall)

    _run(_events("hello sister", "sister"))

    assert recall.messages == [
        ("recall-session_api_sister", "user", "hello sister"),
        ("recall-session_api_sister", "assistant", "history=0"),
    ]
    _key, _title, _personality, workspace_key, workspace_root = recall.sessions[0]
    assert workspace_key
    assert workspace_root


def test_api_persists_user_before_agent_prompt_build(api_runtime, monkeypatch):
    events: list[str] = []

    class OrderingRecall(FakeRecall):
        def log_message(self, session_id, role, content):
            super().log_message(session_id, role, content)
            if role == "user":
                events.append("session_recall:user_written")

    recall = OrderingRecall()

    class OrderingAgent(FakeAgent):
        async def reply_stream(self, _message):
            assert recall.messages[-1][1:] == ("user", "current turn")
            events.append("context_index:build")
            yield {"type": "chunk", "content": "ok"}

    monkeypatch.setattr(api_server, "_session_recall", recall)
    monkeypatch.setattr(api_server, "_agent", OrderingAgent())

    _run(_events("current turn", "sister"))

    assert events.index("session_recall:user_written") < events.index(
        "context_index:build"
    )


def test_session_recall_initialization_failure_is_retried(api_runtime, monkeypatch):
    attempts = []

    class FlakyRecall:
        def init_db(self):
            attempts.append("init")
            if len(attempts) == 1:
                raise RuntimeError("database temporarily locked")

    monkeypatch.setattr(api_server, "_session_recall", None)
    monkeypatch.setitem(
        sys.modules,
        "agent.runtime.session_recall",
        SimpleNamespace(SessionRecall=FlakyRecall),
    )

    with pytest.raises(RuntimeError, match="temporarily locked"):
        api_server._api_session_recall()

    assert api_server._session_recall is None
    assert isinstance(api_server._api_session_recall(), FlakyRecall)
    assert attempts == ["init", "init"]


def test_session_context_copies_tool_risk_provider(api_runtime):
    agent, _session_root = api_runtime
    def provider(_name):
        return "write"

    agent.context.tool_risk_provider = provider

    _run(_events("hello", "sister"))

    assert api_server._sessions["sister"].tool_risk_provider is provider


def test_stream_close_restores_context_and_unlocks_in_same_loop(
    api_runtime,
    monkeypatch,
):
    _agent, _session_root = api_runtime
    agent = PausingAgent()
    parked = agent.context
    monkeypatch.setattr(api_server, "_agent", agent)

    async def close_after_first_chunk():
        stream = api_server._stream_response(
            "chatcmpl-test",
            1,
            "astra",
            "hello",
            "sister",
        )
        await anext(stream)
        await stream.aclose()
        return (
            agent.context is parked,
            api_server._agent_lock.locked(),
            agent.cleanup_state,
        )

    restored, locked, cleanup_state = _run(close_after_first_chunk())

    assert restored is True
    assert locked is False
    assert cleanup_state == (
        str(session_helpers.api_session_path("sister")),
        True,
    )
