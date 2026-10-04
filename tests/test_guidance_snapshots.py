import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from agent.runtime.context import AgentContext
from agent.runtime.project_instructions import ProjectInstructions
from agent.runtime.react import ReActAgent
from agent.runtime.skills import SkillStore
from agent.runtime.tools.registry import ToolDef, ToolRegistry
from agent.core.msg import ContentBlock, Msg
from agent.cli.guidance_commands import execute_guidance_command
from agent.cli.skill_commands import execute_skill_command


def project(tmp_path, name="project"):
    root = tmp_path / name
    (root / ".git").mkdir(parents=True)
    (root / "AGENTS.md").write_text("Root contract v1", encoding="utf-8")
    return root


class RecordingLLM:
    def __init__(self):
        self.requests = []

    async def chat_stream(self, messages, tools, **kwargs):
        self.requests.append(copy.deepcopy((messages, tools)))
        yield {"type": "done", "content": "done", "usage": None}


def agent_for(root, monkeypatch, *, context=None):
    monkeypatch.setenv("ASTRA_PROJECT_TRUST", "trusted")
    store = SkillStore(root / "skills")
    llm = RecordingLLM()
    agent = ReActAgent("fixture", llm, ToolRegistry(), system_prompt="Shared identity", skill_store=store)
    agent._sandbox = SimpleNamespace(workdir=str(root))
    if context is not None:
        agent.context = context
    return agent, llm, store


def reply(agent, text):
    asyncio.run(agent.reply(Msg(content=[ContentBlock.text(text)])))


def test_discovery_is_deduplicated_frozen_and_bounded(tmp_path):
    root = project(tmp_path)
    area = root / "src" / "nested"
    area.mkdir(parents=True)
    (root / "src" / "AGENTS.md").write_text("Area contract", encoding="utf-8")
    (area / "AGENTS.md").write_text("Area contract", encoding="utf-8")
    loader = ProjectInstructions(root, trusted=True, max_bytes=512)
    discovered = loader.discover({str(area / "future.py")})
    assert discovered.count("Area contract") == 1
    assert loader.discover({str(area / "future.py")}) == ""
    (root / "AGENTS.md").write_text("Changed root", encoding="utf-8")
    restored = ProjectInstructions(root, trusted=True, snapshot=loader.snapshot())
    assert "Root contract v1" in restored.base_prompt
    assert "Changed root" not in restored.base_prompt
    assert "Area contract" in restored.discovered_prompt
    assert len((loader.base_prompt + loader.discovered_prompt).encode()) <= 512


def test_discovery_rejects_external_and_symlinked_instructions(tmp_path):
    root = project(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "AGENTS.md").write_text("External authority", encoding="utf-8")
    area = root / "src"
    area.mkdir()
    (area / "AGENTS.md").symlink_to(outside / "AGENTS.md")
    loader = ProjectInstructions(root, trusted=True)
    assert loader.discover({str(outside / "file.py"), str(area / "file.py")}) == ""
    restored = ProjectInstructions(root, trusted=False, snapshot=loader.snapshot())
    assert restored.base_prompt == restored.discovered_prompt == ""


def test_utf8_budget_reports_truncation(tmp_path):
    root = project(tmp_path)
    (root / "AGENTS.md").write_text("中文规则" * 300, encoding="utf-8")
    loader = ProjectInstructions(root, trusted=True, max_bytes=256)
    assert len(loader.base_prompt.encode()) <= 256
    assert "\ufffd" not in loader.base_prompt
    assert loader.warnings


def test_duplicate_content_is_not_readmitted_with_a_smaller_remaining_budget(tmp_path):
    root = project(tmp_path)
    text = "Shared contract. " * 20
    (root / "AGENTS.md").write_text(text, encoding="utf-8")
    area = root / "src"
    area.mkdir()
    (area / "AGENTS.md").write_text(text, encoding="utf-8")
    loader = ProjectInstructions(root, trusted=True, max_bytes=768)
    assert loader.discover({str(area / "file.py")}) == ""
    restored = ProjectInstructions(root, trusted=True, snapshot=loader.snapshot())
    assert restored.discovered_prompt == ""


def test_disk_changes_are_pending_until_explicit_application(tmp_path, monkeypatch):
    root = project(tmp_path)
    agent, llm, store = agent_for(root, monkeypatch)
    reply(agent, "first")
    first_messages, first_tools = llm.requests[0]
    first_tool_cost = agent.context._tools_token_cost
    (root / "AGENTS.md").write_text("Root contract v2", encoding="utf-8")
    store.create("new-skill", "---\nname: new-skill\ndescription: Newly saved guidance\n---\nInstructions")
    agent.tools.register(ToolDef("new_tool", "New tool", {"type": "object", "properties": {}}, lambda: "ok"))
    reply(agent, "second")
    second_messages, second_tools = llm.requests[1]
    assert second_messages[:len(first_messages)] == first_messages
    assert second_tools == first_tools
    assert agent.context._tools_token_cost == first_tool_cost
    assert "Root contract v2" not in second_messages[0]["content"]
    status = agent.guidance_status()
    assert status["pending"]["project"] and status["pending"]["skills"]
    before = copy.deepcopy(agent.context.messages)
    agent.refresh_session_guidance()
    assert agent.context.messages == before
    reply(agent, "third")
    third_messages, third_tools = llm.requests[2]
    assert "Root contract v2" in third_messages[0]["content"]
    assert "new-skill" in third_messages[0]["content"]
    assert any(item["function"]["name"] == "new_tool" for item in third_tools)


def test_guidance_survives_cold_restore_and_workspace_a_b_a(tmp_path, monkeypatch):
    a, b = project(tmp_path, "a"), project(tmp_path, "b")
    (b / "AGENTS.md").write_text("Project B only", encoding="utf-8")
    agent, llm, _store = agent_for(a, monkeypatch)
    agent.context.set_session(str(tmp_path / "a.json"))
    reply(agent, "first")
    first = copy.deepcopy(llm.requests[0])
    agent.context.save()
    (a / "AGENTS.md").write_text("A changed on disk", encoding="utf-8")
    restored = AgentContext(system_prompt="Shared identity")
    restored.set_session(str(tmp_path / "a.json"))
    assert restored.load(readonly=True)
    other, other_llm, _ = agent_for(a, monkeypatch, context=restored)
    reply(other, "continued")
    assert other_llm.requests[0][0][:len(first[0])] == first[0]
    assert other_llm.requests[0][1] == first[1]
    parked = other.context
    other.context = AgentContext(system_prompt="Shared identity")
    other._sandbox.workdir = str(b)
    reply(other, "B")
    assert "Project B only" in other_llm.requests[-1][0][0]["content"]
    other.context = parked
    other._sandbox.workdir = str(a)
    reply(other, "back to A")
    assert "Project B only" not in other_llm.requests[-1][0][0]["content"]
    assert "A changed on disk" not in other_llm.requests[-1][0][0]["content"]


def test_response_versions_inherit_guidance_and_stage_without_mutating_source(tmp_path, monkeypatch):
    from agent.runtime.conversation_branches import ConversationBranches
    from agent.runtime.message_source import message_source_ref
    from agent.runtime.session_store import SessionStore

    root = project(tmp_path)
    agent, _llm, _store = agent_for(root, monkeypatch)
    path = tmp_path / "conversation.json"
    agent.context.set_session(str(path))
    # A replayable image exercises persisted multimodal user-message shapes.
    from PIL import Image
    import base64
    from io import BytesIO
    image = BytesIO()
    Image.new("RGB", (2, 2), "white").save(image, format="PNG")
    data_url = "data:image/png;base64," + base64.b64encode(image.getvalue()).decode()
    asyncio.run(agent.reply(Msg(content=[ContentBlock.text("describe"), ContentBlock.image_url(data_url)])))
    agent.context.save()
    source = SessionStore(path).load(readonly=True)
    source_bytes = SessionStore(path).header_path.read_bytes()
    branches = ConversationBranches(path)
    state = branches.state()
    reference = message_source_ref(source["messages"][-1], len(source["messages"]) - 1)
    candidate = branches.prepare_retry(reference, branch_id=state["branch_id"], expected_revision=state["revision"])
    assert candidate["seed"]["guidance_snapshot"] == source["guidance_snapshot"]
    staged = agent.context.stage_session(str(path), branch_id=candidate["candidate_branch_id"])
    assert staged.guidance_snapshot == agent.context.guidance_snapshot
    assert staged.guidance_snapshot is not agent.context.guidance_snapshot
    staged.guidance_snapshot["names"].append("candidate-only")
    assert "candidate-only" not in agent.context.guidance_snapshot["names"]
    assert SessionStore(path).header_path.read_bytes() == source_bytes
    assert agent.context.system_prompt == "Shared identity"
    assert json.loads(source_bytes)["system_prompt"] == "Shared identity"


@pytest.mark.parametrize("mode", ["minimal_mode", "local_mode"])
def test_guidance_commands_cannot_inject_isolated_modes(tmp_path, monkeypatch, mode):
    root = project(tmp_path)
    agent, _llm, _store = agent_for(root, monkeypatch)
    setattr(agent, mode, True)
    agent.context.set_stable_system_suffix("")
    agent.refresh_session_guidance()
    assert agent.context.effective_system_prompt == "Shared identity"
    assert agent.context.guidance_snapshot == {}


def test_shared_commands_reject_busy_application_before_saving(tmp_path, monkeypatch):
    root = project(tmp_path)
    agent, _llm, store = agent_for(root, monkeypatch)
    before = copy.deepcopy(agent.context.__dict__)
    output, error = execute_skill_command(store, ["create", "blocked", "description", "--now"], agent=agent, busy=True)
    assert error and not output
    assert not (store.root / "user" / "blocked").exists()
    assert agent.context.messages == before["messages"]
    output, error = execute_guidance_command(agent, ["refresh", "--now"], busy=True)
    assert error and not output
    output, error = execute_skill_command(store, ["create", "saved", "description"], agent=agent)
    assert not error and "new conversation" in output
    assert agent.guidance_status()["pending"]["skills"]
    output, error = execute_skill_command(store, ["create", "applied", "description", "--now"], agent=agent)
    assert not error and "Applied saved guidance" in output
    assert not agent.guidance_status()["pending"]["skills"]


def test_main_agent_discovers_regional_guidance_at_tool_boundary(tmp_path, monkeypatch):
    root = project(tmp_path)
    area = root / "src"
    area.mkdir()
    (area / "AGENTS.md").write_text("Regional contract", encoding="utf-8")
    agent, _llm, _ = agent_for(root, monkeypatch)

    class ToolLLM(RecordingLLM):
        async def chat_stream(self, messages, tools, **kwargs):
            self.requests.append(copy.deepcopy((messages, tools)))
            if len(self.requests) == 1:
                yield {"type": "tool_calls", "calls": [{"id": "read-1", "name": "read_file",
                        "arguments": '{"path":"src/file.py"}'}], "content": "", "reasoning_content": "", "usage": None}
            else:
                yield {"type": "done", "content": "done", "usage": None}

    llm = ToolLLM()
    agent.llm = llm
    agent.tools.register(ToolDef("read_file", "Read fixture", {"type": "object", "properties": {"path": {"type": "string"}}}, lambda path: "file content"))
    reply(agent, "read the regional file")
    first, second = llm.requests[0][0], llm.requests[1][0]
    assert second[:len(first)] == first
    assert "Regional contract" not in first[0]["content"]
    assert "Regional contract" in str(second[len(first):])
    assert "Regional contract" not in str(agent.context.messages)


def test_worker_uses_shared_loader_and_restores_frozen_project_guidance(tmp_path, monkeypatch):
    from test_delegate_conversation_resume import Harness, RecordingLLM as WorkerLLM, _load_closed

    root = project(tmp_path)
    monkeypatch.chdir(root)
    monkeypatch.setenv("ASTRA_PROJECT_TRUST", "trusted")
    area = root / "src"
    area.mkdir()
    (area / "AGENTS.md").write_text("Worker regional contract", encoding="utf-8")

    async def scenario():
        llm = WorkerLLM([
            {"content": "", "tool_calls": [{"id": "read-1", "name": "read_file", "arguments": '{"path":"src/file.py"}'}]},
            {"content": "Report", "tool_calls": []},
        ])
        harness = Harness(root, monkeypatch, llm)
        harness.registry.register(ToolDef("read_file", "Read fixture", {"type": "object", "properties": {"path": {"type": "string"}}}, lambda path: "file content", risk="read"))
        team, spawned = await harness.spawn(keep_alive=True)
        agent_id = spawned["agent"]["id"]
        idle = await harness.idle(agent_id, calls=2)
        assert "Root contract v1" in llm.requests[0][0]["content"]
        assert "Worker regional contract" in str(llm.requests[1])
        assert llm.requests[1][:len(llm.requests[0])] == llm.requests[0]
        await harness.cancel(spawned["process"]["process_id"])
        saved = _load_closed(idle["conv_path"])
        assert any(item["body"] == "Root contract v1" for item in saved["state"]["project_guidance"]["records"])
        assert saved["state"]["runtime_context_projection"]["snapshots"]
        (root / "AGENTS.md").write_text("Different root after restart", encoding="utf-8")
        restarted = await harness.call("team_restart", team_id=team["id"], agent="worker")
        try:
            await harness.idle(agent_id, calls=3)
            assert "Root contract v1" in llm.requests[2][0]["content"]
            assert "Different root after restart" not in llm.requests[2][0]["content"]
            assert "Worker regional contract" in str(llm.requests[2])
        finally:
            await harness.cancel(restarted["process"]["process_id"])

    asyncio.run(scenario())
