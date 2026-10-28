"""Deferred tool disclosure (specs/tool-disclosure.md): a conversation's tool list stays fixed, other tools'
definitions arrive as tool_search results, and call_tool runs them as themselves."""

from __future__ import annotations

import asyncio
import json

import pytest

from agent.core.msg import ContentBlock, Msg
from agent.runtime.react import ReActAgent
from agent.runtime.tools.disclosure import catalog
from agent.runtime.tools.registry import ToolDef, ToolRegistry


class ScriptedLLM:
    """Answers each request with the next scripted reply, then finishes."""

    def __init__(self, replies: list[dict]):
        self.replies = list(replies)
        self.calls: list[tuple[list[dict], list[dict]]] = []

    async def chat_stream(self, messages, tools):
        self.calls.append((json.loads(json.dumps(messages)), json.loads(json.dumps(tools))))
        yield self.replies.pop(0) if self.replies else {"type": "done", "content": "done", "usage": None}


def calls(*items: tuple[str, str, dict]) -> dict:
    return {"type": "tool_calls", "content": "", "reasoning_content": "", "usage": None,
            "calls": [{"id": call_id, "name": name, "arguments": json.dumps(args)} for call_id, name, args in items]}


def registry_with(ran: list[tuple[str, dict]], asked: list[dict] | None = None) -> ToolRegistry:
    """read_file is a core tool; browser_open and git_push are deferred; computer_act keeps its arguments local."""
    registry = ToolRegistry()

    def recorder(name: str):
        def run(**args) -> str:
            ran.append((name, args))
            return f"{name} ran"
        return run

    registry.register(ToolDef("read_file", "Read a file.", {"type": "object", "properties": {"path": {"type": "string"}}},
                              recorder("read_file")))
    registry.register(ToolDef("browser_open", "Open a page in the user's browser.",
                              {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
                              recorder("browser_open")))
    registry.register(ToolDef(
        "git_push", "Push the current branch.", {"type": "object", "properties": {"remote": {"type": "string"}}},
        recorder("git_push"), risk="write",
        permission_check=lambda args: {"kind": "push", "scope": f"push:{args.get('remote', '')}", "approval_title": "推送"},
        permission_grant=lambda *_: None))
    registry.register(ToolDef(
        "computer_act", "Act on an app.",
        {"type": "object", "properties": {"text": {"type": "string"}, "app": {"type": "string"}}},
        recorder("computer_act"), argument_persistence="request_local",
        argument_redactor=lambda args: {**args, "text": "[redacted]"}))
    if asked is not None:
        async def decide(request: dict) -> str:
            asked.append(request)
            return "once"
        registry.set_approval_handler(decide)
    return registry


def agent_with(registry: ToolRegistry, llm: ScriptedLLM) -> ReActAgent:
    return ReActAgent("agent", llm, registry, system_prompt="system", max_iterations=6)  # type: ignore[arg-type]


def reply(agent: ReActAgent, text: str = "go") -> list[dict]:
    events: list[dict] = []

    async def collect():
        async for event in agent.reply_stream(Msg(content=[ContentBlock.text(text)], id="turn")):
            events.append(event)

    asyncio.run(collect())
    return events


def names(tools: list[dict]) -> list[str]:
    return [tool["function"]["name"] for tool in tools]


@pytest.fixture
def deferred(monkeypatch):
    monkeypatch.setenv("ASTRA_TOOL_DISCLOSURE", "deferred")


def test_full_disclosure_sends_every_tool_and_no_dispatch_pair():
    llm = ScriptedLLM([])
    reply(agent_with(registry_with([]), llm))
    assert names(llm.calls[0][1]) == ["browser_open", "computer_act", "git_push", "read_file"]


def test_a_loaded_tool_runs_through_call_tool_and_the_tool_list_never_changes(deferred):
    ran: list[tuple[str, dict]] = []
    llm = ScriptedLLM([
        calls(("s1", "tool_search", {"family": "browser"})),
        calls(("c1", "call_tool", {"name": "browser_open", "arguments": {"url": "https://example.com"}})),
    ])
    agent = agent_with(registry_with(ran), llm)
    events = reply(agent)

    sent = [tools for _, tools in llm.calls]
    assert names(sent[0]) == ["call_tool", "read_file", "tool_search"]
    assert all(tools == sent[0] for tools in sent), "the tool list stays byte-identical"
    search = next(tool for tool in sent[0] if tool["function"]["name"] == "tool_search")["function"]
    assert "- browser: " in search["description"] and "- git: " in search["description"]
    assert search["parameters"]["properties"]["family"]["enum"] == ["browser", "computer", "git"]

    # The search result hands over the definition; call_tool then runs the named tool with its own arguments.
    second_messages = llm.calls[1][0]
    assert any(m.get("role") == "tool" and "Loaded tools: browser_open" in str(m.get("content")) for m in second_messages)
    assert ran == [("browser_open", {"url": "https://example.com"})]
    # Everyone after the model sees the real tool; the model's history keeps its own call.
    announced = [call["name"] for event in events if event.get("type") == "tool_calls" for call in event["calls"]]
    assert announced == ["tool_search", "browser_open"]
    result = next(event for event in events if event.get("type") == "tool_result" and event.get("id") == "c1")
    assert result["name"] == "browser_open" and not result["error"]
    history_calls = [call["function"]["name"] for m in agent.context.messages if m.get("role") == "assistant"
                     for call in m.get("tool_calls") or []]
    assert history_calls == ["tool_search", "call_tool"]
    tool_text = next(m["content"] for m in agent.context.messages if m.get("tool_call_id") == "c1")
    assert tool_text.startswith("[Tool result: browser_open | status: success]")


def test_an_unloaded_or_directly_called_tool_does_not_run_and_is_handed_over(deferred):
    ran: list[tuple[str, dict]] = []
    llm = ScriptedLLM([
        calls(("d1", "browser_open", {"url": "https://a.example"})),
        calls(("c1", "call_tool", {"name": "git_push", "arguments": {"remote": "origin"}})),
        calls(("c2", "call_tool", {"name": "browser_open", "arguments": {"url": "https://b.example"}})),
        calls(("c3", "call_tool", {"name": "no_such_tool", "arguments": {}})),
    ])
    agent = agent_with(registry_with(ran, asked=[]), llm)
    events = reply(agent)
    results = {event["id"]: event for event in events if event.get("type") == "tool_result"}

    # A direct call of a deferred tool is refused but hands the definition over, so call_tool may follow.
    assert results["d1"]["code"] == "tool_not_loaded" and "call it with call_tool" in results["d1"]["error"]
    assert '"name":"browser_open"' in results["d1"]["error"]
    # call_tool of a tool never loaded does not run either; its definition comes back in the result.
    assert results["c1"]["code"] == "tool_not_loaded" and "Loaded tools: git_push" in results["c1"]["error"]
    # Loaded by the direct call's refusal, browser_open now runs.
    assert not results["c2"]["error"]
    assert results["c3"]["code"] == "tool_dispatch"
    assert ran == [("browser_open", {"url": "https://b.example"})]
    # A refused dispatch is not a failure of the tool: nothing is blocked for the turn.
    assert not any(event.get("code") in {"tool_circuit", "repeated_tool_call"} for event in events)


def test_approvals_and_argument_privacy_follow_the_named_tool(deferred):
    ran: list[tuple[str, dict]] = []
    asked: list[dict] = []
    llm = ScriptedLLM([
        calls(("s1", "tool_search", {"names": ["git_push", "computer_act"]})),
        calls(("c1", "call_tool", {"name": "git_push", "arguments": {"remote": "origin"}}),
              ("c2", "call_tool", {"name": "computer_act", "arguments": {"app": "Notes", "text": "secret words"}})),
    ])
    agent = agent_with(registry_with(ran, asked), llm)
    reply(agent)

    assert [request["tool_name"] for request in asked] == ["git_push"]
    assert ("computer_act", {"app": "Notes", "text": "secret words"}) in ran
    # computer_act keeps its arguments request-local; wrapped in call_tool, the history still redacts them.
    stored = next(call["function"]["arguments"] for m in agent.context.messages if m.get("role") == "assistant"
                  for call in m.get("tool_calls") or [] if call["id"] == "c2")
    assert "secret words" not in stored
    assert json.loads(stored) == {"name": "computer_act", "arguments": {"app": "Notes", "text": "[redacted]"}}
    assert "secret words" not in json.dumps(agent.context.messages, ensure_ascii=False)


def test_compaction_drops_the_loaded_set_with_the_results_that_carried_it(deferred):
    ran: list[tuple[str, dict]] = []
    llm = ScriptedLLM([calls(("s1", "tool_search", {"names": ["browser_open"]}))])
    agent = agent_with(registry_with(ran), llm)
    reply(agent)
    assert agent._loaded_deferred_tools() == {"browser_open"}
    # A summary replaces the old turns; their tool results go, and the tool must be loaded again.
    agent.context.messages = [{"role": "user", "content": "summary of earlier turns"}]
    assert agent._loaded_deferred_tools() == set()


def test_a_conversation_kind_can_keep_more_tools_at_hand(deferred):
    registry = registry_with([])
    registry.add_core_tools({"git_push"})
    llm = ScriptedLLM([])
    reply(agent_with(registry, llm))
    assert names(llm.calls[0][1]) == ["call_tool", "git_push", "read_file", "tool_search"]


def test_the_catalog_names_each_family_once_and_mcp_servers_by_name():
    registry = ToolRegistry()
    for name in ("browser_open", "browser_click", "team_send", "delegate_poll", "mcp__notes__find", "odd_tool"):
        registry.register(ToolDef(name, f"{name} does a thing.", {"type": "object", "properties": {}}, lambda: ""))
    lines, families = catalog(registry._tools.values())
    assert families == ["browser", "subagents", "mcp:notes", "other"]
    assert lines.count("- browser:") == 1
    assert "- mcp:notes: tools of the notes MCP server" in lines
    assert "- other: other tools: odd_tool" in lines
