"""Tool and session hooks evaluation suite.

Tests the HookRegistry dispatch system: before_tool, after_tool,
tool_error, session_end, and memory_retain hooks, including
modification, rejection, error isolation, and ordering.

Run with:
    pytest tests/test_hooks_eval.py -v
"""

import asyncio
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent.runtime.context import AgentContext
from agent.runtime.hooks import HookRegistry, HookReject, ToolDecision
from agent.runtime.react import ReActAgent
from agent.runtime.tools.policy import ToolPolicy
from agent.runtime.tools.registry import ToolDef, ToolRegistry


def run(coro):
    return asyncio.run(coro)


def _make_tool(name="test_tool", risk="read", **kwargs):
    async def fn(**kw):
        return {"echo": kw}
    return ToolDef(
        name=name,
        description=f"test tool {name}",
        parameters={"type": "object", "properties": {}},
        fn=fn,
        risk=risk,
        **kwargs,
    )


def test_react_end_session_dispatches_once_until_session_changes(tmp_path):
    events = []
    hooks = HookRegistry()
    hooks.on_session_end(lambda session_id, reason: events.append((session_id, reason)))
    agent = object.__new__(ReActAgent)
    # The real context: the lifecycle identity is the session store's scope.
    agent.context = AgentContext()
    agent.context.set_session(str(tmp_path / "alpha.jsonl"))
    agent.tools = SimpleNamespace(hooks=hooks)
    agent._last_ended_session = None

    agent.end_session("shutdown")
    agent.end_session("shutdown")
    agent.context.set_session(str(tmp_path / "beta.jsonl"))
    agent.end_session("session_switch")
    agent.context.set_session(str(tmp_path / "alpha.jsonl"))
    agent.end_session("shutdown")
    agent.begin_session()
    agent.end_session("reset")

    assert events == [
        ("alpha", "shutdown"),
        ("beta", "session_switch"),
        ("alpha", "shutdown"),
        ("alpha", "reset"),
    ]


# ---------------------------------------------------------------------------
# HookRegistry unit tests
# ---------------------------------------------------------------------------

class TestBeforeToolHook:
    def test_no_hooks_returns_args_unchanged(self):
        hr = HookRegistry()
        args = {"x": 1}
        result = hr.dispatch_before_tool("tool", args, None)
        assert result == {"x": 1}

    def test_hook_modifies_args(self):
        hr = HookRegistry()
        hr.on_before_tool(lambda name, args, td: {**args, "injected": True})
        result = hr.dispatch_before_tool("tool", {"x": 1}, None)
        assert result == {"x": 1, "injected": True}

    def test_hook_returns_none_keeps_args(self):
        hr = HookRegistry()
        hr.on_before_tool(lambda name, args, td: None)
        result = hr.dispatch_before_tool("tool", {"x": 1}, None)
        assert result == {"x": 1}

    def test_hook_reject_blocks(self):
        hr = HookRegistry()
        def reject_hook(name, args, td):
            raise HookReject("blocked by policy")
        hr.on_before_tool(reject_hook)
        with pytest.raises(HookReject, match="blocked by policy"):
            hr.dispatch_before_tool("tool", {}, None)

    def test_hook_exception_isolated(self):
        hr = HookRegistry()
        def bad_hook(name, args, td):
            raise RuntimeError("hook crashed")
        hr.on_before_tool(bad_hook)
        # Should not raise — exception is logged and swallowed
        result = hr.dispatch_before_tool("tool", {"x": 1}, None)
        assert result == {"x": 1}

    def test_multiple_hooks_chain(self):
        hr = HookRegistry()
        hr.on_before_tool(lambda name, args, td: {**args, "step1": True})
        hr.on_before_tool(lambda name, args, td: {**args, "step2": True})
        result = hr.dispatch_before_tool("tool", {}, None)
        assert result == {"step1": True, "step2": True}


class TestAfterToolHook:
    def test_no_hooks_returns_result_unchanged(self):
        hr = HookRegistry()
        result = {"output": "hello", "error": ""}
        out = hr.dispatch_after_tool("tool", {}, result, None)
        assert out == result

    def test_hook_modifies_result(self):
        hr = HookRegistry()
        hr.on_after_tool(lambda name, args, result, td: {**result, "audited": True})
        out = hr.dispatch_after_tool("tool", {}, {"output": "hi", "error": ""}, None)
        assert out["audited"] is True

    def test_hook_reject_blocks_result(self):
        hr = HookRegistry()
        def reject_hook(name, args, result, td):
            raise HookReject("result rejected")
        hr.on_after_tool(reject_hook)
        with pytest.raises(HookReject, match="result rejected"):
            hr.dispatch_after_tool("tool", {}, {"output": "hi"}, None)

    def test_hook_exception_isolated(self):
        hr = HookRegistry()
        def bad_hook(name, args, result, td):
            raise ValueError("oops")
        hr.on_after_tool(bad_hook)
        out = hr.dispatch_after_tool("tool", {}, {"output": "hi", "error": ""}, None)
        assert out["output"] == "hi"


class TestToolErrorHook:
    def test_error_hook_called(self):
        hr = HookRegistry()
        captured = []
        hr.on_tool_error(lambda name, args, error, td: captured.append((name, error)))
        hr.dispatch_tool_error("my_tool", {"x": 1}, "something broke", None)
        assert len(captured) == 1
        assert captured[0] == ("my_tool", "something broke")

    def test_error_hook_exception_isolated(self):
        hr = HookRegistry()
        def bad_hook(name, args, error, td):
            raise RuntimeError("hook crashed")
        hr.on_tool_error(bad_hook)
        # Should not raise
        hr.dispatch_tool_error("tool", {}, "err", None)

    def test_multiple_error_hooks(self):
        hr = HookRegistry()
        calls = []
        hr.on_tool_error(lambda n, a, e, td: calls.append("hook1"))
        hr.on_tool_error(lambda n, a, e, td: calls.append("hook2"))
        hr.dispatch_tool_error("tool", {}, "err", None)
        assert calls == ["hook1", "hook2"]


class TestTypedToolStages:
    def test_decisions_are_monotonic_and_deny_wins(self):
        hooks = HookRegistry()
        hooks.on_tool_decision(lambda n, a, td: ToolDecision.ask("review it"))
        hooks.on_tool_decision(lambda n, a, td: ToolDecision.allow("looks fine"))
        hooks.on_tool_decision(lambda n, a, td: ToolDecision.deny("blocked"))
        hooks.on_tool_decision(lambda n, a, td: ToolDecision.allow())

        decision = hooks.dispatch_tool_decision("tool", {}, None)

        assert decision == ToolDecision.deny("blocked")

    def test_around_hooks_are_nested_in_registration_order(self):
        hooks = HookRegistry()
        order = []

        async def outer(name, args, td, call_next):
            order.append("outer-before")
            result = await call_next()
            order.append("outer-after")
            return result

        async def inner(name, args, td, call_next):
            order.append("inner-before")
            result = await call_next()
            order.append("inner-after")
            return result

        async def invoke():
            order.append("tool")
            return "ok"

        hooks.on_around_tool(outer)
        hooks.on_around_tool(inner)

        assert run(hooks.dispatch_around_tool("tool", {}, None, invoke)) == "ok"
        assert order == ["outer-before", "inner-before", "tool", "inner-after", "outer-after"]

    def test_result_observer_cannot_mutate_authoritative_result(self):
        hooks = HookRegistry()
        authoritative = {"output": "ok", "nested": {"value": 1}}

        def mutate(name, args, result, td):
            args["changed"] = True
            result["nested"]["value"] = 99

        hooks.on_tool_result(mutate)
        hooks.dispatch_tool_result("tool", {"x": 1}, authoritative, None)

        assert authoritative["nested"]["value"] == 1


class TestSessionEndHook:
    def test_session_end_called(self):
        hr = HookRegistry()
        captured = []
        hr.on_session_end(lambda sid, reason: captured.append((sid, reason)))
        hr.dispatch_session_end("sess-123", "user_quit")
        assert captured == [("sess-123", "user_quit")]

    def test_session_end_exception_isolated(self):
        hr = HookRegistry()
        def bad_hook(sid, reason):
            raise RuntimeError("crash")
        hr.on_session_end(bad_hook)
        hr.dispatch_session_end("sess", "timeout")  # should not raise


class TestMemoryRetainHook:
    def test_no_hooks_returns_record_unchanged(self):
        hr = HookRegistry()
        record = {"content": "user likes cats", "kind": "preference"}
        out = hr.dispatch_memory_retain(record)
        assert out == record

    def test_hook_modifies_record(self):
        hr = HookRegistry()
        hr.on_memory_retain(lambda rec: {**rec, "reviewed": True})
        out = hr.dispatch_memory_retain({"content": "fact"})
        assert out["reviewed"] is True

    def test_hook_reject_blocks_retention(self):
        hr = HookRegistry()
        def reject_hook(rec):
            raise HookReject("PII detected")
        hr.on_memory_retain(reject_hook)
        with pytest.raises(HookReject, match="PII detected"):
            hr.dispatch_memory_retain({"content": "secret"})

    def test_hook_exception_isolated(self):
        hr = HookRegistry()
        def bad_hook(rec):
            raise ValueError("oops")
        hr.on_memory_retain(bad_hook)
        out = hr.dispatch_memory_retain({"content": "fact"})
        assert out["content"] == "fact"


class TestHookRegistryIntrospection:
    def test_counts(self):
        hr = HookRegistry()
        assert hr.counts == {
            "before_tool": 0, "tool_decision": 0, "around_tool": 0,
            "after_tool": 0, "tool_result": 0, "tool_error": 0,
            "session_end": 0, "memory_retain": 0,
            "pre_compact": 0, "post_compact": 0,
            "runtime_event": 0,
        }
        hr.on_before_tool(lambda n, a, td: None)
        hr.on_after_tool(lambda n, a, r, td: None)
        assert hr.counts["before_tool"] == 1
        assert hr.counts["after_tool"] == 1

    def test_runtime_event_count_is_isolated_from_other_hook_channels(self):
        hr = HookRegistry()
        before = hr.counts

        hr.on_runtime_event(lambda event: None)

        assert hr.counts == {**before, "runtime_event": 1}
        assert all(
            count == 0
            for name, count in hr.counts.items()
            if name != "runtime_event"
        )

    def test_clear(self):
        hr = HookRegistry()
        hr.on_before_tool(lambda n, a, td: None)
        hr.on_tool_error(lambda n, a, e, td: None)
        hr.clear()
        assert all(v == 0 for v in hr.counts.values())


# ---------------------------------------------------------------------------
# Integration: hooks + ToolRegistry.execute()
# ---------------------------------------------------------------------------

class TestRegistryHookIntegration:
    def test_before_tool_hook_receives_call(self):
        hooks = HookRegistry()
        captured = []
        hooks.on_before_tool(lambda name, args, td: captured.append(name) or None)

        registry = ToolRegistry(policy=ToolPolicy(mode="permissive"), hooks=hooks)
        registry.register(_make_tool("my_tool"))
        run(registry.execute("my_tool", {"x": 1}))
        assert "my_tool" in captured

    def test_before_tool_reject_blocks_execution(self):
        hooks = HookRegistry()
        hooks.on_before_tool(lambda name, args, td: (_ for _ in ()).throw(HookReject("nope")))

        registry = ToolRegistry(policy=ToolPolicy(mode="permissive"), hooks=hooks)
        registry.register(_make_tool("blocked_tool"))
        result = run(registry.execute("blocked_tool", {}))
        assert "HookReject" in result.get("error", "")

    def test_after_tool_hook_receives_result(self):
        hooks = HookRegistry()
        captured = []
        hooks.on_after_tool(lambda name, args, result, td: captured.append(result.get("output")) or None)

        registry = ToolRegistry(policy=ToolPolicy(mode="permissive"), hooks=hooks)
        registry.register(_make_tool("echo_tool"))
        run(registry.execute("echo_tool", {"msg": "hello"}))
        assert len(captured) == 1
        assert "hello" in captured[0]

    def test_tool_error_hook_receives_error(self):
        hooks = HookRegistry()
        captured = []
        hooks.on_tool_error(lambda name, args, error, td: captured.append(error))

        async def failing_fn(**kw):
            raise ValueError("intentional failure")

        tool = ToolDef(
            name="fail_tool", description="fails", parameters={"type": "object", "properties": {}},
            fn=failing_fn, risk="read",
        )
        registry = ToolRegistry(policy=ToolPolicy(mode="permissive"), hooks=hooks)
        registry.register(tool)
        result = run(registry.execute("fail_tool", {}))
        assert result.get("error")
        assert len(captured) == 1
        assert "intentional failure" in captured[0]

    def test_before_tool_modifies_args(self):
        hooks = HookRegistry()
        hooks.on_before_tool(lambda name, args, td: {**args, "injected": "by_hook"})

        captured_args = []
        async def capture_fn(**kw):
            captured_args.append(kw)
            return "ok"

        tool = ToolDef(
            name="capture_tool", description="captures", parameters={"type": "object", "properties": {}},
            fn=capture_fn, risk="read",
        )
        registry = ToolRegistry(policy=ToolPolicy(mode="permissive"), hooks=hooks)
        registry.register(tool)
        run(registry.execute("capture_tool", {"original": True}))
        assert captured_args[0].get("injected") == "by_hook"
        assert captured_args[0].get("original") is True

    def test_after_tool_modifies_result(self):
        hooks = HookRegistry()
        hooks.on_after_tool(lambda name, args, result, td: {**result, "output": result.get("output", "") + " [audited]"})

        registry = ToolRegistry(policy=ToolPolicy(mode="permissive"), hooks=hooks)
        registry.register(_make_tool("audit_tool"))
        result = run(registry.execute("audit_tool", {"x": 1}))
        assert "[audited]" in result.get("output", "")

    def test_decision_deny_blocks_execution(self):
        hooks = HookRegistry()
        called = []
        hooks.on_tool_decision(lambda n, a, td: ToolDecision.deny("unsafe context"))
        tool = _make_tool("decision_blocked")
        original_fn = tool.fn

        async def tracked(**kwargs):
            called.append(True)
            return await original_fn(**kwargs)

        tool.fn = tracked
        registry = ToolRegistry(policy=ToolPolicy(mode="permissive"), hooks=hooks)
        registry.register(tool)

        result = run(registry.execute("decision_blocked", {}))

        assert result["error_type"] == "hook_decision_denied"
        assert called == []

    def test_decision_allow_does_not_bypass_locked_policy(self):
        hooks = HookRegistry()
        hooks.on_tool_decision(lambda n, a, td: ToolDecision.allow())
        registry = ToolRegistry(policy=ToolPolicy(mode="locked"), hooks=hooks)
        registry.register(_make_tool("execute_tool", risk="execute"))

        result = run(registry.execute("execute_tool", {}))

        assert result["error_type"] == "approval_required"

    def test_around_and_result_hooks_cover_execution(self):
        hooks = HookRegistry()
        events = []

        async def around(name, args, td, call_next):
            events.append("before")
            result = await call_next()
            events.append("after")
            return result

        hooks.on_around_tool(around)
        hooks.on_tool_result(lambda n, a, result, td: events.append(("result", result["error"])))
        registry = ToolRegistry(policy=ToolPolicy(mode="permissive"), hooks=hooks)
        registry.register(_make_tool("staged_tool"))

        result = run(registry.execute("staged_tool", {}))

        assert result["error"] == ""
        assert events == ["before", "after", ("result", "")]

    def test_default_registry_has_only_internal_session_cleanup_hook(self):
        registry = ToolRegistry()
        assert isinstance(registry.hooks, HookRegistry)
        assert registry.hooks.counts == {
            "before_tool": 0,
            "tool_decision": 0,
            "around_tool": 0,
            "after_tool": 0,
            "tool_result": 0,
            "tool_error": 0,
            "session_end": 1,
            "memory_retain": 0,
            "pre_compact": 0,
            "post_compact": 0,
            "runtime_event": 0,
        }


# ---------------------------------------------------------------------------
# Postcondition verifier
# ---------------------------------------------------------------------------

class TestPostconditionVerifier:
    def test_postcondition_pass_sets_verified_true(self):
        def verify(args, result):
            return True, "all good"

        tool = _make_tool("verified_tool")
        tool.postcondition = verify
        registry = ToolRegistry(policy=ToolPolicy(mode="permissive"))
        registry.register(tool)
        result = run(registry.execute("verified_tool", {"x": 1}))
        assert result.get("verified") is True
        assert "verification_detail" not in result

    def test_postcondition_fail_sets_verified_false(self):
        def verify(args, result):
            return False, "output missing expected field"

        tool = _make_tool("fail_verify_tool")
        tool.postcondition = verify
        registry = ToolRegistry(policy=ToolPolicy(mode="permissive"))
        registry.register(tool)
        result = run(registry.execute("fail_verify_tool", {"x": 1}))
        assert result.get("verified") is False
        assert result.get("verification_detail") == "output missing expected field"
        assert result.get("error", "").startswith("[ToolPostconditionFailed]")
        # Preserve the direct output so a failed side effect can be diagnosed.
        assert result.get("output")

    def test_no_postcondition_no_verified_field(self):
        tool = _make_tool("no_verify_tool")
        registry = ToolRegistry(policy=ToolPolicy(mode="permissive"))
        registry.register(tool)
        result = run(registry.execute("no_verify_tool", {"x": 1}))
        assert "verified" not in result

    def test_postcondition_exception_sets_verified_none(self):
        def bad_verify(args, result):
            raise RuntimeError("verifier crashed")

        tool = _make_tool("crash_verify_tool")
        tool.postcondition = bad_verify
        registry = ToolRegistry(policy=ToolPolicy(mode="permissive"))
        registry.register(tool)
        result = run(registry.execute("crash_verify_tool", {"x": 1}))
        assert result.get("verified") is None
        assert result.get("error", "").startswith("[ToolPostconditionError]")
        assert "RuntimeError: verifier crashed" in result.get("verification_detail", "")
        # Tool output is retained for audit even when verification itself fails.
        assert result.get("output")

    def test_postcondition_failure_dispatches_tool_error_hook(self):
        errors = []
        hooks = HookRegistry()
        hooks.on_tool_error(lambda name, args, error, td: errors.append((name, error)))
        tool = _make_tool("observed_verify_failure")
        tool.postcondition = lambda args, result: (False, "missing artifact")
        registry = ToolRegistry(policy=ToolPolicy(mode="permissive"), hooks=hooks)
        registry.register(tool)

        result = run(registry.execute("observed_verify_failure", {}))

        assert result.get("error", "").startswith("[ToolPostconditionFailed]")
        assert errors == [("observed_verify_failure", result["error"])]

    def test_postcondition_receives_args_and_result(self):
        captured = []
        def verify(args, result):
            captured.append((args, result))
            return True, "ok"

        tool = _make_tool("capture_verify_tool")
        tool.postcondition = verify
        registry = ToolRegistry(policy=ToolPolicy(mode="permissive"))
        registry.register(tool)
        run(registry.execute("capture_verify_tool", {"key": "value"}))
        assert len(captured) == 1
        assert captured[0][0] == {"key": "value"}
        assert "output" in captured[0][1]

    def test_postcondition_runs_before_after_tool_hooks(self):
        order = []
        def verify(args, result):
            order.append("postcondition")
            return True, "ok"

        hooks = HookRegistry()
        hooks.on_after_tool(lambda name, args, result, td: order.append("after_tool") or None)

        tool = _make_tool("order_tool")
        tool.postcondition = verify
        registry = ToolRegistry(policy=ToolPolicy(mode="permissive"), hooks=hooks)
        registry.register(tool)
        run(registry.execute("order_tool", {}))
        assert order == ["postcondition", "after_tool"]
