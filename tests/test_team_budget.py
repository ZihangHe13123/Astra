import asyncio
import json
import sqlite3
import threading
from contextlib import asynccontextmanager

import pytest

from agent.runtime.agent_team import AgentTeamRuntime, AgentTeamStore
from agent.runtime.task_store import TaskStore
from agent.runtime.tools import delegate
from agent.runtime.tools.delegate import register_delegate_tools
from agent.runtime.tools.processes import ProcessManager
from agent.runtime.tools.registry import ToolDef, ToolRegistry
from agent.runtime.worker import WorkerSpec


def test_a_worker_has_a_time_limit_and_no_turn_limit():
    spec = WorkerSpec("worker", "implement", "")
    assert "max_turns" not in spec.public()
    assert spec.timeout_seconds == 1800
    with pytest.raises(ValueError, match="1-14400"):
        WorkerSpec("worker", "implement", "", timeout_seconds=14401)


def make_team(store):
    team = store.create_team("owner", session_id="session", name="budget", goal="test")
    agent = store.register_agent(
        team["id"], name="worker", role="implementation", mode="worker",
        parent_agent_id=team["lead_agent_id"],
    )
    return team, agent


def test_assignment_metadata_is_durable_validated_and_part_of_deduplication(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.runtime.agent_team.time.time", lambda: 100.0)
    runtime = AgentTeamRuntime(tmp_path / "team.db")
    team, agent = make_team(runtime.store)
    task = runtime.store.create_task(team["id"], title="slice")
    assignment = {"team_task_id": task["id"], "episode_estimate": 20, "episode_kind": "implementation"}
    first = runtime.send(team, target="worker", body="x" * 4000, kind="task_assignment", assignment=assignment)
    repeated = runtime.send(team, target="worker", body="x" * 4000, kind="task_assignment", assignment=assignment)
    changed = runtime.send(
        team, target="worker", body="x" * 4000, kind="task_assignment",
        assignment={**assignment, "episode_estimate": 12},
    )
    assert first[0]["id"] == repeated[0]["id"]
    assert first[0]["id"] != changed[0]["id"]
    deliveries, _, _ = runtime.receive_deliveries(agent["id"], after_seq=0)
    assert deliveries[0].assignment == assignment
    reopened = AgentTeamStore(runtime.store.path)
    assert reopened.read_messages(agent["id"])[0]["assignment"] == assignment
    with pytest.raises(ValueError, match="task_assignment"):
        runtime.send(team, target="worker", body="bad", kind="text", assignment=assignment)
    other = runtime.store.create_team("other", session_id="other", name="other", goal="test")
    other_task = runtime.store.create_task(other["id"], title="wrong team")
    with pytest.raises(ValueError, match="task.*team"):
        runtime.send(team, target="worker", body="bad", kind="task_assignment",
                     assignment={**assignment, "team_task_id": other_task["id"]})
    # An estimate describes the assignment; there is no ceiling for it to stay under.
    large = runtime.send(team, target="worker", body="large", kind="task_assignment",
                         assignment={**assignment, "episode_estimate": 500})
    assert large[0]["assignment"]["episode_estimate"] == 500
    for invalid in (0, -3, True, "12"):
        with pytest.raises(ValueError, match="episode_estimate"):
            runtime.send(team, target="worker", body="bad", kind="task_assignment",
                         assignment={**assignment, "episode_estimate": invalid})


@asynccontextmanager
async def running_team(tmp_path, monkeypatch, llm):
    manager = ProcessManager(artifact_dir=tmp_path / "processes")
    monkeypatch.setattr(delegate, "_sub_processes", manager)
    tasks = TaskStore(tmp_path / "tasks.db")
    owner = tasks.start_run("req", "budget test", session_id="s")["id"]
    registry = ToolRegistry()

    async def read_file(path=""):
        return "fixture evidence"

    registry.register(ToolDef(name="read_file", description="fixture", parameters={"type": "object"},
                              fn=read_file, risk="read", approval="never"))
    register_delegate_tools(registry, llm_getter=lambda: llm, task_store=tasks)

    async def call(tool_name, **args):
        response = await registry.execute(tool_name, args, task_id=owner)
        assert not response["error"], response
        return json.loads(response["output"])

    team = await call("team", action="create", name="budget", goal="measure")
    try:
        yield call, team, AgentTeamStore(tasks.path)
    finally:
        await call("team", action="stop", team_id=team["id"])


async def wait_idle(store, agent_id, *, turns, call, team_id):
    for _ in range(300):
        state = await call("team", action="status", team_id=team_id)
        member = next(item for item in state["agents"] if item["id"] == agent_id)
        if member["status"] == "idle" and member.get("turns_used") == turns:
            return state
        await asyncio.sleep(0.01)
    raise AssertionError(store.get_agent(agent_id))


def test_requests_carry_no_turn_budget_and_episode_costs_survive_idle_and_reopen(tmp_path, monkeypatch):
    class LLM:
        def __init__(self):
            self.requests = []

        async def chat(self, **kwargs):
            self.requests.append(list(kwargs["messages"]))
            if len(self.requests) == 2:
                return {"content": "checking", "tool_calls": [{"id": "read", "name": "read_file",
                                                               "arguments": "{}"}]}
            return {"content": "report with evidence", "tool_calls": []}

    async def scenario():
        llm = LLM()
        async with running_team(tmp_path, monkeypatch, llm) as (call, team, store):
            spawned = await call("team_spawn", team_id=team["id"], name="worker", goal="report readiness",
                                 role="implementation", keep_alive=True, timeout=20)
            agent_id = spawned["agent"]["id"]
            assert "max_turns" not in spawned["agent"]["spawn_spec"]
            await wait_idle(store, agent_id, turns=1, call=call, team_id=team["id"])
            task = await call("team_task", action="create", team_id=team["id"], title="slice")
            await call("team_send", team_id=team["id"], to="worker", message="implement slice",
                       kind="task_assignment", team_task_id=task["id"],
                       episode_kind="implementation")
            await wait_idle(store, agent_id, turns=3, call=call, team_id=team["id"])
            await call("team_send", team_id=team["id"], to="worker", message="check correction",
                       kind="task_assignment", team_task_id=task["id"],
                       episode_kind="revision")
            state = await wait_idle(store, agent_id, turns=4, call=call, team_id=team["id"])
            assert len(llm.requests) == 4
            # Nothing tells the member how many turns it has left: there is no such number.
            assert not any(
                "TURN BUDGET" in str(message.get("content")) or "turns_remaining" in str(message.get("content"))
                for request in llm.requests for message in request
            )
            episodes = state["episodes"]
            assert [e["turns_used"] for e in episodes] == [1, 2, 1]
            assert [e["kind"] for e in episodes] == ["startup", "implementation", "revision"]
            assert all(e["outcome"] == "reported" for e in episodes)
            board = await call("team_task", action="list", team_id=team["id"])
            assert board[0]["turns_used"] == 3
            assert board[0]["status"] == "pending"  # usage does not imply acceptance
            assert len(board[0]["episodes"]) == 2
            reopened = AgentTeamStore(store.path)
            assert reopened.get_task(task["id"])["turns_used"] == 3
            assert reopened.get_team(team["id"])["episodes"] == episodes

    asyncio.run(scenario())


def test_interrupted_episode_keeps_last_observed_counter(tmp_path):
    store = AgentTeamStore(tmp_path / "team.db")
    team, agent = make_team(store)
    episode = store.start_episode(agent["id"], start_turn=7, model="test-model")
    store.update_episode(episode["id"], end_turn=10)
    store.recover_interrupted()
    record = store.get_team(team["id"])["episodes"][0]
    assert record["turns_used"] == 3
    assert record["outcome"] == "interrupted"
    assert record["finished_at"] is not None


def test_old_team_database_migrates_without_losing_mail_or_tasks(tmp_path):
    path = tmp_path / "team.db"
    store = AgentTeamStore(path)
    team, agent = make_team(store)
    task = store.create_task(team["id"], title="existing task")
    message = store.send_message(team["id"], team["lead_agent_id"], agent["id"], body="legacy message")
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE team_episodes")
        db.execute("ALTER TABLE agent_messages DROP COLUMN assignment_json")
    migrated = AgentTeamStore(path)
    assert migrated.get_task(task["id"])["turns_used"] == 0
    assert migrated.get_team(team["id"])["episodes"] == []
    saved = migrated.read_messages(agent["id"])[0]
    assert saved["id"] == message["id"]
    assert saved["body"] == "legacy message"
    assert saved["assignment"] == {}


def test_member_runs_past_the_old_fifty_turn_ceiling_until_it_answers(tmp_path, monkeypatch):
    class LLM:
        def __init__(self):
            self.requests = []

        async def chat(self, **kwargs):
            self.requests.append(kwargs)
            if len(self.requests) > 60:
                return {"content": "Report after sixty reads", "tool_calls": []}
            return {"content": "collect evidence", "tool_calls": [
                {"id": f"read-{len(self.requests)}", "name": "read_file", "arguments": "{}"},
            ]}

    async def scenario():
        llm = LLM()
        async with running_team(tmp_path, monkeypatch, llm) as (call, team, store):
            timeout = 60
            spawned = await call("team_spawn", team_id=team["id"], name="worker", goal="investigate", timeout=timeout)
            result = await call("delegate_poll", process_id=spawned["process"]["process_id"], wait_ms=timeout * 1000)
            assert result["worker"]["status"] == "completed"
            assert result["result"]["result"] == "Report after sixty reads"
            assert len(llm.requests) == 61
            # Every request kept its tools: no turn was set aside for a forced report.
            assert all(request["tools"] for request in llm.requests)
            episode = store.get_team(team["id"])["episodes"][0]
            assert episode["turns_used"] == 61
            assert episode["outcome"] == "reported"

    asyncio.run(scenario())


def test_cancellation_preserves_completed_response_cost(tmp_path, monkeypatch):
    class LLM:
        def __init__(self):
            self.calls = 0
            self.waiting = asyncio.Event()

        async def chat(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return {"content": "evidence", "tool_calls": [{"id": "read", "name": "read_file", "arguments": "{}"}]}
            self.waiting.set()
            await asyncio.Event().wait()

    async def scenario():
        llm = LLM()
        async with running_team(tmp_path, monkeypatch, llm) as (call, team, store):
            await call("team_spawn", team_id=team["id"], name="worker", goal="investigate", timeout=30)
            await asyncio.wait_for(llm.waiting.wait(), timeout=3)
            await call("team", action="stop", team_id=team["id"])
            episode = store.get_team(team["id"])["episodes"][0]
            assert episode["turns_used"] == 1
            assert episode["outcome"] == "cancelled"

    asyncio.run(scenario())


def test_cancellation_during_episode_insert_does_not_leave_a_running_record(tmp_path, monkeypatch):
    from agent.runtime.team_budget import TeamBudgetTracker

    async def scenario():
        runtime = AgentTeamRuntime(tmp_path / "team.db")
        team, agent = make_team(runtime.store)
        entered, release = threading.Event(), threading.Event()
        original = runtime.store.start_episode

        def slow_insert(*args, **kwargs):
            entered.set()
            assert release.wait(timeout=3)
            return original(*args, **kwargs)

        monkeypatch.setattr(runtime.store, "start_episode", slow_insert)
        spec = WorkerSpec("worker", "test", "", team_id=team["id"], team_agent_id=agent["id"])
        tracker = TeamBudgetTracker(runtime, spec, model="test")

        async def start_then_close():
            try:
                await tracker.ensure(0, kind="startup")
            finally:
                await tracker.finish(0, outcome="cancelled")

        task = asyncio.create_task(start_then_close())
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        episodes = runtime.store.get_team(team["id"])["episodes"]
        assert len(episodes) == 1
        assert episodes[0]["outcome"] == "cancelled"
        assert episodes[0]["turns_used"] == 0

    asyncio.run(scenario())
