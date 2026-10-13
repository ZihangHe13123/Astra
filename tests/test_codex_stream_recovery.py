import asyncio
import json

import httpx
import pytest

from agent.runtime.codex_provider import CodexProvider
from agent.runtime.llm import LLMOverallTimeout, LLMResponseError, _RequestBudget
from test_codex_provider import config, message, signed_in, sse, terminal


class InterruptedStream(httpx.AsyncByteStream):
    def __init__(self, payload, failure="read"):
        self.payload = payload
        self.failure = failure
        self.closed = False

    async def __aiter__(self):
        yield self.payload
        if self.failure == "read":
            raise httpx.RemoteProtocolError("peer closed connection without sending complete message body (incomplete chunked read)")
        if self.failure == "done":
            yield b"data: [DONE]\n\n"
        if self.failure == "idle":
            await asyncio.Event().wait()

    async def aclose(self):
        self.closed = True


def partial(call_id="pending", name="write_file"):
    return sse(
        {"type": "response.reasoning_summary_text.delta", "output_index": 0, "summary_index": 0, "delta": "abandoned reasoning"},
        {"type": "response.output_text.delta", "output_index": 1, "delta": "abandoned draft"},
        {"type": "response.output_item.added", "output_index": 2,
         "item": {"type": "function_call", "call_id": call_id, "name": name, "arguments": ""}},
        {"type": "response.function_call_arguments.delta", "output_index": 2, "delta": '{"value":"abandoned'},
    )


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRA_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("AGENT_USER_MODELS_FILE", str(tmp_path / "models.yaml"))
    signed_in()


@pytest.mark.parametrize("failure", ["read", "eof", "done", "idle"])
def test_interrupted_attempt_closes_and_discards_before_retry(failure, monkeypatch, caplog):
    broken = InterruptedStream(partial(), failure)
    requests, delays = [], []

    async def backoff(budget, delay):
        assert broken.closed
        delays.append(delay)

    monkeypatch.setattr(_RequestBudget, "backoff", backoff)

    def handler(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(200, stream=broken, headers={"x-request-id": "req-broken"})
        return httpx.Response(200, content=sse(terminal()))

    async def work():
        provider = CodexProvider(config(max_retries=1, idle_timeout=.03), transport=httpx.MockTransport(handler))
        return [e async for e in provider.chat_stream([{"role": "user", "content": "write"}])]

    events = asyncio.run(work())
    assert len(requests) == 2 and requests[0] == requests[1]
    assert delays == [1]
    assert events[-1]["type"] == "done" and events[-1]["content"] == "Done"
    assert events[-1]["reasoning_content"] == ""
    assert not any(e["type"] == "tool_calls" for e in events)
    recovery = next(i for i, e in enumerate(events) if e["type"] == "generation_recovery")
    assert events[recovery - 1]["type"] == "tool_preparing"
    assert events[recovery - 1]["state"] == "discarded"
    assert "req-broken" in caplog.text and '"status": 200' in caplog.text
    if failure == "read":
        assert "RemoteProtocolError" in caplog.text and "incomplete_chunked_read" in caplog.text
    elif failure == "eof":
        assert "eof_before_completion" in caplog.text
    elif failure == "done":
        assert "done_without_completion" in caplog.text
    else:
        assert "_RequestCapTimeout" in caplog.text


def test_recovery_is_bounded_and_keeps_safe_cause_on_final_failure(monkeypatch, caplog):
    streams = []

    async def backoff(*args):
        pass

    monkeypatch.setattr(_RequestBudget, "backoff", backoff)

    def handler(request):
        stream = InterruptedStream(partial())
        streams.append(stream)
        return httpx.Response(200, stream=stream, headers={"x-request-id": "req-final"})

    async def work():
        provider = CodexProvider(config(max_retries=2), transport=httpx.MockTransport(handler))
        events = []
        with pytest.raises(LLMResponseError) as failure:
            async for event in provider.chat_stream([]):
                events.append(event)
        assert failure.value.code == "stream_closed"
        assert isinstance(failure.value.__cause__, httpx.RemoteProtocolError)
        assert "RemoteProtocolError" in str(failure.value) and "req-final" in str(failure.value)
        assert sum(e["type"] == "generation_recovery" for e in events) == 2
        assert not any(e["type"] in {"tool_calls", "done"} for e in events)

    asyncio.run(work())
    assert len(streams) == 3 and all(s.closed for s in streams)
    assert "retrying=False" in caplog.text


def test_original_deadline_includes_retry_backoff():
    streams = []

    def handler(request):
        stream = InterruptedStream(partial())
        streams.append(stream)
        return httpx.Response(200, stream=stream)

    provider = CodexProvider(config(overall_timeout=.1, max_retries=2), transport=httpx.MockTransport(handler))
    with pytest.raises(LLMOverallTimeout):
        asyncio.run(provider.chat([]))
    assert len(streams) == 1 and streams[0].closed


def test_cancel_during_recovery_never_starts_another_request(monkeypatch):
    async def work():
        waiting = asyncio.Event()
        streams = []

        async def backoff(*args):
            waiting.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(_RequestBudget, "backoff", backoff)

        def handler(request):
            stream = InterruptedStream(partial())
            streams.append(stream)
            return httpx.Response(200, stream=stream)

        provider = CodexProvider(config(), transport=httpx.MockTransport(handler))
        task = asyncio.create_task(provider.chat([]))
        await asyncio.wait_for(waiting.wait(), 2)
        assert streams[0].closed
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(streams) == 1

    asyncio.run(work())


@pytest.mark.parametrize("ending", [
    {"type": "response.incomplete", "response": {"status": "incomplete"}},
    {"type": "response.failed"},
    terminal([{"type": "function_call", "call_id": "bad", "name": "write_file", "arguments": "{"}]),
])
def test_semantic_failures_are_not_transport_retries(ending):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, content=sse(ending))

    provider = CodexProvider(config(max_retries=2), transport=httpx.MockTransport(handler))
    with pytest.raises(LLMResponseError):
        asyncio.run(provider.chat([]))
    assert len(requests) == 1


@pytest.mark.parametrize("error", [httpx.ReadError("close failed"), OSError(54, "connection reset")])
def test_close_failure_after_completion_does_not_replay_tools(error, caplog):
    call = {"type": "function_call", "call_id": "write", "name": "write_file", "arguments": "{}"}
    requests = []

    class CloseFailure(InterruptedStream):
        async def aclose(self):
            self.closed = True
            raise error

    def handler(request):
        requests.append(request)
        return httpx.Response(200, stream=CloseFailure(sse(terminal([call]))))

    result = asyncio.run(CodexProvider(config(), transport=httpx.MockTransport(handler)).chat([]))
    assert len(requests) == 1 and len(result["tool_calls"]) == 1
    assert "response close failed" in caplog.text


def test_client_close_failure_preserves_completed_response(caplog):
    requests = []

    class CloseFailure(httpx.MockTransport):
        async def aclose(self):
            raise OSError(54, "connection reset")

    def handler(request):
        requests.append(request)
        return httpx.Response(200, content=sse(terminal()))

    result = asyncio.run(CodexProvider(config(), transport=CloseFailure(handler)).chat([]))
    assert len(requests) == 1 and result["content"] == "Done"
    assert "client close failed" in caplog.text


@pytest.mark.parametrize("completed_item", [False, True])
def test_real_agent_recovery_executes_completed_and_pending_writes_once(tmp_path, monkeypatch, completed_item):
    from agent.core.msg import ContentBlock, Msg
    from agent.runtime.context import AgentContext
    from agent.runtime.llm import LLMClient
    from agent.runtime.react import ReActAgent
    from agent.runtime.tools.registry import ToolDef, ToolRegistry

    requests, streams = [], []
    written = tmp_path / "writes.txt"
    first = {"type": "function_call", "call_id": "first", "name": "fixture_write", "arguments": '{"value":"first"}'}
    second = {**first, "call_id": "second", "arguments": '{"value":"second"}'}

    async def backoff(*args):
        assert streams[0].closed

    monkeypatch.setattr(_RequestBudget, "backoff", backoff)

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return httpx.Response(200, content=sse(terminal([first])))
        if len(requests) == 2:
            payload = (sse({"type": "response.output_item.done", "output_index": 0,
                            "item": {**second, "arguments": '{"value":"abandoned"}'}})
                       if completed_item else partial("second", "fixture_write"))
            stream = InterruptedStream(payload)
            streams.append(stream)
            return httpx.Response(200, stream=stream)
        if len(requests) == 3:
            assert body == requests[1]
            assert [i["call_id"] for i in body["input"] if i["type"] == "function_call_output"] == ["first"]
            return httpx.Response(200, content=sse(terminal([message("Recovered", "commentary"), second])))
        assert len(requests) == 4
        return httpx.Response(200, content=sse(terminal()))

    def write(value):
        with written.open("a", encoding="utf-8") as file:
            file.write(value + "\n")
        return "written " + value

    async def work():
        registry = ToolRegistry()
        registry.register(ToolDef("fixture_write", "write", {"type": "object", "properties": {
            "value": {"type": "string"}}, "required": ["value"]}, write, risk="write"))
        llm = LLMClient(config(max_retries=1), provider=CodexProvider(config(max_retries=1), transport=httpx.MockTransport(handler)))
        agent = ReActAgent("test", llm, registry, max_iterations=4, progressive_tools=False, timing_log_enabled=False)
        session = str(tmp_path / "session.json")
        agent.context.set_session(session)
        events = [e async for e in agent.reply_stream(Msg(content=[ContentBlock.text("write two values")]))]
        errors = [e for e in events if e["type"] == "error"]
        assert len(errors) == 1 and errors[0]["recoverable"] and errors[0]["code"] == "stream_closed"
        assert written.read_text(encoding="utf-8").splitlines() == ["first", "second"]
        assert len([e for e in events if e["type"] == "tool_result"]) == 2
        agent.context.save()
        restored = AgentContext()
        restored.set_session(session)
        assert restored.load()
        assert "abandoned" not in json.dumps(restored.messages)
        results = [m for m in restored.messages if m["role"] == "tool"]
        assert [m["tool_call_id"] for m in results] == ["first", "second"]
        assert restored.messages[-1]["content"] == "Done"

    asyncio.run(work())


def test_final_synthesis_recovery_emits_boundary_without_replaying_tool(monkeypatch):
    from agent.core.msg import ContentBlock, Msg
    from agent.runtime.llm import LLMClient
    from agent.runtime.react import ReActAgent
    from agent.runtime.tools.registry import ToolDef, ToolRegistry

    requests, calls = [], []
    broken = InterruptedStream(sse(
        {"type": "response.reasoning_summary_text.delta", "output_index": 0, "summary_index": 0,
         "delta": "abandoned reasoning"},
        {"type": "response.output_text.delta", "output_index": 1, "delta": "abandoned draft"},
    ))

    async def backoff(*args):
        assert broken.closed

    monkeypatch.setattr(_RequestBudget, "backoff", backoff)

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            call = {"type": "function_call", "call_id": "once", "name": "probe", "arguments": "{}"}
            return httpx.Response(200, content=sse(terminal([call])))
        if len(requests) == 2:
            return httpx.Response(200, stream=broken)
        assert len(requests) == 3 and body == requests[1]
        assert not body.get("tools")
        return httpx.Response(200, content=sse(
            {"type": "response.output_text.delta", "output_index": 0, "delta": "Recovered final"},
            terminal([message("Recovered final")]),
        ))

    async def work():
        registry = ToolRegistry()
        registry.register(ToolDef("probe", "probe", {"type": "object", "properties": {}},
                                  lambda: calls.append("once") or "probe result"))
        llm = LLMClient(config(max_retries=1), provider=CodexProvider(
            config(max_retries=1), transport=httpx.MockTransport(handler)))
        agent = ReActAgent("test", llm, registry, max_iterations=1,
                           progressive_tools=False, timing_log_enabled=False)
        events = [e async for e in agent.reply_stream(Msg(content=[ContentBlock.text("probe then summarize")]))]
        boundaries = [i for i, e in enumerate(events) if e.get("code") == "stream_closed"]
        assert len(boundaries) == 1
        boundary = boundaries[0]
        assert events[boundary]["type"] == "error" and events[boundary]["recoverable"]
        assert any(e["type"] == "chunk" and e["content"] == "abandoned draft" for e in events[:boundary])
        assert any(e["type"] == "chunk" and e["content"] == "Recovered final" for e in events[boundary + 1:])
        assert calls == ["once"] and len(requests) == 3
        assert "abandoned" not in json.dumps(agent.context.messages)
        assert agent.context.messages[-1]["content"] == "Recovered final"

    asyncio.run(work())


@pytest.mark.parametrize("errno,winerror", [(54, None), (104, None), (10054, 10054)])
def test_transport_diagnostics_preserve_native_cause_without_secrets(errno, winerror):
    from agent.runtime.provider_errors import transport_error_diagnostics

    cause = ConnectionResetError(errno, "connection reset Bearer SECRET /private/path\x1b[31m")
    if winerror is not None:
        cause.winerror = winerror
    exc = httpx.ReadError("https://user:SECRET@proxy/path?token=SECRET")
    exc.__cause__ = cause
    cause.__cause__ = exc  # Cycles must remain bounded.
    response = httpx.Response(200, headers={"x-request-id": "bad SECRET id"})
    details = transport_error_diagnostics(exc, response=response)
    assert details["causes"] == [{"type": "ReadError"}, {
        "type": "ConnectionResetError", "errno": errno,
        **({"winerror": winerror} if winerror is not None else {}), "reason": "connection_reset"}]
    assert details["request_id"] is None
    assert "SECRET" not in json.dumps(details) and "/private/path" not in json.dumps(details)
