"""Persistent WSL PTY lifecycle tests with a deterministic subprocess double."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shlex
import sys
import time

import pytest

from agent.runtime.tools.code import register_code_tools
from agent.runtime.tools.registry import ToolRegistry
from agent.sandbox.local import LocalSandbox
from agent.sandbox.wsl_pty import PersistentWslShell


class _FakeStdin:
    def __init__(self, process: "_FakeProcess") -> None:
        self.process = process

    def write(self, payload: bytes) -> None:
        self.process.receive(payload.decode("utf-8"))

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.process.returncode = 0
        self.process.stdout.feed_eof()


class _FakeProcess:
    instances = 0

    def __init__(self) -> None:
        type(self).instances += 1
        self.returncode: int | None = None
        self.stdout = asyncio.StreamReader()
        self.stdin = _FakeStdin(self)
        self.commands: list[str] = []
        self.cwd = "/workspace"
        self.wait_calls = 0

    def receive(self, script: str) -> None:
        markers = re.findall(r"(__ASTRA_WSL_PTY_RESULT_[0-9a-f]+__:)", script)
        marker = markers[-1]
        if "__astra_cmd=" not in script:
            echoed = f"printf '\\n{marker}0\\n'\n".encode("utf-8")
            self.stdout.feed_data(echoed + f"\n{marker}0\n".encode("utf-8"))
            return
        encoded = re.search(r"printf %s ([A-Za-z0-9+/=]+) \| base64", script).group(1)
        command = base64.b64decode(encoded).decode("utf-8")
        self.commands.append(command)
        if command.startswith("cd "):
            self.cwd = command[3:].strip()
            output = b""
        else:
            output = f"{self.cwd}\n".encode("utf-8")
        self.stdout.feed_data(output + f"{marker}0\n".encode("utf-8"))

    def kill(self) -> None:
        self.returncode = -9
        self.stdout.feed_eof()

    async def wait(self) -> int:
        self.wait_calls += 1
        return self.returncode or 0


def test_persistent_wsl_shell_reuses_pty_and_preserves_state(monkeypatch) -> None:
    _FakeProcess.instances = 0

    async def spawn(*args, **kwargs):
        return _FakeProcess()

    monkeypatch.setattr("agent.sandbox.wsl_pty.asyncio.create_subprocess_exec", spawn)

    async def scenario() -> None:
        shell = PersistentWslShell(
            ["fake-wsl", "script"],
            workdir=".",
            timeout=1,
            max_output_bytes=10_000,
        )
        first = await shell.execute("cd /tmp")
        second = await shell.execute("pwd")
        assert first["exit_code"] == 0
        assert second["output"] == "/tmp"
        assert _FakeProcess.instances == 1
        await shell.close()

    asyncio.run(scenario())


def test_persistent_bash_shell_reports_configured_posix_environment(monkeypatch) -> None:
    """The transport is platform-neutral even though its compatibility name remains WSL."""
    async def spawn(*args, **kwargs):
        return _FakeProcess()

    monkeypatch.setattr("agent.sandbox.wsl_pty.asyncio.create_subprocess_exec", spawn)

    async def scenario() -> None:
        shell = PersistentWslShell(
            ["script", "-q", "/dev/null", "/bin/bash", "-i"],
            workdir=".",
            timeout=1,
            max_output_bytes=10_000,
            environment="posix",
        )
        result = await shell.execute("pwd")

        assert result["environment"] == "posix"
        assert "WSL" not in str(result)
        await shell.close()

    asyncio.run(scenario())


def test_persistent_wsl_shell_reports_exit_and_resets_after_shell_exit(monkeypatch) -> None:
    class ExitingProcess(_FakeProcess):
        def receive(self, script: str) -> None:
            markers = re.findall(r"(__ASTRA_WSL_PTY_RESULT_[0-9a-f]+__:)", script)
            marker = markers[-1]
            if "__astra_cmd=" not in script:
                self.stdout.feed_data(f"\n{marker}0\n".encode("utf-8"))
                return
            self.stdout.feed_data(b"partial output\n")
            self.returncode = 7
            self.stdout.feed_eof()

    processes: list[_FakeProcess] = []

    async def spawn(*args, **kwargs):
        process = ExitingProcess()
        processes.append(process)
        return process

    monkeypatch.setattr("agent.sandbox.wsl_pty.asyncio.create_subprocess_exec", spawn)

    async def scenario() -> None:
        shell = PersistentWslShell(
            ["fake-wsl", "script"],
            workdir=".",
            timeout=1,
            max_output_bytes=5,
        )
        result = await shell.execute("exit")
        assert result["exit_code"] == -1
        assert "persistent bash shell was reset" in result["output"]
        assert len(processes) == 1
        await shell.close()

    asyncio.run(scenario())


def test_close_nowait_reaps_the_killed_pty_on_the_running_loop(monkeypatch) -> None:
    processes: list[_FakeProcess] = []

    async def spawn(*args, **kwargs):
        process = _FakeProcess()
        processes.append(process)
        return process

    monkeypatch.setattr("agent.sandbox.wsl_pty.asyncio.create_subprocess_exec", spawn)

    async def scenario() -> None:
        shell = PersistentWslShell(
            ["fake-wsl", "script"],
            workdir=".",
            timeout=1,
            max_output_bytes=10_000,
        )
        await shell.execute("pwd")
        shell.close_nowait()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert processes[0].wait_calls == 1

    asyncio.run(scenario())


class _HangingProcess(_FakeProcess):
    """Answers the startup, then writes part of a command's output and never finishes it."""

    written = b"\x1b[32mbuilt 3 of 9\x1b[0m\r\nstill going\r\n"

    def receive(self, script: str) -> None:
        if "__astra_cmd=" not in script:
            super().receive(script)
            return
        self.commands.append(script)
        self.stdout.feed_data(self.written)


def _spawning(monkeypatch, *kinds: type[_FakeProcess]) -> list[_FakeProcess]:
    """Start one process of each kind in turn; the last kind serves every later start."""
    processes: list[_FakeProcess] = []

    async def spawn(*args, **kwargs):
        process = kinds[min(len(processes), len(kinds) - 1)]()
        processes.append(process)
        return process

    monkeypatch.setattr("agent.sandbox.wsl_pty.asyncio.create_subprocess_exec", spawn)
    return processes


def test_timed_out_command_returns_what_it_had_written_and_the_shell_is_reset(monkeypatch) -> None:
    processes = _spawning(monkeypatch, _HangingProcess, _FakeProcess)

    async def scenario() -> None:
        shell = PersistentWslShell(
            ["fake-script", "bash"], workdir=".", timeout=0.05, max_output_bytes=10_000,
            environment="posix",
        )
        streamed: list[tuple[str, str]] = []
        result = await shell.execute("make", on_output=lambda stream, text: streamed.append((stream, text)))

        # Cleaned like the output of a finished command.
        assert result["output"] == "built 3 of 9\nstill going"
        assert streamed == [("stdout", result["output"])]
        assert result["exit_code"] == -1 and result["timed_out"] is True
        assert result["error"].startswith(
            "[Timeout] Execution exceeded 0.05s; persistent bash shell was reset"
        )
        # The reset is real: the shell that held the command is gone and the next call gets a new one.
        assert result["shell_reset"] is True
        assert processes[0].returncode is not None
        after = await shell.execute("pwd")
        assert after["output"] == "/workspace" and "shell_reset" not in after
        assert len(processes) == 2
        await shell.close()

    asyncio.run(scenario())


def test_timed_out_command_output_is_bounded_like_a_finished_one(monkeypatch) -> None:
    class Chatty(_HangingProcess):
        written = b"0123456789\n"

    _spawning(monkeypatch, Chatty)

    async def scenario() -> None:
        shell = PersistentWslShell(["fake-script", "bash"], workdir=".", timeout=0.05, max_output_bytes=5)
        result = await shell.execute("yes")
        assert result["output"] == "01234\n[Output truncated: persistent bash output exceeded 5 bytes]"
        assert result["timed_out"] is True
        await shell.close()

    asyncio.run(scenario())


def test_cancelled_command_resets_the_shell_and_the_next_result_says_so(monkeypatch) -> None:
    processes = _spawning(monkeypatch, _HangingProcess, _FakeProcess)

    async def scenario() -> None:
        shell = PersistentWslShell(["fake-script", "bash"], workdir=".", timeout=30, max_output_bytes=10_000)
        call = asyncio.create_task(shell.execute("sleep 100"))
        while not (processes and processes[0].commands):
            await asyncio.sleep(0)
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        assert processes[0].returncode is not None

        # The cancelled call returned nothing, so the next one carries the news.
        first = await shell.execute("pwd")
        assert first["output"].startswith("[An earlier bash command was cancelled")
        notice, output = first["output"].split("\n", 1)
        assert "fresh current directory and environment" in notice
        assert output == "/workspace" and first["exit_code"] == 0 and "shell_reset" not in first
        assert (await shell.execute("pwd"))["output"] == "/workspace"
        await shell.close()

    asyncio.run(scenario())


def test_shell_that_does_not_start_is_not_reported_as_the_command_timing_out(monkeypatch) -> None:
    class Silent(_FakeProcess):
        def receive(self, script: str) -> None:
            return None

    _spawning(monkeypatch, Silent)
    monkeypatch.setattr("agent.sandbox.wsl_pty._STARTUP_TIMEOUT_SECONDS", 0.05, raising=False)

    async def scenario() -> None:
        shell = PersistentWslShell(["fake-script", "bash"], workdir=".", timeout=300, max_output_bytes=10_000)
        with pytest.raises(RuntimeError, match="did not start within 0.05s; the command was not run"):
            await shell.execute("pwd")
        await shell.close()

    asyncio.run(scenario())


_REAL_PTY = pytest.mark.skipif(
    sys.platform != "darwin",
    reason="runs the real persistent Bash PTY; its hangup behaviour was checked on macOS only",
)


def _late_writer(started, late, delay: float) -> str:
    """A shell command for a child that records its start and, unless it is stopped first, writes a file later."""
    code = (
        "import time; "
        f"open({str(started)!r}, 'w').write(str(time.time())); "
        "print('child-before', flush=True); "
        f"time.sleep({delay}); open({str(late)!r}, 'w').write('late')"
    )
    return shlex.join([sys.executable, "-c", code])


async def _child_started(started, within: float = 15.0) -> float:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        try:
            return float(started.read_text())
        except (OSError, ValueError):
            await asyncio.sleep(0.02)
    raise AssertionError("the child process did not start")


@_REAL_PTY
def test_real_bash_stopped_at_the_limit_keeps_its_output_and_is_hung_up(tmp_path, monkeypatch) -> None:
    # Room for a slow interpreter start; the child would write after the limit.
    monkeypatch.setenv("ASTRA_PERSISTENT_BASH_TIMEOUT", "2")

    async def scenario() -> None:
        registry = ToolRegistry()
        sandbox = LocalSandbox(timeout=1, workdir=str(tmp_path))
        register_code_tools(registry, sandbox)
        started, late = tmp_path / "started", tmp_path / "late"
        try:
            await registry.execute("bash", {"command": "mkdir inner && cd inner && export KEPT=1"})
            result = await registry.execute(
                "bash", {"command": f"echo shell-before; {_late_writer(started, late, 2.6)}; echo shell-after"}
            )
            text = result["output"]
            assert "shell-before" in text and "child-before" in text and "shell-after" not in text
            assert text.index("child-before") < text.index("[Timeout] Execution exceeded 2s")
            assert "persistent bash shell was reset and its command hung up" in text
            assert result["partial"] is True
            assert result["execution"] == {"status": "unknown", "exit_code": -1}
            began = float(started.read_text())
            await asyncio.sleep(max(0.0, began + 2.6 + 0.6 - time.time()))
            assert not late.exists()
            # What the result said about the shell is true: the directory and the variable are gone.
            after = await registry.execute("bash", {"command": "pwd; echo KEPT=$KEPT"})
            where, kept = after["output"].splitlines()
            assert os.path.samefile(where, tmp_path) and kept == "KEPT="
        finally:
            await sandbox.close()

    asyncio.run(scenario())


@_REAL_PTY
def test_cancelling_a_real_bash_call_stops_its_command(tmp_path) -> None:
    async def scenario() -> None:
        registry = ToolRegistry()
        sandbox = LocalSandbox(timeout=30, workdir=str(tmp_path))
        register_code_tools(registry, sandbox)
        started, late = tmp_path / "started", tmp_path / "late"
        call = asyncio.create_task(registry.execute(
            "bash", {"command": f"echo shell-before; {_late_writer(started, late, 1.0)}; echo shell-after"}
        ))
        try:
            began = await _child_started(started)
            call.cancel()
            with pytest.raises(asyncio.CancelledError):
                await call
            await asyncio.sleep(max(0.0, began + 1.0 + 0.6 - time.time()))
            assert not late.exists()
            assert json.loads((await registry.execute("process_list", {}))["output"]) == []
            # The shell is free again at once, and says what happened to it.
            after = await asyncio.wait_for(registry.execute("bash", {"command": "echo next"}), timeout=20)
            assert after["output"].startswith("[An earlier bash command was cancelled")
            assert after["output"].endswith("next")
        finally:
            call.cancel()
            await sandbox.close()

    asyncio.run(scenario())
