"""Hidden browser aliases remain executable without weakening mode boundaries."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from agent.runtime.browser_session import BackendCapabilities, BrowserSessionManager
from agent.runtime.code_mode import CODE_MODE_DIRECT_TOOLS, register_run_code_tool
from agent.runtime.react import ReActAgent
from agent.runtime.tools.browser import register_browser_tools
from agent.runtime.tools.registry import ToolDef, ToolRegistry


class CountingBackend:
    name = "test"
    capabilities = BackendCapabilities(True, True, True)

    def __init__(self):
        self.writes = 0

    async def assert_origin(self, **kwargs):
        pass

    async def interactive_type(self, *args, **kwargs):
        self.writes += 1
        return json.dumps({
            "status": "observed",
            "message": "legacy write observed",
            "after": {
                "url": "https://example.com/form", "title": "Form",
                "snapshotId": "after", "text": "replacement", "elements": [],
            },
        })


def make_agent(tmp_path, mode):
    registry = ToolRegistry()
    backend = CountingBackend()
    manager = BrowserSessionManager(path=tmp_path / "browser.db")
    register_browser_tools(registry, manager=manager, backend=backend)
    agent = ReActAgent("test", SimpleNamespace(), registry, code_mode=mode)
    register_run_code_tool(registry, agent_getter=lambda: agent)
    return agent, backend


async def execute(agent, mode, name="browser_type", args=None):
    args = args if args is not None else {"selector": "#field", "text": "replacement"}
    if mode == "code":
        # Exercise the actual program RPC dispatcher, not just _tool_allowed.
        args = {"code": f"return await tools.{name}(**{args!r})", "description": "legacy call"}
        name = "run_code"
    events = await agent._execute_tool_calls([
        {"id": "compat-call", "name": name, "arguments": json.dumps(args)},
    ])
    assert len(events) == 1
    return events[0]


def test_legacy_alias_stays_hidden_from_default_schema(tmp_path):
    agent, _ = make_agent(tmp_path, "both")
    schemas = agent.tools.to_openai_tools()
    assert "browser_type" not in {tool["function"]["name"] for tool in schemas}
    assert "browser_type" not in agent.tools.tool_names_for_group("browser")
    code_schema = next(
        tool for tool in agent._apply_code_mode(schemas)
        if tool["function"]["name"] == "run_code"
    )
    assert "browser_type" not in code_schema["function"]["parameters"]["properties"]["code"]["description"]
    explicit = agent.tools.to_openai_tools(names={"browser_type"})
    assert [tool["function"]["name"] for tool in explicit] == ["browser_type"]


@pytest.mark.parametrize("mode", ["native", "code"])
def test_legacy_alias_executes_once_through_agent_gate(tmp_path, mode):
    async def scenario():
        agent, backend = make_agent(tmp_path, mode)
        opened = await agent.tools.execute("browser_open", {"url": "https://example.com/form", "extract": False})
        assert not opened["error"]
        result = await execute(agent, mode)
        assert not result["error"], result
        assert "legacy write observed" in result["output"]
        assert backend.writes == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["native", "code"])
@pytest.mark.parametrize("restriction", ["allowlist", "policy"])
def test_legacy_alias_cannot_bypass_explicit_restrictions(tmp_path, mode, restriction):
    async def scenario():
        agent, backend = make_agent(tmp_path, mode)
        opened = await agent.tools.execute("browser_open", {"url": "https://example.com/form", "extract": False})
        assert not opened["error"]
        if restriction == "allowlist":
            agent.tool_allowlist = {"run_code", "browser_fill"}
        else:
            agent.tools.policy.add_rule_shortcut("browser_type", "deny")
        result = await execute(agent, mode)
        failure = result["error"] or result["output"]
        if restriction == "allowlist":
            assert "ToolDisabled" in failure
        else:
            assert "denied" in failure.lower()
        assert backend.writes == 0
    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["native", "code"])
def test_unrelated_hidden_tool_remains_disabled(tmp_path, mode):
    async def scenario():
        agent, backend = make_agent(tmp_path, mode)
        calls = []
        agent.tools.register(ToolDef(
            "private_tool", "private", {"type": "object", "properties": {}},
            lambda: calls.append("called"), expose_by_default=False,
        ))
        result = await execute(agent, mode, "private_tool", {})
        assert "ToolDisabled" in (result["error"] or result["output"])
        assert calls == []
        assert backend.writes == 0
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["direct", "request_local"])
@pytest.mark.parametrize("explicit_allowlist", [False, True])
def test_hidden_opt_in_cannot_bypass_code_mode_exclusions(tmp_path, kind, explicit_allowlist):
    """Compatibility admission never overrides the subprocess safety boundary."""
    async def scenario():
        agent, _ = make_agent(tmp_path, "code")
        name = (
            next(item for item in sorted(CODE_MODE_DIRECT_TOOLS) if item != "run_code")
            if kind == "direct" else "private_request_local"
        )
        calls = []
        agent.tools.register(ToolDef(
            name, "protected", {"type": "object", "properties": {}},
            lambda: calls.append("called"), expose_by_default=False,
            allow_hidden_execution=True,
            result_persistence="request_local" if kind == "request_local" else "durable",
        ))
        if explicit_allowlist:
            agent.tool_allowlist = {"run_code", name}
        result = await execute(agent, "code", name, {})
        assert "ToolDisabled" in (result["error"] or result["output"])
        assert calls == []
    asyncio.run(scenario())
