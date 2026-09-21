"""Desktop delegate status observes, but never controls, worker execution."""

import asyncio
import json

import pytest

from agent.runtime.tools import delegate
from agent.runtime.tools.delegate import register_delegate_tools
from agent.runtime.tools.processes import ProcessManager
from agent.runtime.tools.registry import ToolDef, ToolRegistry
from agent.sandbox.local import LocalSandbox


@pytest.fixture
def manager(monkeypatch, tmp_path):
    manager = ProcessManager(artifact_dir=tmp_path / "processes")
    monkeypatch.setattr(delegate, "_sub_processes", manager)
    return manager


class SequenceLLM:
    def __init__(self, responses):
        self.responses = list(responses)

    async def chat(self, **kwargs):
        return self.responses.pop(0)


def test_sync_delegate_has_independent_progress_and_terminal_report(manager):
    async def scenario():
        events, legacy, persisted = [], [], []
        registry = ToolRegistry()
        registry.register(ToolDef("read_file", "read", {"type": "object", "properties": {}}, lambda **args: "observation", group="files"))
        llm = SequenceLLM([
            {"content": "", "reasoning_content": "PRIVATE REASONING", "tool_calls": [
                {"id": "one", "name": "read_file", "arguments": '{"secret":"PRIVATE ARGUMENT"}'}]},
            {"content": "Final report", "tool_calls": []},
        ])
        register_delegate_tools(registry, llm_getter=lambda: llm,
            session_id_getter=lambda: "session", on_delegate_event=events.append,
            on_session_event=lambda session, event: persisted.append(event))
        result = await registry.execute("delegate_task", {"goal": "inspect", "timeout": 5},
            task_id="parent", on_progress=lambda event, **kwargs: legacy.append(event))
        assert not result["error"]
        assert events[0]["status"] == "queued"
        assert events[-1]["status"] == "completed" and events[-1]["result"] == "Final report"
        assert {e["process_id"] for e in events} == {events[0]["process_id"]}
        assert all(e["task_id"] == "parent" and e["session_id"] == "session" for e in events)
        assert any(e["current_tool"] == "read_file" for e in events)
        assert events[-1]["current_tool"] == "" and events[-1]["turns_used"] == 2
        assert events[-1]["duration_ms"] >= 0 and events[-1]["completed_at"] >= events[0]["started_at"]
        assert "PRIVATE" not in json.dumps(events)
        assert any(e["stage"] == "tool_call" for e in legacy)
        assert [e["status"] for e in persisted if e["type"] == "delegate_status"] == ["queued", "running", "completed"]
        assert manager.list() == [], "foreground UI status must not expose a process"
    asyncio.run(scenario())


def test_batch_children_keep_queued_and_running_identities_separate(manager, monkeypatch):
    monkeypatch.setenv("ASTRA_DELEGATE_CONCURRENCY", "1")
    async def scenario():
        release, entered = asyncio.Event(), asyncio.Event()
        events = []
        class LLM:
            async def chat(self, **kwargs):
                entered.set()
                await release.wait()
                return {"content": "done", "tool_calls": []}
        registry = ToolRegistry()
        register_delegate_tools(registry, llm_getter=LLM, on_delegate_event=events.append)
        pending = asyncio.create_task(registry.execute("delegate_task", {"tasks": [
            {"goal": "one"}, {"goal": "two"}, {"goal": "three"}]}))
        await entered.wait()
        for _ in range(3):
            await asyncio.sleep(0)
        latest = {e["process_id"]: e for e in events}
        assert len(latest) == 3
        assert sorted(e["status"] for e in latest.values()) == ["queued", "queued", "running"]
        release.set()
        result = await pending
        assert not result["error"]
        terminal = [e for e in events if e["status"] == "completed"]
        assert {e["goal"] for e in terminal} == {"one", "two", "three"}
        assert len({e["process_id"] for e in terminal}) == 3
    asyncio.run(scenario())


def test_startup_failure_and_throwing_display_callback_do_not_change_outcome(manager):
    async def scenario():
        observed = []
        def broken(event):
            observed.append(event)
            raise RuntimeError("display unavailable")
        registry = ToolRegistry()
        register_delegate_tools(registry, llm_getter=lambda: None, on_delegate_event=broken)
        result = await registry.execute("delegate_task", {"goal": "unconfigured"})
        assert json.loads(result["output"])["worker_status"] == "failed"
        assert observed[0]["status"] == "queued"
        assert observed[-1]["status"] == "failed"
        assert observed[-1]["error"] == "LLM client unavailable"
    asyncio.run(scenario())


def test_preprocess_failure_keeps_unique_display_record_without_fake_process(manager, monkeypatch, tmp_path):
    def fail(*args):
        raise RuntimeError("worktree setup unavailable")
    monkeypatch.setattr(delegate, "_create_detached_worktree", fail)
    async def scenario():
        events = []
        registry = ToolRegistry()
        register_delegate_tools(registry, sandbox=LocalSandbox(workdir=str(tmp_path)), on_delegate_event=events.append)
        result = await registry.execute("delegate_task", {"goal": "setup", "mode": "worker", "isolation": "worktree"})
        assert "worktree setup unavailable" in result["error"]
        assert [e["status"] for e in events] == ["queued", "failed"]
        assert events[0]["process_id"] == events[1]["process_id"]
        assert manager.list() == []
    asyncio.run(scenario())


@pytest.mark.parametrize("start_first", [False, True])
def test_background_cancel_including_before_first_instruction_is_terminal(manager, start_first):
    async def scenario():
        entered = asyncio.Event()
        class LLM:
            async def chat(self, **kwargs):
                entered.set()
                await asyncio.Future()
        events = []
        registry = ToolRegistry()
        register_delegate_tools(registry, llm_getter=LLM, on_delegate_event=events.append)
        # No await between dispatch and cancellation, except the dispatch itself.
        result = await registry.execute("delegate_task", {"goal": "cancel", "background": True})
        process = manager.get(json.loads(result["output"])["process_id"])
        if start_first:
            await entered.wait()
        else:
            assert not entered.is_set()
        await manager.cancel(process)
        await asyncio.sleep(0)
        assert events[0]["status"] == "queued"
        assert events[-1]["status"] == "cancelled"
        assert len([e for e in events if e["status"] == "cancelled"]) == 1
    asyncio.run(scenario())
