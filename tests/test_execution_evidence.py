"""Execution receipts survive real tools, process lifecycle, and durable shaping."""

import asyncio
import json
import os
import shlex
import signal
import subprocess
import sys
import time

import pytest

from agent.runtime.coding_contracts import append_check, new_contract
from agent.runtime.react import ReActAgent
from agent.runtime.task_store import TaskStore, format_task_detail
from agent.runtime.tool_execution import ExecutionResult
from agent.runtime.tools.code import register_code_tools
from agent.runtime.tools.registry import ToolDef, ToolRegistry
from agent.sandbox.docker import DockerSandbox
from agent.sandbox.local import LocalSandbox


@pytest.mark.parametrize("tool,args,code", [
    ("execute_python", {"code": "print('ok')"}, 0),
    ("execute_python", {"code": "raise SystemExit(7)"}, 7),
    ("execute_shell", {"command": "exit 9"}, 9),
])
def test_local_execution_receipt_survives_registry(tool, args, code, tmp_path):
    async def run():
        registry = ToolRegistry()
        sandbox = LocalSandbox(timeout=3, workdir=str(tmp_path))
        register_code_tools(registry, sandbox)
        try:
            result = await registry.execute(tool, {**args, "foreground_yield_ms": 0})
            assert result["execution"] == {"status": "completed", "exit_code": code}
            safe = registry.persistence_safe_result(registry.get(tool), result)
            assert json.loads(json.dumps(safe))["execution"] == result["execution"]
            context = ReActAgent._tool_result_context({**result, "name": tool, "tool_output": result.get("output")})
            assert "status: success" not in context if code else "status: success" in context
        finally:
            await sandbox.close()
    asyncio.run(run())


def _python_command(code: str) -> str:
    argv = [sys.executable, "-c", code]
    return subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)


@pytest.mark.parametrize("tool,args", [
    ("execute_python", {"code": "import time\ntime.sleep(5)"}),
    ("execute_shell", {"command": _python_command("import time; time.sleep(5)")}),
])
def test_foreground_run_stopped_at_the_sandbox_limit_is_timed_out_and_says_how_to_rerun(
    tool, args, tmp_path,
):
    async def run():
        registry = ToolRegistry()
        sandbox = LocalSandbox(timeout=1, workdir=str(tmp_path))
        register_code_tools(registry, sandbox)
        try:
            result = await registry.execute(tool, {**args, "foreground_yield_ms": 0})
            assert result["execution"] == {"status": "timed_out", "exit_code": -1}
            text = result["output"] + result["error"]
            assert "[Timeout] Execution exceeded 1s" in text
            for expected in ("foreground_yield_ms", "background=true", "process_poll", "process_read"):
                assert expected in text, expected
            context = ReActAgent._tool_result_context({**result, "name": tool, "tool_output": text})
            assert "status: success" not in context
        finally:
            await sandbox.close()
    asyncio.run(run())


# One line, so the same text is valid as execute_python code and as a `python -c` argument.
_PRINT_THEN_HANG = (
    "import sys, time; print('printed-before-0'); print('printed-before-1'); "
    "print('warned-before', file=sys.stderr); sys.stdout.flush(); sys.stderr.flush(); "
    "time.sleep(8); print('printed-after')"
)
# These runs must have printed before the limit, so it leaves room for a slow interpreter start.
_LIMIT = 2


@pytest.mark.parametrize("tool,args", [
    ("execute_python", {"code": _PRINT_THEN_HANG}),
    ("execute_shell", {"command": _python_command(_PRINT_THEN_HANG)}),
])
def test_foreground_run_stopped_at_the_sandbox_limit_keeps_what_it_had_written(tool, args, tmp_path):
    async def run():
        registry = ToolRegistry()
        sandbox = LocalSandbox(timeout=_LIMIT, workdir=str(tmp_path))
        register_code_tools(registry, sandbox)
        try:
            result = await registry.execute(tool, {**args, "foreground_yield_ms": 0})
            assert result["execution"] == {"status": "timed_out", "exit_code": -1}
            text = result["output"] + result["error"]
            for expected in ("printed-before-0", "printed-before-1", "warned-before"):
                assert expected in text, expected
            assert "printed-after" not in text
            # The output comes first, then the stop and how to rerun.
            assert (
                text.index("printed-before-1")
                < text.index(f"[Timeout] Execution exceeded {_LIMIT}s")
                < text.index("background=true")
            )
        finally:
            await sandbox.close()
    asyncio.run(run())


def test_python_run_stopped_at_the_sandbox_limit_is_marked_partial(tmp_path):
    async def run():
        registry = ToolRegistry()
        sandbox = LocalSandbox(timeout=_LIMIT, workdir=str(tmp_path))
        register_code_tools(registry, sandbox)
        try:
            stopped = await registry.execute(
                "execute_python", {"code": _PRINT_THEN_HANG, "foreground_yield_ms": 0}
            )
            assert stopped["partial"] is True
            context = ReActAgent._tool_result_context(
                {**stopped, "name": "execute_python", "tool_output": stopped["output"]}
            )
            assert "printed-before-1" in context
            assert "Result completeness: partial" in context
            assert "Result completeness: complete" not in context
            finished = await registry.execute(
                "execute_python", {"code": "print('done')", "foreground_yield_ms": 0}
            )
            assert "partial" not in finished

            # A shell run reports the stop as a failure; that failure is partial too.
            shell = await registry.execute(
                "execute_shell",
                {"command": "echo shell-before; sleep 8", "foreground_yield_ms": 0},
            )
            assert shell["partial"] is True
            shell_context = ReActAgent._tool_result_context(
                {**shell, "name": "execute_shell", "tool_output": shell["error"]}
            )
            assert "shell-before" in shell_context
            assert "Result completeness: partial" in shell_context
            failed = await registry.execute(
                "execute_shell", {"command": "exit 3", "foreground_yield_ms": 0}
            )
            assert failed["error"] and not failed.get("partial")
        finally:
            await sandbox.close()
    asyncio.run(run())


def test_stopped_run_output_is_bounded_and_readable_like_a_finished_one(tmp_path):
    async def run():
        registry = ToolRegistry(artifact_dir=tmp_path / "artifacts")
        sandbox = LocalSandbox(timeout=_LIMIT, workdir=str(tmp_path), max_output_bytes=64)
        register_code_tools(registry, sandbox)
        code = "import time\nprint('begin-' + 'x' * 200 + '-end', flush=True)\ntime.sleep(8)"
        try:
            result = await registry.execute("execute_python", {"code": code, "foreground_yield_ms": 0})
            assert result["execution"]["status"] == "timed_out"
            assert "begin-" in result["output"] and "x" * 200 not in result["output"]
            assert "[Output truncated: stdout exceeded 64 bytes]" in result["output"]
            assert f"[Timeout] Execution exceeded {_LIMIT}s" in result["output"]
            handle = json.loads(result["output"].split("[Read full output: ")[1].split("]")[0])
            readback = await registry.execute(handle["tool"], handle["arguments"])
            assert not readback["error"], readback
            assert "begin-" + "x" * 200 + "-end" in json.loads(readback["output"])["content"]
        finally:
            await sandbox.close()
    asyncio.run(run())


def _late_writer(started, late, delay: float) -> str:
    """Python source of a child that records when it started and, unless it is stopped first, writes a file later."""
    return (
        "import time; "
        f"open({str(started)!r}, 'w').write(str(time.time())); "
        "print('child-before', flush=True); "
        f"time.sleep({delay}); open({str(late)!r}, 'w').write('late')"
    )


async def _child_started(started, within: float = 15.0) -> float:
    """Wait for the child of ``_late_writer`` and return the time it started at."""
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        try:
            return float(started.read_text())
        except (OSError, ValueError):
            await asyncio.sleep(0.02)
    raise AssertionError("the child process did not start")


async def _until(moment: float) -> None:
    await asyncio.sleep(max(0.0, moment - time.time()))


_POSIX_GROUPS = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX process groups; the Windows tree kill is not exercised by this test",
)


@_POSIX_GROUPS
@pytest.mark.parametrize("tool", ["execute_shell", "execute_python"])
def test_run_stopped_at_the_sandbox_limit_stops_the_processes_it_started(tool, tmp_path):
    """The limit used to kill only the run's own process: a child lived on and kept writing."""
    async def run():
        registry = ToolRegistry()
        sandbox = LocalSandbox(timeout=_LIMIT, workdir=str(tmp_path))
        register_code_tools(registry, sandbox)
        started, late = tmp_path / "started", tmp_path / "late"
        # Later than the limit, so only a child that outlived the stop writes it.
        delay = _LIMIT + 0.6
        child = _late_writer(started, late, delay)
        if tool == "execute_shell":
            args = {"command": f"echo shell-before; {_python_command(child)}; echo shell-after"}
        else:
            args = {"code": (
                "import subprocess, sys, time\n"
                f"subprocess.Popen([sys.executable, '-c', {child!r}])\n"
                "time.sleep(30)"
            )}
        try:
            result = await registry.execute(tool, {**args, "foreground_yield_ms": 0})
            assert result["execution"] == {"status": "timed_out", "exit_code": -1}
            text = result["output"] + result["error"]
            assert "child-before" in text and "shell-after" not in text
            assert "still holds its output" not in text
            began = float(started.read_text())
            await _until(began + delay + 0.6)
            assert not late.exists()
        finally:
            await sandbox.close()
    asyncio.run(run())


def test_only_a_process_the_sandbox_really_spawned_has_its_tree_signalled(monkeypatch):
    """The pid of a stand-in (as tests pass in) names nothing the sandbox created: only the object is killed."""
    killed, signalled = [], []

    class StandIn:
        pid = 4242
        returncode = None
        stdin = None

        def __init__(self):
            self.stdout, self.stderr = asyncio.StreamReader(), asyncio.StreamReader()
            self._ended = asyncio.Event()

        def kill(self):
            killed.append(self.pid)
            self.returncode = -9
            self.stdout.feed_eof()
            self.stderr.feed_eof()
            self._ended.set()

        async def wait(self):
            await self._ended.wait()
            return self.returncode

    async def spawn(*_args, **_kwargs):
        return StandIn()

    monkeypatch.setattr("agent.sandbox.local.asyncio.create_subprocess_exec", spawn)
    monkeypatch.setattr("agent.sandbox.local.asyncio.create_subprocess_shell", spawn)
    if hasattr(os, "killpg"):
        # Would the group be signalled, it would look like one the pid leads.
        monkeypatch.setattr("agent.sandbox.local.os.getpgid", lambda pid: pid)
        monkeypatch.setattr("agent.sandbox.local.os.killpg", lambda pid, _signal: signalled.append(pid))

    result = asyncio.run(LocalSandbox(timeout=0.05).execute_shell_stream("sleep 5"))

    assert result["timed_out"] is True
    assert killed == [4242]
    assert signalled == []


@_POSIX_GROUPS
def test_stopped_run_returns_without_waiting_for_a_process_that_left_its_group(tmp_path):
    """A process in a session of its own is out of the stop's reach and may hold stdout; the result says so."""
    async def run():
        sandbox = LocalSandbox(timeout=_LIMIT, workdir=str(tmp_path))
        child = (
            "import os, time; print('child-before', flush=True); "
            "open('child.pid', 'w').write(str(os.getpid())); time.sleep(12)"
        )
        launcher = (
            "import subprocess, sys; "
            f"subprocess.Popen([sys.executable, '-c', {child!r}], start_new_session=True)"
        )
        streamed: list[str] = []
        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            result = await sandbox.execute_shell_stream(
                f"echo shell-before; {_python_command(launcher)}; sleep 12; echo shell-after",
                on_output=lambda _stream, text: streamed.append(text),
            )
            elapsed = loop.time() - started
            assert result["exit_code"] == -1 and result["timed_out"] is True
            assert result["error"] == (
                f"[Timeout] Execution exceeded {_LIMIT}s; a process the run started still holds "
                "its output and may still be running"
            )
            assert "shell-before" in result["output"] and "child-before" in result["output"]
            assert "shell-after" not in result["output"]
            assert "".join(streamed) == result["output"]
            # The child sleeps for 12s; waiting for its pipes to close would take that long.
            assert elapsed < _LIMIT + 5, elapsed
        finally:
            pid_file = tmp_path / "child.pid"
            if pid_file.exists():
                try:
                    os.kill(int(pid_file.read_text()), signal.SIGKILL)
                except (OSError, ValueError):
                    pass
            # Let the closed pipes be seen before the loop goes away.
            await asyncio.sleep(0.2)
            await sandbox.close()
    asyncio.run(run())


@_POSIX_GROUPS
def test_process_cancel_stops_what_a_background_run_started(tmp_path):
    async def run():
        registry = ToolRegistry()
        sandbox = LocalSandbox(timeout=30, workdir=str(tmp_path))
        manager = register_code_tools(registry, sandbox)
        started, late = tmp_path / "started", tmp_path / "late"
        child = _late_writer(started, late, delay=1.0)
        launched = await registry.execute("execute_shell", {
            "command": f"echo shell-before; {_python_command(child)}; echo shell-after",
            "background": True,
        })
        pid = launched["execution"]["process_id"]
        try:
            began = await _child_started(started)
            cancelled = await registry.execute("process_cancel", {"process_id": pid})
            assert cancelled["execution"]["status"] == "cancelled"
            await _until(began + 1.0 + 0.6)
            assert not late.exists()
        finally:
            await manager.cancel(manager.get(pid))
            await sandbox.close()
    asyncio.run(run())


@pytest.mark.skipif(sys.platform == "win32", reason="the stand-in CLI is a POSIX script")
def test_docker_run_stopped_at_the_limit_keeps_what_it_had_written(tmp_path):
    """DockerSandbox's own execution path, with a script standing in for the docker CLI.

    The script runs the command locally, so this covers how the sandbox handles
    its child process and not Docker itself.
    """
    cli = tmp_path / "docker-stand-in"
    cli.write_text(
        "#!/bin/sh\n"
        'if [ "$1 $2" = "image inspect" ]; then exit 0; fi\n'
        "for command; do :; done\n"  # the last argument of `run ... sh -c <command>`
        'exec sh -c "$command"\n',
        encoding="utf-8",
    )
    cli.chmod(0o755)

    async def run():
        sandbox = DockerSandbox(timeout=_LIMIT, workdir=str(tmp_path), docker_cmd=str(cli))
        result = await sandbox.execute_shell_stream("exec " + _python_command(_PRINT_THEN_HANG))
        assert result["exit_code"] == -1 and result["timed_out"] is True
        assert result["output"].split() == ["printed-before-0", "printed-before-1"]
        assert result["error"].splitlines() == [
            "warned-before", f"[Timeout] Docker execution exceeded {_LIMIT}s",
        ]
    asyncio.run(run())


@pytest.mark.skipif(sys.platform == "win32", reason="the stand-in CLI is a POSIX script")
@pytest.mark.parametrize("stop", ["limit", "cancel"])
def test_docker_one_shot_container_is_removed_when_its_run_is_stopped(stop, tmp_path):
    """Killing the docker client leaves its container running. The stand-in records what it was asked to do."""
    cli = tmp_path / "docker-stand-in"
    log = tmp_path / "docker.log"
    cli.write_text(
        "#!/bin/sh\n"
        'if [ "$1 $2" = "image inspect" ]; then exit 0; fi\n'
        f'if [ "$1 $2" = "rm -f" ]; then echo "rm $3" >> {shlex.quote(str(log))}; exit 0; fi\n'
        'name=""; previous=""\n'
        'for argument; do\n'
        '  if [ "$previous" = "--name" ]; then name="$argument"; fi\n'
        '  previous="$argument"\n'
        "done\n"
        f'echo "run $name" >> {shlex.quote(str(log))}\n'
        'exec sh -c "$previous"\n',  # the last argument of `run ... sh -c <command>`
        encoding="utf-8",
    )
    cli.chmod(0o755)

    async def run():
        sandbox = DockerSandbox(
            timeout=1 if stop == "limit" else 30, workdir=str(tmp_path), docker_cmd=str(cli),
        )
        ready = asyncio.Event()
        call = asyncio.create_task(sandbox.execute_shell_stream(
            "echo ready; exec sleep 20", on_output=lambda _stream, _text: ready.set(),
        ))
        if stop == "limit":
            assert (await call)["timed_out"] is True
        else:
            await asyncio.wait_for(ready.wait(), timeout=15)
            call.cancel()
            with pytest.raises(asyncio.CancelledError):
                await call
        started, removed = log.read_text(encoding="utf-8").split("\n")[:2]
        assert started.startswith("run agent-sandbox-run-")
        assert removed == "rm " + started.removeprefix("run ")
    asyncio.run(run())


def test_rerunning_as_the_timeout_message_says_outlives_the_sandbox_limit(tmp_path):
    async def run():
        registry = ToolRegistry()
        sandbox = LocalSandbox(timeout=1, workdir=str(tmp_path))
        register_code_tools(registry, sandbox)
        code = "import time\ntime.sleep(1.5)\nprint('finished')"
        try:
            stopped = await registry.execute("execute_python", {"code": code, "foreground_yield_ms": 0})
            assert stopped["execution"]["status"] == "timed_out"
            rerun = await registry.execute("execute_python", {"code": code, "foreground_yield_ms": 20_000})
            assert rerun["execution"] == {"status": "completed", "exit_code": 0}
            assert "finished" in rerun["output"]
        finally:
            await sandbox.close()
    asyncio.run(run())


def test_timeout_without_an_unlimited_background_path_does_not_suggest_one(tmp_path):
    """A sandbox whose limit also covers background runs must not be told to rerun that way."""
    class LimitedSandbox:
        workdir = str(tmp_path)

        async def execute_python_stream(self, code, on_output):
            return {"output": "", "error": "[Timeout] Docker execution exceeded 5s", "exit_code": -1}

    registry = ToolRegistry()
    register_code_tools(registry, LimitedSandbox())
    result = asyncio.run(registry.execute("execute_python", {"code": "slow", "foreground_yield_ms": 0}))
    assert result["execution"] == {"status": "timed_out", "exit_code": -1}
    assert "[Timeout] Docker execution exceeded 5s" in result["output"]
    assert "background=true" not in result["output"]


def test_failed_shell_command_error_keeps_stdout_stderr_and_exit_code(tmp_path):
    async def run():
        registry = ToolRegistry()
        sandbox = LocalSandbox(timeout=10, workdir=str(tmp_path))
        register_code_tools(registry, sandbox)
        command = _python_command(
            "import sys; print('out-line'); print('err-line', file=sys.stderr); sys.exit(7)"
        )
        try:
            result = await registry.execute("execute_shell", {"command": command, "foreground_yield_ms": 0})
            assert result["output"] == ""
            for expected in ("out-line", "err-line", "exit code 7"):
                assert expected in result["error"], expected
            assert result["execution"] == {"status": "completed", "exit_code": 7}
        finally:
            await sandbox.close()
    asyncio.run(run())


def test_foreground_yield_ceiling_allows_ninety_seconds(tmp_path):
    """The foreground wait ceiling for code tools is 90s (raised from 60s)."""
    async def run():
        registry = ToolRegistry()
        sandbox = LocalSandbox(timeout=3, workdir=str(tmp_path))
        register_code_tools(registry, sandbox)
        try:
            for name in ("execute_python", "execute_shell", "notebook_execute"):
                properties = registry.get(name).parameters["properties"]
                assert properties["foreground_yield_ms"]["maximum"] == 90_000
            accepted = await registry.execute(
                "execute_shell", {"command": "echo ok", "foreground_yield_ms": 90_000}
            )
            assert accepted["execution"] == {"status": "completed", "exit_code": 0}
            rejected = await registry.execute(
                "execute_shell", {"command": "echo ok", "foreground_yield_ms": 90_001}
            )
            assert "must be between 0 and 90000" in rejected["error"]
        finally:
            await sandbox.close()
    asyncio.run(run())


@pytest.mark.parametrize("terminal", ["completed", "cancelled"])
def test_background_receipts_remain_pending_until_observed_terminal_result(tmp_path, terminal):
    async def run():
        release = asyncio.Event()

        class ControlledSandbox:
            workdir = str(tmp_path)

            async def execute_python_stream(self, code, on_output):
                await release.wait()
                return {"output": "", "error": "", "exit_code": 3}

        registry = ToolRegistry()
        manager = register_code_tools(registry, ControlledSandbox())
        initial = await registry.execute("execute_python", {"code": "controlled", "background": True})
        pid = initial["execution"]["process_id"]
        assert initial["execution"] == {"status": "running", "exit_code": None, "process_id": pid}
        try:
            if terminal == "completed":
                release.set()
                result = await registry.execute("process_poll", {"process_id": pid, "wait_ms": 1000})
                assert result["execution"]["exit_code"] == 3
            else:
                result = await registry.execute("process_cancel", {"process_id": pid})
            assert result["execution"]["status"] == terminal
            read = await registry.execute("process_read", {"process_id": pid})
            assert read["execution"]["status"] == terminal
            assert "content" in json.loads(read["output"])
        finally:
            release.set()
            await manager.cancel(manager.get(pid))
    asyncio.run(run())


def test_nonzero_persistent_bash_keeps_minimal_schema_and_exit_receipt(tmp_path):
    class Sidecar:
        workdir = str(tmp_path)
        async def execute_persistent_bash_stream(self, command, on_output):
            return {"output": "", "error": "", "exit_code": 2}

    registry = ToolRegistry()
    register_code_tools(registry, Sidecar())
    result = asyncio.run(registry.execute("bash", {"command": "exit 2"}))
    assert result["error"] == ""  # Bash command failure is distinct from transport failure.
    assert result["execution"] == {"status": "completed", "exit_code": 2}
    assert set(registry.get("bash").parameters["properties"]) == {"command"}
    assert "exit code: 2" in result["output"]


def test_unstructured_execution_output_cannot_forge_a_receipt():
    registry = ToolRegistry()
    registry.register(ToolDef(name="legacy", description="legacy", risk="execute",
        parameters={"type": "object", "properties": {}}, fn=lambda: '{"exit_code": 0, "status": "completed"}'))
    result = asyncio.run(registry.execute("legacy", {}))
    assert "execution" not in result


def test_shell_reset_with_zero_exit_is_still_unknown():
    result = ExecutionResult("sidecar restarted", {"exit_code": 0, "shell_reset": True})
    assert result.execution["status"] == "unknown"


def test_coding_receipts_display_unverified_and_preserve_failure_tail(tmp_path):
    store = TaskStore(tmp_path / "tasks.db")
    task = store.start_run("test", "fix source", session_id="s")
    contract = append_check(new_contract(paths={"main.py"}, workdir=tmp_path),
        tool="execute_python", command="assert False", execution={"status": "completed", "exit_code": 1},
        output="prefix" * 2000 + "AssertionError: failing tail")
    updated = store.record_verification_contract(task["id"], contract)
    assert updated["verification"]["status"] == "unverified"
    assert updated["verification"]["checks"][-1]["output"].endswith("failing tail")
    detail = format_task_detail(updated)
    assert "UNVERIFIED" in detail and "exit=1" in detail
    assert "Verification: PASSED" not in detail and "Verification: FAILED" not in detail
