"""The installation's host filesystem roots apply in every workspace and to its delegates."""

import asyncio
import json

import pytest

from agent.runtime.tools import delegate
from agent.runtime.tools.delegate import register_delegate_tools
from agent.runtime.tools.processes import ProcessManager
from agent.runtime.tools.registry import ToolRegistry
from agent.runtime.tools.workspace import register_workspace_tools
from agent.sandbox.local import LocalSandbox

NOTE = "visible through the installation policy"


@pytest.fixture
def installation(tmp_path, monkeypatch):
    home, shared, exchange = tmp_path / "home", tmp_path / "shared", tmp_path / "exchange"
    for path in (home, shared, exchange):
        path.mkdir()
    (shared / "note.txt").write_text(NOTE, encoding="utf-8")
    (home / "filesystem.json").write_text(json.dumps({"version": 1, "roots": [
        {"path": str(shared), "mode": "ro"},
        {"path": str(exchange), "mode": "rw"},
    ]}), encoding="utf-8")
    monkeypatch.setenv("ASTRA_HOME", str(home))
    monkeypatch.delenv("AGENT_FILESYSTEM_CONFIG", raising=False)
    return shared, exchange


@pytest.fixture
def project_with_own_policy(tmp_path):
    """A checkout that ships its own .astra/filesystem.json naming another directory."""
    project, rogue = tmp_path / "project-b", tmp_path / "rogue"
    (project / ".astra").mkdir(parents=True)
    rogue.mkdir()
    (rogue / "secret.txt").write_text("not configured by the installation", encoding="utf-8")
    (project / ".astra" / "filesystem.json").write_text(json.dumps({"roots": [
        {"path": str(rogue), "mode": "rw"},
    ]}), encoding="utf-8")
    return project, rogue


def _main_agent(workspace):
    """The registry the backend, CLI and API server build for a conversation's workspace."""
    registry = ToolRegistry()
    register_workspace_tools(registry, LocalSandbox(timeout=5, workdir=str(workspace)), workdir=str(workspace))
    # YOLO skips approval prompts, leaving the roots as the only boundary (as in the GUI).
    registry.yolo = True
    return registry


def test_every_workspace_uses_the_installation_roots(tmp_path, installation, project_with_own_policy):
    shared, exchange = installation
    project_b, rogue = project_with_own_policy
    project_a = tmp_path / "project-a"
    project_a.mkdir()

    async def scenario():
        for index, workspace in enumerate((project_a, project_b, project_a)):
            registry = _main_agent(workspace)
            read = await registry.execute("read_file", {"path": str(shared / "note.txt")})
            assert read["error"] == "" and NOTE in read["output"], (workspace, read)

            written = exchange / f"from-{index}.txt"
            write = await registry.execute("write_file", {"path": str(written), "content": "ok"})
            assert write["error"] == "" and written.read_text(encoding="utf-8") == "ok", (workspace, write)

            refused = shared / f"from-{index}.txt"
            write_ro = await registry.execute("write_file", {"path": str(refused), "content": "no"})
            assert write_ro["error"] and not refused.exists(), (workspace, write_ro)

            # A checkout's own policy file does not widen what the installation exposes.
            rogue_read = await registry.execute("read_file", {"path": str(rogue / "secret.txt")})
            assert rogue_read["error"] and "not configured" not in rogue_read["output"], (workspace, rogue_read)

    asyncio.run(scenario())


def test_configured_roots_still_need_approval_outside_yolo(tmp_path, installation):
    shared, _exchange = installation
    project = tmp_path / "project"
    project.mkdir()

    async def scenario():
        registry = _main_agent(project)
        registry.yolo = False
        requests = []

        async def deny(request):
            requests.append(request)
            return "deny"

        registry.set_approval_handler(deny)
        result = await registry.execute("read_file", {"path": str(shared / "note.txt")})
        assert result["error_type"] == "approval_denied"
        assert [request["target"] for request in requests] == [str((shared / "note.txt").resolve())]

    asyncio.run(scenario())


def test_delegates_see_the_same_roots_as_their_parent(tmp_path, monkeypatch, installation):
    shared, _exchange = installation
    project = tmp_path / "project"
    feature = project / "feature"
    feature.mkdir(parents=True)
    monkeypatch.setattr(delegate, "_sub_processes", ProcessManager(artifact_dir=tmp_path / "processes"))
    note = str(shared / "note.txt")

    class ReadThenReport:
        def __init__(self):
            self.calls = 0

        async def chat(self, **kwargs):
            del kwargs
            self.calls += 1
            if self.calls % 2:
                return {"content": "", "tool_calls": [{
                    "id": f"call-{self.calls}", "name": "read_file", "arguments": json.dumps({"path": note}),
                }]}
            return {"content": "read", "tool_calls": []}

    async def scenario():
        registry = _main_agent(project)
        llm = ReadThenReport()
        tool_outputs = []

        def record(_session_id, event):
            if event.get("type") == "tool":
                tool_outputs.append(event["output"])
            return tmp_path / "session.subagents.jsonl"

        register_delegate_tools(
            registry,
            llm_getter=lambda: llm,
            session_id_getter=lambda: "session",
            on_session_event=record,
            sandbox=LocalSandbox(timeout=5, workdir=str(project)),
        )
        parent = await registry.execute("read_file", {"path": note})
        assert parent["error"] == "" and NOTE in parent["output"]
        # Delegates take the parent's policy as it is, not a fresh read of the file.
        (tmp_path / "home" / "filesystem.json").unlink()

        for arguments in ({}, {"workspace_root": str(feature), "isolation": "shared"}):
            result = await registry.execute("delegate_task", {"goal": "read the note", "timeout": 5, **arguments})
            assert json.loads(result["output"])["worker_status"] == "completed", result
        assert len(tool_outputs) == 2
        for output in tool_outputs:
            assert NOTE in output and "outside allowed filesystem roots" not in output, output

    asyncio.run(scenario())
