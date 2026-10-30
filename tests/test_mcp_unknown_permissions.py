import asyncio
from types import SimpleNamespace as NS

import pytest

from agent.runtime.mcp import MCPManager
from agent.runtime.tools.policy import ToolPolicy
from agent.runtime.tools.registry import ToolRegistry


@pytest.mark.parametrize("name", ["create_record", "update_event", "set_access"])
@pytest.mark.parametrize("hint", [None, False, True])
def test_unknown_mcp_side_effects_require_permission(tmp_path, name, hint):
    async def run():
        dispatched, asked = [], []
        class Session:
            async def call_tool(self, name, arguments):
                dispatched.append(name)
                return NS(content=[NS(text="done")], isError=False)
        async def deny(request):
            asked.append(request)
            return "deny"
        registry = ToolRegistry(policy=ToolPolicy(mode="locked"))
        registry.set_approval_handler(deny)
        manager = MCPManager(tmp_path / "mcp.json")
        manager._register_tools(registry, "fixture", Session(), [NS(name=name, description="fixture",
            inputSchema={"type": "object", "properties": {}}, annotations=NS(readOnlyHint=hint))], {})
        result = await registry.execute(f"mcp__fixture__{name}", {})
        assert result["code"] == "approval_denied"
        assert len(asked) == 1
        assert dispatched == []
    asyncio.run(run())


def test_locally_configured_read_only_mcp_still_runs(tmp_path):
    async def run():
        dispatched = []
        class Session:
            async def call_tool(self, name, arguments):
                dispatched.append(name)
                return NS(content=[NS(text="read fixture")], isError=False)
        registry = ToolRegistry(policy=ToolPolicy(mode="locked"))
        MCPManager(tmp_path / "mcp.json")._register_tools(registry, "fixture", Session(),
            [NS(name="lookup", description="read", inputSchema={}, annotations=None)], {"risk": "read"})
        result = await registry.execute("mcp__fixture__lookup", {})
        assert result["output"] == "read fixture" and dispatched == ["lookup"]
    asyncio.run(run())


def test_unknown_mcp_cannot_dispatch_without_an_approval_handler(tmp_path):
    class Session:
        async def call_tool(self, **kwargs):
            pytest.fail("unapproved call dispatched")
    registry = ToolRegistry()
    MCPManager(tmp_path / "mcp.json")._register_tools(registry, "fixture", Session(),
        [NS(name="create_record", description="write", inputSchema={}, annotations=None)], {})
    result = asyncio.run(registry.execute("mcp__fixture__create_record", {}))
    assert result["code"] == "approval_required" and result["not_executed"]
