import asyncio
from types import SimpleNamespace as NS

import pytest

from agent.runtime.mcp import MCPManager, MCPServerStatus
from agent.runtime.tools.registry import ToolRegistry


def page(*names, cursor=None):
    return NS(tools=[NS(name=name, inputSchema={"type": "object", "properties": {}}, description=name) for name in names], nextCursor=cursor)


class Session:
    def __init__(self, *pages):
        self.pages = iter(pages)
        self.cursors = []

    async def list_tools(self, cursor=None):
        self.cursors.append(cursor)
        value = next(self.pages)
        if isinstance(value, Exception):
            raise value
        return value


def test_all_pages_collected_in_order():
    session = Session(page("one", cursor="next"), page("two"))
    result = asyncio.run(MCPManager._list_all_tools(session))
    assert [tool.name for tool in result.tools] == ["one", "two"]
    assert session.cursors == [None, "next"]


@pytest.mark.parametrize("pages", [
    [page("one", cursor="next"), RuntimeError("offline")],
    [page("one", cursor="next"), page("two", cursor="next")],
])
def test_failed_refresh_preserves_valid_tools_and_connection(tmp_path, pages):
    manager = MCPManager(tmp_path / "config.json")
    registry = ToolRegistry()
    session = Session(*pages)
    manager._register_tools(registry, "server", session, page("existing").tools, {})
    previous = registry.get("mcp__server__existing")
    manager._registry = registry
    manager._sessions["server"] = session
    manager._server_configs["server"] = {}
    manager.statuses = [MCPServerStatus("server", "ready", tools=1)]
    asyncio.run(manager._refresh_server_tools("server"))
    assert registry.get("mcp__server__existing") is previous
    assert manager._sessions["server"] is session
    assert manager.loaded_tools == 1
    assert "retaining previous tools" in manager.statuses[0].error


@pytest.mark.parametrize("pages,usable,reason", [
    ([page("same", cursor="next"), page("same")], {"same"}, 'a repeated "same" was skipped'),
    ([page("one", cursor="next"), page("")], {"one"}, "a tool was skipped: its name is empty"),
    ([page("same.name", "same name")], {"same.name", "same name"}, "2 tools of this server would be named"),
])
def test_one_flawed_tool_does_not_fail_the_refresh(tmp_path, pages, usable, reason):
    manager = MCPManager(tmp_path / "config.json")
    registry = ToolRegistry()
    session = Session(*pages)
    manager._register_tools(registry, "server", session, page("existing").tools, {})
    manager._registry = registry
    manager._sessions["server"] = session
    manager._server_configs["server"] = {}
    manager.statuses = [MCPServerStatus("server", "ready", tools=1)]
    asyncio.run(manager._refresh_server_tools("server"))
    # These listings used to be refused whole, so the server kept its old tools (or,
    # at startup, had none); now the usable tools are registered and the status says
    # what happened to the other.
    status = manager.statuses[0]
    assert status.state == "ready" and status.error == ""
    assert registry.get("mcp__server__existing") is None
    # page() gives each tool its remote name as the description's first line.
    described = sorted(registry.get(name).description.splitlines()[0] for name in registry.tool_names)
    assert described == sorted(usable)
    assert status.tools == len(usable)
    assert any(reason in note for note in status.notes)
    assert reason in manager.report()
