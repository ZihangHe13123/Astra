"""Trace correlation evaluation suite.

Tests TraceContext creation, attribute flattening, tool-call scoping,
and trace_span integration with correlation IDs.

Run with:
    pytest tests/test_trace_correlation_eval.py -v
"""

import asyncio
import json
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent.runtime.browser_session import BrowserSessionManager
from agent.runtime.memory import MemoryStore
from agent.runtime.react import ReActAgent
from agent.runtime.tools.browser import register_browser_tools
from agent.runtime.tools.policy import ToolPolicy
from agent.runtime.tools.registry import ToolRegistry
from agent.runtime.tracing import TraceContext, trace_span


class TestTraceContext:
    def test_empty_context_no_attributes(self):
        ctx = TraceContext()
        assert ctx.to_attributes() == {}

    def test_full_context_flattens(self):
        ctx = TraceContext(
            session_id="sess-1",
            task_id="task-2",
            request_id="req-3",
            tool_call_id="tc-4",
            memory_id="mem-5",
            browser_session_id="bs-6",
        )
        attrs = ctx.to_attributes()
        assert attrs["agent.session_id"] == "sess-1"
        assert attrs["agent.task_id"] == "task-2"
        assert attrs["agent.request_id"] == "req-3"
        assert attrs["agent.tool_call_id"] == "tc-4"
        assert attrs["agent.memory_id"] == "mem-5"
        assert attrs["agent.browser_session_id"] == "bs-6"

    def test_partial_context_only_includes_set_fields(self):
        ctx = TraceContext(session_id="s1", request_id="r1")
        attrs = ctx.to_attributes()
        assert "agent.session_id" in attrs
        assert "agent.request_id" in attrs
        assert "agent.task_id" not in attrs
        assert "agent.tool_call_id" not in attrs

    def test_extra_attributes_prefixed(self):
        ctx = TraceContext(extra={"custom_key": "custom_val"})
        attrs = ctx.to_attributes()
        assert attrs["agent.custom_key"] == "custom_val"

    def test_with_tool_call_creates_scoped_copy(self):
        parent = TraceContext(session_id="s1", task_id="t1", request_id="r1")
        child = parent.with_tool_call("tc-99")
        assert child.tool_call_id == "tc-99"
        assert child.session_id == "s1"
        assert child.task_id == "t1"
        assert child.request_id == "r1"
        # Parent unchanged
        assert parent.tool_call_id == ""

    def test_with_tool_call_preserves_extra(self):
        parent = TraceContext(session_id="s1", extra={"k": "v"})
        child = parent.with_tool_call("tc-1")
        assert child.extra == {"k": "v"}
        # Mutating child extra doesn't affect parent
        child.extra["k2"] = "v2"
        assert "k2" not in parent.extra

    def test_with_tool_call_preserves_browser_and_memory(self):
        parent = TraceContext(memory_id="m1", browser_session_id="b1")
        child = parent.with_tool_call("tc-1")
        assert child.memory_id == "m1"
        assert child.browser_session_id == "b1"

    def test_memory_and_browser_scopes_preserve_other_ids(self):
        parent = TraceContext(session_id="s1", memory_id="m1", browser_session_id="b1")
        memory = parent.with_memory_id("m2")
        browser = parent.with_browser_session("b2")
        assert memory.memory_id == "m2" and memory.browser_session_id == "b1"
        assert browser.browser_session_id == "b2" and browser.memory_id == "m1"
        assert parent.memory_id == "m1" and parent.browser_session_id == "b1"


class TestTraceSpanWithContext:
    def test_trace_span_noop_without_otel(self):
        """trace_span with ctx should not crash when OTel is disabled."""
        ctx = TraceContext(session_id="s1", task_id="t1")
        with trace_span("test.span", {"key": "value"}, ctx=ctx) as span:
            # OTel not configured in test env — span is None
            assert span is None

    def test_trace_span_without_ctx_still_works(self):
        with trace_span("test.span", {"key": "value"}) as span:
            assert span is None

    def test_trace_span_ctx_none_is_safe(self):
        with trace_span("test.span", ctx=None) as span:
            assert span is None


class TestTraceContextImmutability:
    def test_with_tool_call_returns_new_instance(self):
        parent = TraceContext(session_id="s1")
        child = parent.with_tool_call("tc-1")
        assert parent is not child
        assert isinstance(child, TraceContext)

    def test_multiple_scopes_independent(self):
        parent = TraceContext(session_id="s1", task_id="t1")
        c1 = parent.with_tool_call("tc-1")
        c2 = parent.with_tool_call("tc-2")
        assert c1.tool_call_id == "tc-1"
        assert c2.tool_call_id == "tc-2"
        assert parent.tool_call_id == ""


class TestRuntimeTracePopulation:
    class FakeLLM:
        def __init__(self):
            self.config = SimpleNamespace(model="fake", capabilities=frozenset())

    def test_context_index_memory_recall_populates_memory_trace_context(self, tmp_path, monkeypatch):
        from agent.runtime.context_index.broker import ContextIndexBroker
        from agent.runtime.context_index.models import SourceResult
        from tests.test_context_index_broker import FakeSource

        monkeypatch.setenv("HINDSIGHT_ENABLED", "0")
        store = MemoryStore(path=tmp_path / "memory.db", core_dir=tmp_path / "core")
        record = store.add_record(kind="observation", content="Astra recovery requires an atomic SQLite transaction")
        empty = SourceResult("absent")

        def traced(broker):
            agent = ReActAgent(
                "agent",
                self.FakeLLM(),  # type: ignore[arg-type]
                ToolRegistry(),
                memory_store=store,
                context_index_broker=broker,
            )
            agent.context.set_session(str(tmp_path / "session-a.jsonl"))
            trace_ctx = TraceContext(session_id="session-a", request_id="request-a")
            asyncio.run(agent._prepare_prompt_for_llm("Astra recovery constraints", None, trace_ctx))
            return trace_ctx.memory_id

        # Not a test of the 200 ms source deadline, which can run out on a busy runner before a source thread starts.
        assert traced(ContextIndexBroker(
            "session", 900, FakeSource("session", result=empty), FakeSource("activity", result=empty),
            source_deadline_seconds=5.0,
        )) == record.record_id
        # With the Context Index off nothing is recalled into the turn, so no memory is traced.
        assert not traced(None)

    def test_browser_tool_populates_trace_context(self, tmp_path):
        registry = ToolRegistry(policy=ToolPolicy(mode="permissive"))
        manager = BrowserSessionManager(path=tmp_path / "browser.db")
        register_browser_tools(registry, manager=manager)
        agent = ReActAgent("agent", self.FakeLLM(), registry)  # type: ignore[arg-type]
        trace_ctx = TraceContext(session_id="session-a", request_id="request-a")

        event = asyncio.run(agent._execute_tool_call(
            {
                "id": "call-browser",
                "name": "browser_open",
                "arguments": json.dumps({"url": "https://example.test", "extract": False}),
            },
            trace_ctx=trace_ctx,
        ))

        assert event["error"] == ""
        assert trace_ctx.browser_session_id
