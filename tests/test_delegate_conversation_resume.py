"""Acceptance checks for durable worker continuations and crash boundaries."""

import asyncio
import copy
import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from agent.runtime.agent_team import AgentTeamStore
from agent.runtime.session_store import SessionStore
from agent.runtime.task_store import TaskStore
from agent.runtime.tools import delegate
from agent.runtime.tools.delegate import register_delegate_tools
from agent.runtime.tools.processes import ProcessManager
from agent.runtime.tools.registry import ToolDef, ToolRegistry


class RecordingLLM:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.requests = []
        self.before_request = None

    async def chat(self, **kwargs):
        self.requests.append(copy.deepcopy(kwargs["messages"]))
        if self.before_request is not None:
            self.before_request(kwargs)
        if self.responses:
            return self.responses.pop(0)
        return {"content": f"Report {len(self.requests)}", "tool_calls": []}


def _calls(*names):
    return {
        "content": "",
        "tool_calls": [
            {"id": f"call-{index}", "name": name, "arguments": "{}"}
            for index, name in enumerate(names)
        ],
    }


class Harness:
    def __init__(self, root, monkeypatch, llm, *, parent=None, registry=None, persist=True):
        self.root = root
        self.manager = ProcessManager(artifact_dir=root / "processes")
        monkeypatch.setattr(delegate, "_sub_processes", self.manager)
        self.tasks = TaskStore(root / "tasks.db")
        self.parent = parent or self.tasks.start_run(
            "request-a", "conversation acceptance", session_id="session-a"
        )["id"]
        self.llm = llm
        self.registry = registry or ToolRegistry()
        self.registry.yolo = True
        self.session = SessionStore(root / "session.json")
        self.keys = []

        def conv_store_for(session_id, key):
            assert session_id == "session-a"
            self.keys.append(key)
            return SessionStore.for_subagent(self.session.legacy_path, key)

        register_delegate_tools(
            self.registry,
            llm_getter=lambda: self.llm,
            session_id_getter=lambda: "session-a",
            task_store=self.tasks,
            on_session_event=lambda _sid, event: self.session.append_subagent_event(event),
            conv_store_for=conv_store_for if persist else None,
        )
        self.teams = AgentTeamStore(self.tasks.path)

    async def call(self, tool_name, **args):
        result = await self.registry.execute(tool_name, args, task_id=self.parent)
        assert not result["error"], result
        return json.loads(result["output"])

    async def spawn(self, *, keep_alive=False, mode="explorer", max_turns=8):
        team = await self.call("team", action="create", name="resume", goal="recover")
        spawned = await self.call(
            "team_spawn", team_id=team["id"], name="worker", goal="Keep the evidence",
            context="Original task context", keep_alive=keep_alive, mode=mode,
            max_turns=max_turns, timeout=30,
        )
        return team, spawned

    async def wait(self, process_id):
        process = self.manager.get(process_id)
        await asyncio.wait_for(asyncio.shield(process.task), timeout=5)
        return process.task.result()

    async def idle(self, agent_id, *, calls=1):
        deadline = asyncio.get_running_loop().time() + 5
        while asyncio.get_running_loop().time() < deadline:
            agent = self.teams.get_agent(agent_id) or {}
            if agent.get("status") == "idle" and len(self.llm.requests) >= calls:
                return agent
            await asyncio.sleep(0.01)
        raise AssertionError(f"Worker did not idle: {self.teams.get_agent(agent_id)}")

    async def cancel(self, process_id):
        await self.manager.cancel(self.manager.get(process_id))


def _load_closed(path):
    from agent.runtime.subagent_conversation import SubagentConversation

    conversation = SubagentConversation(SessionStore(path))
    conversation.acquire()
    try:
        return conversation.load()
    finally:
        conversation.close()


def _register_tool(registry, name, fn, risk="read"):
    registry.register(ToolDef(
        name=name, description="Recovery acceptance probe",
        parameters={"type": "object", "properties": {}}, fn=fn, risk=risk,
    ))


def test_explicit_total_turn_ceiling_survives_the_next_default_restart(tmp_path, monkeypatch):
    async def scenario():
        harness = Harness(tmp_path, monkeypatch, RecordingLLM())
        team, spawned = await harness.spawn(max_turns=2)
        await harness.wait(spawned["process"]["process_id"])
        first = await harness.call("team_restart", team_id=team["id"], agent="worker", max_turns=4)
        await harness.wait(first["process"]["process_id"])
        second = await harness.call("team_restart", team_id=team["id"], agent="worker")
        await harness.wait(second["process"]["process_id"])
        agent = harness.teams.get_agent(spawned["agent"]["id"])
        state = _load_closed(agent["conv_path"])["state"]
        assert state["max_turns"] == 4
        assert state["turns_used"] == 3
    asyncio.run(scenario())


def test_final_report_is_durable_before_idle_and_restart_instruction_reaches_model(tmp_path, monkeypatch):
    async def scenario():
        llm = RecordingLLM()
        harness = Harness(tmp_path, monkeypatch, llm)
        team, spawned = await harness.spawn(keep_alive=True)
        agent_id = spawned["agent"]["id"]
        process_id = spawned["process"]["process_id"]
        first = await harness.idle(agent_id)
        assert first["conv_path"]
        persisted = SessionStore(first["conv_path"]).load(readonly=True)["messages"]
        assert any(m.get("content") == "Report 1" for m in persisted)
        await harness.cancel(process_id)
        restarted = await harness.call(
            "team_restart", team_id=team["id"], agent="worker",
            instruction="Inspect the remaining failure before editing",
        )
        try:
            assert restarted["restart_kind"] == "conversation_resume"
            assert restarted["continuation"] is True
            await harness.idle(agent_id, calls=2)
            messages = llm.requests[1]
            assert any(m.get("content") == "Report 1" for m in messages)
            assert any(m.get("role") == "user" and
                       "Inspect the remaining failure before editing" in str(m.get("content"))
                       for m in messages)
            assert "CHECKPOINT RESTART" not in json.dumps(messages)
        finally:
            await harness.cancel(restarted["process"]["process_id"])

    asyncio.run(scenario())


def test_resumed_conversation_takes_an_instruction_longer_than_the_context_limit(tmp_path, monkeypatch):
    async def scenario():
        llm = RecordingLLM()
        harness = Harness(tmp_path, monkeypatch, llm)
        team, spawned = await harness.spawn()
        await harness.wait(spawned["process"]["process_id"])
        # The limit bounds a new worker's seed. A resumed conversation receives
        # the instruction as its next message, so nothing is cut or refused.
        instruction = "LONG-START " + "n" * delegate._DEFAULT_CONTEXT_CHARS + " LONG-END"

        restarted = await harness.call(
            "team_restart", team_id=team["id"], agent="worker", instruction=instruction,
        )

        assert restarted["restart_kind"] == "conversation_resume"
        await harness.wait(restarted["process"]["process_id"])
        assert any(
            m.get("role") == "user" and m.get("content") == instruction
            for m in llm.requests[-1]
        )

    asyncio.run(scenario())


def test_batch_delegates_with_same_parent_have_distinct_canonical_histories(tmp_path, monkeypatch):
    async def scenario():
        harness = Harness(tmp_path, monkeypatch, RecordingLLM())
        result = await harness.call(
            "delegate_task", tasks=[{"goal": "Find ALPHA"}, {"goal": "Find BETA"}],
            max_turns=3, timeout=30,
        )
        assert result["count"] == 2
        keys = set(harness.keys)
        assert len(keys) == 2
        assert harness.parent not in keys
        texts = [json.dumps(SessionStore.for_subagent(harness.session.legacy_path, key)
                            .load(readonly=True)["messages"]) for key in keys]
        assert sum("Find ALPHA" in text for text in texts) == 1
        assert sum("Find BETA" in text for text in texts) == 1
        assert all(not ("Find ALPHA" in text and "Find BETA" in text) for text in texts)

    asyncio.run(scenario())


def test_two_restarts_count_cumulative_turns_without_replaying_mailbox(tmp_path, monkeypatch):
    async def scenario():
        llm = RecordingLLM()
        harness = Harness(tmp_path, monkeypatch, llm)
        team, spawned = await harness.spawn(keep_alive=True, max_turns=8)
        agent_id = spawned["agent"]["id"]
        await harness.idle(agent_id)
        await harness.call("team_send", team_id=team["id"], to="worker",
                           message="Unique assignment DELIVERY-123")
        await harness.idle(agent_id, calls=2)
        await harness.cancel(spawned["process"]["process_id"])
        agent = harness.teams.get_agent(agent_id)
        assert _load_closed(agent["conv_path"])["state"]["turns_used"] == 2
        for expected in (3, 4):
            restarted = await harness.call(
                "team_restart", team_id=team["id"], agent="worker", instruction=f"Continue {expected}",
            )
            await harness.idle(agent_id, calls=expected)
            await harness.cancel(restarted["process"]["process_id"])
            payload = _load_closed(agent["conv_path"])
            assert payload["state"]["turns_used"] == expected
            assert sum("Unique assignment DELIVERY-123" in str(m.get("content"))
                       for m in llm.requests[-1]) == 1
            assert payload["state"]["team_message_cursor"] > 0

    asyncio.run(scenario())


def test_cumulative_turn_limit_refuses_another_restart(tmp_path, monkeypatch):
    async def scenario():
        llm = RecordingLLM()
        harness = Harness(tmp_path, monkeypatch, llm)
        team, spawned = await harness.spawn(max_turns=2)
        await harness.wait(spawned["process"]["process_id"])
        restarted = await harness.call("team_restart", team_id=team["id"], agent="worker")
        await harness.wait(restarted["process"]["process_id"])
        agent = harness.teams.get_agent(spawned["agent"]["id"])
        assert _load_closed(agent["conv_path"])["state"]["turns_used"] == 2
        refused = await harness.registry.execute(
            "team_restart", {"team_id": team["id"], "agent": "worker"}, task_id=harness.parent,
        )
        assert refused["error"]
        assert len(llm.requests) == 2
        assert harness.teams.get_agent(agent["id"])["restart_count"] == 1

    asyncio.run(scenario())


def test_live_worker_cannot_restart_even_if_durable_status_is_interrupted(tmp_path, monkeypatch):
    async def scenario():
        llm = RecordingLLM()
        harness = Harness(tmp_path, monkeypatch, llm)
        team, spawned = await harness.spawn(keep_alive=True)
        agent_id = spawned["agent"]["id"]
        process_id = spawned["process"]["process_id"]
        await harness.idle(agent_id)
        harness.teams.set_agent_status(agent_id, "interrupted")
        try:
            refused = await harness.registry.execute(
                "team_restart", {"team_id": team["id"], "agent": "worker"}, task_id=harness.parent,
            )
            assert refused["error"]
            assert not harness.manager.get(process_id).task.done()
            assert len(llm.requests) == 1
            assert harness.teams.get_agent(agent_id)["restart_count"] == 0
        finally:
            await harness.cancel(process_id)

    asyncio.run(scenario())


def test_legacy_teammate_still_receives_checkpoint_fallback(tmp_path, monkeypatch):
    async def scenario():
        llm = RecordingLLM()
        original = Harness(tmp_path, monkeypatch, llm, persist=False)
        team, spawned = await original.spawn()
        await original.wait(spawned["process"]["process_id"])
        assert not original.teams.get_agent(spawned["agent"]["id"])["conv_path"]
        harness = Harness(tmp_path, monkeypatch, llm, parent=original.parent)
        restarted = await harness.call(
            "team_restart", team_id=team["id"], agent="worker", instruction="Legacy follow-up",
        )
        assert restarted["restart_kind"] == "checkpoint_restart"
        assert restarted["continuation"] is False
        await harness.wait(restarted["process"]["process_id"])
        prompt = json.dumps(llm.requests[-1])
        assert "CHECKPOINT RESTART" in prompt
        assert "Legacy follow-up" in prompt
        assert "Report 1" in prompt

    asyncio.run(scenario())


def test_legacy_restart_does_not_discard_an_explicit_workspace_without_sandbox(tmp_path, monkeypatch):
    async def scenario():
        harness = Harness(tmp_path, monkeypatch, RecordingLLM(), persist=False)
        team, spawned = await harness.spawn()
        await harness.wait(spawned["process"]["process_id"])
        agent = harness.teams.get_agent(spawned["agent"]["id"])
        spawn_spec = agent["spawn_spec"]
        spawn_spec["workspace_root"] = str(tmp_path)
        with harness.teams._connection() as db:
            db.execute("UPDATE team_agents SET spawn_spec_json=? WHERE id=?",
                       (json.dumps(spawn_spec), agent["id"]))
        result = await harness.registry.execute(
            "team_restart", {"team_id": team["id"], "agent": "worker"}, task_id=harness.parent,
        )
        assert "workspace_root is unavailable" in result["error"]
        assert len(harness.llm.requests) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_assigned_canonical_history_damage_refuses_checkpoint_fallback(tmp_path, monkeypatch, damage):
    async def scenario():
        llm = RecordingLLM()
        harness = Harness(tmp_path, monkeypatch, llm)
        team, spawned = await harness.spawn()
        await harness.wait(spawned["process"]["process_id"])
        agent = harness.teams.get_agent(spawned["agent"]["id"])
        store = SessionStore(agent["conv_path"])
        if damage == "missing":
            for path in (store.jsonl_path, store.legacy_path, store.snapshot_path, store.header_path):
                path.unlink(missing_ok=True)
        else:
            store.jsonl_path.write_text("{broken interior record}\n{}\n", encoding="utf-8")
        result = await harness.registry.execute(
            "team_restart", {"team_id": team["id"], "agent": "worker"}, task_id=harness.parent,
        )
        assert result["error"], result
        assert len(llm.requests) == 1

    asyncio.run(scenario())


def test_initial_messages_and_reserved_turn_are_durable_before_model_request(tmp_path, monkeypatch):
    async def scenario():
        llm = RecordingLLM()
        harness = Harness(tmp_path, monkeypatch, llm)

        def inspect_checkpoint(_kwargs):
            paths = list((tmp_path / ".artifacts").glob("*.conv.jsonl"))
            assert len(paths) == 1
            payload = SessionStore(paths[0]).load(readonly=True)
            assert payload["subagent_state"]["turns_used"] == 1
            assert any("Initial state sentinel" in str(m.get("content"))
                       for m in payload["messages"])

        llm.before_request = inspect_checkpoint
        result = await harness.call("delegate_task", goal="Initial state sentinel", timeout=30)
        assert result["worker_status"] == "completed"
        assert len(llm.requests) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("failure_point", ["intent", "first_result"])
def test_failed_checkpoint_prevents_subsequent_side_effects(tmp_path, monkeypatch, failure_point):
    from agent.runtime.subagent_conversation import SubagentConversation

    async def scenario():
        writes = []
        registry = ToolRegistry()
        _register_tool(registry, "write_file", lambda: writes.append("first") or "first saved", "write")
        _register_tool(registry, "edit_file", lambda: writes.append("second") or "second saved", "write")
        llm = RecordingLLM([_calls("write_file", "edit_file")])
        harness = Harness(tmp_path, monkeypatch, llm, registry=registry)
        original_save = SubagentConversation.save
        failed = []

        def failing_save(self, messages, state):
            has_intent = any(m.get("role") == "assistant" and m.get("tool_calls") for m in messages)
            has_result = any(m.get("role") == "tool" for m in messages)
            if (failure_point == "intent" and has_intent or
                    failure_point == "first_result" and has_result):
                failed.append(True)
                raise OSError("injected checkpoint storage failure")
            return original_save(self, messages, state)

        monkeypatch.setattr(SubagentConversation, "save", failing_save)
        result = await harness.call("delegate_task", goal="Two ordered writes", mode="worker", timeout=30)
        assert failed, "Fault injection must hit a real persistence boundary"
        assert writes == ([] if failure_point == "intent" else ["first"])
        assert result["worker_status"] != "completed"
        assert len(llm.requests) == 1

    asyncio.run(scenario())


def test_interrupted_read_allows_later_authorized_write(tmp_path, monkeypatch):
    async def scenario():
        reading = asyncio.Event()
        writes = []

        async def paused_read():
            reading.set()
            await asyncio.Future()

        registry = ToolRegistry()
        _register_tool(registry, "read_file", paused_read)
        _register_tool(registry, "write_file", lambda: writes.append("done") or "saved", "write")
        llm = RecordingLLM([_calls("read_file")])
        harness = Harness(tmp_path, monkeypatch, llm, registry=registry)
        team, spawned = await harness.spawn(mode="worker")
        await asyncio.wait_for(reading.wait(), timeout=3)
        await harness.cancel(spawned["process"]["process_id"])
        llm.responses.extend([_calls("write_file"), {"content": "Safely continued", "tool_calls": []}])
        restarted = await harness.call("team_restart", team_id=team["id"], agent="worker")
        await harness.wait(restarted["process"]["process_id"])
        assert writes == ["done"]
        agent = harness.teams.get_agent(spawned["agent"]["id"])
        assert _load_closed(agent["conv_path"])["state"]["write_blocked"] is False

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("first_write", "later_write_runs"),
    [
        # The tool reported what happened, or never ran: the worker knows the outcome.
        ("reported_failure", True),
        ("refused_arguments", True),
        # The call was cut off: nobody knows what it left behind.
        ("raised_midway", False),
        ("stopped_with_partial_output", False),
    ],
)
def test_only_a_cut_off_write_blocks_the_workers_later_writes(tmp_path, monkeypatch, first_write, later_write_runs):
    from agent.runtime.tool_failure import ToolFailure

    async def scenario():
        writes = []

        def edit():
            if first_write == "reported_failure":
                return ToolFailure("edit_match_not_found", "Exact edit text was not found", True)
            if first_write == "stopped_with_partial_output":
                return ToolFailure("edit_timeout", "stopped after writing part of the file", False, partial=True)
            raise RuntimeError("connection lost while writing")

        registry = ToolRegistry()
        registry.register(ToolDef(
            name="edit_file", description="Recovery acceptance probe",
            parameters={
                "type": "object", "properties": {"path": {"type": "string"}},
                "required": ["path"] if first_write == "refused_arguments" else [],
            },
            fn=lambda path="": edit(), risk="write",
        ))
        _register_tool(registry, "write_file", lambda: writes.append("done") or "saved", "write")
        llm = RecordingLLM([
            _calls("edit_file"), _calls("write_file"), {"content": "Reported", "tool_calls": []},
        ])
        harness = Harness(tmp_path, monkeypatch, llm, registry=registry)
        _team, spawned = await harness.spawn(mode="worker")
        await harness.wait(spawned["process"]["process_id"])

        assert writes == (["done"] if later_write_runs else [])
        seen_by_worker = json.dumps(llm.requests[-1])
        assert ("recovery_write_blocked" in seen_by_worker) is not later_write_runs
        agent = harness.teams.get_agent(spawned["agent"]["id"])
        assert _load_closed(agent["conv_path"])["state"]["write_blocked"] is not later_write_runs

    asyncio.run(scenario())


def test_canonical_history_redacts_request_local_arguments_without_mutating_active_call(tmp_path, monkeypatch):
    async def scenario():
        registry = ToolRegistry()
        seen = []
        registry.register(ToolDef(
            name="read_file", description="Request-local argument probe",
            parameters={"type": "object", "properties": {"credential": {"type": "string"}}},
            fn=lambda credential: seen.append(credential) or "observed",
            argument_persistence="request_local",
            argument_redactor=lambda args: {"credential": "<redacted>"},
        ))
        raw = _calls("read_file")
        raw["tool_calls"][0]["arguments"] = json.dumps({"credential": "PRIVATE-ARGUMENT-SENTINEL"})
        llm = RecordingLLM([raw, {"content": "Inspection done", "tool_calls": []}])
        harness = Harness(tmp_path, monkeypatch, llm, registry=registry)
        result = await harness.call("delegate_task", goal="Inspect privately", timeout=30)
        assert result["worker_status"] == "completed"
        assert seen == ["PRIVATE-ARGUMENT-SENTINEL"]
        paths = list((tmp_path / ".artifacts").glob("*.conv.jsonl"))
        assert len(paths) == 1
        raw_log = paths[0].read_text(encoding="utf-8")
        assert "PRIVATE-ARGUMENT-SENTINEL" not in raw_log
        assert "<redacted>" in raw_log

    asyncio.run(scenario())


_CRASH_WORKER = textwrap.dedent("""
    import asyncio
    import json
    import sys
    from pathlib import Path

    from agent.runtime.session_store import SessionStore
    from agent.runtime.task_store import TaskStore
    from agent.runtime.tools import delegate
    from agent.runtime.tools.delegate import register_delegate_tools
    from agent.runtime.tools.processes import ProcessManager
    from agent.runtime.tools.registry import ToolDef, ToolRegistry

    root = Path(sys.argv[1])

    class LLM:
        async def chat(self, **kwargs):
            return {"content": "", "tool_calls": [
                {"id": "crash-call", "name": "write_file", "arguments": "{}"}
            ]}

    async def write_then_pause():
        counter = root / "counter.txt"
        counter.write_text(str(int(counter.read_text()) + 1) if counter.exists() else "1")
        # This is the external-effect/result-persistence crash window.
        (root / "effect-complete").write_text("ready")
        await asyncio.Future()

    async def main():
        delegate._sub_processes = ProcessManager(artifact_dir=root / "processes")
        tasks = TaskStore(root / "tasks.db")
        parent = tasks.start_run("request-a", "crash acceptance", session_id="session-a")["id"]
        registry = ToolRegistry()
        registry.yolo = True
        registry.register(ToolDef(
            "write_file", "crash boundary probe", {"type": "object", "properties": {}},
            write_then_pause, risk="write",
        ))
        llm = LLM()
        register_delegate_tools(
            registry, llm_getter=lambda: llm, session_id_getter=lambda: "session-a", task_store=tasks,
            conv_store_for=lambda _sid, key: SessionStore.for_subagent(root / "session.json", key),
        )
        async def call(name, args):
            result = await registry.execute(name, args, task_id=parent)
            assert not result["error"], result
            return json.loads(result["output"])
        team = await call("team", {"action": "create", "name": "crash", "goal": "once"})
        spawned = await call("team_spawn", {
            "team_id": team["id"], "name": "worker", "goal": "Write once then report",
            "mode": "worker", "max_turns": 8, "timeout": 60,
        })
        (root / "handle.json").write_text(json.dumps({"parent": parent, "team": team, "spawned": spawned}))
        await asyncio.Future()

    asyncio.run(main())
""")


@pytest.mark.skipif(os.name == "nt", reason="SIGKILL acceptance is a POSIX process-death test")
def test_sigkill_after_effect_before_result_blocks_writes_across_two_real_worker_restarts(tmp_path, monkeypatch):
    repo = Path(__file__).resolve().parents[1]
    script = tmp_path / "crash_worker.py"
    script.write_text(_CRASH_WORKER, encoding="utf-8")
    log_path = tmp_path / "child.log"
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            [sys.executable, "-u", str(script), str(tmp_path)], cwd=repo,
            env={**os.environ, "PYTHONPATH": str(repo)}, stdout=log, stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if (tmp_path / "effect-complete").exists() and (tmp_path / "handle.json").exists():
                    break
                if process.poll() is not None:
                    pytest.fail(f"Crash worker exited before its barrier: {log_path.read_text()}")
                time.sleep(0.02)
            else:
                pytest.fail(f"Crash worker never reached effect barrier: {log_path.read_text()}")
            process.kill()
            assert process.wait(timeout=5) == -signal.SIGKILL
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)

    assert (tmp_path / "counter.txt").read_text() == "1"
    handle = json.loads((tmp_path / "handle.json").read_text())

    async def scenario():
        registry = ToolRegistry()
        attempted_writes = []

        def forbidden_write():
            attempted_writes.append("write")
            counter = tmp_path / "counter.txt"
            counter.write_text(str(int(counter.read_text()) + 1))
            return "unexpected write"

        _register_tool(registry, "write_file", forbidden_write, "write")
        _register_tool(registry, "edit_file", forbidden_write, "write")
        _register_tool(registry, "read_file", lambda: "counter=" + (tmp_path / "counter.txt").read_text())
        llm = RecordingLLM()
        harness = Harness(tmp_path, monkeypatch, llm, parent=handle["parent"], registry=registry)
        for expected_turns in (3, 5):
            llm.responses.extend([
                _calls("write_file", "edit_file", "read_file"),
                {"content": "Verified outcome; writes remain blocked", "tool_calls": []},
            ])
            restarted = await harness.call(
                "team_restart", team_id=handle["team"]["id"], agent="worker",
                instruction="Check the external result before proceeding",
            )
            assert restarted["restart_kind"] == "conversation_resume"
            await harness.wait(restarted["process"]["process_id"])
            agent = harness.teams.get_agent(handle["spawned"]["agent"]["id"])
            payload = _load_closed(agent["conv_path"])
            assert payload["state"]["write_blocked"] is True
            assert payload["state"]["turns_used"] == expected_turns
            tool_outputs = [str(m.get("content")) for m in llm.requests[-1] if m.get("role") == "tool"]
            assert sum("recovery_write_blocked" in text for text in tool_outputs) >= 2
            assert any("counter=1" in text for text in tool_outputs)
            assert not attempted_writes
            assert (tmp_path / "counter.txt").read_text() == "1"

    asyncio.run(scenario())


def test_unavailable_llm_does_not_publish_missing_journal_and_can_restart(tmp_path, monkeypatch):
    async def scenario():
        harness = Harness(tmp_path, monkeypatch, None)
        team, spawned = await harness.spawn()
        first = await harness.wait(spawned["process"]["process_id"])
        agent_id = spawned["agent"]["id"]
        assert first["error_code"] == "llm_unavailable"
        assert harness.teams.get_agent(agent_id)["conv_path"] == ""

        llm = RecordingLLM()
        harness.llm = llm

        def inspect_first_dispatch(_kwargs):
            path = harness.teams.get_agent(agent_id)["conv_path"]
            assert path and Path(path).is_file()
            state = SessionStore(path).load(readonly=True)["subagent_state"]
            assert state["turns_used"] == 1

        llm.before_request = inspect_first_dispatch
        restarted = await harness.call("team_restart", team_id=team["id"], agent="worker")
        result = await harness.wait(restarted["process"]["process_id"])
        assert restarted["restart_kind"] == "checkpoint_restart"
        assert result["worker_status"] == "completed"
        assert len(llm.requests) == 1
        assert _load_closed(harness.teams.get_agent(agent_id)["conv_path"])["state"]["turns_used"] == 1

    asyncio.run(scenario())


def test_committed_journal_is_discovered_after_missing_database_binding(tmp_path, monkeypatch):
    async def scenario():
        llm = RecordingLLM()
        harness = Harness(tmp_path, monkeypatch, llm)
        team, spawned = await harness.spawn()
        await harness.wait(spawned["process"]["process_id"])
        agent_id = spawned["agent"]["id"]
        path = harness.teams.get_agent(agent_id)["conv_path"]
        assert _load_closed(path)["state"]["turns_used"] == 1
        # Simulate a crash after the journal commit but before publishing its
        # deterministic location in the Team database.
        harness.teams.set_agent_conv_path(agent_id, "")

        restarted = await harness.call(
            "team_restart", team_id=team["id"], agent="worker",
            instruction="Continue the unbound committed conversation",
        )
        result = await harness.wait(restarted["process"]["process_id"])
        assert restarted["restart_kind"] == "conversation_resume"
        assert restarted["continuation"] is True
        assert result["worker_status"] == "completed"
        assert harness.teams.get_agent(agent_id)["conv_path"] == path
        assert any(message.get("content") == "Report 1" for message in llm.requests[1])
        assert "CHECKPOINT RESTART" not in json.dumps(llm.requests[1])
        assert _load_closed(path)["state"]["turns_used"] == 2
        assert len(list((tmp_path / ".artifacts").glob("*.conv.jsonl"))) == 1

    asyncio.run(scenario())


def test_recovery_blocks_team_task_mutation_but_allows_board_read(tmp_path, monkeypatch):
    async def scenario():
        started = asyncio.Event()

        async def paused_write():
            started.set()
            await asyncio.Future()

        registry = ToolRegistry()
        _register_tool(registry, "write_file", paused_write, "write")
        llm = RecordingLLM([_calls("write_file")])
        harness = Harness(tmp_path, monkeypatch, llm, registry=registry)
        team, spawned = await harness.spawn(mode="worker")
        await asyncio.wait_for(started.wait(), timeout=3)
        await harness.call("team_task", action="create", team_id=team["id"],
                           title="Existing board evidence")
        await harness.cancel(spawned["process"]["process_id"])
        llm.responses.extend([
            {"content": "", "tool_calls": [
                {"id": "blocked-create", "name": "team_task", "arguments": json.dumps({
                    "action": "create", "team_id": team["id"], "title": "MUST NOT BE CREATED",
                })},
                {"id": "allowed-list", "name": "team_task", "arguments": json.dumps({
                    "action": "list", "team_id": team["id"],
                })},
            ]},
            {"content": "Inspected board without mutating it", "tool_calls": []},
        ])
        restarted = await harness.call("team_restart", team_id=team["id"], agent="worker")
        assert restarted["recovery_readonly"] is True
        result = await harness.wait(restarted["process"]["process_id"])
        assert result["worker_status"] == "completed"
        assert [task["title"] for task in harness.teams.list_tasks(team["id"])] == ["Existing board evidence"]
        outputs = {message["tool_call_id"]: json.loads(message["content"])
                   for message in llm.requests[-1] if message.get("role") == "tool"}
        assert outputs["blocked-create"]["error"] == "recovery_write_blocked"
        assert outputs["allowed-list"]["error"] == ""
        assert "Existing board evidence" in outputs["allowed-list"]["output"]
        agent = harness.teams.get_agent(spawned["agent"]["id"])
        assert _load_closed(agent["conv_path"])["state"]["write_blocked"] is True

    asyncio.run(scenario())
