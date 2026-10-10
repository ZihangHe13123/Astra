import asyncio
import json
import os
import shlex
import subprocess
import sys

import pytest

from agent.runtime.task_store import TaskStore
from agent.runtime.agent_team import AgentTeamStore
from agent.runtime.tools import delegate
from agent.runtime.tools.code import register_code_tools
from agent.runtime.tools.delegate import register_delegate_tools
from agent.runtime.tools.files import FilesystemPolicy, FilesystemRoot, register_file_tools
from agent.runtime.tools.git import register_git_tools
from agent.runtime.tools.processes import ProcessManager
from agent.runtime.tools.registry import ToolRegistry
from agent.sandbox.docker import DockerSandbox
from agent.sandbox.local import LocalSandbox


def _print_working_directory() -> str:
    """A command cmd.exe and bash both run; on Windows `pwd` would be sent to WSL."""
    argv = [sys.executable, "-c", "import os; print(os.getcwd())"]
    return subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], text=True, capture_output=True, check=True).stdout.strip()


class WorkspaceLLM:
    def __init__(self, phase):
        self.phase = phase
        self.index = 0
        self.results = []
        self.prompts = []

    async def chat(self, messages, **kwargs):
        self.prompts.append(messages[0]["content"])
        self.results = [message["content"] for message in messages if message["role"] == "tool"]
        suffix = self.phase
        calls = [
            ("read_file", {"path": "marker.txt"}),
            ("stat_file", {"path": "marker.txt"}),
            ("edit_file", {"path": "marker.txt", "old": f"child-{suffix}", "new": f"edited-{suffix}"}),
            ("apply_patch", {"patch": f"*** Begin Patch\n*** Add File: patch-{suffix}.txt\n+patch\n*** End Patch"}),
            ("execute_shell", {"command": f"{_print_working_directory()} && git rev-parse --show-toplevel"
                                          f" && echo shell > shell-{suffix}.txt",
                               "foreground_yield_ms": 5000}),
            ("git_status", {}),
        ]
        if self.index == len(calls):
            return {"content": "verified selected workspace", "tool_calls": []}
        name, args = calls[self.index]
        self.index += 1
        return {"content": "", "tool_calls": [{"id": f"call-{self.index}", "name": name, "arguments": json.dumps(args)}]}


@pytest.mark.parametrize("external", [False, True])
def test_spawn_and_restart_execute_in_selected_worktree(tmp_path, monkeypatch, external):
    async def scenario():
        root = tmp_path / "main"
        root.mkdir()
        git(root, "init", "-q")
        (root / "marker.txt").write_text("MAIN MUST STAY\n")
        git(root, "add", "marker.txt")
        git(root, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "initial")
        child = tmp_path / "external" if external else root / ".trees" / "child"
        git(root, "worktree", "add", "--detach", str(child), "HEAD")
        sandbox = LocalSandbox(workdir=str(root))
        registry = ToolRegistry()
        registry.yolo = True
        policy = FilesystemPolicy(root)
        if external:
            policy.grant(str(child), "rw")
        register_file_tools(registry, workdir=str(root), policy=policy)
        register_code_tools(registry, sandbox)
        register_git_tools(registry, workdir=str(root))
        manager = ProcessManager(artifact_dir=tmp_path / "processes")
        monkeypatch.setattr(delegate, "_sub_processes", manager)
        tasks = TaskStore(tmp_path / "tasks.db")
        parent = tasks.start_run("test", "workspace", session_id="session")
        llm = WorkspaceLLM("first")
        register_delegate_tools(registry, llm_getter=lambda: llm, task_store=tasks,
                                session_id_getter=lambda: "session", sandbox=sandbox)

        async def invoke(tool_name, **args):
            result = await registry.execute(tool_name, args, task_id=parent["id"])
            assert not result["error"], result["error"]
            return json.loads(result["output"])

        team = await invoke("team", action="create", name="workspace", goal="test")
        requested = ["read_file", "stat_file", "edit_file", "apply_patch", "execute_shell", "git_status"]
        for phase in ("first", "restart"):
            llm = WorkspaceLLM(phase)
            (child / "marker.txt").write_text(f"child-{phase}\n")
            if phase == "first":
                handle = await invoke("team_spawn", team_id=team["id"], name="worker", goal="verify",
                                      mode="worker", workspace_root=str(child), tools=requested, max_turns=12, timeout=30)
            else:
                AgentTeamStore(tasks.path).set_agent_status(handle["agent"]["id"], "interrupted")
                handle = await invoke("team_restart", team_id=team["id"], agent="worker", instruction="verify again")
            binding = handle["process"]["execution_binding"]
            assert binding["workspace_root"] == str(child.resolve())
            assert binding["tools"] == sorted(requested)
            assert binding["shell_cwd"] == str(child.resolve())
            assert handle["process"]["runtime"]["run_id"]
            result = await invoke("delegate_poll", process_id=handle["process"]["process_id"], wait_ms=5000)
            assert result["worker"]["status"] == "completed"
            evidence = result["result"]["execution_evidence"]
            assert evidence["workspace_root"] == str(child.resolve())
            assert evidence["source"] == "tool_runtime"
            assert evidence["observations"] == [
                {"tool": "execute_shell", "call_id": "call-5", "status": "completed", "exit_code": 0},
            ]
            assert (root / "marker.txt").read_text() == "MAIN MUST STAY\n"
            assert (child / "marker.txt").read_text() == f"edited-{phase}\n"
            assert (child / f"patch-{phase}.txt").read_text() == "patch\n"
            assert (child / f"shell-{phase}.txt").read_text().strip() == "shell"
            assert not (root / f"patch-{phase}.txt").exists()
            assert not (root / f"shell-{phase}.txt").exists()
            assert str(child.resolve()) in llm.results[4]
            assert f"patch-{phase}.txt" in llm.results[5]
            assert "MAIN MUST STAY" not in llm.results[0]
            assert str(child.resolve()) in llm.results[1]
            assert f"Native workspace root: {child.resolve()}" in llm.prompts[0]
    asyncio.run(scenario())


def test_workspace_grants_and_escapes_are_checked_before_spawn(tmp_path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)
    policy = FilesystemPolicy(root, [FilesystemRoot(outside, "rw")])
    sandbox = LocalSandbox(workdir=str(root))
    for path in (outside, root / ".." / "outside", root / "escape"):
        with pytest.raises(ValueError, match="explicitly granted"):
            delegate._validated_workspace_root(sandbox, str(path), isolation="shared", filesystem_policy=policy)
    grant = policy.grant(str(outside), "ro")
    assert delegate._validated_workspace_root(sandbox, str(outside), isolation="shared",
                                             filesystem_policy=policy, mode="explorer") == str(outside.resolve())
    with pytest.raises(ValueError, match="write"):
        delegate._validated_workspace_root(sandbox, str(outside), isolation="shared", filesystem_policy=policy)
    policy.revoke(grant)
    with pytest.raises(ValueError, match="explicitly granted"):
        delegate._validated_workspace_root(sandbox, str(outside), isolation="shared", filesystem_policy=policy)


def test_external_workspace_grant_revocation_stops_later_tool_calls(tmp_path, monkeypatch):
    async def scenario():
        root, outside = tmp_path / "main", tmp_path / "outside"
        root.mkdir()
        outside.mkdir()
        (outside / "source.txt").write_text("permitted read\n")
        policy = FilesystemPolicy(root)
        grant = policy.grant(str(outside), "rw")
        registry = ToolRegistry()
        registry.yolo = True
        sandbox = LocalSandbox(workdir=str(root))
        register_file_tools(registry, workdir=str(root), sandbox=sandbox, policy=policy)
        monkeypatch.setattr(delegate, "_sub_processes", ProcessManager(artifact_dir=tmp_path / "processes"))

        class RevokingLLM:
            calls = 0
            results = []

            async def chat(self, messages, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    tool, args = "read_file", {"path": "source.txt"}
                elif self.calls == 2:
                    policy.revoke(grant)
                    tool, args = "write_file", {"path": "blocked.txt", "content": "must not write"}
                else:
                    self.results = [m["content"] for m in messages if m["role"] == "tool"]
                    return {"content": "grant was revoked", "tool_calls": []}
                return {"content": "", "tool_calls": [{
                    "id": str(self.calls), "name": tool, "arguments": json.dumps(args),
                }]}

        llm = RevokingLLM()
        register_delegate_tools(registry, llm_getter=lambda: llm, sandbox=sandbox)
        result = await registry.execute("delegate_task", {
            "goal": "verify scoped grant", "mode": "worker", "workspace_root": str(outside),
            "max_turns": 6, "timeout": 10,
        }, task_id="owner")
        assert not result["error"]
        assert "permitted read" in llm.results[0]
        assert "explicitly granted write directory" in llm.results[1]
        assert not (outside / "blocked.txt").exists()
    asyncio.run(scenario())


def test_rebinding_docker_keeps_isolation_and_maps_selected_host_tree(tmp_path):
    base = DockerSandbox(workdir=str(tmp_path), memory_limit="1g", network=False, image="local-astra:test")
    child = delegate._workspace_sandbox(base, tmp_path / "child")
    assert isinstance(child, DockerSandbox)
    assert child.image == base.image
    assert child.memory_limit == base.memory_limit
    assert child.network is False
    assert f"{tmp_path / 'child'}:/workspace" in child._build_args("pwd")
    assert child.container_name != base.container_name


def test_capability_preflight_and_static_evidence_do_not_claim_execution(tmp_path, monkeypatch):
    class StaticReview:
        calls = 0

        async def chat(self, messages, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return {"content": "", "tool_calls": [{
                    "id": "read-log", "name": "read_file",
                    "arguments": json.dumps({"path": "previous-test.txt"}),
                }]}
            return {"content": "The archived log says tests passed.", "tool_calls": []}

    async def scenario():
        (tmp_path / "previous-test.txt").write_text('Tests passed. {"exit_code": 0}\n')
        registry = ToolRegistry()
        sandbox = LocalSandbox(workdir=str(tmp_path))
        register_file_tools(registry, workdir=str(tmp_path), sandbox=sandbox)
        register_code_tools(registry, sandbox)
        manager = ProcessManager(artifact_dir=tmp_path / "processes")
        monkeypatch.setattr(delegate, "_sub_processes", manager)
        llm = StaticReview()
        mailbox = register_delegate_tools(registry, llm_getter=lambda: llm, sandbox=sandbox)
        denied = await registry.execute("delegate_task", {
            "goal": "Execute tests", "mode": "explorer", "tools": ["execute_shell"],
        }, task_id="owner")
        assert "explorer mode" in denied["error"]
        assert "worker mode" in denied["error"]
        assert llm.calls == 0
        assert manager.list() == []

        started = await registry.execute("delegate_task", {
            "goal": "Read the prior log", "mode": "explorer", "tools": ["read_file"],
            "max_turns": 4, "timeout": 10, "background": True,
        }, task_id="owner")
        assert not started["error"]
        process_id = json.loads(started["output"])["process_id"]
        process = manager.get(process_id)
        await manager.wait(process, 3000)
        await asyncio.sleep(0)
        expected = {
            "source": "tool_runtime", "scope": "worker_lifetime",
            "workspace_root": str(tmp_path.resolve()),
            "observations": [], "omitted_observations": 0,
        }
        assert process.result["execution_evidence"] == expected
        notices = mailbox.drain("owner")
        assert len(notices) == 1
        assert '"observations": []' in notices[0]
        assert delegate.DelegateMailbox._bounded_payload(process.result)["execution_evidence"] == expected
    asyncio.run(scenario())
