import asyncio
import json

import pytest

from agent.runtime.agent_team import AgentTeamStore, team_execution_context
from agent.runtime.task_store import TaskStore
from agent.runtime.tools import delegate
from agent.runtime.tools.delegate import register_delegate_tools
from agent.runtime.tools.processes import ProcessManager
from agent.runtime.tools.registry import ToolRegistry
from agent.runtime.worker_lifecycle import WorkerLifecycle
from test_team_adoption import call, wait_status


def test_timing_distinguishes_sleep_from_active_budget():
    mono, wall = [100.0], [1000.0]
    timer = WorkerLifecycle({"idle_timeout_seconds": 30}, clock=lambda: mono[0], wall_clock=lambda: wall[0])
    mono[0] += 2
    wall[0] += 2
    timer.transition("idle", idle_deadline=132)
    # Sleep advances wall time without consuming the host monotonic budget.
    mono[0] += 30
    wall[0] += 930
    ended = timer.transition("terminal", reason="idle_timeout")
    assert ended["active_elapsed_seconds"] == 2
    assert ended["idle_elapsed_seconds"] == 30
    assert ended["wall_elapsed_seconds"] == 932
    assert ended["last_idle_at"] == 1002
    assert ended["idle_deadline_monotonic"] == 132


@pytest.mark.parametrize("reason", ["idle_timeout", "idle_quota", "shutdown_request", "cancelled", "reported", "active_timeout", "keep_alive_lifetime", "startup_failed"])
def test_worker_reasons_and_actual_limits_survive_process_artifacts(tmp_path, monkeypatch, reason):
    class Reporter:
        async def chat(self, **kwargs):
            if reason == "active_timeout":
                await asyncio.sleep(5)
            return {"content": "ready", "tool_calls": []}

    def provider():
        if reason == "startup_failed":
            raise RuntimeError("provider configuration unavailable")
        return Reporter()

    async def scenario():
        monkeypatch.setenv("ASTRA_TEAM_IDLE_TIMEOUT_SECONDS", "1" if reason == "idle_timeout" else "20")
        monkeypatch.setenv("ASTRA_TEAM_KEEP_ALIVE_LIFETIME_SECONDS", "1" if reason == "keep_alive_lifetime" else "0")
        manager = ProcessManager(artifact_dir=tmp_path / "processes")
        monkeypatch.setattr(delegate, "_sub_processes", manager)
        tasks = TaskStore(tmp_path / "tasks.db")
        parent = tasks.start_run("request", "diagnostics", session_id="session")
        registry = ToolRegistry()
        register_delegate_tools(registry, llm_getter=provider, task_store=tasks, session_id_getter=lambda: "session")
        team = await call(registry, parent, "team", action="create", keep_alive_limit=0 if reason == "idle_quota" else 4)
        handle = await call(registry, parent, "team_spawn", team_id=team["id"], name="reporter", goal="report readiness",
                            keep_alive=reason != "reported", timeout=1 if reason in {"active_timeout", "keep_alive_lifetime"} else 10)
        store = AgentTeamStore(tasks.path)
        process = manager.get(handle["process"]["process_id"])
        if reason in {"shutdown_request", "cancelled"}:
            await wait_status(store, handle["agent"]["id"], "idle")
            if reason == "shutdown_request":
                await call(registry, parent, "team_send", team_id=team["id"], to="reporter", kind="shutdown_request", message="done")
            else:
                await manager.cancel(process)
        if reason == "cancelled":
            assert manager.status(process) == "cancelled"
        else:
            assert await manager.wait(process, 3000)
        persisted = AgentTeamStore(tasks.path).get_agent(handle["agent"]["id"])["lifecycle"]
        if reason == "startup_failed":
            assert store.get_agent(handle["agent"]["id"])["status"] == "failed"
        assert persisted["completion_reason"] == reason
        assert persisted["state"] == "terminal"
        assert "max_turns" not in persisted["limits"]
        if reason in {"idle_timeout", "shutdown_request", "cancelled", "keep_alive_lifetime", "idle_quota"}:
            assert persisted["limits"]["idle_capacity"] == (0 if reason == "idle_quota" else 4)
        if reason == "idle_timeout":
            assert persisted["idle_elapsed_seconds"] >= 0.95
            assert persisted["active_elapsed_seconds"] < 1
        # Removing the process observation cannot remove the durable reason.
        manager._processes.pop(process.process_id)
        status = await call(registry, parent, "team", action="status", team_id=team["id"])
        member = next(a for a in status["agents"] if a["id"] == handle["agent"]["id"])
        assert member["lifecycle"] == persisted
        restarted = store.prepare_agent_restart(team["id"], "reporter")
        assert restarted["lifecycle"] == {}
        assert restarted["keep_alive_reason"] == ""
        assert restarted["keep_alive_state"] == ("disabled" if reason == "reported" else "pending")

    asyncio.run(scenario())


def test_read_only_status_compaction_and_automatic_adoption_keep_authority_checks(tmp_path, monkeypatch):
    async def scenario():
        monkeypatch.setattr(delegate, "_sub_processes", ProcessManager(artifact_dir=tmp_path / "processes"))
        tasks = TaskStore(tmp_path / "tasks.db")
        owner = tasks.start_run("old", "old", session_id="session")
        observer = tasks.start_run("next", "next", session_id="session")
        current_session = ["session"]
        registry = ToolRegistry(max_inline_chars=100000, artifact_dir=tmp_path / "artifacts")
        register_delegate_tools(registry, task_store=tasks, session_id_getter=lambda: current_session[0])
        team = await call(registry, owner, "team", action="create")
        store = AgentTeamStore(tasks.path)
        member = store.register_agent(team["id"], name="member", role="worker", mode="worker",
                                      parent_agent_id=team["lead_agent_id"], spawn_spec={"context": "evidence " * 3000, "max_turns": 50})
        store.create_task(team["id"], title="bounded view", description="task detail " * 100)
        store.bind_agent(member["id"], "missing-process")
        store.set_agent_lifecycle(member["id"], {"state": "idle", "observed_at": 123, "idle_elapsed_seconds": 10})
        compact = await call(registry, observer, "team", action="status", team_id=team["id"])
        full = await call(registry, observer, "team", action="status", team_id=team["id"], detail=True)
        assert len(json.dumps(compact)) < len(json.dumps(full)) / 5
        assert "spawn_spec_json" not in json.dumps(full)
        assert "episodes" not in compact["tasks"][0]
        assert "episodes" in full["tasks"][0]
        assert len(compact["tasks"][0]["description"]) == 240
        assert compact["owner_task_id"] == owner["id"]
        denied = await registry.execute("team_send", {"team_id": team["id"], "to": "member", "message": "work"}, task_id=observer["id"])
        assert "still active" in denied["error"]
        tasks.finish_run(owner["id"], "completed")
        configured = await call(registry, observer, "team", action="configure", team_id=team["id"], keep_alive_limit=5)
        assert configured["owner_task_id"] == observer["id"]
        assert configured["effective_keep_alive_limit"] == 5
        recovered = store.get_agent(member["id"])["lifecycle"]
        assert recovered["completion_reason"] == "process_missing"
        assert recovered["observed_at"] == 123  # No invented time of death.
        with team_execution_context(member["id"], 0):
            denied = await registry.execute("team", {"action": "configure", "team_id": team["id"], "keep_alive_limit": 9}, task_id=owner["id"])
            assert "only the team lead" in denied["error"]
        current_session[0] = "foreign"
        foreign = tasks.start_run("foreign", "foreign", session_id="foreign")
        denied = await registry.execute("team", {"action": "status", "team_id": team["id"]}, task_id=foreign["id"])
        assert "another parent task" in denied["error"]

    asyncio.run(scenario())
