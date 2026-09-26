"""Portable regressions for Claude login probes and subprocess cleanup."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from agent.runtime import claude_code_provider as ccp


@pytest.mark.parametrize("output, expected", [
    (b'{"loggedIn": true, "authMethod": "claude.ai", "subscriptionType": "max"}',
     (True, "Claude Code signed in (claude.ai · max)")),
    (b'{"loggedIn": false}',
     (False, "Claude Code is not signed in. Run `claude auth login` in a terminal.")),
    (b'[]', (False, "Claude Code is not signed in. Run `claude auth login` in a terminal.")),
    (b'not json', (False, "Claude Code returned an unreadable login status.")),
])
def test_login_probe_has_private_stdin_and_reaps_the_child(monkeypatch, output, expected):
    """An inherited Windows pipe can be locked by the TUI's pending stdin read."""
    process = SimpleNamespace(
        returncode=0, communicate=AsyncMock(return_value=(output, None)),
        wait=AsyncMock(return_value=0), kill=Mock(),
    )
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(ccp.asyncio, "create_subprocess_exec", spawn)

    assert asyncio.run(ccp.login_status("claude")) == expected

    assert spawn.call_args.args == ("claude", "auth", "status")
    assert spawn.call_args.kwargs["stdin"] == asyncio.subprocess.DEVNULL
    assert spawn.call_args.kwargs["stdout"] == asyncio.subprocess.PIPE
    assert spawn.call_args.kwargs["stderr"] == asyncio.subprocess.DEVNULL
    process.wait.assert_awaited_once()
    process.kill.assert_not_called()


@pytest.mark.parametrize("stop", ["timeout", "cancel", "read_error"])
def test_interrupted_login_probe_is_terminated_and_reaped(monkeypatch, stop):
    async def run():
        started = asyncio.Event()

        async def communicate():
            started.set()
            if stop == "read_error":
                raise OSError("broken output pipe")
            await asyncio.Event().wait()

        process = SimpleNamespace(
            returncode=None, communicate=communicate, wait=AsyncMock(return_value=-1),
        )
        terminate = Mock()
        monkeypatch.setattr(ccp.asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
        monkeypatch.setattr(ccp, "_terminate", terminate)
        task = asyncio.create_task(ccp.login_status("claude", timeout=0.01 if stop == "timeout" else 5))
        await asyncio.wait_for(started.wait(), 1)
        if stop == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        elif stop == "read_error":
            with pytest.raises(OSError, match="broken output pipe"):
                await task
        else:
            assert await task == (False, "Claude Code did not report its login status in time.")
        terminate.assert_called_once_with(process)
        process.wait.assert_awaited_once()

    asyncio.run(run())


@pytest.mark.parametrize("exited", [False, True])
def test_windows_termination_needs_no_posix_apis(monkeypatch, exited):
    # Replace only the provider's references, not Python's global os/signal modules.
    monkeypatch.setattr(ccp, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(ccp, "signal", SimpleNamespace())
    process = SimpleNamespace(returncode=0 if exited else None, kill=Mock())
    ccp._terminate(process)
    assert process.kill.call_count == (0 if exited else 1)


def test_windows_termination_tolerates_an_exit_race(monkeypatch):
    monkeypatch.setattr(ccp, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(ccp, "signal", SimpleNamespace())
    process = SimpleNamespace(returncode=None, kill=Mock(side_effect=ProcessLookupError))
    ccp._terminate(process)
    process.kill.assert_called_once_with()


@pytest.mark.parametrize("error", [None, ProcessLookupError, PermissionError])
def test_posix_termination_keeps_process_group_cleanup(monkeypatch, error):
    killpg = Mock(side_effect=error)
    monkeypatch.setattr(ccp, "os", SimpleNamespace(name="posix", killpg=killpg))
    monkeypatch.setattr(ccp, "signal", SimpleNamespace(SIGKILL=9))
    process = SimpleNamespace(returncode=None, pid=1234, kill=Mock())
    ccp._terminate(process)
    killpg.assert_called_once_with(1234, 9)
    assert process.kill.call_count == (0 if error is None else 1)


def test_posix_termination_tolerates_an_exit_race(monkeypatch):
    monkeypatch.setattr(ccp, "os", SimpleNamespace(name="posix", killpg=Mock(side_effect=ProcessLookupError)))
    monkeypatch.setattr(ccp, "signal", SimpleNamespace(SIGKILL=9))
    process = SimpleNamespace(returncode=None, pid=1234, kill=Mock(side_effect=ProcessLookupError))
    ccp._terminate(process)


def test_posix_termination_leaves_an_exited_process_alone(monkeypatch):
    killpg = Mock()
    monkeypatch.setattr(ccp, "os", SimpleNamespace(name="posix", killpg=killpg))
    process = SimpleNamespace(returncode=0, pid=1234, kill=Mock())
    ccp._terminate(process)
    killpg.assert_not_called()
    process.kill.assert_not_called()
