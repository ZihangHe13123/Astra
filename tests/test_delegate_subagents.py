"""Every subagent of a conversation has a name and stays: more work goes on from its saved conversation.

The Agent Team tools are merged into delegate_*: a conversation's subagents form one crew (a Team kept
out of sight), subagents given the same group may write to each other, and the old Team tools only
serve conversations whose tool list already has them.
"""

import asyncio
import copy
import json
import re
import subprocess

from agent.runtime.agent_team import MESSAGE_ENVELOPE_START
from agent.runtime.session_store import SessionStore
from agent.runtime.task_store import TaskStore
from agent.runtime.tools import delegate
from agent.runtime.tools.delegate import register_delegate_tools
from agent.runtime.tools.files import register_file_tools
from agent.runtime.tools.processes import ProcessManager
from agent.runtime.tools.registry import ToolDef, ToolRegistry
from agent.sandbox.local import LocalSandbox
from test_delegate_conversation_resume import Harness, RecordingLLM


def _envelopes(request):
    return [m["content"] for m in request if str(m.get("content") or "").startswith(MESSAGE_ENVELOPE_START)]


def _next_turn(harness):
    """The parent's turn ends and the next one starts in the same conversation."""
    harness.tasks.finish_run(harness.parent, "completed")
    harness.parent = harness.tasks.start_run("request-b", "next turn", session_id="session-a")["id"]


async def _failed(harness, tool_name, **args):
    result = await harness.registry.execute(tool_name, args, task_id=harness.parent)
    assert result["error"], result
    return result["error"]


def test_a_subagent_keeps_its_name_and_goes_on_from_its_conversation_in_a_later_turn(tmp_path, monkeypatch):
    async def scenario():
        llm = RecordingLLM()
        harness = Harness(tmp_path, monkeypatch, llm)
        first = await harness.call("delegate_task", goal="Read the parser", name="reader", timeout=30)
        assert first["subagent"] == "reader" and first["result"] == "Report 1"
        listed = await harness.call("delegate_list")
        assert [(item["name"], item["status"]) for item in listed["subagents"]] == [("reader", "idle")]

        _next_turn(harness)
        sent = await harness.call("delegate_send", to="reader", message="Now read its tests")
        assert sent["status"] == "resumed" and sent["to"] == "reader"
        await harness.wait(sent["process_id"])

        earlier, later = llm.requests
        # The same conversation goes on: the earlier request is the start of the later one.
        assert later[:len(earlier)] == earlier
        assert later[len(earlier)] == {"role": "assistant", "content": "Report 1"}
        (message,) = _envelopes(later)
        assert "Now read its tests" in message and "From: parent" in message
        assert "You are the subagent reader" in later[0]["content"]
        waited = await harness.registry.execute("delegate_wait", {"timeout_ms": 5000}, task_id=harness.parent)
        assert "Report 2" in waited["output"] and "reader: idle" in waited["output"]

    asyncio.run(scenario())


def test_names_are_unique_in_a_conversation_and_given_when_left_out(tmp_path, monkeypatch):
    async def scenario():
        harness = Harness(tmp_path, monkeypatch, RecordingLLM())
        batch = await harness.call("delegate_task", tasks=[{"goal": "one"}, {"goal": "two"}], timeout=30)
        assert sorted(item["subagent"] for item in batch["results"]) == ["explorer-1", "explorer-2"]
        refused = await _failed(harness, "delegate_task", goal="three", name="explorer-1")
        assert 'delegate_send(to="explorer-1")' in refused
        assert "parent" in await _failed(harness, "delegate_task", goal="four", name="parent")
        assert "own name" in await _failed(
            harness, "delegate_task", tasks=[{"goal": "a", "name": "same"}, {"goal": "b", "name": "same"}],
        )
        worker = await harness.call("delegate_task", goal="five", mode="worker", timeout=30)
        assert worker["subagent"] == "worker-1"
        names = [item["name"] for item in (await harness.call("delegate_list"))["subagents"]]
        assert sorted(names) == ["explorer-1", "explorer-2", "worker-1"]

    asyncio.run(scenario())


class GroupLLM:
    """Three subagents: a writes to b (its group) and to its parent; c, outside the group, tries to."""

    def __init__(self):
        self.requests: dict[str, list] = {}
        self.a_sent = asyncio.Event()

    async def chat(self, **kwargs):
        messages = copy.deepcopy(kwargs["messages"])
        name = re.search(r"You are the subagent (\S+) \(", messages[0]["content"]).group(1)
        calls = self.requests.setdefault(name, [])
        calls.append(messages)
        tool = lambda *items: {"content": "", "tool_calls": [  # noqa: E731
            {"id": f"{name}-{index}", "name": tool_name, "arguments": json.dumps(args)}
            for index, (tool_name, args) in enumerate(items)]}
        if name == "a" and len(calls) == 1:
            return tool(("delegate_send", {"to": "b", "message": "The interface is parse(text)."}),
                        ("delegate_send", {"to": "parent", "message": "FYI: b knows the interface."}))
        if name == "a":
            self.a_sent.set()
        if name == "b" and len(calls) == 1:
            await asyncio.wait_for(self.a_sent.wait(), 5)
            return tool(("current_time", {}))
        if name == "c" and len(calls) == 1:
            return tool(("delegate_send", {"to": "b", "message": "Let me in."}))
        return {"content": f"{name} done", "tool_calls": []}


def test_subagents_of_one_group_write_to_each_other_and_others_only_to_their_parent(tmp_path, monkeypatch):
    async def scenario():
        llm = GroupLLM()
        harness = Harness(tmp_path, monkeypatch, llm)
        harness.registry.register(ToolDef("current_time", "Now", {"type": "object", "properties": {}},
                                          lambda: "noon", risk="read", approval="never"))
        started = await harness.call("delegate_task", background=True, timeout=30, tasks=[
            {"goal": "Implement the parser", "name": "b", "group": "parser"},
            {"goal": "Design the interface", "name": "a", "group": "parser"},
            {"goal": "Write the docs", "name": "c"},
        ])
        for item in started["results"]:
            await harness.wait(item["process_id"])

        (to_b,) = _envelopes(llm.requests["b"][1])
        assert "parse(text)" in to_b and "From: a" in to_b
        refused = [m["content"] for m in llm.requests["c"][1] if m["role"] == "tool"]
        assert "not in your group" in refused[0] and "Let me in" not in json.dumps(llm.requests["b"])
        assert "You work in the group parser" in llm.requests["a"][0][0]["content"]
        assert "group" not in llm.requests["c"][0][0]["content"].split("## Your parent", 1)[1]
        waited = await harness.registry.execute("delegate_wait", {"timeout_ms": 5000}, task_id=harness.parent)
        assert "FYI: b knows the interface." in waited["output"] and "From: a" in waited["output"]

    asyncio.run(scenario())


def test_a_subagent_stopped_mid_write_comes_back_only_read_only_and_a_closed_one_not_at_all(tmp_path, monkeypatch):
    async def scenario():
        started = asyncio.Event()
        llm = RecordingLLM([{"content": "", "tool_calls": [{"id": "write-1", "name": "write_file", "arguments": "{}"}]}])
        harness = Harness(tmp_path, monkeypatch, llm)

        async def write_file():
            started.set()
            await asyncio.Event().wait()

        harness.registry.register(ToolDef("write_file", "Write", {"type": "object", "properties": {}}, write_file,
                                          group="files", risk="write", approval="never"))
        spawned = await harness.call("delegate_task", goal="Write it", name="writer", mode="worker",
                                     background=True, timeout=30)
        await asyncio.wait_for(started.wait(), 5)
        stopped = await harness.call("delegate_cancel", process_id="writer")
        assert stopped["subagent"] == "writer" and not stopped["closed"]

        refused = await _failed(harness, "delegate_send", to="writer", message="carry on")
        assert "unknown" in refused and "recover=true" in refused
        recovered = await harness.call("delegate_send", to="writer", message="Check what was written", recover=True)
        assert recovered["status"] == "recovered" and recovered["read_only"] is True
        await harness.wait(recovered["process_id"])
        assert spawned["process_id"] != recovered["process_id"]

        closed = await harness.call("delegate_cancel", process_id="writer", close=True)
        assert closed["closed"] is True
        assert [item["status"] for item in (await harness.call("delegate_list"))["subagents"]] == ["closed"]
        assert "closed" in await _failed(harness, "delegate_send", to="writer", message="again")

    asyncio.run(scenario())


def test_the_merged_tools_are_offered_and_the_team_tools_only_run_for_conversations_that_have_them(tmp_path, monkeypatch):
    async def scenario():
        harness = Harness(tmp_path, monkeypatch, RecordingLLM())
        offered = {tool["function"]["name"] for tool in harness.registry.to_openai_tools()}
        assert {"delegate_task", "delegate_send", "delegate_wait", "delegate_list", "delegate_read",
                "delegate_cancel"} <= offered
        assert not offered & {"team", "team_spawn", "team_send", "team_wait", "team_inbox", "team_task",
                              "team_restart", "delegate_poll"}
        # A restored conversation whose tool list has them still runs them.
        team = await harness.call("team", action="create", name="earlier", goal="kept")
        assert team["id"]

    asyncio.run(scenario())


class WorktreeLLM:
    """A worker that adds one file per assignment through apply_patch."""

    def __init__(self):
        self.requests = []

    async def chat(self, **kwargs):
        self.requests.append(copy.deepcopy(kwargs["messages"]))
        tools = [m for m in kwargs["messages"] if m["role"] == "tool"]
        reports = sum(1 for m in kwargs["messages"] if m["role"] == "assistant" and not m.get("tool_calls"))
        assignment = reports + 1
        if len(tools) == reports:
            patch = f"*** Begin Patch\n*** Add File: added-{assignment}.txt\n+{assignment}\n*** End Patch"
            return {"content": "", "tool_calls": [
                {"id": f"patch-{assignment}", "name": "apply_patch", "arguments": json.dumps({"patch": patch})}]}
        return {"content": f"added {assignment}", "tool_calls": []}


def test_a_worker_in_a_worktree_goes_on_in_the_same_worktree(tmp_path, monkeypatch):
    def git(*args):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)

    async def scenario():
        git("init", "-q")
        (root / "main.txt").write_text("main\n")
        git("add", "main.txt")
        git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "initial")
        registry = ToolRegistry()
        registry.yolo = True
        register_file_tools(registry, workdir=str(root))
        monkeypatch.setattr(delegate, "_sub_processes", ProcessManager(artifact_dir=tmp_path / "processes"))
        tasks = TaskStore(tmp_path / "tasks.db")
        parent = {"id": tasks.start_run("a", "first", session_id="session")["id"]}
        llm = WorktreeLLM()
        session = SessionStore(tmp_path / "session.json")
        register_delegate_tools(
            registry, llm_getter=lambda: llm, task_store=tasks, session_id_getter=lambda: "session",
            sandbox=LocalSandbox(workdir=str(root)),
            conv_store_for=lambda _sid, key: SessionStore.for_subagent(session.legacy_path, key),
        )

        async def call(tool_name, **args):
            result = await registry.execute(tool_name, args, task_id=parent["id"])
            assert not result["error"], result
            return json.loads(result["output"])

        first = await call("delegate_task", goal="Add a file", name="builder", mode="worker",
                           isolation="worktree", timeout=30)
        listed = (await call("delegate_list"))["subagents"][0]
        tree = root / ".astra" / "delegate-worktrees" / listed["agent_id"]
        assert first["worktree_retained"] is True and first["worktree_path"] == str(tree)
        assert (tree / "added-1.txt").read_text() == "1\n" and not (root / "added-1.txt").exists()

        tasks.finish_run(parent["id"], "completed")
        parent["id"] = tasks.start_run("b", "second", session_id="session")["id"]
        sent = await call("delegate_send", to="builder", message="Add another file")
        assert sent["status"] == "resumed"
        process = delegate._sub_processes.get(sent["process_id"])
        await asyncio.wait_for(asyncio.shield(process.task), 10)
        assert (tree / "added-1.txt").exists() and (tree / "added-2.txt").read_text() == "2\n"
        assert not (root / "added-2.txt").exists()
        earlier = llm.requests[2]
        assert llm.requests[3][:len(earlier)] == earlier

    root = tmp_path / "repo"
    root.mkdir()
    asyncio.run(scenario())
