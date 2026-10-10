"""Command turns keep evidence, require a user decision, and preserve storage contracts."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.cli.conversation_commands import register_conversation_tools
from agent.cli.main import handle_slash
from agent.runtime.command_workflows import READ_TOOLS, raw_command, workflow_message
from agent.runtime.memory import MemoryStore
from agent.runtime.react import ReActAgent
from agent.runtime.skill_learning import LearnedSkills
from agent.runtime.skills import SkillStore
from agent.runtime.tools.registry import ToolDef, ToolRegistry


@pytest.mark.parametrize("command,kind", [
    ("/learn REVIEW", "skill_review"), ("/learn review MySkill", "skill_review"),
    ("/doctor computer", "doctor"), ("/diagnostics tasks", "diagnostics"),
    ("/skills create", "skill_create"), ("/skills create example 'A careful method'", "skill_create"),
    ("/memory review old paths", "memory_review"), ("/handoff Windows details", "handoff"),
    ("/conclave Compare A and B", "conclave"),
    ("/conclave What's useful here?", "conclave"), (r"/doctor Can't read C:\Users\example", "doctor"),
])
def test_frontends_share_expansion_and_preserve_arguments(command, kind):
    msg = workflow_message(command)
    assert msg.metadata["command_workflow"] == kind
    assert msg.metadata["display_command"] == command
    assert command in msg.get_text()
    cli = asyncio.run(handle_slash(command, SimpleNamespace()))
    assert cli.get_text() == msg.get_text()


@pytest.mark.parametrize("command", [
    "/learn history", "/learn undo sl_example", "/learn migrate", "/memory inspect", "/memory correct id replacement",
    "/skills show x", "/skills create --template x y", "/doctor --raw computer", "/diagnostics json",
    "/diagnostics --raw tasks", "/handoff --raw Windows notes", "/conclave config", "/mode high", "/cancel",
])
def test_exact_commands_do_not_start_model_work(command):
    assert workflow_message(command) is None


def test_direct_escape_hatches_are_narrow():
    assert raw_command("/doctor --raw computer") == "/doctor computer"
    assert raw_command("/skills create --template Example 'keep Case'") == "/skills create Example 'keep Case'"
    assert raw_command("/memory remember --raw") == "/memory remember --raw"
    assert raw_command(r"/handoff --raw Keep C:\Users\example intact") == r"/handoff Keep C:\Users\example intact"
    with pytest.raises(ValueError, match="Usage"):
        workflow_message("/learn review one two")


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ASTRA_HOME", str(tmp_path / "state"))
    registry = ToolRegistry()
    agent = SimpleNamespace(
        tools=registry, skill_store=SkillStore(tmp_path / "skills"), memory_store=MemoryStore(tmp_path / "memory.db"),
        task_store=None, llm=SimpleNamespace(config=SimpleNamespace(model="test")),
        context=SimpleNamespace(session_path=str(tmp_path / "session-one.json"), persona_id="",
                                messages=[{"role": "user", "content": "/learn review", "provenance": "command_workflow"}]),
        _refresh_skill_catalog=lambda **kwargs: None,
    )
    register_conversation_tools(agent)
    return agent


def call(agent, tool_name, **args):
    return asyncio.run(agent.tools.execute(tool_name, args))


def save_skill(agent, name="example", body="Read the file, then validate it."):
    return LearnedSkills(agent.skill_store).manage("create", name, content=f"---\nname: {name}\ndescription: A reusable method\n---\n{body}\n")


def snapshot(agent):
    result = call(agent, "skill_review_snapshot")
    assert not result["error"], result
    return json.loads(result["output"])


def test_proposal_never_writes_and_selected_changes_are_undoable(service):
    save_skill(service)
    save_skill(service, "second")
    learned = LearnedSkills(service.skill_store)
    before = learned._snapshot("example")
    proposal = snapshot(service)
    assert learned._snapshot("example") == before
    assert all(row["kind"] == "save" for row in learned.history())
    actions = [{"action": "rewrite", "names": ["example"], "reason": "User selected this improvement.",
                "patches": [{"old_string": "Read the file, then validate it.", "new_string": "Validate a temporary copy before replacing the file."}]},
               {"action": "keep", "names": ["second"], "reason": "User declined this change."}]
    rejected = call(service, "skill_review_apply", snapshot_id=proposal["snapshot_id"], actions=actions)
    assert rejected["error"]
    service.context.messages.append({"role": "user", "content": "Only change the first one."})
    applied = call(service, "skill_review_apply", snapshot_id=proposal["snapshot_id"], actions=actions)
    assert not applied["error"], applied
    assert "temporary copy" in learned._snapshot("example")["SKILL.md"]
    assert learned._snapshot("second")["SKILL.md"].endswith("Read the file, then validate it.\n")
    record = next(r for r in learned.history() if r["kind"] == "review")
    assert record["kind"] == "review"
    learned.undo(record["id"])
    assert learned._snapshot("example") == before
    assert call(service, "skill_review_apply", snapshot_id=proposal["snapshot_id"], actions=actions)["error"]


@pytest.mark.parametrize("case", ["session", "file", "wakeup", "goal"])
def test_snapshot_rejects_wrong_session_stale_files_and_synthetic_consent(service, case):
    save_skill(service)
    proposal = snapshot(service)
    service.context.messages.append({"role": "user", "content": "continue"})
    if case == "session":
        service.context.session_path += ".other"
    elif case == "file":
        path = LearnedSkills(service.skill_store)._path("example")
        path.write_text(path.read_text() + "User edit.\n")
    else:
        service.context.messages[-1]["provenance"] = "session_wakeup" if case == "wakeup" else "goal_continuation"
    result = call(service, "skill_review_apply", snapshot_id=proposal["snapshot_id"], actions=[
        {"action": "archive", "names": ["example"], "reason": "Remove it."},
    ])
    assert result["error"]
    assert LearnedSkills(service.skill_store)._path("example").exists()


def test_snapshot_excludes_user_skills_and_supports_a_named_auto_skill(service):
    save_skill(service)
    service.skill_store.create("manual", "---\nname: manual\ndescription: User method\n---\nKeep this.")
    proposal = snapshot(service)
    assert set(proposal["items"]) == {"example"}
    assert not call(service, "skill_review_snapshot", name="example")["error"]
    assert call(service, "skill_review_snapshot", name="manual")["error"]


def test_memory_correction_keeps_core_scope_and_rejects_stale_content(service):
    entry = service.memory_store.add_core("user", "Use the old path.")
    service.memory_store.add_core("user", "Keep this preference.")
    inspect = json.loads(call(service, "memory_inspect")["output"])
    assert any(item["id"] == entry["id"] for item in inspect["core"])
    assert call(service, "memory_correct", memory_id=entry["id"], expected_content="Wrong", content="New")["error"]
    result = call(service, "memory_correct", memory_id=entry["id"], expected_content=entry["content"], content="Use the new path.")
    assert not result["error"], result
    assert [r["content"] for r in service.memory_store.list_core("user")] == ["Use the new path.", "Keep this preference."]
    assert call(service, "memory_correct", memory_id=entry["id"], expected_content=entry["content"], content="Again")["error"]


def test_structured_correction_preserves_old_record_as_history(service):
    entry = service.memory_store.add_record(kind="observation", content="Old fact")
    result = call(service, "memory_correct", memory_id=entry.record_id, expected_content="Old fact", content="New fact")
    assert not result["error"], result
    assert service.memory_store.resolve_record(entry.record_id).status == "superseded"
    new = json.loads(result["output"])["replacement"]
    assert service.memory_store.resolve_record(new).supersedes_id == entry.record_id
    assert call(service, "memory_correct", memory_id=entry.record_id, expected_content="Old fact", content="Again")["error"]


def test_handoff_saves_redacted_content_in_fixed_directory(service):
    drafted = call(service, "session_handoff", action="draft")
    assert not drafted["error"]
    saved = call(service, "session_handoff", action="save", content="# Handoff\nAPI key: sk-abcdefghijklmnopqrstuv\nUnverified: GUI behavior.")
    assert not saved["error"], saved
    content = Path(".astra/handoffs/latest.md").read_text()
    assert "sk-abcdefghijklmnopqrstuv" not in content
    assert "Unverified: GUI behavior." in content
    from agent.runtime.session_handoff import save_handoff
    prepared = Path(saved["output"].removeprefix("Saved redacted session handoff to ").strip())
    save_handoff("Automatic snapshot", session_id=Path(service.context.session_path).stem)
    assert prepared.read_text() == content


@pytest.mark.parametrize("finish", ["normal", "cancel", "error"])
def test_readonly_scope_rejects_unadvertised_write_and_restores_after_exit(tmp_path, monkeypatch, finish):
    monkeypatch.chdir(tmp_path)
    writes = []
    registry = ToolRegistry()
    registry.register(ToolDef("write_file", "write", {"type": "object", "properties": {}}, lambda: writes.append(True)))
    agent = ReActAgent("test", SimpleNamespace(), registry)
    agent.context.set_session(str(tmp_path / "session.json"))
    observed = []

    async def fake_loop(msg, emit_events):
        observed.append(set(agent.tool_allowlist))
        results = await agent._execute_tool_calls([{"id": "write", "name": "write_file", "arguments": "{}"}])
        assert results[0]["error"].startswith("[ToolDisabled]")
        yield {"type": "chunk", "content": "Proposal"}
        if finish == "error":
            raise ValueError("fixture error")

    monkeypatch.setattr(agent, "_run_react_loop_active", fake_loop)

    async def run():
        stream = agent._run_react_loop(workflow_message("/learn review"))
        await anext(stream)
        if finish == "cancel":
            await stream.aclose()
        elif finish == "error":
            with pytest.raises(ValueError, match="fixture error"):
                await anext(stream)
        else:
            with pytest.raises(StopAsyncIteration):
                await anext(stream)
    asyncio.run(run())
    assert observed == [set(READ_TOOLS)]
    assert writes == []
    assert agent.tool_allowlist is None


def test_workflow_ui_metadata_is_not_sent_as_provider_fields(tmp_path):
    agent = ReActAgent("test", SimpleNamespace(), ToolRegistry())
    message = {"role": "user", "content": "Expanded workflow", "display_command": "/learn review", "provenance": "command_workflow"}
    provider = agent.context._provider_message(message)
    assert "display_command" not in provider
    assert "provenance" not in provider
    assert provider["content"] == "Expanded workflow"


def test_explicit_review_still_works_when_automatic_learning_is_off(service):
    from agent.runtime.learning import LearningStore
    save_skill(service)
    LearningStore(service.skill_store.root.parent / "learning.db").set_mode("off")
    proposal = snapshot(service)
    service.context.messages.append({"role": "user", "content": "Keep it."})
    result = call(service, "skill_review_apply", snapshot_id=proposal["snapshot_id"], actions=[
        {"action": "keep", "names": ["example"], "reason": "User chose to keep it."},
    ])
    assert not result["error"], result


def test_memory_forget_checks_content_and_retains_structured_history(service):
    item = service.memory_store.add_record(kind="observation", content="Obsolete fact")
    assert call(service, "memory_forget", memory_id=item.record_id, expected_content="Wrong")["error"]
    result = call(service, "memory_forget", memory_id=item.record_id, expected_content=item.content)
    assert not result["error"], result
    assert service.memory_store.resolve_record(item.record_id).status == "forgotten"


def test_review_tools_are_not_execution_evidence():
    from agent.runtime.learning_evidence import is_evidence_tool
    assert not is_evidence_tool("skill_review_snapshot")
    assert not is_evidence_tool("skill_review_apply")


def test_reloading_the_same_user_turn_does_not_authorize_apply(service):
    save_skill(service)
    service.context.messages = [{"role": "user", "content": "Inspect this again", "timestamp": 123.0}]
    proposal = snapshot(service)
    service.context.messages = json.loads(json.dumps(service.context.messages))
    result = call(service, "skill_review_apply", snapshot_id=proposal["snapshot_id"], actions=[
        {"action": "archive", "names": ["example"], "reason": "No actual next user turn."},
    ])
    assert result["error"]


def test_later_apply_does_not_rewind_another_sessions_batch_cursor(service):
    for i in range(6):
        save_skill(service, f"example-{i}")
    proposal = snapshot(service)
    learned = LearnedSkills(service.skill_store)
    state = learned._state()
    state["cursor"] = "example-5"
    learned._json(learned.home / "index.json", state)
    service.context.messages.append({"role": "user", "content": "Keep these four."})
    result = call(service, "skill_review_apply", snapshot_id=proposal["snapshot_id"], actions=[
        {"action": "keep", "names": [name], "reason": "Keep useful material."} for name in proposal["items"]
    ])
    assert not result["error"], result
    assert learned._state()["cursor"] == "example-5"


def test_diagnostic_adapter_reads_live_state_and_plain_cli_raw_is_direct(capsys):
    from test_runtime_diagnostics import _agent
    agent = _agent()
    agent.task_store = None
    register_conversation_tools(agent)
    first = call(agent, "runtime_diagnostics", section="json")
    assert not first["error"], first
    assert json.loads(first["output"])["context"]["role_messages"]["user"] == 1
    agent.context.add_user("Another real turn")
    second = call(agent, "runtime_diagnostics", section="json")
    assert json.loads(second["output"])["context"]["role_messages"]["user"] == 2
    assert not call(agent, "runtime_diagnostics", kind="doctor", section="metrics")["error"]
    assert asyncio.run(handle_slash("/diagnostics --raw json", agent)) is None
    assert json.loads(capsys.readouterr().out)["context"]["role_messages"]["user"] == 2


def test_concurrent_memory_proposals_cannot_create_two_active_replacements(service):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    item = service.memory_store.add_record(kind="observation", content="Original")
    other = MemoryStore(service.memory_store.path)
    ready = Barrier(2)

    def correct(store, replacement):
        ready.wait()
        try:
            return store.supersede_record(item.record_id, content=replacement, expected_content="Original")
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(correct, store, text) for store, text in [
            (service.memory_store, "First correction"), (other, "Second correction"),
        ]]
        results = [future.result() for future in futures]
    assert sum(result is not None for result in results) == 1
    records = service.memory_store.recall_records(limit=20)
    assert len([r for r in records if r.supersedes_id == item.record_id]) == 1


def test_snapshot_says_how_to_reach_the_skills_after_its_batch(service):
    for i in range(6):
        save_skill(service, f"example-{i}")

    first = call(service, "skill_review_snapshot")
    batch = json.loads(first["output"])

    assert batch["remaining"] == 6 - len(batch["items"]) > 0
    # Asking again returns the same skills, so the way forward has to be stated.
    assert "skill_review_apply" in batch["notice"] and str(batch["remaining"]) in batch["notice"]
    assert first["partial"] is True
    assert set(json.loads(call(service, "skill_review_snapshot")["output"])["items"]) == set(batch["items"])

    service.context.messages.append({"role": "user", "content": "Keep these."})
    applied = call(service, "skill_review_apply", snapshot_id=batch["snapshot_id"], actions=[
        {"action": "keep", "names": [name], "reason": "Still useful."} for name in batch["items"]
    ])
    assert not applied["error"], applied
    rest = call(service, "skill_review_snapshot")
    assert set(json.loads(rest["output"])["items"]).isdisjoint(batch["items"])
    assert json.loads(rest["output"])["remaining"] == 0 and not rest.get("partial")


def test_memory_inspect_says_when_the_record_list_is_capped(service):
    for index in range(3):
        service.memory_store.add_record(kind="observation", content=f"Deployment note {index}")
    few = call(service, "memory_inspect")
    assert len(json.loads(few["output"])["records"]) == 3
    assert "more may match" not in json.loads(few["output"])["notice"] and not few.get("partial")

    for index in range(3, 24):
        service.memory_store.add_record(kind="observation", content=f"Deployment note {index}")
    capped = call(service, "memory_inspect")
    payload = json.loads(capped["output"])
    assert len(payload["records"]) == payload["limit"] < 24
    assert "narrower query" in payload["notice"]
    assert capped["partial"] is True


def test_handoff_save_refusals_name_the_actual_problem(service):
    empty = call(service, "session_handoff", action="save", content="  ")
    assert "save needs the handoff text in content" in empty["error"]
    too_long = call(service, "session_handoff", action="save", content="x" * 40_001)
    assert "content is 40,001 characters" in too_long["error"] and "40,000" in too_long["error"]
    unknown = call(service, "session_handoff", action="send", content="# Handoff")
    assert "'send'" in unknown["error"] and "draft or save" in unknown["error"]
    assert not Path(".astra/handoffs").exists()


def test_unknown_diagnostics_section_is_a_failure_that_lists_the_sections():
    from test_runtime_diagnostics import _agent
    agent = _agent()
    agent.task_store = None
    register_conversation_tools(agent)

    runtime = call(agent, "runtime_diagnostics", section="memory")
    doctor = call(agent, "runtime_diagnostics", kind="doctor", section="nonsense")

    for result in (runtime, doctor):
        assert result["output"] == "" and result["code"] == "invalid_arguments"
        assert "Available:" in result["error"]
    assert "context" in runtime["error"] and "sandbox" in doctor["error"]


def test_conversation_tools_state_their_turn_limit(service):
    for name in ("skill_review_snapshot", "skill_review_apply", "runtime_diagnostics", "memory_inspect",
                 "memory_correct", "memory_forget", "session_handoff"):
        tool = service.tools.get(name)
        assert f"At most {tool.max_calls_per_turn} calls per turn" in tool.description, name
