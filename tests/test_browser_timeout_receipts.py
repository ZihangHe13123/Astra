"""Outer registry timeouts must preserve browser dispatch uncertainty."""

import asyncio
import inspect
import json
from types import SimpleNamespace

import pytest

from agent.runtime.browser_session import BackendCapabilities, BrowserSessionManager
from agent.runtime.code_mode import register_run_code_tool
from agent.runtime.react import ReActAgent
from agent.runtime.tools.browser import register_browser_tools
from agent.runtime.tools.registry import ToolDef, ToolRegistry


OPERATIONS = ("click", "type", "fill", "check")


class WaitingBackend:
    name = "test"
    capabilities = BackendCapabilities(True, True, True)

    def __init__(self, stage):
        self.stage = stage
        self.writes = 0
        self.reads = 0
        self.entered = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def wait(self):
        self.entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.cancelled.set()

    async def assert_origin(self, **kwargs):
        if self.stage == "origin":
            await self.wait()

    async def action(self, *args, **kwargs):
        self.writes += 1
        if self.stage == "backend":
            await self.wait()
        if self.stage == "failure":
            raise RuntimeError("inner failure")
        if self.stage == "observation":
            return "legacy receipt requiring observation"
        return json.dumps({"status": "observed", "after": {
            "url": "https://example.com/form", "title": "Form",
            "snapshotId": "after", "text": "changed", "elements": [],
        }})

    interactive_click = action
    interactive_type = action
    interactive_fill = action
    interactive_check = action

    async def interactive_read(self, *args, **kwargs):
        self.reads += 1
        await self.wait()

    async def interactive_state(self, **kwargs):
        self.reads += 1
        await self.wait()


def arguments(operation):
    args = {"selector": "#field"}
    if operation in {"type", "fill"}:
        args["text"] = "replacement"
    return args


async def setup(tmp_path, stage="backend", filename="browser.db"):
    registry = ToolRegistry()
    backend = WaitingBackend(stage)
    manager = BrowserSessionManager(path=tmp_path / filename)
    register_browser_tools(registry, manager=manager, backend=backend)
    opened = await registry.execute("browser_open", {"url": "https://example.com/form", "extract": False})
    assert not opened["error"]
    for operation in (*OPERATIONS, "read"):
        registry.get("browser_" + operation).timeout = 0.1
    return registry, backend


def assert_receipt(result, operation, dispatched):
    state = "unknown" if dispatched else "not_dispatched"
    assert result["code"] == "overall_timeout", result
    assert result["error_type"] == "overall_timeout"
    assert result["retryable"] is False
    assert result["partial"] is dispatched
    assert result["details"] == {"operation": operation, "dispatch_state": state}
    assert "browser_snapshot/browser_read" in result["recovery_hint"]
    assert "Do not replay" in result["recovery_hint"]
    # Code Mode's RPC sends only this string, not structured failure fields.
    assert f"dispatch_state={state}" in result["error"]
    assert "browser_snapshot/browser_read" in result["error"]
    assert "Do not replay" in result["error"]


def assert_generic_timeout(result):
    assert result["code"] == "overall_timeout", result
    assert result["retryable"] is False
    assert "partial" not in result and "details" not in result
    assert result["error"].startswith("[ToolTimeout]")
    assert "dispatch_state" not in result["error"]
    assert "This exact invocation already exhausted" in result["recovery_hint"]


def register_wait(registry, name="generic_wait", timeout=0.03, fn=None):
    async def wait():
        await asyncio.Event().wait()
    registry.register(ToolDef(name, name, {"type": "object", "properties": {}}, fn or wait, timeout=timeout))


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("stage", ["origin", "backend", "observation"])
def test_outer_timeout_preserves_mutation_state(tmp_path, operation, stage):
    async def scenario():
        registry, backend = await setup(tmp_path, stage)
        result = await registry.execute("browser_" + operation, arguments(operation))
        dispatched = stage != "origin"
        assert backend.writes == int(dispatched)
        assert backend.reads == int(stage == "observation")
        assert backend.cancelled.is_set()
        assert_receipt(result, operation, dispatched)
    asyncio.run(scenario())


def test_waiting_for_lifecycle_lock_is_not_dispatched(tmp_path):
    async def scenario():
        registry, backend = await setup(tmp_path)
        registry.get("browser_click").timeout = None
        first = asyncio.create_task(registry.execute("browser_click", arguments("click")))
        try:
            await asyncio.wait_for(backend.entered.wait(), 1)
            waiting = await registry.execute("browser_fill", arguments("fill"))
            assert backend.writes == 1
            assert_receipt(waiting, "fill", False)
        finally:
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
    asyncio.run(scenario())


def test_concurrent_timeout_slots_are_isolated(tmp_path):
    async def scenario():
        first, first_backend = await setup(tmp_path, "backend", "first.db")
        second, second_backend = await setup(tmp_path, "origin", "second.db")
        register_wait(first)
        dispatched, pending, generic = await asyncio.gather(
            first.execute("browser_fill", arguments("fill")),
            second.execute("browser_type", arguments("type")),
            first.execute("generic_wait", {}),
        )
        assert (first_backend.writes, second_backend.writes) == (1, 0)
        assert_receipt(dispatched, "fill", True)
        assert_receipt(pending, "type", False)
        assert_generic_timeout(generic)
    asyncio.run(scenario())


def test_consecutive_invocations_do_not_reuse_timeout_state(tmp_path):
    async def scenario():
        registry, backend = await setup(tmp_path)
        register_wait(registry)
        first = await registry.execute("browser_fill", arguments("fill"))
        generic = await registry.execute("generic_wait", {})
        backend.stage = "origin"
        second = await registry.execute("browser_fill", arguments("fill"))
        backend.stage = "success"
        success = await registry.execute("browser_fill", arguments("fill"))
        assert backend.writes == 2
        assert not success["error"] and "partial" not in success
        assert_receipt(first, "fill", True)
        assert_receipt(second, "fill", False)
        assert_generic_timeout(generic)
    asyncio.run(scenario())


@pytest.mark.parametrize("browser_outer", [False, True])
def test_nested_registry_calls_restore_parent_slot(tmp_path, browser_outer):
    async def scenario():
        registry, backend = await setup(tmp_path)
        inner_results = []
        if browser_outer:
            register_wait(registry)

            async def action(*args, **kwargs):
                backend.writes += 1
                inner_results.append(await registry.execute("generic_wait", {}))
                await backend.wait()

            backend.interactive_fill = action
            result = await registry.execute("browser_fill", arguments("fill"))
            assert_generic_timeout(inner_results[0])
            assert_receipt(result, "fill", True)
        else:
            async def outer():
                inner_results.append(await registry.execute("browser_fill", arguments("fill")))
                await asyncio.Event().wait()

            register_wait(registry, "outer", timeout=0.3, fn=outer)
            result = await registry.execute("outer", {})
            assert_generic_timeout(result)
            assert_receipt(inner_results[0], "fill", True)
        assert backend.writes == 1
    asyncio.run(scenario())


def test_preflight_timeout_has_initialized_generic_fallback(tmp_path):
    async def scenario():
        registry, backend = await setup(tmp_path)
        await registry.execute("browser_fill", arguments("fill"))

        async def permission_check(args):
            raise TimeoutError("permission preflight timed out")

        registry.get("browser_fill").permission_check = permission_check
        result = await registry.execute("browser_fill", arguments("fill"))
        assert backend.writes == 1
        assert_generic_timeout(result)
    asyncio.run(scenario())


def test_read_timeout_remains_generic(tmp_path):
    async def scenario():
        registry, backend = await setup(tmp_path)
        result = await registry.execute("browser_read", arguments("read"))
        assert backend.writes == 0 and backend.reads == 1
        assert_generic_timeout(result)
    asyncio.run(scenario())


def test_completed_inner_failure_is_not_replaced_by_timeout_state(tmp_path):
    async def scenario():
        registry, backend = await setup(tmp_path, "failure")
        result = await registry.execute("browser_fill", arguments("fill"))
        assert backend.writes == 1
        assert result["code"] == "browser_error"
        assert result["partial"] is True and result["retryable"] is False
        assert result["details"] == {"operation": "fill", "dispatch_state": "unknown"}
        assert "inner failure" in result["error"] and "ToolTimeout" not in result["error"]
    asyncio.run(scenario())


@pytest.mark.parametrize("cancel", [False, True])
def test_timeout_and_external_cancellation_clean_up_permissions_and_lock(tmp_path, cancel):
    async def scenario():
        registry, backend = await setup(tmp_path)
        cleaned = []
        finalized = []
        tool = registry.get("browser_fill")
        if cancel:
            tool.timeout = None
        tool.permission_check = lambda args: {"reason": "test scoped permission"}
        tool.permission_grant = lambda *args: lambda: cleaned.append("cleaned")
        tool.permission_finalizer = lambda args: finalized.append("finalized")

        async def approve(request):
            return "once"

        registry.set_approval_handler(approve)
        task = asyncio.create_task(registry.execute("browser_fill", arguments("fill")))
        await asyncio.wait_for(backend.entered.wait(), 1)
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            result = await task
            assert result["code"] == "overall_timeout"
        assert backend.cancelled.is_set()
        assert cleaned == ["cleaned"] and finalized == ["finalized"]
        register_wait(registry)
        assert_generic_timeout(await registry.execute("generic_wait", {}))
        backend.stage = "success"
        success = await asyncio.wait_for(registry.execute("browser_fill", arguments("fill")), 1)
        assert not success["error"] and backend.writes == 2
    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["native", "code"])
@pytest.mark.parametrize("stage", ["origin", "backend"])
def test_real_agent_paths_preserve_observe_before_replay_guidance(tmp_path, mode, stage):
    async def scenario():
        registry, backend = await setup(tmp_path, stage)
        agent = ReActAgent("test", SimpleNamespace(), registry, code_mode=mode)
        register_run_code_tool(registry, agent_getter=lambda: agent)
        args = arguments("fill")
        name = "browser_fill"
        if mode == "code":
            args = {"code": f"return await tools.browser_fill(**{args!r})", "description": "one write"}
            name = "run_code"
        result, = await agent._execute_tool_calls([
            {"id": "timeout-call", "name": name, "arguments": json.dumps(args)},
        ])
        dispatched = stage != "origin"
        assert backend.writes == int(dispatched)
        if mode == "native":
            assert_receipt(result, "fill", dispatched)
        else:
            text = result["error"] or result["output"]
            state = "unknown" if dispatched else "not_dispatched"
            assert "ToolTimeout" in text
            assert f"dispatch_state={state}" in text
            assert "browser_snapshot/browser_read" in text and "Do not replay" in text
    asyncio.run(scenario())


def test_timeout_context_does_not_enter_browser_signatures_or_schemas(tmp_path):
    async def scenario():
        registry, _ = await setup(tmp_path)
        for operation in OPERATIONS:
            tool = registry.get("browser_" + operation)
            signature = inspect.signature(tool.fn)
            assert "selector" in signature.parameters
            assert not any(param.kind in {param.VAR_POSITIONAL, param.VAR_KEYWORD} for param in signature.parameters.values())
            assert not any("timeout" in name or "dispatch" in name for name in tool.parameters["properties"])
    asyncio.run(scenario())
