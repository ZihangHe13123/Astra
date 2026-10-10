import asyncio
import json
import threading
import time
from pathlib import Path

import pytest

from agent.runtime.agent_team import (
    TEAM_INBOX_PAGE_SIZE,
    AgentTeamRuntime,
    AgentTeamStore,
    team_execution_context,
)
from agent.runtime.session_store import SessionStore
from agent.runtime.task_store import TaskStore
from agent.runtime.tools import delegate
from agent.runtime.tools.delegate import register_delegate_tools
from agent.runtime.tools.processes import ProcessManager
from agent.runtime.tools.registry import ToolRegistry
from agent.sandbox.local import LocalSandbox


def _team(store: AgentTeamStore) -> dict:
    return store.create_team(
        "parent-task",
        session_id="session-a",
        name="repair-team",
        goal="repair one bounded issue",
    )


def _agent(store: AgentTeamStore, team: dict, name: str = "worker") -> dict:
    return store.register_agent(
        team["id"],
        name=name,
        role="implementation",
        mode="worker",
        parent_agent_id=team["lead_agent_id"],
    )


def test_store_persists_agent_tree_mailbox_and_ack(tmp_path: Path):
    path = tmp_path / "tasks.db"
    store = AgentTeamStore(path)
    team = _team(store)
    worker = _agent(store, team)

    message = store.send_message(
        team["id"], team["lead_agent_id"], worker["id"], body="Inspect the failing test"
    )
    duplicate = store.send_message(
        team["id"], team["lead_agent_id"], worker["id"], body="Inspect the failing test"
    )

    assert duplicate["id"] == message["id"]
    unread = store.read_messages(worker["id"])
    assert [item["body"] for item in unread] == ["Inspect the failing test"]
    store.mark_messages([message["id"]], acknowledged=True)
    reloaded = AgentTeamStore(path)
    state = reloaded.get_team(team["id"])
    assert state is not None
    by_name = {item["name"]: item for item in state["agents"]}
    assert by_name["worker"]["unread_count"] == 0


def test_default_read_returns_unacknowledged_messages_and_after_seq_rereads(tmp_path: Path):
    store = AgentTeamStore(tmp_path / "tasks.db")
    team = _team(store)
    worker = _agent(store, team)
    lead = team["lead_agent_id"]
    sent = [store.send_message(team["id"], lead, worker["id"], body=f"note {index}") for index in range(4)]

    def bodies(**arguments) -> list[str]:
        return [item["body"] for item in store.read_messages(worker["id"], **arguments)]

    assert bodies() == ["note 0", "note 1", "note 2", "note 3"]
    # Acknowledged out of order, and one message only delivered: it is still unread.
    store.mark_messages([sent[0]["id"], sent[2]["id"]], acknowledged=True)
    store.mark_messages([sent[1]["id"]], acknowledged=False)

    # The default used to start from the oldest message again, acknowledged or not.
    assert bodies() == ["note 1", "note 3"]
    unread_counts = {item["name"]: item["unread_count"] for item in store.get_team(team["id"])["agents"]}
    assert unread_counts["worker"] == len(bodies())
    # An explicit cursor is how acknowledged messages are read again.
    assert bodies(after_seq=0) == ["note 0", "note 1", "note 2", "note 3"]
    assert bodies(after_seq=sent[1]["seq"]) == ["note 2", "note 3"]
    assert bodies(after_seq=sent[1]["seq"], unread_only=True) == ["note 3"]
    assert bodies(unread_only=False) == bodies(after_seq=0)

    store.mark_messages([item["id"] for item in sent], acknowledged=True)
    assert bodies() == []
    # With nothing unread, this is the cursor that returns only what arrives later.
    cursor = store.last_message_seq(worker["id"])
    assert cursor == sent[-1]["seq"] and bodies(after_seq=cursor) == []
    assert store.last_message_seq(lead) == 0
    store.send_message(team["id"], lead, worker["id"], body="note 4")
    assert bodies() == bodies(after_seq=cursor) == ["note 4"]


def test_default_reads_page_through_a_mailbox_without_a_cursor(tmp_path: Path):
    store = AgentTeamStore(tmp_path / "tasks.db")
    team = _team(store)
    worker = _agent(store, team)
    for index in range(5):
        store.send_message(team["id"], team["lead_agent_id"], worker["id"], body=f"note {index}")

    pages = []
    follow_ups = []
    while page := store.read_messages(worker["id"], limit=2):
        pages.append([item["body"] for item in page])
        follow_ups.append(bool(
            store.read_messages(worker["id"], after_seq=page[-1]["seq"], limit=1, unread_only=True)
        ))
        store.mark_messages([item["id"] for item in page], acknowledged=True)

    # Each default read continues after what was acknowledged instead of repeating the first page.
    assert pages == [["note 0", "note 1"], ["note 2", "note 3"], ["note 4"]]
    assert follow_ups == [True, True, False]
    # The acknowledged messages are still there for an explicit cursor.
    assert len(store.read_messages(worker["id"], after_seq=0)) == 5


def test_runtime_exposes_structured_shutdown_delivery_and_legacy_envelope(tmp_path: Path):
    runtime = AgentTeamRuntime(tmp_path / "tasks.db")
    team = _team(runtime.store)
    worker = _agent(runtime.store, team)
    message = runtime.store.send_message(
        team["id"],
        team["lead_agent_id"],
        worker["id"],
        body="stop now",
        kind="shutdown_request",
    )

    deliveries, cursor, ids = runtime.receive_deliveries(worker["id"], after_seq=0)

    assert ids == [message["id"]]
    assert cursor == message["seq"]
    assert deliveries[0].message_id == message["id"]
    assert deliveries[0].kind == "shutdown_request"
    assert "stop now" in deliveries[0].envelope

    second = runtime.store.send_message(
        team["id"],
        team["lead_agent_id"],
        worker["id"],
        body="ordinary follow-up",
        kind="text",
    )
    envelopes, legacy_cursor, legacy_ids = runtime.receive(worker["id"], after_seq=cursor)
    assert legacy_ids == [second["id"]]
    assert legacy_cursor == second["seq"]
    assert "ordinary follow-up" in envelopes[0]


def test_store_reclaims_only_unreferenced_failed_spawn_names(tmp_path: Path):
    store = AgentTeamStore(tmp_path / "tasks.db")
    team = _team(store)

    orphan = _agent(store, team, "retryable")
    store.set_agent_status(orphan["id"], "failed")
    replacement = _agent(store, team, "retryable")
    assert replacement["id"] != orphan["id"]
    assert store.get_agent(orphan["id"]) is None

    retained = _agent(store, team, "retained-history")
    store.send_message(
        team["id"],
        team["lead_agent_id"],
        retained["id"],
        body="durable history",
    )
    store.set_agent_status(retained["id"], "failed")
    with pytest.raises(ValueError, match="already used by a failed agent"):
        _agent(store, team, "retained-history")


def test_recover_interrupted_fences_only_active_agents_and_is_idempotent(
    tmp_path: Path,
):
    store = AgentTeamStore(tmp_path / "tasks.db")
    team = _team(store)
    active = _agent(store, team, "active-helper")
    finished = _agent(store, team, "finished-helper")
    store.set_agent_status(active["id"], "running")
    store.set_agent_status(finished["id"], "completed")

    recovered = store.recover_interrupted()

    assert recovered == 2  # lead + active helper
    state = store.get_team(team["id"])
    assert state is not None and state["status"] == "interrupted"
    by_name = {agent["name"]: agent for agent in state["agents"]}
    assert by_name["lead"]["status"] == "interrupted"
    assert by_name["active-helper"]["status"] == "interrupted"
    assert by_name["finished-helper"]["status"] == "completed"
    assert store.recover_interrupted() == 0


def test_recovery_marks_effective_idle_worker_interrupted(tmp_path: Path):
    path = tmp_path / "tasks.db"
    store = AgentTeamStore(path)
    team = _team(store)
    worker = store.register_agent(
        team["id"],
        name="retained",
        role="implementation",
        mode="worker",
        parent_agent_id=team["lead_agent_id"],
        spawn_spec={
            "keep_alive_requested": True,
            "keep_alive_state": "pending",
            "keep_alive_reason": "",
        },
    )
    store.set_agent_status(worker["id"], "running")
    admitted = store.try_enter_idle(worker["id"], limit=1)
    assert admitted["keep_alive_state"] == "effective"
    store.set_agent_lifecycle(worker["id"], {"state": "idle", "observed_at": 123, "idle_elapsed_seconds": 10})

    recovered = AgentTeamStore(path)
    recovered.recover_interrupted()
    state = recovered.get_agent(worker["id"])
    assert state["status"] == "interrupted"
    assert state["keep_alive_state"] == "effective"
    assert state["lifecycle"]["completion_reason"] == "backend_interrupted"
    assert state["lifecycle"]["observed_at"] == 123
    assert state["lifecycle"]["timing_freshness"] == "last_durable_observation"


def test_mailbox_is_bounded_and_malformed_kind_is_rejected(tmp_path: Path):
    store = AgentTeamStore(tmp_path / "tasks.db")
    team = _team(store)
    worker = _agent(store, team)

    with pytest.raises(ValueError, match="invalid message kind"):
        store.send_message(
            team["id"], team["lead_agent_id"], worker["id"], body="x", kind="permission_grant"
        )
    for index in range(50):
        store.send_message(
            team["id"],
            team["lead_agent_id"],
            worker["id"],
            body=f"message-{index}",
        )
    with pytest.raises(ValueError, match="mailbox is full"):
        store.send_message(
            team["id"], team["lead_agent_id"], worker["id"], body="overflow"
        )


def test_atomic_task_claim_has_one_winner(tmp_path: Path):
    path = tmp_path / "tasks.db"
    store = AgentTeamStore(path)
    team = _team(store)
    agents = [_agent(store, team, f"worker-{index}") for index in range(8)]
    task = store.create_task(team["id"], title="one side effect")
    barrier = threading.Barrier(len(agents))
    winners: list[str] = []
    failures: list[str] = []

    def claim(agent_id: str) -> None:
        contender = AgentTeamStore(path)
        barrier.wait()
        try:
            contender.claim_task(team["id"], task["id"], agent_id, lease_seconds=300)
            winners.append(agent_id)
        except ValueError as exc:
            failures.append(str(exc))

    threads = [threading.Thread(target=claim, args=(agent["id"],)) for agent in agents]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert len(winners) == 1
    assert len(failures) == len(agents) - 1
    assert store.get_task(task["id"])["owner_agent_id"] == winners[0]


def test_atomic_idle_quota_has_one_winner(tmp_path: Path):
    path = tmp_path / "tasks.db"
    store = AgentTeamStore(path)
    team = _team(store)
    agents = [
        store.register_agent(
            team["id"],
            name=f"worker-{index}",
            role="implementation",
            mode="worker",
            parent_agent_id=team["lead_agent_id"],
            spawn_spec={
                "keep_alive_requested": True,
                "keep_alive_state": "pending",
                "keep_alive_reason": "",
            },
        )
        for index in range(2)
    ]
    for agent in agents:
        store.set_agent_status(agent["id"], "running")

    barrier = threading.Barrier(len(agents))
    results: list[dict] = []

    def enter_idle(agent_id: str) -> None:
        contender = AgentTeamStore(path)
        barrier.wait()
        results.append(contender.try_enter_idle(agent_id, limit=1))

    threads = [threading.Thread(target=enter_idle, args=(agent["id"],)) for agent in agents]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert len(results) == 2
    assert sum(bool(result["idle_accepted"]) for result in results) == 1
    states = {result["keep_alive_state"] for result in results}
    assert states == {"effective", "quota_rejected"}
    by_status = {result["status"]: result for result in results}
    assert set(by_status) == {"idle", "running"}
    assert by_status["running"]["keep_alive_reason"] == "team_idle_quota"


def test_task_dependencies_owner_update_and_lease_reclaim(tmp_path: Path):
    store = AgentTeamStore(tmp_path / "tasks.db")
    team = _team(store)
    first = _agent(store, team, "first")
    second = _agent(store, team, "second")
    prerequisite = store.create_task(team["id"], title="research")
    dependent = store.create_task(
        team["id"], title="implement", blocked_by=[prerequisite["id"]]
    )

    with pytest.raises(ValueError, match="blocked by"):
        store.claim_task(team["id"], dependent["id"], second["id"], lease_seconds=30)
    store.claim_task(team["id"], prerequisite["id"], first["id"], lease_seconds=30)
    with pytest.raises(ValueError, match="only the task owner"):
        store.update_task(
            team["id"], prerequisite["id"], second["id"], status="completed"
        )
    store.update_task(
        team["id"], prerequisite["id"], first["id"], status="completed", result="done"
    )
    claimed = store.claim_task(
        team["id"], dependent["id"], second["id"], lease_seconds=30
    )
    assert claimed["owner_agent_id"] == second["id"]


def test_runtime_sender_identity_scope_and_message_budget(tmp_path: Path):
    runtime = AgentTeamRuntime(tmp_path / "tasks.db")
    team = runtime.store.create_team(
        "parent-task", session_id="session-a", name="team", goal="coordinate"
    )
    alpha = _agent(runtime.store, team, "alpha")
    beta = _agent(runtime.store, team, "beta")

    with team_execution_context(alpha["id"], 1):
        for index in range(4):
            sent = runtime.send(
                team, target="beta", body=f"note {index}", kind="text"
            )
            assert sent[0]["sender_agent_id"] == alpha["id"]
        with pytest.raises(ValueError, match="budget exhausted"):
            runtime.send(team, target="beta", body="too many", kind="text")
    assert len(runtime.store.read_messages(beta["id"])) == 4


def test_claim_agent_resolution_is_not_message_routing(tmp_path: Path):
    store = AgentTeamStore(tmp_path / "tasks.db")
    team = _team(store)
    helper = _agent(store, team, "helper")

    lead = store.resolve_agent(team["id"], team["lead_agent_id"], require_active=True)
    named = store.resolve_agent(team["id"], "helper", require_active=True)
    assert lead["id"] == team["lead_agent_id"]
    assert named["id"] == helper["id"]

    store.set_agent_status(helper["id"], "completed")
    with pytest.raises(ValueError, match="helper is completed and cannot claim tasks"):
        store.resolve_agent(team["id"], "helper", require_active=True)
    with pytest.raises(ValueError, match="unknown team agent"):
        store.resolve_agent(team["id"], "missing", require_active=True)
    with pytest.raises(ValueError, match="message recipient helper is completed"):
        store.resolve_recipients(
            team["id"], team["lead_agent_id"], "helper"
        )


def test_async_send_keeps_event_loop_responsive(tmp_path: Path, monkeypatch):
    async def scenario():
        runtime = AgentTeamRuntime(tmp_path / "tasks.db")
        team = runtime.store.create_team(
            "parent-task", session_id="session-a", name="team", goal="coordinate"
        )
        worker = _agent(runtime.store, team, "worker")
        original = runtime.store.send_message
        release = threading.Event()

        def delayed_send(*args, **kwargs):
            release.wait(timeout=0.15)
            return original(*args, **kwargs)

        monkeypatch.setattr(runtime.store, "send_message", delayed_send)
        timer = threading.Timer(0.1, release.set)
        timer.start()
        started = time.perf_counter()
        pending = asyncio.create_task(
            runtime.send_async(team, target="worker", body="hello", kind="text")
        )
        await asyncio.sleep(0.01)

        assert time.perf_counter() - started < 0.06
        assert not pending.done()
        messages = await pending
        timer.join(timeout=1)
        assert messages[0]["recipient_agent_id"] == worker["id"]

    asyncio.run(scenario())


def test_unread_count_uses_partial_recipient_index(tmp_path: Path):
    store = AgentTeamStore(tmp_path / "tasks.db")
    with store._connection() as db:
        indexes = {row[1] for row in db.execute("PRAGMA index_list('agent_messages')")}
        plan = db.execute(
            "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM agent_messages "
            "WHERE recipient_agent_id=? AND acknowledged_at IS NULL",
            ("agent-none",),
        ).fetchall()

    assert "idx_agent_messages_unread_recipient" in indexes
    assert any("idx_agent_messages_unread_recipient" in str(row[3]) for row in plan)


class TeamLLM:
    def __init__(self):
        self.calls = 0
        self.second_messages = []

    async def chat(self, messages, tools=None, tool_choice=None, **kwargs):
        del tool_choice
        self.calls += 1
        if self.calls == 1:
            names = {tool["function"]["name"] for tool in (tools or [])}
            assert "team_send" in names
            assert "team_spawn" not in names
            return {
                "content": "",
                "tool_calls": [{
                    "id": "send-1",
                    "name": "team_send",
                    "arguments": json.dumps({
                        "team_id": self.team_id,
                        "to": "lead",
                        "message": "Investigation is halfway complete",
                    }),
                }],
            }
        self.second_messages = list(messages)
        return {"content": "Final teammate report", "tool_calls": []}


class ImmediateTeamLLM:
    async def chat(self, **kwargs):
        del kwargs
        return {"content": "Teammate completed.", "tool_calls": []}


class CapturingTeamLLM:
    def __init__(self):
        self.prompts = []

    async def chat(self, **kwargs):
        self.prompts.append(list(kwargs.get("messages") or []))
        return {"content": "Checkpoint worker completed.", "tool_calls": []}


class EpisodicTeamLLM:
    def __init__(self):
        self.requests: list[list[dict]] = []

    async def chat(self, **kwargs):
        self.requests.append(list(kwargs.get("messages") or []))
        report = "RED phase report" if len(self.requests) == 1 else "GREEN phase report"
        return {"content": report, "tool_calls": []}


async def _wait_for_agent_status(
    store: AgentTeamStore,
    agent_id: str,
    expected: str,
    *,
    timeout: float = 2.0,
) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        agent = store.get_agent(agent_id) or {}
        if agent.get("status") == expected:
            return agent
        await asyncio.sleep(0.01)
    raise AssertionError(
        f"agent {agent_id} did not reach {expected}: {store.get_agent(agent_id)}"
    )


def test_keep_alive_worker_idles_wakes_with_context_and_shutdowns_without_model_call(
    tmp_path: Path,
    monkeypatch,
):
    async def scenario():
        manager = ProcessManager(artifact_dir=tmp_path / "processes")
        monkeypatch.setattr(delegate, "_sub_processes", manager)
        store = TaskStore(tmp_path / "tasks.db")
        parent_task = store.start_run("request-a", "test keep alive", session_id="session-a")
        llm = EpisodicTeamLLM()
        events: list[dict] = []
        registry = ToolRegistry()
        register_delegate_tools(
            registry,
            llm_getter=lambda: llm,
            session_id_getter=lambda: "session-a",
            task_store=store,
            on_process_event=events.append,
        )
        team = json.loads((await registry.execute(
            "team",
            {"action": "create", "name": "tdd", "goal": "red then green"},
            task_id=parent_task["id"],
        ))["output"])
        spawned = json.loads((await registry.execute(
            "team_spawn",
            {
                "team_id": team["id"],
                "name": "implementer",
                "goal": "run red then green",
                "max_turns": 4,
                "timeout": 10,
                "keep_alive": True,
            },
            task_id=parent_task["id"],
        ))["output"])
        agent_id = spawned["agent"]["id"]
        process_id = spawned["process"]["process_id"]
        team_store = AgentTeamStore(store.path)

        first_idle = await _wait_for_agent_status(team_store, agent_id, "idle")
        assert first_idle["keep_alive_state"] == "effective"
        assert len(llm.requests) == 1
        live_team = json.loads((await registry.execute(
            "team",
            {"action": "status", "team_id": team["id"]},
            task_id=parent_task["id"],
        ))["output"])
        live_worker = next(
            item for item in live_team["agents"] if item["id"] == agent_id
        )
        assert live_worker["turns_used"] == 1
        assert live_worker["max_turns"] == 4
        assert live_worker["turns_remaining"] == 3

        await registry.execute(
            "team_send",
            {
                "team_id": team["id"],
                "to": "implementer",
                "message": "Proceed to GREEN",
            },
            task_id=parent_task["id"],
        )
        deadline = asyncio.get_running_loop().time() + 2.0
        while asyncio.get_running_loop().time() < deadline:
            current = team_store.get_agent(agent_id) or {}
            if len(llm.requests) == 2 and current.get("status") == "idle":
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError(
                f"second episode did not return idle: {team_store.get_agent(agent_id)}"
            )
        assert len(llm.requests) == 2
        second_request = llm.requests[1]
        assert any(
            message.get("role") == "assistant"
            and message.get("content") == "RED phase report"
            for message in second_request
        )
        assert any("Proceed to GREEN" in str(message.get("content")) for message in second_request)

        await registry.execute(
            "team_send",
            {
                "team_id": team["id"],
                "to": "implementer",
                "message": "stop",
                "kind": "shutdown_request",
            },
            task_id=parent_task["id"],
        )
        polled = json.loads((await registry.execute(
            "delegate_poll",
            {"process_id": process_id, "wait_ms": 5000},
            task_id=parent_task["id"],
        ))["output"])
        assert polled["worker"]["status"] == "completed"
        assert len(llm.requests) == 2
        assert any(event.get("event") == "team_agent_idle" for event in events)
        assert any(event.get("event") == "team_agent_awakened" for event in events)

    asyncio.run(scenario())


def test_keep_alive_forced_finalizer_reports_latest_task_assignment(
    tmp_path: Path,
    monkeypatch,
):
    class AssignmentFinalizerLLM:
        def __init__(self):
            self.finalizer_request = None

        async def chat(self, **kwargs):
            del kwargs
            return {"content": "ready and idle", "tool_calls": []}

        async def chat_limited(self, **kwargs):
            self.finalizer_request = kwargs
            return {"content": "implemented assigned slice", "tool_calls": []}

    async def scenario():
        manager = ProcessManager(artifact_dir=tmp_path / "processes")
        monkeypatch.setattr(delegate, "_sub_processes", manager)
        store = TaskStore(tmp_path / "tasks.db")
        parent_task = store.start_run(
            "request-a", "assignment finalizer", session_id="session-a"
        )
        llm = AssignmentFinalizerLLM()
        registry = ToolRegistry()
        register_delegate_tools(
            registry,
            llm_getter=lambda: llm,
            session_id_getter=lambda: "session-a",
            task_store=store,
        )
        team = json.loads((await registry.execute(
            "team",
            {"action": "create", "name": "tdd", "goal": "one slice"},
            task_id=parent_task["id"],
        ))["output"])
        spawned = json.loads((await registry.execute(
            "team_spawn",
            {
                "team_id": team["id"],
                "name": "implementer",
                "goal": "Report readiness, then wait for work",
                "max_turns": 2,
                "timeout": 10,
                "keep_alive": True,
            },
            task_id=parent_task["id"],
        ))["output"])
        team_store = AgentTeamStore(store.path)
        await _wait_for_agent_status(
            team_store, spawned["agent"]["id"], "idle"
        )

        await registry.execute(
            "team_send",
            {
                "team_id": team["id"],
                "to": "implementer",
                "kind": "task_assignment",
                "message": "Implement ellipsize with RED then GREEN",
            },
            task_id=parent_task["id"],
        )
        polled = json.loads((await registry.execute(
            "delegate_poll",
            {"process_id": spawned["process"]["process_id"], "wait_ms": 5000},
            task_id=parent_task["id"],
        ))["output"])

        assert polled["result"]["result"] == "implemented assigned slice"
        finalizer_text = llm.finalizer_request["messages"][1]["content"]
        assert "Latest assigned task:" in finalizer_text
        assert "Implement ellipsize with RED then GREEN" in finalizer_text
        assert "Standing goal:" in finalizer_text

    asyncio.run(scenario())


def test_keep_alive_repeated_truncated_finalizer_returns_assignment_scoped_partial(
    tmp_path: Path,
    monkeypatch,
):
    class TruncatedAssignmentFinalizerLLM:
        def __init__(self):
            self.finalizer_calls = 0

        async def chat(self, **kwargs):
            del kwargs
            return {"content": "ready and idle", "tool_calls": []}

        async def chat_limited(self, **kwargs):
            del kwargs
            self.finalizer_calls += 1
            return {
                "content": f"generic truncated draft {self.finalizer_calls}",
                "tool_calls": [],
                "finish_reason": "length",
            }

    async def scenario():
        manager = ProcessManager(artifact_dir=tmp_path / "processes")
        monkeypatch.setattr(delegate, "_sub_processes", manager)
        store = TaskStore(tmp_path / "tasks.db")
        parent_task = store.start_run(
            "request-a", "truncated assignment finalizer", session_id="session-a"
        )
        llm = TruncatedAssignmentFinalizerLLM()
        registry = ToolRegistry()
        register_delegate_tools(
            registry,
            llm_getter=lambda: llm,
            session_id_getter=lambda: "session-a",
            task_store=store,
        )
        team = json.loads((await registry.execute(
            "team",
            {"action": "create", "name": "tdd", "goal": "one slice"},
            task_id=parent_task["id"],
        ))["output"])
        spawned = json.loads((await registry.execute(
            "team_spawn",
            {
                "team_id": team["id"],
                "name": "implementer",
                "goal": "Report readiness, then wait for work",
                "max_turns": 2,
                "timeout": 10,
                "keep_alive": True,
            },
            task_id=parent_task["id"],
        ))["output"])
        team_store = AgentTeamStore(store.path)
        await _wait_for_agent_status(
            team_store, spawned["agent"]["id"], "idle"
        )

        await registry.execute(
            "team_send",
            {
                "team_id": team["id"],
                "to": "implementer",
                "kind": "task_assignment",
                "message": "Implement ellipsize with RED then GREEN",
            },
            task_id=parent_task["id"],
        )
        polled = json.loads((await registry.execute(
            "delegate_poll",
            {"process_id": spawned["process"]["process_id"], "wait_ms": 5000},
            task_id=parent_task["id"],
        ))["output"])

        report = polled["result"]["result"]
        assert polled["result"]["worker_status"] == "partial"
        assert llm.finalizer_calls == 2
        assert "Partial report" in report
        assert "Task: Implement ellipsize with RED then GREEN" in report
        assert "Standing goal: Report readiness, then wait for work" in report
        assert "no model-written conclusion was accepted" in report

    asyncio.run(scenario())


def test_keep_alive_worker_releases_slot_and_reacquires_on_wake(
    tmp_path: Path,
    monkeypatch,
):
    class CapacityLLM:
        def __init__(self):
            self.retained_calls = 0
            self.one_shot_calls = 0

        async def chat(self, **kwargs):
            messages = list(kwargs.get("messages") or [])
            initial = str(messages[1].get("content") if len(messages) > 1 else "")
            if "retained task" in initial:
                self.retained_calls += 1
                return {
                    "content": f"retained report {self.retained_calls}",
                    "tool_calls": [],
                }
            self.one_shot_calls += 1
            return {"content": "one shot complete", "tool_calls": []}

    async def scenario():
        monkeypatch.setenv("ASTRA_DELEGATE_CONCURRENCY", "1")
        monkeypatch.setenv("ASTRA_DELEGATE_OWNER_CONCURRENCY", "1")
        manager = ProcessManager(artifact_dir=tmp_path / "processes")
        monkeypatch.setattr(delegate, "_sub_processes", manager)
        store = TaskStore(tmp_path / "tasks.db")
        parent_task = store.start_run("request-a", "capacity", session_id="session-a")
        llm = CapacityLLM()
        registry = ToolRegistry()
        register_delegate_tools(
            registry,
            llm_getter=lambda: llm,
            session_id_getter=lambda: "session-a",
            task_store=store,
        )
        team = json.loads((await registry.execute(
            "team",
            {"action": "create", "name": "capacity", "goal": "share one slot"},
            task_id=parent_task["id"],
        ))["output"])
        retained = json.loads((await registry.execute(
            "team_spawn",
            {
                "team_id": team["id"],
                "name": "retained",
                "goal": "retained task",
                "max_turns": 4,
                "timeout": 5,
                "keep_alive": True,
            },
            task_id=parent_task["id"],
        ))["output"])
        team_store = AgentTeamStore(store.path)
        await _wait_for_agent_status(team_store, retained["agent"]["id"], "idle")

        one_shot = json.loads((await registry.execute(
            "team_spawn",
            {
                "team_id": team["id"],
                "name": "one-shot",
                "goal": "one shot task",
                "max_turns": 2,
                "timeout": 1,
            },
            task_id=parent_task["id"],
        ))["output"])
        one_shot_poll = json.loads((await registry.execute(
            "delegate_poll",
            {"process_id": one_shot["process"]["process_id"], "wait_ms": 2000},
            task_id=parent_task["id"],
        ))["output"])
        assert one_shot_poll["worker"]["status"] == "completed"
        assert llm.one_shot_calls == 1

        await registry.execute(
            "team_send",
            {
                "team_id": team["id"],
                "to": "retained",
                "message": "resume retained task",
            },
            task_id=parent_task["id"],
        )
        deadline = asyncio.get_running_loop().time() + 2
        while asyncio.get_running_loop().time() < deadline:
            if (
                llm.retained_calls == 2
                and (team_store.get_agent(retained["agent"]["id"]) or {}).get("status")
                == "idle"
            ):
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("retained worker did not reacquire the slot")

        await registry.execute(
            "team_send",
            {
                "team_id": team["id"],
                "to": "retained",
                "message": "stop",
                "kind": "shutdown_request",
            },
            task_id=parent_task["id"],
        )
        retained_poll = json.loads((await registry.execute(
            "delegate_poll",
            {"process_id": retained["process"]["process_id"], "wait_ms": 2000},
            task_id=parent_task["id"],
        ))["output"])
        assert retained_poll["worker"]["status"] == "completed"

    asyncio.run(scenario())


def test_keep_alive_idle_preserves_active_budget_and_lifetime_cap_reclaims(
    tmp_path: Path,
    monkeypatch,
):
    async def scenario():
        monkeypatch.setenv("ASTRA_TEAM_KEEP_ALIVE_LIFETIME_SECONDS", "1")
        manager = ProcessManager(artifact_dir=tmp_path / "processes")
        monkeypatch.setattr(delegate, "_sub_processes", manager)
        store = TaskStore(tmp_path / "tasks.db")
        parent_task = store.start_run("request-a", "lifetime", session_id="session-a")
        llm = EpisodicTeamLLM()
        registry = ToolRegistry()
        register_delegate_tools(
            registry,
            llm_getter=lambda: llm,
            session_id_getter=lambda: "session-a",
            task_store=store,
        )
        team = json.loads((await registry.execute(
            "team",
            {"action": "create", "name": "lifetime", "goal": "bounded idle"},
            task_id=parent_task["id"],
        ))["output"])
        spawned = json.loads((await registry.execute(
            "team_spawn",
            {
                "team_id": team["id"],
                "name": "retained",
                "goal": "produce one retained report",
                "max_turns": 4,
                "timeout": 1,
                "keep_alive": True,
            },
            task_id=parent_task["id"],
        ))["output"])
        team_store = AgentTeamStore(store.path)
        await _wait_for_agent_status(team_store, spawned["agent"]["id"], "idle")
        assert len(llm.requests) == 1

        # Poll now returns an episode report while the member is retained.
        # Wait on the actual lifetime to verify that the hard cap still reaps it.
        process = manager.get(spawned["process"]["process_id"])
        assert await manager.wait(process, 2000)
        polled = json.loads((await registry.execute(
            "delegate_poll",
            {"process_id": spawned["process"]["process_id"], "wait_ms": 2000},
            task_id=parent_task["id"],
        ))["output"])
        assert polled["worker"]["status"] == "completed"
        assert polled["worker"]["turns_used"] == 1
        assert polled["result"]["result"] == "RED phase report"
        assert len(llm.requests) == 1

    asyncio.run(scenario())


def test_keep_alive_wall_cap_interrupts_in_flight_llm(tmp_path: Path, monkeypatch):
    interrupted_calls: list[float] = []

    class SlowWallLLM:
        async def chat(self, **kwargs):
            del kwargs
            started = asyncio.get_running_loop().time()
            try:
                await asyncio.sleep(2)
            except asyncio.CancelledError:
                interrupted_calls.append(asyncio.get_running_loop().time() - started)
                raise
            return {"content": "too late", "tool_calls": []}

    async def scenario():
        monkeypatch.setenv("ASTRA_TEAM_KEEP_ALIVE_LIFETIME_SECONDS", "1")
        manager = ProcessManager(artifact_dir=tmp_path / "processes")
        monkeypatch.setattr(delegate, "_sub_processes", manager)
        store = TaskStore(tmp_path / "tasks.db")
        parent_task = store.start_run("request-a", "wall cap", session_id="session-a")
        registry = ToolRegistry()
        register_delegate_tools(
            registry,
            llm_getter=SlowWallLLM,
            session_id_getter=lambda: "session-a",
            task_store=store,
        )
        team = json.loads((await registry.execute(
            "team",
            {"action": "create", "name": "wall", "goal": "hard lifetime"},
            task_id=parent_task["id"],
        ))["output"])
        spawned = json.loads((await registry.execute(
            "team_spawn",
            {
                "team_id": team["id"],
                "name": "worker",
                "goal": "slow call",
                "keep_alive": True,
                "timeout": 5,
            },
            task_id=parent_task["id"],
        ))["output"])
        polled = json.loads((await registry.execute(
            "delegate_poll",
            {"process_id": spawned["process"]["process_id"], "wait_ms": 2000},
            task_id=parent_task["id"],
        ))["output"])
        # Measure the calls being interrupted, not database/process setup or
        # terminal-state persistence on the runner. Keep the same time bound.
        assert interrupted_calls
        assert sum(interrupted_calls) < 1.6
        assert polled["worker"]["status"] == "timed_out"
        assert AgentTeamStore(store.path).get_agent(spawned["agent"]["id"])["status"] == "timed_out"

    asyncio.run(scenario())


def test_keep_alive_quota_rejection_preserves_report_and_emits_one_terminal(
    tmp_path: Path,
    monkeypatch,
):
    async def scenario():
        monkeypatch.setenv("ASTRA_TEAM_KEEP_ALIVE_LIMIT", "0")
        manager = ProcessManager(artifact_dir=tmp_path / "processes")
        monkeypatch.setattr(delegate, "_sub_processes", manager)
        store = TaskStore(tmp_path / "tasks.db")
        parent_task = store.start_run("request-a", "quota", session_id="session-a")
        events: list[dict] = []
        registry = ToolRegistry()
        register_delegate_tools(
            registry,
            llm_getter=ImmediateTeamLLM,
            session_id_getter=lambda: "session-a",
            task_store=store,
            on_process_event=events.append,
        )
        team = json.loads((await registry.execute(
            "team",
            {"action": "create", "name": "quota", "goal": "reject idle"},
            task_id=parent_task["id"],
        ))["output"])
        spawned = json.loads((await registry.execute(
            "team_spawn",
            {
                "team_id": team["id"],
                "name": "worker",
                "goal": "finish once",
                "keep_alive": True,
                "timeout": 5,
            },
            task_id=parent_task["id"],
        ))["output"])
        polled = json.loads((await registry.execute(
            "delegate_poll",
            {"process_id": spawned["process"]["process_id"], "wait_ms": 2000},
            task_id=parent_task["id"],
        ))["output"])
        assert polled["worker"]["status"] == "completed"
        assert polled["result"]["result"] == "Teammate completed."
        assert polled["result"]["keep_alive_state"] == "quota_rejected"
        assert polled["result"]["keep_alive_reason"] == "team_idle_quota"
        team_state = AgentTeamStore(store.path).get_team(team["id"])
        agent = next(
            item for item in team_state["agents"]
            if item["id"] == spawned["agent"]["id"]
        )
        assert agent["keep_alive_state"] == "quota_rejected"
        assert len([
            event for event in events
            if event.get("event") == "team_agent_terminal"
            and event.get("agent_id") == spawned["agent"]["id"]
        ]) == 1

    asyncio.run(scenario())


def test_active_shutdown_stops_at_next_boundary(tmp_path: Path, monkeypatch):
    class InFlightLLM:
        def __init__(self):
            self.calls = 0
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def chat(self, **kwargs):
            del kwargs
            self.calls += 1
            self.started.set()
            await self.release.wait()
            return {"content": "in-flight report", "tool_calls": []}

    async def scenario():
        monkeypatch.setenv("ASTRA_TEAM_KEEP_ALIVE_LIMIT", "0")
        manager = ProcessManager(artifact_dir=tmp_path / "processes")
        monkeypatch.setattr(delegate, "_sub_processes", manager)
        store = TaskStore(tmp_path / "tasks.db")
        parent_task = store.start_run("request-a", "shutdown", session_id="session-a")
        llm = InFlightLLM()
        events: list[dict] = []
        registry = ToolRegistry()
        register_delegate_tools(
            registry,
            llm_getter=lambda: llm,
            session_id_getter=lambda: "session-a",
            task_store=store,
            on_process_event=events.append,
        )
        team = json.loads((await registry.execute(
            "team",
            {"action": "create", "name": "shutdown", "goal": "stop cleanly"},
            task_id=parent_task["id"],
        ))["output"])
        spawned = json.loads((await registry.execute(
            "team_spawn",
            {
                "team_id": team["id"],
                "name": "worker",
                "goal": "wait in flight",
                "keep_alive": True,
                "timeout": 5,
            },
            task_id=parent_task["id"],
        ))["output"])
        await asyncio.wait_for(llm.started.wait(), timeout=1)
        await registry.execute(
            "team_send",
            {
                "team_id": team["id"],
                "to": "worker",
                "message": "stop after this call",
                "kind": "shutdown_request",
            },
            task_id=parent_task["id"],
        )
        llm.release.set()
        polled = json.loads((await registry.execute(
            "delegate_poll",
            {"process_id": spawned["process"]["process_id"], "wait_ms": 2000},
            task_id=parent_task["id"],
        ))["output"])
        assert polled["worker"]["status"] == "completed"
        assert polled["result"]["result"] == "in-flight report"
        assert llm.calls == 1
        team_state = AgentTeamStore(store.path).get_team(team["id"])
        agent = next(
            item for item in team_state["agents"]
            if item["id"] == spawned["agent"]["id"]
        )
        assert agent["unread_count"] == 0
        assert agent["keep_alive_state"] == "pending"
        assert not any(event.get("event") == "team_agent_idle" for event in events)

    asyncio.run(scenario())


def test_keep_alive_idle_receive_failure_terminalizes_agent(tmp_path: Path, monkeypatch):
    async def scenario():
        manager = ProcessManager(artifact_dir=tmp_path / "processes")
        monkeypatch.setattr(delegate, "_sub_processes", manager)
        original_receive = AgentTeamRuntime.receive_deliveries_async
        calls = 0

        async def fail_after_initial_delivery(self, agent_id: str, *, after_seq: int):
            nonlocal calls
            calls += 1
            if calls > 1:
                raise RuntimeError("mailbox unavailable")
            return await original_receive(self, agent_id, after_seq=after_seq)

        monkeypatch.setattr(
            AgentTeamRuntime,
            "receive_deliveries_async",
            fail_after_initial_delivery,
        )
        store = TaskStore(tmp_path / "tasks.db")
        parent_task = store.start_run("request-a", "fault", session_id="session-a")
        events: list[dict] = []
        registry = ToolRegistry()
        register_delegate_tools(
            registry,
            llm_getter=ImmediateTeamLLM,
            session_id_getter=lambda: "session-a",
            task_store=store,
            on_process_event=events.append,
        )
        team = json.loads((await registry.execute(
            "team",
            {"action": "create", "name": "fault", "goal": "terminal invariant"},
            task_id=parent_task["id"],
        ))["output"])
        spawned = json.loads((await registry.execute(
            "team_spawn",
            {
                "team_id": team["id"],
                "name": "worker",
                "goal": "enter idle",
                "keep_alive": True,
                "timeout": 5,
            },
            task_id=parent_task["id"],
        ))["output"])
        polled = json.loads((await registry.execute(
            "delegate_poll",
            {"process_id": spawned["process"]["process_id"], "wait_ms": 2000},
            task_id=parent_task["id"],
        ))["output"])
        assert polled["worker"]["status"] == "failed"
        assert AgentTeamStore(store.path).get_agent(spawned["agent"]["id"])["status"] == "failed"
        assert len([
            event for event in events
            if event.get("event") == "team_agent_terminal"
            and event.get("agent_id") == spawned["agent"]["id"]
        ]) == 1

    asyncio.run(scenario())


def test_team_stop_still_hard_cancels_keep_alive_worker(tmp_path: Path, monkeypatch):
    async def scenario():
        manager = ProcessManager(artifact_dir=tmp_path / "processes")
        monkeypatch.setattr(delegate, "_sub_processes", manager)
        store = TaskStore(tmp_path / "tasks.db")
        parent_task = store.start_run("request-a", "stop", session_id="session-a")
        registry = ToolRegistry()
        register_delegate_tools(
            registry,
            llm_getter=ImmediateTeamLLM,
            session_id_getter=lambda: "session-a",
            task_store=store,
        )
        team = json.loads((await registry.execute(
            "team",
            {"action": "create", "name": "stop", "goal": "hard cancel"},
            task_id=parent_task["id"],
        ))["output"])
        spawned = json.loads((await registry.execute(
            "team_spawn",
            {
                "team_id": team["id"],
                "name": "worker",
                "goal": "stay idle",
                "keep_alive": True,
                "timeout": 5,
            },
            task_id=parent_task["id"],
        ))["output"])
        team_store = AgentTeamStore(store.path)
        await _wait_for_agent_status(team_store, spawned["agent"]["id"], "idle")
        stopped = json.loads((await registry.execute(
            "team",
            {"action": "stop", "team_id": team["id"]},
            task_id=parent_task["id"],
        ))["output"])
        assert stopped["status"] == "stopped"
        assert team_store.get_agent(spawned["agent"]["id"])["status"] == "cancelled"

    asyncio.run(scenario())


def test_team_tools_spawn_addressable_teammate_and_emit_events(tmp_path: Path, monkeypatch):
    async def scenario():
        manager = ProcessManager(artifact_dir=tmp_path / "processes")
        monkeypatch.setattr(delegate, "_sub_processes", manager)
        store = TaskStore(tmp_path / "tasks.db")
        parent_task = store.start_run("request-a", "test team", session_id="session-a")
        llm = TeamLLM()
        events = []
        registry = ToolRegistry()
        register_delegate_tools(
            registry,
            llm_getter=lambda: llm,
            session_id_getter=lambda: "session-a",
            task_store=store,
            on_process_event=events.append,
        )
        created = await registry.execute(
            "team",
            {"action": "create", "name": "testers", "goal": "inspect"},
            task_id=parent_task["id"],
        )
        team = json.loads(created["output"])
        llm.team_id = team["id"]
        spawned = await registry.execute(
            "team_spawn",
            {
                "team_id": team["id"],
                "name": "researcher",
                "goal": "report one finding",
                "model": "flash",
                "timeout": 5,
            },
            task_id=parent_task["id"],
        )
        handle = json.loads(spawned["output"])
        assert (
            handle["process"]["metadata"]["worker_spec"]["model"]
            == "deepseek-flash"
        )
        process_id = handle["process"]["process_id"]
        polled = await registry.execute(
            "delegate_poll", {"process_id": process_id, "wait_ms": 5000}, task_id=parent_task["id"]
        )
        assert json.loads(polled["output"])["worker"]["status"] == "completed"
        state = json.loads((await registry.execute(
            "team", {"action": "status", "team_id": team["id"]}, task_id=parent_task["id"]
        ))["output"])
        researcher = next(item for item in state["agents"] if item["name"] == "researcher")
        assert researcher["status"] == "completed"
        lead_messages = next(item for item in state["agents"] if item["name"] == "lead")
        assert lead_messages["unread_count"] == 1
        inbox = json.loads((await registry.execute(
            "team_inbox", {"team_id": team["id"]}, task_id=parent_task["id"]
        ))["output"])
        assert inbox["count"] == 1
        assert inbox["messages"][0]["message"] == "Investigation is halfway complete"
        refreshed = json.loads((await registry.execute(
            "team", {"action": "status", "team_id": team["id"]}, task_id=parent_task["id"]
        ))["output"])
        assert next(item for item in refreshed["agents"] if item["name"] == "lead")["unread_count"] == 0
        assert any(event.get("event") == "team_agent_started" for event in events)
        assert any(event.get("event") == "team_message" for event in events)

    asyncio.run(scenario())


def test_team_spawn_validates_before_registering_agent(tmp_path: Path, monkeypatch):
    async def scenario():
        manager = ProcessManager(artifact_dir=tmp_path / "processes")
        monkeypatch.setattr(delegate, "_sub_processes", manager)
        store = TaskStore(tmp_path / "tasks.db")
        parent_task = store.start_run("request-a", "test team", session_id="session-a")
        registry = ToolRegistry()
        register_delegate_tools(
            registry,
            llm_getter=ImmediateTeamLLM,
            session_id_getter=lambda: "session-a",
            task_store=store,
        )
        spawn_schema = registry.get("team_spawn").parameters["properties"]
        assert spawn_schema["max_turns"]["maximum"] == 50
        assert spawn_schema["max_turns"]["default"] == 50
        assert spawn_schema["timeout"]["maximum"] == 1800
        assert spawn_schema["keep_alive"]["type"] == "boolean"
        assert spawn_schema["keep_alive"]["default"] is False
        assert spawn_schema["workspace_root"]["default"] == ""
        description = registry.get("team_spawn").description
        assert "keep_alive" in description
        assert "pending" in description
        assert "active time" in description
        assert registry.get("team_spawn").max_calls_per_turn == 12
        assert registry.get("team_send").max_calls_per_turn == 48
        assert registry.get("team_restart").max_calls_per_turn == 12
        team = json.loads((await registry.execute(
            "team",
            {"action": "create", "name": "validation", "goal": "inspect"},
            task_id=parent_task["id"],
        ))["output"])

        invalid = await registry.execute(
            "team_spawn",
            {
                "team_id": team["id"],
                "name": "researcher",
                "goal": "report one finding",
                "max_turns": 51,
            },
            task_id=parent_task["id"],
        )
        assert "max_turns must be 1-50" in invalid["error"]
        state = AgentTeamStore(store.path).get_team(team["id"])
        assert [agent["name"] for agent in state["agents"]] == ["lead"]

        invalid_tools = await registry.execute(
            "team_spawn",
            {
                "team_id": team["id"],
                "name": "researcher",
                "goal": "report one finding",
                "tools": ["definitely_missing"],
            },
            task_id=parent_task["id"],
        )
        assert "tools are not available" in invalid_tools["error"]
        state = AgentTeamStore(store.path).get_team(team["id"])
        assert [agent["name"] for agent in state["agents"]] == ["lead"]

        spawned = await registry.execute(
            "team_spawn",
            {
                "team_id": team["id"],
                "name": "researcher",
                "goal": "report one finding",
                "max_turns": 2,
                "timeout": 5,
                "keep_alive": True,
            },
            task_id=parent_task["id"],
        )
        assert spawned["error"] == ""
        handle = json.loads(spawned["output"])
        assert handle["agent"]["keep_alive_requested"] is True
        assert handle["agent"]["keep_alive_state"] == "pending"
        await registry.execute(
            "delegate_poll",
            {"process_id": handle["process"]["process_id"], "wait_ms": 5000},
            task_id=parent_task["id"],
        )

    asyncio.run(scenario())


def test_team_spawn_persists_and_prompts_explicit_workspace_root(
    tmp_path: Path,
    monkeypatch,
):
    class WorkspacePromptLLM:
        def __init__(self):
            self.system_prompt = ""

        async def chat(self, **kwargs):
            self.system_prompt = kwargs["messages"][0]["content"]
            return {"content": "workspace confirmed", "tool_calls": []}

    async def scenario():
        repo = tmp_path / "repo"
        workspace = repo / ".worktrees" / "feature"
        workspace.mkdir(parents=True)
        manager = ProcessManager(artifact_dir=tmp_path / "processes")
        monkeypatch.setattr(delegate, "_sub_processes", manager)
        store = TaskStore(tmp_path / "tasks.db")
        parent_task = store.start_run(
            "request-a", "workspace root", session_id="session-a"
        )
        llm = WorkspacePromptLLM()
        registry = ToolRegistry()
        register_delegate_tools(
            registry,
            llm_getter=lambda: llm,
            session_id_getter=lambda: "session-a",
            task_store=store,
            sandbox=LocalSandbox(workdir=str(repo)),
        )
        team = json.loads((await registry.execute(
            "team",
            {"action": "create", "name": "workspace", "goal": "inspect"},
            task_id=parent_task["id"],
        ))["output"])

        outside = tmp_path / "outside"
        outside.mkdir()
        rejected = await registry.execute(
            "team_spawn",
            {
                "team_id": team["id"],
                "name": "outside-reviewer",
                "goal": "must not start",
                "workspace_root": str(outside),
            },
            task_id=parent_task["id"],
        )
        assert "inside the sandbox root" in rejected["error"]
        assert [
            item["name"]
            for item in AgentTeamStore(store.path).get_team(team["id"])["agents"]
        ] == ["lead"]

        spawned = await registry.execute(
            "team_spawn",
            {
                "team_id": team["id"],
                "name": "reviewer",
                "goal": "confirm workspace",
                "workspace_root": str(workspace),
                "max_turns": 2,
                "timeout": 5,
            },
            task_id=parent_task["id"],
        )

        assert spawned["error"] == ""
        handle = json.loads(spawned["output"])
        resolved = str(workspace.resolve())
        assert handle["process"]["metadata"]["worker_spec"]["workspace_root"] == resolved
        persisted = AgentTeamStore(store.path).get_agent(handle["agent"]["id"])
        assert persisted["spawn_spec"]["workspace_root"] == resolved
        status = json.loads((await registry.execute(
            "team",
            {"action": "status", "team_id": team["id"]},
            task_id=parent_task["id"],
        ))["output"])
        reported = next(
            item for item in status["agents"] if item["id"] == handle["agent"]["id"]
        )
        assert reported["workspace_root"] == resolved
        await registry.execute(
            "delegate_poll",
            {"process_id": handle["process"]["process_id"], "wait_ms": 5000},
            task_id=parent_task["id"],
        )
        assert f"Native workspace root: {resolved}" in llm.system_prompt

    asyncio.run(scenario())


def test_team_spawn_discards_unbound_agent_after_start_failure(
    tmp_path: Path,
    monkeypatch,
):
    async def scenario():
        manager = ProcessManager(artifact_dir=tmp_path / "processes")
        monkeypatch.setattr(delegate, "_sub_processes", manager)
        store = TaskStore(tmp_path / "tasks.db")
        parent_task = store.start_run("request-a", "test team", session_id="session-a")
        registry = ToolRegistry()
        register_delegate_tools(
            registry,
            llm_getter=ImmediateTeamLLM,
            session_id_getter=lambda: "session-a",
            task_store=store,
        )
        team = json.loads((await registry.execute(
            "team",
            {"action": "create", "name": "cleanup", "goal": "inspect"},
            task_id=parent_task["id"],
        ))["output"])
        original_start = manager.start

        def fail_start(*args, **kwargs):
            del args, kwargs
            raise RuntimeError("process capacity unavailable")

        monkeypatch.setattr(manager, "start", fail_start)
        failed = await registry.execute(
            "team_spawn",
            {
                "team_id": team["id"],
                "name": "researcher",
                "goal": "report one finding",
                "max_turns": 2,
            },
            task_id=parent_task["id"],
        )
        assert "process capacity unavailable" in failed["error"]
        state = AgentTeamStore(store.path).get_team(team["id"])
        assert [agent["name"] for agent in state["agents"]] == ["lead"]

        monkeypatch.setattr(manager, "start", original_start)
        retried = await registry.execute(
            "team_spawn",
            {
                "team_id": team["id"],
                "name": "researcher",
                "goal": "report one finding",
                "max_turns": 2,
                "timeout": 5,
            },
            task_id=parent_task["id"],
        )
        assert retried["error"] == ""
        handle = json.loads(retried["output"])
        await registry.execute(
            "delegate_poll",
            {"process_id": handle["process"]["process_id"], "wait_ms": 5000},
            task_id=parent_task["id"],
        )

    asyncio.run(scenario())


def test_team_restart_seeds_new_worker_from_durable_checkpoint(tmp_path: Path, monkeypatch):
    async def scenario():
        repo = tmp_path / "repo"
        workspace = repo / ".worktrees" / "feature"
        workspace.mkdir(parents=True)
        manager = ProcessManager(artifact_dir=tmp_path / "processes")
        monkeypatch.setattr(delegate, "_sub_processes", manager)
        store = TaskStore(tmp_path / "tasks.db")
        parent_task = store.start_run("request-a", "test restart", session_id="session-a")
        llm = CapturingTeamLLM()
        session_store = SessionStore(tmp_path / "session.json")
        registry = ToolRegistry()
        register_delegate_tools(
            registry,
            llm_getter=lambda: llm,
            session_id_getter=lambda: "session-a",
            task_store=store,
            on_session_event=lambda _session_id, event: session_store.append_subagent_event(event),
            sandbox=LocalSandbox(workdir=str(repo)),
        )
        team = json.loads((await registry.execute(
            "team",
            {"action": "create", "name": "restart", "goal": "inspect"},
            task_id=parent_task["id"],
        ))["output"])
        spawned = json.loads((await registry.execute(
            "team_spawn",
            {
                "team_id": team["id"],
                "name": "researcher",
                "goal": "report one finding",
                "context": "Original bounded context",
                "workspace_root": str(workspace),
                "max_turns": 2,
                "timeout": 5,
            },
            task_id=parent_task["id"],
        ))["output"])
        first_process_id = spawned["process"]["process_id"]
        await registry.execute(
            "delegate_poll",
            {"process_id": first_process_id, "wait_ms": 5000},
            task_id=parent_task["id"],
        )
        team_store = AgentTeamStore(store.path)
        first_agent = next(
            item for item in team_store.get_team(team["id"])["agents"]
            if item["name"] == "researcher"
        )
        assert first_agent["transcript_path"] == str(session_store.subagent_path)
        team_store.set_agent_status(first_agent["id"], "interrupted")

        restarted_result = await registry.execute(
            "team_restart",
            {
                "team_id": team["id"],
                "agent": "researcher",
                "instruction": "Focus on the unresolved failure path",
                "max_turns": 3,
                "timeout": 5,
            },
            task_id=parent_task["id"],
        )
        assert restarted_result["error"] == ""
        restarted = json.loads(restarted_result["output"])
        assert restarted["restart_kind"] == "checkpoint_restart"
        assert restarted["continuation"] is False
        assert restarted["agent"]["id"] == first_agent["id"]
        assert restarted["agent"]["restart_count"] == 1
        assert restarted["agent"]["previous_process_id"] == first_process_id
        assert restarted["process"]["process_id"] != first_process_id
        assert (
            restarted["process"]["metadata"]["worker_spec"]["workspace_root"]
            == str(workspace.resolve())
        )

        await registry.execute(
            "delegate_poll",
            {"process_id": restarted["process"]["process_id"], "wait_ms": 5000},
            task_id=parent_task["id"],
        )
        prompt_text = json.dumps(llm.prompts, ensure_ascii=False)
        assert "CHECKPOINT RESTART" in prompt_text
        assert "Focus on the unresolved failure path" in prompt_text
        assert "Checkpoint worker completed" in prompt_text

    asyncio.run(scenario())


def test_team_restart_reapplies_keep_alive_for_retained_member(tmp_path: Path, monkeypatch):
    async def scenario():
        manager = ProcessManager(artifact_dir=tmp_path / "processes")
        monkeypatch.setattr(delegate, "_sub_processes", manager)
        store = TaskStore(tmp_path / "tasks.db")
        parent_task = store.start_run("request-a", "restart retained", session_id="session-a")
        llm = EpisodicTeamLLM()
        registry = ToolRegistry()
        register_delegate_tools(
            registry,
            llm_getter=lambda: llm,
            session_id_getter=lambda: "session-a",
            task_store=store,
        )
        team = json.loads((await registry.execute(
            "team",
            {"action": "create", "name": "retained-restart", "goal": "survive a host restart"},
            task_id=parent_task["id"],
        ))["output"])
        spawned = json.loads((await registry.execute(
            "team_spawn",
            {
                "team_id": team["id"],
                "name": "implementer",
                "goal": "run red then green",
                "max_turns": 4,
                "timeout": 10,
                "keep_alive": True,
            },
            task_id=parent_task["id"],
        ))["output"])
        agent_id = spawned["agent"]["id"]
        team_store = AgentTeamStore(store.path)
        first_idle = await _wait_for_agent_status(team_store, agent_id, "idle")
        assert first_idle["keep_alive_state"] == "effective"
        assert len(llm.requests) == 1

        # A host restart kills the live process; recovery fences the member.
        await manager.cancel(manager.get(spawned["process"]["process_id"]))
        team_store.set_agent_status(agent_id, "interrupted")

        restarted_result = await registry.execute(
            "team_restart",
            {
                "team_id": team["id"],
                "agent": "implementer",
                "instruction": "Resume the retained assignment",
                "max_turns": 4,
                "timeout": 10,
            },
            task_id=parent_task["id"],
        )
        assert restarted_result["error"] == ""
        restarted = json.loads(restarted_result["output"])
        assert restarted["process"]["metadata"]["worker_spec"]["keep_alive"] is True

        revived = await _wait_for_agent_status(team_store, agent_id, "idle", timeout=5.0)
        assert revived["keep_alive_state"] == "effective"
        assert len(llm.requests) == 2

        await registry.execute(
            "team_send",
            {
                "team_id": team["id"],
                "to": "implementer",
                "message": "second assignment",
            },
            task_id=parent_task["id"],
        )
        deadline = asyncio.get_running_loop().time() + 3.0
        while asyncio.get_running_loop().time() < deadline:
            current = team_store.get_agent(agent_id) or {}
            if len(llm.requests) == 3 and current.get("status") == "idle":
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError(
                f"revived member did not wake: {team_store.get_agent(agent_id)}"
            )

        await registry.execute(
            "team_send",
            {
                "team_id": team["id"],
                "to": "implementer",
                "message": "stop",
                "kind": "shutdown_request",
            },
            task_id=parent_task["id"],
        )
        await _wait_for_agent_status(team_store, agent_id, "completed", timeout=3.0)
        assert len(llm.requests) == 3

    asyncio.run(scenario())


def test_team_restart_accepts_completed_member(tmp_path: Path, monkeypatch):
    async def scenario():
        manager = ProcessManager(artifact_dir=tmp_path / "processes")
        monkeypatch.setattr(delegate, "_sub_processes", manager)
        store = TaskStore(tmp_path / "tasks.db")
        parent_task = store.start_run("request-a", "restart completed", session_id="session-a")
        llm = CapturingTeamLLM()
        registry = ToolRegistry()
        register_delegate_tools(
            registry,
            llm_getter=lambda: llm,
            session_id_getter=lambda: "session-a",
            task_store=store,
        )
        team = json.loads((await registry.execute(
            "team",
            {"action": "create", "name": "recall", "goal": "revive a finished member"},
            task_id=parent_task["id"],
        ))["output"])
        spawned = json.loads((await registry.execute(
            "team_spawn",
            {
                "team_id": team["id"],
                "name": "researcher",
                "goal": "report one finding",
                "max_turns": 2,
                "timeout": 5,
            },
            task_id=parent_task["id"],
        ))["output"])
        agent_id = spawned["agent"]["id"]
        await registry.execute(
            "delegate_poll",
            {"process_id": spawned["process"]["process_id"], "wait_ms": 5000},
            task_id=parent_task["id"],
        )
        team_store = AgentTeamStore(store.path)
        await _wait_for_agent_status(team_store, agent_id, "completed")

        restarted_result = await registry.execute(
            "team_restart",
            {
                "team_id": team["id"],
                "agent": "researcher",
                "instruction": "Recheck the finding with fresh evidence",
                "max_turns": 2,
                "timeout": 5,
            },
            task_id=parent_task["id"],
        )
        assert restarted_result["error"] == ""
        restarted = json.loads(restarted_result["output"])
        assert restarted["restart_kind"] == "checkpoint_restart"
        assert restarted["agent"]["restart_count"] == 1
        assert restarted["agent"]["previous_process_id"] == spawned["process"]["process_id"]

        await registry.execute(
            "delegate_poll",
            {"process_id": restarted["process"]["process_id"], "wait_ms": 5000},
            task_id=parent_task["id"],
        )
        prompt_text = json.dumps(llm.prompts, ensure_ascii=False)
        assert "CHECKPOINT RESTART" in prompt_text
        assert "Recheck the finding with fresh evidence" in prompt_text

    asyncio.run(scenario())


def _team_tools(tmp_path: Path, monkeypatch, llm, *, session_store: SessionStore | None = None):
    """A real registry with the delegate/Team tools and one running parent task."""

    manager = ProcessManager(artifact_dir=tmp_path / "processes")
    monkeypatch.setattr(delegate, "_sub_processes", manager)
    store = TaskStore(tmp_path / "tasks.db")
    parent_task = store.start_run("request-a", "team tools", session_id="session-a")
    registry = ToolRegistry()
    register_delegate_tools(
        registry,
        llm_getter=lambda: llm,
        session_id_getter=lambda: "session-a",
        task_store=store,
        on_session_event=(
            (lambda _session_id, event: session_store.append_subagent_event(event))
            if session_store is not None
            else None
        ),
    )

    async def run(tool: str, /, **args) -> dict:
        return await registry.execute(tool, args, task_id=parent_task["id"])

    async def call(tool: str, /, **args) -> dict:
        result = await run(tool, **args)
        assert result["error"] == "", result["error"]
        return json.loads(result["output"])

    return manager, AgentTeamStore(store.path), run, call


def test_team_wait_reports_a_change_made_between_waits(tmp_path: Path, monkeypatch):
    async def scenario():
        _, team_store, _, call = _team_tools(tmp_path, monkeypatch, ImmediateTeamLLM())
        loop = asyncio.get_running_loop()
        team = await call("team", action="create", name="waiters", goal="observe")
        # A retained idle teammate keeps polling the same Team signal.
        retained = await call(
            "team_spawn", team_id=team["id"], name="retained", goal="stay available",
            max_turns=4, timeout=10, keep_alive=True,
        )
        await _wait_for_agent_status(team_store, retained["agent"]["id"], "idle")
        await call("team_wait", team_id=team["id"])

        finisher = await call(
            "team_spawn", team_id=team["id"], name="finisher", goal="finish quickly",
            max_turns=2, timeout=10,
        )
        await _wait_for_agent_status(team_store, finisher["agent"]["id"], "completed")
        await asyncio.sleep(0.3)  # the lead is busy elsewhere while the change sits there

        started = loop.time()
        state = await call("team_wait", team_id=team["id"], timeout_ms=5000)
        waited = loop.time() - started

        assert waited < 2.0
        by_name = {agent["name"]: agent for agent in state["agents"]}
        assert by_name["finisher"]["status"] == "completed"

        # That change is now reported; the lead's own action is not news to it.
        await call("team_task", action="create", team_id=team["id"], title="own change")
        started = loop.time()
        await call("team_wait", team_id=team["id"], timeout_ms=400)
        assert loop.time() - started >= 0.3

        started = loop.time()
        await call("team_wait", team_id=team["id"])
        assert loop.time() - started < 0.3
        await call("team", action="stop", team_id=team["id"])

    asyncio.run(scenario())


def test_team_spawn_rejects_context_over_limit_before_registering_agent(
    tmp_path: Path, monkeypatch,
):
    async def scenario():
        manager, team_store, run, call = _team_tools(tmp_path, monkeypatch, ImmediateTeamLLM())
        team = await call("team", action="create", name="briefing", goal="inspect")
        long_context = "x" * (delegate._DEFAULT_CONTEXT_CHARS + 1)

        rejected = await run(
            "team_spawn", team_id=team["id"], name="researcher", goal="report one finding",
            context=long_context,
        )

        assert f"context is {len(long_context)} characters" in rejected["error"]
        assert f"limit is {delegate._DEFAULT_CONTEXT_CHARS}" in rejected["error"]
        assert [agent["name"] for agent in team_store.get_team(team["id"])["agents"]] == ["lead"]
        assert manager.list() == []

    asyncio.run(scenario())


def test_team_restart_checkpoint_delivers_instruction_and_context_whole(
    tmp_path: Path, monkeypatch,
):
    async def scenario():
        llm = CapturingTeamLLM()
        session_store = SessionStore(tmp_path / "session.json")
        _, team_store, run, call = _team_tools(
            tmp_path, monkeypatch, llm, session_store=session_store,
        )
        team = await call("team", action="create", name="restart", goal="inspect")
        original_context = "ORIGINAL-START " + "c" * 2000 + " ORIGINAL-END"
        spawned = await call(
            "team_spawn", team_id=team["id"], name="researcher", goal="report one finding",
            context=original_context, max_turns=2, timeout=5,
        )
        agent_id = spawned["agent"]["id"]
        first_process_id = spawned["process"]["process_id"]
        await call("delegate_poll", process_id=first_process_id, wait_ms=5000)
        # A long prior transcript used to push the new instruction out of the seed.
        for index in range(12):
            session_store.append_subagent_event({
                "process_id": first_process_id, "type": "tool", "turn": index + 1,
                "output": f"evidence-{index} " + "t" * 1100,
            })
        team_store.set_agent_status(agent_id, "interrupted")
        instruction = "INSTRUCTION-START " + "i" * 2500 + " INSTRUCTION-END"

        restarted = await call(
            "team_restart", team_id=team["id"], agent="researcher", instruction=instruction,
            max_turns=3, timeout=5,
        )
        assert restarted["restart_kind"] == "checkpoint_restart"
        await call("delegate_poll", process_id=restarted["process"]["process_id"], wait_ms=5000)

        seed = llm.prompts[-1][1]["content"]
        assert instruction in seed
        assert original_context in seed
        # The transcript gives way instead: its end is kept, its beginning is not.
        assert "evidence-11 " in seed
        assert "evidence-0 " not in seed

        # An instruction with no room in the checkpoint is refused, not cut.
        await _wait_for_agent_status(team_store, agent_id, "completed")
        too_long = "i" * delegate._DEFAULT_CONTEXT_CHARS
        prompts_before = len(llm.prompts)
        rejected = await run(
            "team_restart", team_id=team["id"], agent="researcher", instruction=too_long,
        )
        assert f"instruction is {len(too_long)} characters" in rejected["error"]
        after = team_store.get_agent(agent_id)
        assert after["status"] == "completed"
        assert after["restart_count"] == 1
        assert len(llm.prompts) == prompts_before

    asyncio.run(scenario())


def test_team_send_to_ended_teammate_names_team_restart(tmp_path: Path, monkeypatch):
    async def scenario():
        _, team_store, run, call = _team_tools(tmp_path, monkeypatch, ImmediateTeamLLM())
        team = await call("team", action="create", name="recall", goal="inspect")
        spawned = await call(
            "team_spawn", team_id=team["id"], name="researcher", goal="report one finding",
            max_turns=2, timeout=5,
        )
        await _wait_for_agent_status(team_store, spawned["agent"]["id"], "completed")

        refused = await run(
            "team_send", team_id=team["id"], to="researcher", message="one more question",
        )

        assert "researcher is completed" in refused["error"]
        assert "team_restart" in refused["error"]
        # The named next step is one the runtime accepts for this teammate.
        restarted = await call(
            "team_restart", team_id=team["id"], agent="researcher",
            instruction="one more question", timeout=5,
        )
        await call("delegate_poll", process_id=restarted["process"]["process_id"], wait_ms=5000)

    asyncio.run(scenario())


def test_team_inbox_says_when_more_messages_remain(tmp_path: Path, monkeypatch):
    async def scenario():
        _, team_store, run, call = _team_tools(tmp_path, monkeypatch, ImmediateTeamLLM())
        team = await call("team", action="create", name="mail", goal="inspect")
        helper = _agent(team_store, team, "helper")

        def send(count: int, label: str) -> None:
            for index in range(count):
                team_store.send_message(
                    team["id"], helper["id"], team["lead_agent_id"], body=f"{label}-{index}"
                )

        send(TEAM_INBOX_PAGE_SIZE, "old")
        first = await run("team_inbox", team_id=team["id"])
        first_page = json.loads(first["output"])
        assert first_page["count"] == TEAM_INBOX_PAGE_SIZE
        assert first_page["has_more"] is False
        assert not first.get("partial")

        send(3, "new")
        unread = await run("team_inbox", team_id=team["id"])
        unread_page = json.loads(unread["output"])
        # The first page was acknowledged, so the default read does not return it again.
        assert [item["message"] for item in unread_page["messages"]] == ["new-0", "new-1", "new-2"]
        assert unread_page["has_more"] is False
        assert not unread.get("partial")

        empty = json.loads((await run("team_inbox", team_id=team["id"]))["output"])
        assert empty["count"] == 0 and empty["next_seq"] == unread_page["next_seq"]

        # An explicit cursor reads acknowledged messages again, one page at a time.
        again = await run("team_inbox", team_id=team["id"], after_seq=0)
        again_page = json.loads(again["output"])
        assert again_page["messages"][0]["message"] == "old-0"
        assert again_page["has_more"] is True
        assert again["partial"] is True
        assert f"after_seq={again_page['next_seq']}" in again_page["note"]

        rest = await run("team_inbox", team_id=team["id"], after_seq=again_page["next_seq"])
        rest_page = json.loads(rest["output"])
        assert [item["message"] for item in rest_page["messages"]] == ["new-0", "new-1", "new-2"]
        assert rest_page["has_more"] is False
        assert not rest.get("partial")

        # Unacknowledged reads stay unread and come back on the next default read.
        send(1, "kept")
        for _ in range(2):
            kept = json.loads((await run("team_inbox", team_id=team["id"], acknowledge=False))["output"])
            assert [item["message"] for item in kept["messages"]] == ["kept-0"]

    asyncio.run(scenario())


def test_team_task_update_keeps_result_unless_given_and_names_what_is_missing(
    tmp_path: Path, monkeypatch,
):
    async def scenario():
        _, team_store, run, call = _team_tools(tmp_path, monkeypatch, ImmediateTeamLLM())
        team = await call("team", action="create", name="board", goal="inspect")
        task = await call("team_task", action="create", team_id=team["id"], title="collect evidence")

        async def update(**args) -> dict:
            return await call(
                "team_task", action="update", team_id=team["id"], team_task_id=task["id"], **args
            )

        await call("team_task", action="claim", team_id=team["id"], team_task_id=task["id"])
        assert (await update(status="completed", result="three findings"))["result"] == "three findings"
        # A status-only update no longer blanks what was stored.
        assert (await update(status="pending"))["result"] == "three findings"
        assert (await update(status="completed", result="four findings"))["result"] == "four findings"
        assert (await update(status="completed", result=""))["result"] == ""

        no_status = await run(
            "team_task", action="update", team_id=team["id"], team_task_id=task["id"],
            result="late note",
        )
        assert "update requires status" in no_status["error"]
        no_id = await run("team_task", action="claim", team_id=team["id"])
        assert "claim requires team_task_id" in no_id["error"]
        assert team_store.get_task(task["id"])["result"] == ""

    asyncio.run(scenario())


def test_team_claim_target_and_cross_turn_resume(tmp_path: Path):
    async def scenario():
        store = TaskStore(tmp_path / "tasks.db")
        first_run = store.start_run("request-1", "first", session_id="session-a")
        active_session = "session-a"
        registry = ToolRegistry()
        events = []
        register_delegate_tools(
            registry,
            llm_getter=lambda: None,
            session_id_getter=lambda: active_session,
            task_store=store,
            on_process_event=events.append,
        )
        created = await registry.execute(
            "team",
            {"action": "create", "name": "durable", "goal": "survive turns"},
            task_id=first_run["id"],
        )
        team = json.loads(created["output"])
        helper = AgentTeamStore(store.path).register_agent(
            team["id"],
            name="helper",
            role="worker",
            mode="worker",
            parent_agent_id=team["lead_agent_id"],
        )
        created_task = await registry.execute(
            "team_task",
            {"action": "create", "team_id": team["id"], "title": "recover me"},
            task_id=first_run["id"],
        )
        team_task = json.loads(created_task["output"])

        lead_claim = await registry.execute(
            "team_task",
            {
                "action": "claim",
                "team_id": team["id"],
                "team_task_id": team_task["id"],
                "agent": team["lead_agent_id"],
            },
            task_id=first_run["id"],
        )
        assert json.loads(lead_claim["output"])["owner_agent_id"] == team["lead_agent_id"]

        second_task = json.loads((await registry.execute(
            "team_task",
            {"action": "create", "team_id": team["id"], "title": "helper work"},
            task_id=first_run["id"],
        ))["output"])
        helper_claim = await registry.execute(
            "team_task",
            {
                "action": "claim",
                "team_id": team["id"],
                "team_task_id": second_task["id"],
                "agent": "helper",
            },
            task_id=first_run["id"],
        )
        assert json.loads(helper_claim["output"])["owner_agent_id"] == helper["id"]
        AgentTeamStore(store.path).send_message(
            team["id"], helper["id"], team["lead_agent_id"], body="durable note"
        )
        store.finish_run(first_run["id"], "completed")

        second_run = store.start_run("request-2", "second", session_id="session-a")
        inspected = await registry.execute(
            "team", {"action": "status", "team_id": team["id"]}, task_id=second_run["id"]
        )
        assert not inspected.get("error"), inspected
        assert json.loads(inspected["output"])["owner_task_id"] == first_run["id"]
        listed = json.loads((await registry.execute(
            "team", {"action": "list"}, task_id=second_run["id"]
        ))["output"])
        assert team["id"] in {item["id"] for item in listed}

        resumed = json.loads((await registry.execute(
            "team", {"action": "resume", "team_id": team["id"]}, task_id=second_run["id"]
        ))["output"])
        assert resumed["owner_task_id"] == second_run["id"]
        assert resumed["resumed_from_task_id"] == first_run["id"]
        assert next(item for item in resumed["agents"] if item["name"] == "lead")["status"] == "running"
        # There is no corresponding live process for this persisted helper.
        assert next(item for item in resumed["agents"] if item["id"] == helper["id"])["status"] == "interrupted"
        recovered_task = next(item for item in resumed["tasks"] if item["id"] == second_task["id"])
        assert recovered_task["lease_until"] is None

        inbox = json.loads((await registry.execute(
            "team_inbox", {"team_id": team["id"]}, task_id=second_run["id"]
        ))["output"])
        assert [item["message"] for item in inbox["messages"]] == ["durable note"]

        competing_run = store.start_run("request-3", "third", session_id="session-a")
        owner_active = await registry.execute(
            "team", {"action": "resume", "team_id": team["id"]}, task_id=competing_run["id"]
        )
        assert "previous Agent Team owner task is still active" in owner_active["error"]

        active_session = "session-b"
        foreign_run = store.start_run("request-4", "foreign", session_id="session-b")
        foreign_list = json.loads((await registry.execute(
            "team", {"action": "list"}, task_id=foreign_run["id"]
        ))["output"])
        assert team["id"] not in {item["id"] for item in foreign_list}
        foreign_resume = await registry.execute(
            "team", {"action": "resume", "team_id": team["id"]}, task_id=foreign_run["id"]
        )
        assert "belongs to another session" in foreign_resume["error"]
        assert any(event.get("event") == "team_resumed" for event in events)

    asyncio.run(scenario())
