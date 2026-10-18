import asyncio
from types import SimpleNamespace

import pytest

from agent.cli.turn_budget import execute_budget_command
from agent.core.msg import ContentBlock, Msg
from agent.runtime.llm import LLMOverallTimeout, _RequestBudget, _RequestCapTimeout
from agent.runtime.task_resume import budget_resume_candidate
from agent.runtime.task_store import TaskStore
from agent.runtime.tools.registry import ToolDef, ToolRegistry
from agent.runtime.turn_budget import (
    TurnBudgetExceeded, budgeted_events, current_turn_budget, parse_turn_budget,
)
from test_search_responsiveness import make_agent, call


def test_one_deadline_covers_all_requests_and_retries_and_closes_the_source():
    async def scenario():
        deadlines = []
        closed = asyncio.Event()

        async def stream():
            try:
                while True:
                    request = _RequestBudget.from_policy(10, 5)
                    deadlines.extend([request.deadline, request.fork_attempts(2).deadline])
                    await request.run(lambda: asyncio.sleep(0.02))
                    yield {"type": "reasoning", "content": "still working"}
            finally:
                closed.set()

        with pytest.raises(TurnBudgetExceeded):
            async for _ in budgeted_events(stream(), 0.09):
                pass
        assert len(deadlines) >= 4
        assert len(set(deadlines)) == 1
        assert closed.is_set()
        assert current_turn_budget() is None
    asyncio.run(scenario())


@pytest.mark.parametrize(("request_seconds", "cap", "error"), [
    (0, None, TurnBudgetExceeded),
    (120, None, TurnBudgetExceeded),
    (1, None, LLMOverallTimeout),
    (0, 0.01, _RequestCapTimeout),
])
def test_early_timer_expiry_is_attributed_to_its_limiting_deadline(monkeypatch, request_seconds, cap, error):
    from agent.runtime import turn_budget
    wait = asyncio.wait
    races = []

    async def early(aws, timeout=None, **kwargs):
        # The request races its deadline with asyncio.wait. Report that race as timed out
        # before the monotonic deadline, reproducing a coarse clock tick without depending
        # on host timing; later waits (stopping the request) run normally.
        races.append(timeout)
        if len(races) == 1:
            return set(), set(aws)
        return await wait(aws, timeout=timeout, **kwargs)

    async def scenario():
        budget = turn_budget.TurnBudget(60)
        token = turn_budget._CURRENT.set(budget)
        try:
            request = _RequestBudget.from_policy(request_seconds, 0)
            monkeypatch.setattr(asyncio, "wait", early)
            with pytest.raises(error):
                await request.run(lambda: asyncio.Event().wait(), cap=cap)
            assert request.remaining() > 0
        finally:
            turn_budget._CURRENT.reset(token)
    asyncio.run(scenario())


def test_turn_budget_does_not_cancel_the_consumers_task_while_it_handles_an_event():
    async def scenario():
        closed = asyncio.Event()
        async def stream():
            try:
                yield {"type": "reasoning", "content": "first"}
                await asyncio.Event().wait()
            finally:
                closed.set()
        output = budgeted_events(stream(), 0.03)
        assert (await anext(output))["content"] == "first"
        await asyncio.sleep(0.06)
        assert closed.is_set()
        with pytest.raises(TurnBudgetExceeded):
            await anext(output)
        assert asyncio.current_task().cancelling() == 0
    asyncio.run(scenario())


def test_completed_turn_disarms_the_deadline_during_bookkeeping():
    async def scenario():
        async def stream():
            yield {"type": "done"}
            await asyncio.sleep(0.05)
            assert current_turn_budget() is None
        assert [e async for e in budgeted_events(stream(), 0.02)] == [{"type": "done"}]
    asyncio.run(scenario())


def test_fast_producer_delivers_every_event_including_its_terminal_event():
    async def scenario():
        expected = [{"type": "chunk", "content": str(i)} for i in range(200)] + [{"type": "done"}]
        async def stream():
            for event in expected:
                yield event
        assert [e async for e in budgeted_events(stream(), 5)] == expected
    asyncio.run(scenario())


def test_disabled_budget_and_stream_closure_keep_existing_cancellation_behavior():
    async def scenario():
        for seconds in [0, 10]:
            closed = asyncio.Event()
            async def stream(closed=closed):
                try:
                    yield {"type": "chunk", "content": "first"}
                    await asyncio.Event().wait()
                finally:
                    closed.set()
            source = stream()
            output = budgeted_events(source, seconds)
            await anext(output)
            await output.aclose()
            await source.aclose()
            assert closed.is_set()
        assert current_turn_budget() is None
    asyncio.run(scenario())


def test_budget_state_is_isolated_between_simultaneous_turns():
    async def scenario():
        async def run(seconds):
            async def stream():
                budget = current_turn_budget()
                await asyncio.sleep(0.01)
                assert current_turn_budget() is budget
                yield {"type": "done", "seconds": budget.seconds}
            return [e async for e in budgeted_events(stream(), seconds)]
        first, second = await asyncio.gather(run(1), run(2))
        assert first[0]["seconds"] == 1 and second[0]["seconds"] == 2
    asyncio.run(scenario())


class WaitingLLM:
    def __init__(self, tool=None):
        self.tool = tool
        self.calls = 0

    async def chat_stream(self, messages, **kwargs):
        self.calls += 1
        if self.tool and self.calls == 1:
            yield {"type": "tool_calls", "calls": [call(self.tool, "one")], "content": ""}
        else:
            yield {"type": "reasoning", "content": "working"}
            await asyncio.Event().wait()


@pytest.mark.parametrize("tool", [False, True])
def test_expiry_ends_real_agent_once_and_preserves_uncertain_tool_outcomes(tmp_path, tool):
    async def scenario():
        store = TaskStore(tmp_path / "tasks.db")
        task = store.start_run("request", "bounded task", session_id="session", model="test")
        registry = ToolRegistry(artifact_dir=tmp_path / "results")
        writes = []
        async def mutation():
            writes.append("dispatched")
            await asyncio.Event().wait()
        registry.register(ToolDef("mutation", "mutation", {"type": "object"}, mutation, risk="write"))
        # Include real persistence and tool dispatch before the intended stall;
        # this is an outcome test, not an 80 ms startup performance benchmark.
        agent = make_agent(registry, WaitingLLM("mutation" if tool else None), task_store=store, turn_timeout_seconds=2)
        agent.context.set_session(str(tmp_path / "session.json"))
        events = [e async for e in agent.reply_stream(Msg(content=[ContentBlock.text("go")], metadata={"task_id": task["id"]}))]
        assert sum(e.get("code") == "turn_budget_exhausted" for e in events) == 1
        assert sum(e["type"] == "done" for e in events) == 1
        task = store.get_task(task["id"])
        assert task["checkpoint"]["resume_kind"] == "time_budget"
        if tool:
            assert writes == ["dispatched"]
            assert next(s for s in task["steps"] if s["kind"] == "tool")["status"] == "unknown"
        store.finish_run(task["id"], "interrupted")
        assert budget_resume_candidate(store, "session", "继续")["id"] == task["id"]
    asyncio.run(scenario())


def test_approval_arriving_in_the_reserve_cannot_start_a_new_mutation(tmp_path):
    async def scenario():
        registry = ToolRegistry(artifact_dir=tmp_path)
        writes = []
        async def approve(_):
            budget = current_turn_budget()
            # Move into the reserve deterministically before returning approval.
            budget.started -= budget.seconds
            return "once"
        registry.set_approval_handler(approve)
        registry.register(ToolDef("mutation", "mutation", {"type": "object"}, lambda: writes.append("write"), risk="write", approval="always"))
        agent = make_agent(registry, WaitingLLM("mutation"), turn_timeout_seconds=10)
        events = [e async for e in agent.reply_stream(Msg(content=[ContentBlock.text("go")]))]
        assert not writes
        assert any(e.get("code") == "turn_budget_exhausted" for e in events)
    asyncio.run(scenario())


def test_an_admitted_atomic_tool_can_finish_readback_in_the_reserve(tmp_path):
    async def scenario():
        registry = ToolRegistry(artifact_dir=tmp_path)
        state = []
        async def mutation():
            state.append("written")
            budget = current_turn_budget()
            budget.started -= budget.seconds
            await asyncio.sleep(0)
            state.append("verified")
            return "verified"
        registry.register(ToolDef("mutation", "mutation", {"type": "object"}, mutation, risk="write"))
        llm = WaitingLLM("mutation")
        agent = make_agent(registry, llm, turn_timeout_seconds=10)
        events = [e async for e in agent.reply_stream(Msg(content=[ContentBlock.text("go")]))]
        assert state == ["written", "verified"]
        assert llm.calls == 1
        assert any(e["type"] == "tool_result" and e.get("output") == "verified" for e in events)
    asyncio.run(scenario())


@pytest.mark.parametrize("value", [True, None, "nan", "inf", -1, 86401, "60 extra"])
def test_invalid_budget_is_rejected_without_changing_the_existing_setting(value):
    with pytest.raises(ValueError):
        parse_turn_budget(value)
    agent = SimpleNamespace(turn_timeout_seconds=30)
    output, error = execute_budget_command(agent, str(value))
    assert not output and error
    assert agent.turn_timeout_seconds == 30


def test_budget_command_sets_queries_and_disables_the_setting():
    agent = SimpleNamespace(turn_timeout_seconds=0)
    assert not execute_budget_command(agent, "60")[1]
    assert agent.turn_timeout_seconds == 60
    assert "approval/question waits" in execute_budget_command(agent, "")[0]
    assert not execute_budget_command(agent, "off")[1]
    assert agent.turn_timeout_seconds == 0


@pytest.mark.parametrize("streaming", [False, True])
def test_preparation_is_bounded_before_the_first_provider_call(tmp_path, streaming):
    async def scenario():
        store = TaskStore(tmp_path / "tasks.db")
        task = store.start_run("prep", "prepare task", session_id="session")
        store.checkpoint(task["id"], {"iteration": 4, "last_known_result": "preserve me"})
        llm = WaitingLLM()
        agent = make_agent(ToolRegistry(artifact_dir=tmp_path), llm, task_store=store, turn_timeout_seconds=0.04)
        async def prepare(_):
            await asyncio.Event().wait()
        agent._prepare_turn = prepare
        msg = Msg(content=[ContentBlock.text("go")], metadata={"task_id": task["id"]})
        if streaming:
            events = [e async for e in agent.reply_stream(msg)]
            assert sum(e.get("code") == "turn_budget_exhausted" for e in events) == 1
        else:
            with pytest.raises(TurnBudgetExceeded):
                await agent.reply(msg)
        assert llm.calls == 0
        checkpoint = store.get_task(task["id"])["checkpoint"]
        assert checkpoint["resume_kind"] == "time_budget"
        assert checkpoint["last_known_result"] == "preserve me"
    asyncio.run(scenario())


def test_real_elapsed_reserve_allows_only_the_admitted_tools_readback(tmp_path):
    async def scenario():
        registry = ToolRegistry(artifact_dir=tmp_path)
        observations = []
        async def mutation():
            import time
            budget = current_turn_budget()
            await asyncio.sleep(max(0, budget.work_deadline - time.monotonic()) + 0.01)
            observations.append(budget.work_deadline < time.monotonic() < budget.deadline)
            return "readback finished"
        registry.register(ToolDef("mutation", "mutation", {"type": "object"}, mutation, risk="write"))
        llm = WaitingLLM("mutation")
        agent = make_agent(registry, llm, turn_timeout_seconds=1)
        events = [e async for e in agent.reply_stream(Msg(content=[ContentBlock.text("go")]))]
        assert observations == [True]
        assert llm.calls == 1
        assert any(e.get("output") == "readback finished" for e in events)
        assert any(e.get("code") == "turn_budget_exhausted" for e in events)
    asyncio.run(scenario())


def test_expiry_during_post_action_readback_blocks_replay_of_the_unknown_write(tmp_path):
    async def scenario():
        store = TaskStore(tmp_path / "tasks.db")
        task = store.start_run("request", "tracked write", session_id="session")
        registry = ToolRegistry(artifact_dir=tmp_path / "results")
        writes = []
        async def mutation():
            writes.append("dispatched")
            return "written"
        registry.register(ToolDef("mutation", "mutation", {"type": "object"}, mutation, risk="write"))
        agent = make_agent(registry, WaitingLLM("mutation"), task_store=store, turn_timeout_seconds=2)
        agent._source_mutation_intent = lambda *_: {"mode": "track"}
        snapshots = 0
        async def snapshot():
            nonlocal snapshots
            snapshots += 1
            if snapshots == 1:
                return {}
            await asyncio.Event().wait()
        agent._git_worktree_snapshot = snapshot
        events = [e async for e in agent.reply_stream(Msg(content=[ContentBlock.text("go")], metadata={"task_id": task["id"]}))]
        assert snapshots == 2
        assert any(e.get("code") == "turn_budget_exhausted" for e in events)
        step = next(s for s in store.get_task(task["id"])["steps"] if s["kind"] == "tool")
        assert step["status"] == "unknown"
        agent._source_mutation_intent = lambda *_: {"mode": "none"}
        blocked = await agent._execute_tool_call(call("mutation", "one"), task_id=task["id"])
        assert blocked["recovery_blocked"]
        assert writes == ["dispatched"]
    asyncio.run(scenario())
