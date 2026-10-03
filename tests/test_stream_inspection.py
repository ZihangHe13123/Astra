"""Runtime display events cannot execute tools or become model context."""
import asyncio
import json

from agent.core.msg import ContentBlock, Msg
from agent.runtime.message_source import message_source_ref
from agent.runtime.react import ReActAgent
from agent.runtime.session_store import SessionStore
from agent.runtime.stream_progress import stream_with_progress
from agent.runtime.tools.registry import ToolDef, ToolRegistry


def test_preparation_then_durable_sources_across_tool_steps(tmp_path):
    executions, prompts = [], []

    class Model:
        async def chat_stream(self, messages, tools):
            prompts.append(messages)
            if len(prompts) == 1:
                yield {"type": "tool_preparing", "attempt_id": "first", "state": "preparing", "calls": [
                    {"index": 0, "call_id": "read", "name": "read_result", "argument_chars": 1, "summary": ""}]}
                assert executions == []
                yield {"type": "chunk", "content": "Checking."}
                yield {"type": "tool_preparing", "attempt_id": "first", "state": "finished", "calls": []}
                yield {"type": "tool_calls", "content": "Checking.", "calls": [
                    {"id": "read", "name": "read_result", "arguments": "{}"}]}
            else:
                yield {"type": "chunk", "content": "Checked."}
                yield {"type": "done", "content": "Checked."}

    async def read():
        executions.append("read")
        return "result"

    async def run():
        registry = ToolRegistry()
        registry.register(ToolDef("read_result", "Read result", {"type": "object"}, read))
        agent = ReActAgent("test", Model(), registry, max_iterations=3, timing_log_enabled=False)
        path = tmp_path / "session.json"
        agent.context.set_session(str(path))
        message = Msg(content=[ContentBlock.text("run")], metadata={"submission_id": "submission"})
        events = []
        async for event in agent.reply_stream(message):
            if event["type"] == "tool_preparing":
                assert executions == []
            if event["type"] == "message_source":
                saved = SessionStore(path).load(readonly=True)["messages"]
                ref = event["source_ref"]
                assert message_source_ref(saved[ref["index"]], ref["index"]) == ref
            events.append(event)
        return events

    events = asyncio.run(run())
    assert executions == ["read"]
    sources = [e for e in events if e["type"] == "message_source"]
    assert next(e for e in sources if e.get("submission_id"))["submission_id"] == "submission"
    chunks = [e for e in events if e["type"] == "chunk"]
    assert len({e["stream_id"] for e in chunks}) == 2
    assert {e["stream_id"] for e in chunks} == {e["stream_id"] for e in sources if e.get("stream_id")}
    assert all(e.get("request_id") for e in sources + chunks)
    assert all(token not in json.dumps(prompts) for token in ("source_ref", "stream_id", "tool_preparing", "attempt_id", "submission_id"))


def test_parameter_only_progress_is_liveness_without_inventing_tool_dispatch():
    async def source():
        yield {"type": "tool_preparing", "state": "preparing", "calls": [{"name": "write_file"}]}
        yield {"type": "tool_preparing", "state": "discarded", "calls": []}

    async def run():
        return [e async for e in stream_with_progress(source())]

    events = asyncio.run(run())
    assert [e["phase"] for e in events if e["type"] == "generation_progress"] == ["requesting", "streaming", "finished"]
    assert not any(e["type"] in {"tool_calls", "tool_result"} for e in events)


def test_runtime_argument_policy_overrides_provider_preview_allowlist(tmp_path):
    class Model:
        async def chat_stream(self, messages, tools):
            yield {"type": "tool_preparing", "attempt_id": "one", "state": "preparing", "calls": [
                {"index": 0, "call_id": "c", "name": "write_file", "argument_chars": 99, "summary": "private-target"}]}
            yield {"type": "done", "content": "No call was executed."}

    async def run():
        registry = ToolRegistry()
        registry.register(ToolDef("write_file", "Private operation", {"type": "object"}, lambda: "unused",
                                  argument_persistence="request_local"))
        agent = ReActAgent("test", Model(), registry, timing_log_enabled=False)
        agent.context.set_session(str(tmp_path / "session.json"))
        return [e async for e in agent.reply_stream(Msg(content=[ContentBlock.text("inspect")]))]

    events = asyncio.run(run())
    preview = next(e for e in events if e["type"] == "tool_preparing")
    assert preview["calls"][0]["summary"] == ""
    assert "private-target" not in json.dumps(events)
    assert not any(e["type"] in {"tool_calls", "tool_result"} for e in events)


def test_discarded_reasoning_from_provider_recovery_never_binds_to_saved_answer(tmp_path):
    from test_llm_stream_semantics import chunk, provider

    llm, _, _ = provider([chunk(reasoning="discarded draft", finish="stop")], [chunk("saved answer", finish="stop")])
    llm._estimate_calibration = 1.0

    async def run():
        agent = ReActAgent("test", llm, ToolRegistry(), timing_log_enabled=False)
        agent.context.set_session(str(tmp_path / "session.json"))
        agent.context.show_reasoning = True
        return [e async for e in agent.reply_stream(Msg(content=[ContentBlock.text("hi")]))]

    events = asyncio.run(run())
    abandoned = next(e["stream_id"] for e in events if e["type"] == "reasoning")
    saved = next(e["stream_id"] for e in events if e["type"] == "chunk")
    assert abandoned != saved
    assert {e["stream_id"] for e in events if e["type"] == "message_source"} == {saved}


def test_fallback_answer_does_not_claim_discarded_synthesis_text(tmp_path):
    class Model:
        calls = 0

        async def chat_stream(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                yield {"type": "tool_calls", "calls": [{"id": "first", "name": "read_result", "arguments": "{}"}]}
            else:
                yield {"type": "chunk", "content": "discarded synthesis"}
                yield {"type": "tool_calls", "content": "discarded synthesis", "calls": [
                    {"id": "second", "name": "read_result", "arguments": "{}"}]}

    async def run():
        registry = ToolRegistry()
        registry.register(ToolDef("read_result", "Read", {"type": "object"}, lambda: "read"))
        agent = ReActAgent("test", Model(), registry, max_iterations=1, timing_log_enabled=False)
        agent.context.set_session(str(tmp_path / "session.json"))
        return [e async for e in agent.reply_stream(Msg(content=[ContentBlock.text("go")]))]

    events = asyncio.run(run())
    chunks = [e for e in events if e["type"] == "chunk"]
    assert chunks[0]["content"] == "discarded synthesis"
    assert chunks[0]["stream_id"] != chunks[-1]["stream_id"]
    anchored = {e["stream_id"] for e in events if e["type"] == "message_source"}
    assert chunks[0]["stream_id"] not in anchored
    assert chunks[-1]["stream_id"] in anchored
