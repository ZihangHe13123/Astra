import asyncio
import json

import httpx
import pytest

from agent.runtime.claude_code_provider import ClaudeCodeProvider
from agent.runtime.codex_provider import CodexProvider
from agent.runtime.llm import LLMConfig, LLMResponseError
from agent.runtime.tool_preparation import ToolPreparation, with_tool_preparation
from test_codex_provider import config, signed_in, sse, terminal
from test_llm_stream_semantics import call, chunk, provider


def previews(events):
    return [event for event in events if event["type"] == "tool_preparing"]


def test_incremental_preview_bounds_content_and_decodes_split_unicode():
    preview = ToolPreparation(interval=0)
    prefix = '{"content":"' + "sensitive body" * 10000 + '","nested":{"path":"wrong"},"path":"'
    preview.observe({"index": 0, "name": "write_file", "call_id": "one", "delta": prefix})
    for part in ['\\u4e2', 'd\\ud83d', '\\ude00\\nfile', '.txt"}']:
        event = preview.observe({"index": 0, "delta": part})
    assert event["calls"][0]["summary"] == "中😀 file.txt"
    assert event["calls"][0]["argument_chars"] == len(prefix) + len('\\u4e2d\\ud83d\\ude00\\nfile.txt"}')
    assert "sensitive" not in json.dumps(event)
    assert len(json.dumps(preview.fields[0].__dict__, ensure_ascii=False)) < 1500


def test_preview_allowlist_does_not_expose_computer_input_shell_secrets_or_nested_paths():
    preview = ToolPreparation(interval=0)
    for index, name, args in [
        (0, "computer_act", '{"path":"private","command":"credential","text":"secret"}'),
        (1, "execute_shell", '{"command":"curl -H \'Authorization: secret\' https://example.test"}'),
        (2, "write_file", '{"other":{"path":"secret"},"content":"secret"}'),
        (3, "bash", '{"command":"TOKEN=secret command"}'),
    ]:
        event = preview.observe({"index": index, "name": name, "arguments": args})
    assert [call["summary"] for call in event["calls"]] == ["", "curl …", "", ""]
    assert "secret" not in json.dumps(event)


def test_preview_throttles_bounded_snapshots_and_keeps_monotonic_counts():
    now = [0.0]
    preview = ToolPreparation(clock=lambda: now[0])
    first = preview.observe({"index": 0, "name": "write_file", "arguments": '{"path":"x"'})
    assert first is not None
    assert preview.observe({"index": 0, "arguments": '{"path":"x"'}) is None
    now[0] = 0.11
    second = preview.observe({"index": 0, "arguments": '{"path":"x","content":"' + "x" * 100000})
    assert second["calls"][0]["argument_chars"] > first["calls"][0]["argument_chars"]
    for index in range(1, 20):
        now[0] += 0.11
        result = preview.observe({"index": index, "name": "read_file", "delta": '{"path":"' + "x" * 500})
        if result:
            second = result
    assert len(second["calls"]) == 8
    assert max(len(call["summary"]) for call in second["calls"]) <= 160
    assert len(json.dumps(second)) < 4000
    assert preview.end("finished")["calls"] == []
    assert preview.end("finished") is None


def test_attempt_reset_and_errors_discard_only_the_matching_preview():
    @with_tool_preparation
    async def stream():
        yield {"type": "_tool_preparation", "index": 0, "name": "read_file", "delta": "{"}
        yield {"type": "_tool_preparation_reset"}
        yield {"type": "_tool_preparation", "index": 0, "name": "read_file", "delta": "{"}
        raise ValueError("failed")

    async def run():
        events = []
        with pytest.raises(ValueError):
            async for event in stream():
                events.append(event)
        return events

    events = asyncio.run(run())
    assert [e["state"] for e in events] == ["preparing", "discarded", "preparing", "discarded"]
    assert events[0]["attempt_id"] == events[1]["attempt_id"] != events[2]["attempt_id"]
    assert events[2]["attempt_id"] == events[3]["attempt_id"]


def test_openai_preview_precedes_complete_calls_without_changing_arguments():
    llm, streams, requests = provider([
        chunk(calls=[call(0, "write_file", "one", '{"path":"x.txt",')]),
        chunk(calls=[call(0, args='"content":"body"}')], finish="tool_calls"),
    ])

    async def run():
        return [event async for event in llm.chat_stream([])]

    events = asyncio.run(run())
    assert [event["state"] for event in previews(events)] == ["preparing", "finished"]
    assert previews(events)[0]["calls"][0]["summary"] == "x.txt"
    assert events[-2]["state"] == "finished"
    assert json.loads(events[-1]["calls"][0]["arguments"]) == {"path": "x.txt", "content": "body"}
    assert len(requests) == 1 and streams[0].closed


def test_openai_interleaved_call_indices_keep_separate_previews(monkeypatch):
    from agent.runtime import tool_preparation
    original = tool_preparation.ToolPreparation
    monkeypatch.setattr(tool_preparation, "ToolPreparation", lambda: original(interval=0))
    llm, _, _ = provider([
        chunk(calls=[call(1, "write_file", "two", '{"path":"b'), call(0, "read_file", "one", '{"path":"a')]),
        chunk(calls=[call(0, args='.txt"}'), call(1, args='.txt","content":"body"}')], finish="tool_calls"),
    ])

    async def run():
        return [event async for event in llm.chat_stream([])]

    events = asyncio.run(run())
    last = [e for e in previews(events) if e["state"] == "preparing"][-1]
    assert [(c["index"], c["call_id"], c["summary"]) for c in last["calls"]] == [(0, "one", "a.txt"), (1, "two", "b.txt")]
    assert [c["id"] for c in events[-1]["calls"]] == ["one", "two"]


def test_openai_preview_consumer_backpressure_is_not_provider_idle_time():
    llm, _, _ = provider([
        chunk(calls=[call(0, "read_file", "one", '{"path":"x')]),
        chunk(calls=[call(0, args='"}')], finish="tool_calls"),
    ])
    llm.config.idle_timeout = 0.01

    async def run():
        events = []
        async for event in llm.chat_stream([]):
            events.append(event)
            if event["type"] == "tool_preparing" and event["state"] == "preparing":
                await asyncio.sleep(0.04)
        return events

    assert asyncio.run(run())[-1]["type"] == "tool_calls"


@pytest.mark.parametrize("finish", [None, "length"])
def test_openai_incomplete_tool_previews_are_discarded(finish):
    llm, streams, requests = provider([chunk(calls=[call(0, "write_file", "one", '{"path":"x"')], finish=finish)])

    async def run():
        events = []
        try:
            async for event in llm.chat_stream([]):
                events.append(event)
        except LLMResponseError:
            pass
        return events

    events = asyncio.run(run())
    assert [event["state"] for event in previews(events)] == ["preparing", "discarded"]
    assert len(requests) == 1 and streams[0].closed


def test_codex_preview_does_not_turn_unfinished_item_into_executable_call(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRA_HOME", str(tmp_path))
    signed_in()
    item = {"type": "function_call", "id": "item", "call_id": "one", "name": "write_file", "arguments": ""}
    events = sse(
        {"type": "response.output_item.added", "output_index": 0, "item": item},
        {"type": "response.function_call_arguments.delta", "output_index": 0, "delta": '{"path":"x"'},
    )
    llm = CodexProvider(config(), transport=httpx.MockTransport(lambda _: httpx.Response(200, content=events)))

    async def run():
        output = []
        with pytest.raises(LLMResponseError):
            async for event in llm.chat_stream([]):
                output.append(event)
        return output

    output = asyncio.run(run())
    assert [e["state"] for e in previews(output)] == ["preparing", "discarded"]
    assert not any(e["type"] == "tool_calls" for e in output)


def test_codex_complete_stream_preserves_final_arguments(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRA_HOME", str(tmp_path))
    signed_in()
    item = {"type": "function_call", "id": "item", "call_id": "one", "name": "read_file", "arguments": '{"path":"x"}'}
    llm = CodexProvider(config(), transport=httpx.MockTransport(lambda _: httpx.Response(200, content=sse(
        {"type": "response.output_item.added", "output_index": 0, "item": {**item, "arguments": ""}},
        {"type": "response.function_call_arguments.delta", "output_index": 0, "delta": item["arguments"]},
        terminal([item]),
    ))))

    async def run():
        return [event async for event in llm.chat_stream([])]

    output = asyncio.run(run())
    assert output[-2]["state"] == "finished"
    assert output[-1]["calls"][0]["arguments"] == item["arguments"]


def test_codex_identity_only_preview_preserves_existing_transport_retry(tmp_path, monkeypatch):
    from agent.runtime.llm import _RequestBudget
    monkeypatch.setenv("ASTRA_HOME", str(tmp_path))
    signed_in()
    item = {"type": "function_call", "id": "item", "call_id": "one", "name": "read_file", "arguments": ""}
    attempts = []

    class Disconnect(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield sse({"type": "response.output_item.added", "output_index": 0, "item": item})
            raise httpx.ReadError("disconnected")

    async def backoff(*args):
        pass

    monkeypatch.setattr(_RequestBudget, "backoff", backoff)

    def transport(request):
        attempts.append(request)
        return httpx.Response(200, stream=Disconnect()) if len(attempts) == 1 else httpx.Response(200, content=sse(
            terminal([{**item, "arguments": '{"path":"x"}'}])))

    llm = CodexProvider(config(max_retries=1), transport=httpx.MockTransport(transport))

    async def run():
        return [event async for event in llm.chat_stream([])]

    output = asyncio.run(run())
    observed = previews(output)
    assert len(attempts) == 2
    assert [event["state"] for event in observed] == ["preparing", "discarded", "preparing", "finished"]
    assert observed[0]["attempt_id"] != observed[2]["attempt_id"]
    assert output[-1]["type"] == "tool_calls"


def test_claude_preview_does_not_disable_existing_compatibility_fallback(monkeypatch):
    from agent.runtime.claude_code_provider import _Fallback
    monkeypatch.setattr(ClaudeCodeProvider, "cache_marker", True)
    llm = ClaudeCodeProvider(LLMConfig())
    attempts = []

    async def source(*args):
        attempts.append(True)
        if len(attempts) == 1:
            yield {"type": "_tool_preparation", "index": 0, "name": "read_file", "delta": "{"}
            raise _Fallback("cache_marker", "rejected marker")
        yield {"type": "done", "content": "answer"}

    llm._stream = source

    async def run():
        return [event async for event in llm.chat_stream([])]

    output = asyncio.run(run())
    assert len(attempts) == 2
    assert [event["state"] for event in previews(output)] == ["preparing", "discarded"]
    assert output[-1]["content"] == "answer"


@pytest.mark.parametrize("success", [True, False])
def test_claude_native_partial_input_is_display_only(success):
    llm = ClaudeCodeProvider(LLMConfig())
    item = {"type": "tool_use", "id": "one", "name": "mcp__astra__read_file", "input": {"path": "x"}}
    frames = [
        {"type": "stream_event", "event": {"type": "content_block_start", "index": 2, "content_block": {**item, "input": {}}}},
        {"type": "stream_event", "event": {"type": "content_block_delta", "index": 2,
            "delta": {"type": "input_json_delta", "partial_json": '{"path":"x"}'}}},
        {"type": "assistant", "message": {"content": [item]}},
        {"type": "result", "subtype": "error_max_turns" if success else "error_during_execution", "is_error": True},
    ]

    class Lines:
        async def read(self, _timeout):
            return json.dumps(frames.pop(0)).encode() if frames else b""

    async def source(*args):
        async for event in llm._events(Lines()):
            yield event

    llm._stream = source

    async def run():
        output = []
        try:
            async for event in llm.chat_stream([]):
                output.append(event)
        except LLMResponseError:
            assert not success
        return output

    output = asyncio.run(run())
    assert previews(output)[0]["calls"][0]["name"] == "read_file"
    assert previews(output)[-1]["state"] == ("finished" if success else "discarded")
    if success:
        assert json.loads(output[-1]["calls"][0]["arguments"]) == {"path": "x"}
    else:
        assert not any(event["type"] == "tool_calls" for event in output)


def test_closing_preview_closes_underlying_stream_without_yielding_during_generator_exit():
    closed = []

    @with_tool_preparation
    async def source():
        try:
            yield {"type": "_tool_preparation", "index": 0, "name": "write_file", "delta": "{"}
            await asyncio.sleep(60)
        finally:
            closed.append(True)

    async def run():
        stream = source()
        assert (await anext(stream))["type"] == "tool_preparing"
        await stream.aclose()

    asyncio.run(run())
    assert closed == [True]
