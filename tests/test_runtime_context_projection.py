import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from agent.core.msg import ContentBlock, Msg
from agent.runtime.context import AgentContext
from agent.runtime.react import ReActAgent
from agent.runtime.runtime_context_projection import RuntimeContextProjection, wrap_turn_context
from agent.runtime.tools.registry import ToolDef, ToolRegistry


def test_changed_snapshot_appends_and_clear_supersedes_without_rewriting_user():
    context = AgentContext(system_prompt="system")
    context.add_user("first question")
    first = context.get_prompt(runtime_context="state v1")
    assert context.get_prompt(runtime_context="state v1") == first
    second = context.get_prompt(runtime_context="state v2")
    assert second[:-1] == first
    context.add_assistant("answer")
    context.add_user("new question")
    cleared = context.get_prompt(runtime_context="")
    assert cleared[:len(second)] == second
    assert "Earlier snapshots no longer apply" in cleared[-1]["content"]
    assert context.get_prompt(runtime_context="") == cleared
    assert [m["content"] for m in context.messages] == ["first question", "answer", "new question"]


def test_projection_restore_and_staged_session_are_detached(tmp_path):
    path = str(tmp_path / "session.json")
    context = AgentContext(system_prompt="system")
    context.set_session(path)
    context.add_user("hello")
    first = context.get_prompt(runtime_context="memory one")
    context.add_assistant("answer")
    context.add_user("second question")
    expected = context.get_prompt(runtime_context="memory two")
    context.save()
    assert expected[:len(first)] == first
    assert "runtime_context_projection" in json.loads((tmp_path / "session.header.json").read_text())
    saved = copy.deepcopy(context.runtime_projection.state)
    staged = context.stage_session(path)
    assert staged.get_prompt() == expected
    assert context.runtime_projection.state == saved
    staged.get_prompt(runtime_context="different")
    assert context.runtime_projection.state == saved
    context.adopt_session(staged)
    context.reset()
    assert context.runtime_projection.state == {}


@pytest.mark.parametrize("rewrite", ["edit", "compression", "tool_repair"])
def test_changed_canonical_history_rebases_to_latest_snapshot(rewrite):
    context = AgentContext(system_prompt="system")
    context.add_user("original")
    context.get_prompt(runtime_context="old state")
    context.add_assistant("answer")
    context.get_prompt(runtime_context="current state")
    if rewrite == "edit":
        context.replace_message_content(0, "edited")
    elif rewrite == "compression":
        context.messages = [{"role": "user", "content": "summary and current task"}]
    else:
        context.messages.insert(0, {"role": "tool", "tool_call_id": "orphan", "content": "cancelled"})
        context.sanitize_tool_history()
        context.messages.pop()
    replay = context.get_prompt()
    assert "old state" not in str(replay)
    assert "current state" in replay[-1]["content"]
    assert len(context.runtime_projection.state["snapshots"]) == 1


def test_never_insert_snapshot_inside_parallel_tool_result_chain():
    projection = RuntimeContextProjection()
    history = [{"role": "user", "content": "question"}]
    projection.project(history, "initial")
    history.extend([
        {"role": "assistant", "content": "", "tool_calls": [{"id": "a"}, {"id": "b"}]},
        {"role": "tool", "tool_call_id": "a", "content": "first result"},
    ])
    inserts = projection.project(history, "new state")
    assert list(inserts) == [1]
    history.append({"role": "tool", "tool_call_id": "b", "content": "second result"})
    inserts = projection.project(history, "new state")
    assert list(inserts) == [1, 4]


@pytest.mark.parametrize("state", [
    None, {"version": 99}, {"version": 1, "snapshots": [None]},
    {"version": 1, "snapshots": [{"after": -1, "prefix": "a" * 64, "content": wrap_turn_context("bad")}]},
    {"version": 1, "snapshots": [{"after": 1, "prefix": "a" * 64, "content": "unmarked saved text"}]},
])
def test_malformed_projection_does_not_revive_saved_text(state):
    projection = RuntimeContextProjection(state)
    assert projection.project([{"role": "user", "content": "hello"}]) == {}


class DecliningSummary:
    """A compressor with nothing it can summarize."""

    async def compress(self, messages, max_prompt_tokens, force=False):
        return messages


@pytest.mark.parametrize("compressor", [None, DecliningSummary()])
def test_budget_retires_superseded_snapshots_before_canonical_history(compressor):
    """Superseded snapshots are kept for the cached prefix. Over the budget, with no summary that could
    shorten the conversation, retiring them is the rewrite that loses nothing of it."""
    context = AgentContext(system_prompt="system")
    context.compressor = compressor
    context.add_user("keep my complete task")
    context.get_prompt(runtime_context="old " * 4000)
    context.add_assistant("keep my answer")
    context.get_prompt(runtime_context="current " * 4000)
    before = copy.deepcopy(context.messages)
    size = context.estimate_compaction_tokens()
    assert context.prompt_token_breakdown()["runtime_context"] > 1000
    context.max_prompt_tokens = size - 1
    asyncio.run(context.compress_if_needed())
    assert context.messages == before
    assert context.estimate_compaction_tokens() < context.max_prompt_tokens * .9
    assert context._last_compaction_report["method"] == "cleanup"
    assert "old old" not in str(context.get_prompt())
    assert "current current" in str(context.get_prompt())


def test_runtime_and_system_updates_replay_together_after_restore(tmp_path):
    context = AgentContext(system_prompt="system v1")
    path = str(tmp_path / "combined.json")
    context.set_session(path)
    context.add_user("question")
    first = context.system_projection.project(context.get_prompt(runtime_context="state one"), "series")
    context.add_assistant("answer")
    context.set_system_prompt("system v2")
    second = context.system_projection.project(context.get_prompt(runtime_context="state two"), "series")
    assert second[:len(first)] == first
    context.save()
    restored = context.stage_session(path)
    assert restored.system_projection.project(restored.get_prompt(), "series") == second
    restored.add_user("next question")
    third = restored.system_projection.project(restored.get_prompt(runtime_context="state three"), "series")
    assert third[:len(second)] == second


def test_snapshot_count_is_bounded_without_losing_current_authority(monkeypatch):
    import agent.runtime.runtime_context_projection as module

    monkeypatch.setattr(module, "_MAX_SNAPSHOTS", 3)
    context = AgentContext(system_prompt="system")
    context.add_user("question")
    for version in range(6):
        prompt = context.get_prompt(runtime_context=f"state {version}")
        assert len(context.runtime_projection.state["snapshots"]) <= 3
        assert f"state {version}" in prompt[-1]["content"]


def test_skill_activation_updates_restore_and_clear_keep_sent_provider_prefix(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    class Skills:
        text = "Skill version one"

        def list(self):
            return [{"name": "review"}]

        def catalog_prompt(self):
            return "Available skill: review"

        def view(self, name):
            return self.text

    class LLM:
        def __init__(self):
            self.requests = []

        async def chat_stream(self, messages, tools):
            self.requests.append(copy.deepcopy(messages))
            if len(self.requests) == 1:
                yield {"type": "tool_calls", "calls": [{
                    "id": "skill", "name": "skill_view", "arguments": '{"name":"review"}',
                }], "content": "", "reasoning_content": "", "usage": None}
            else:
                yield {"type": "done", "content": "done", "usage": None}

    skills, llm, tools = Skills(), LLM(), ToolRegistry()
    tools.register(ToolDef("skill_view", "Read skill", {
        "type": "object", "properties": {"name": {"type": "string"}},
    }, skills.view))
    agent = ReActAgent("agent", llm, tools, system_prompt="system", skill_store=skills)
    path = str(tmp_path / "session.json")
    agent.context.set_session(path)
    asyncio.run(agent.reply(Msg(content=[ContentBlock.text("review this")], id="first")))
    first, activated = llm.requests
    assert activated[:len(first)] == first
    assert "Skill version one" in activated[-1]["content"]
    skills.text = "Skill version two"
    asyncio.run(agent.reply(Msg(content=[ContentBlock.text("continue")], id="second")))
    updated = llm.requests[-1]
    assert updated[:len(activated)] == activated
    assert "Skill version two" in updated[-1]["content"]
    agent.context.save()

    restored = ReActAgent("agent", llm, tools, system_prompt="system", skill_store=skills)
    restored.context.set_session(path)
    assert restored.context.load()
    asyncio.run(restored.reply(Msg(content=[ContentBlock.text("unrelated new task")], id="third")))
    assert llm.requests[-1][:len(updated)] == updated
    assert "Earlier snapshots no longer apply" in llm.requests[-1][-1]["content"]
    assert not any("Active skill contract" in str(m["content"]) for m in restored.context.messages if m["role"] == "user")


def test_ephemeral_tool_images_and_transient_steering_never_enter_projection(tmp_path):
    agent = ReActAgent("agent", SimpleNamespace(), ToolRegistry(), system_prompt="system")
    agent.context.set_session(str(tmp_path / "session.json"))
    agent.context.add_user("hello")
    agent.context.messages.extend([
        {"role": "assistant", "tool_calls": [{"id": "one", "type": "function", "function": {"name": "observe", "arguments": "{}"}}], "content": ""},
        {"role": "tool", "tool_call_id": "one", "content": "durable redacted result"},
    ])
    agent.runtime_turn_context_provider = lambda: "runtime state"
    agent._fresh_tool_context["one"] = "SECRET_FRESH_OBSERVATION"
    prompt, _ = asyncio.run(agent._prepare_prompt_for_llm("hello", None, transient_messages=[
        {"role": "system", "content": "SECRET_STEERING"},
        {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "SECRET_IMAGE"}}]},
    ]))
    assert all(word in str(prompt) for word in ("SECRET_FRESH", "SECRET_STEERING", "SECRET_IMAGE"))
    agent.context.save()
    assert "SECRET" not in json.dumps(agent.context._session_store.load())


@pytest.mark.parametrize("mode", ["restricted", "minimal"])
def test_restricted_interaction_does_not_replay_or_mutate_work_snapshots(mode):
    agent = ReActAgent("agent", SimpleNamespace(), ToolRegistry(), system_prompt="system")
    agent.context.add_user("hello")
    agent.context.get_prompt(runtime_context="WORK_ONLY_MEMORY")
    before = copy.deepcopy(agent.context.runtime_projection.state)
    if mode == "restricted":
        agent.tool_allowlist = set()
        agent.runtime_turn_context_provider = lambda: "BAR_STATE"
    else:
        agent.minimal_mode = True
    prompt, _ = asyncio.run(agent._prepare_prompt_for_llm("hello", None))
    assert "WORK_ONLY_MEMORY" not in str(prompt)
    if mode == "restricted":
        assert "BAR_STATE" in prompt[-1]["content"]
    assert agent.context.runtime_projection.state == before
