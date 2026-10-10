import asyncio
from types import SimpleNamespace as NS

import pytest

from agent.runtime.mcp import MCPManager
from agent.runtime.tools.registry import ToolRegistry


class Session:
    def __init__(self, mode="disconnect"):
        self.calls = 0
        self.mode = mode
        self.started = asyncio.Event()

    async def call_tool(self, name, arguments):
        self.calls += 1  # The remote action has happened; its response may be lost.
        self.started.set()
        if self.mode == "disconnect":
            raise ConnectionError("reply lost")
        if self.mode == "wait":
            await asyncio.Event().wait()
        return NS(content=[NS(text="timeout after remote action")], isError=True)


def setup(tmp_path, session, risk="network", hint=None):
    manager, registry = MCPManager(tmp_path / "mcp.json"), ToolRegistry()
    tool = NS(name="action", description="fixture", inputSchema={"type": "object", "properties": {}},
              annotations=NS(readOnlyHint=hint))
    config = {"risk": risk, "timeout": 0.02}
    manager._register_tools(registry, "fixture", session, [tool], config)
    return manager, registry, tool, config


@pytest.mark.parametrize("mode", ["disconnect", "wait", "error"])
@pytest.mark.parametrize("risk", ["network", "write", "execute"])
def test_mutating_failure_never_retries_or_suggests_replay(tmp_path, mode, risk):
    async def scenario():
        session = Session(mode)
        _, registry, _, _ = setup(tmp_path, session, risk)
        result = await registry.execute("mcp__fixture__action", {})
        assert session.calls == 1
        assert result["retryable"] is False
        assert "repeat" in result["recovery_hint"].lower()
        if mode != "error":
            assert result["code"] == "mcp_unknown_outcome" and result["partial"]
            assert result["details"]["dispatch_state"] == "unknown"
    asyncio.run(scenario())


@pytest.mark.parametrize("risk,hint,can_retry", [("read", None, True), ("network", True, True),
                                              ("write", True, False), ("network", None, False)])
def test_read_recovery_does_not_override_configured_side_effects(tmp_path, risk, hint, can_retry):
    session = Session()
    _, registry, _, _ = setup(tmp_path, session, risk, hint)
    result = asyncio.run(registry.execute("mcp__fixture__action", {}))
    assert result["retryable"] is can_retry and session.calls == 1


def test_cancel_propagates_and_old_definition_cannot_dispatch_after_reconnect(tmp_path):
    async def scenario():
        session = Session("wait")
        manager, registry, remote, config = setup(tmp_path, session)
        old = registry.get("mcp__fixture__action")
        task = asyncio.create_task(registry.execute(old.name, {}))
        await session.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert "Do not replay" in manager.statuses[0].error
        disconnected = await old.fn()
        assert disconnected.details["dispatch_state"] == "not_dispatched"
        fresh = Session("error")
        manager._register_tools(registry, "fixture", fresh, [remote], config)
        stale = await old.fn()
        assert stale.code == "mcp_stale_connection"
        assert fresh.calls == 0 and session.calls == 1
    asyncio.run(scenario())


def test_timeout_says_how_long_astra_waited(tmp_path):
    async def scenario():
        _, registry, _, _ = setup(tmp_path, Session("wait"), "read")
        return await registry.execute("mcp__fixture__action", {})

    result = asyncio.run(scenario())

    # The exception itself has no text; the message used to end in "TimeoutError: ".
    assert "did not answer mcp__fixture__action within 0.02s" in result["error"]
    assert "TimeoutError" not in result["error"]
    assert result["details"]["timeout_seconds"] == 0.02
    assert result["retryable"] is True


class RpcError(Exception):
    """Shaped like the SDK's error for a JSON-RPC error response."""

    def __init__(self, code, message):
        super().__init__(message)
        self.error = NS(code=code, message=message)


class RejectingSession(Session):
    def __init__(self, code):
        super().__init__()
        self.code = code

    async def call_tool(self, name, arguments):
        self.calls += 1
        raise RpcError(self.code, "Invalid arguments for tool action: path is required")


def test_json_rpc_error_reply_is_the_servers_answer_and_keeps_the_connection(tmp_path):
    async def scenario():
        session = RejectingSession(-32602)
        manager, registry, _, _ = setup(tmp_path, session)
        first = await registry.execute("mcp__fixture__action", {})
        second = await registry.execute("mcp__fixture__action", {})
        return manager, session, first, second

    manager, session, first, second = asyncio.run(scenario())

    assert first["code"] == "mcp_tool_error"
    assert "Invalid arguments for tool action: path is required" in first["error"]
    assert "lost the call result" not in first["error"] and not first["partial"]
    # The server answered, so nothing is torn down and the next call reaches it.
    assert manager._sessions["fixture"] is session
    assert manager.statuses == []
    assert second["code"] == "mcp_tool_error" and session.calls == 2


def test_closed_connection_code_is_still_a_lost_result(tmp_path):
    async def scenario():
        session = RejectingSession(-32000)
        manager, registry, _, _ = setup(tmp_path, session)
        return manager, await registry.execute("mcp__fixture__action", {})

    manager, result = asyncio.run(scenario())

    assert result["code"] == "mcp_unknown_outcome" and result["partial"]
    assert "fixture" not in manager._sessions


def test_call_to_a_disconnected_server_reports_its_state(tmp_path):
    async def scenario():
        session = Session()
        _, registry, _, _ = setup(tmp_path, session, "read")
        await registry.execute("mcp__fixture__action", {})
        return session, await registry.execute("mcp__fixture__action", {})

    session, result = asyncio.run(scenario())

    assert session.calls == 1
    assert result["code"] == "mcp_unavailable"
    assert result["details"]["dispatch_state"] == "not_dispatched"
    assert "state: error: ConnectionError: reply lost" in result["error"]
    # No tool lists tools again, and nothing reconnects when no lifecycle task runs.
    assert "rediscover" not in result["recovery_hint"]
    assert "Nothing is reconnecting" in result["recovery_hint"]
