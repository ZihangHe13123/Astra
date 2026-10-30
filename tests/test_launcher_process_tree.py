"""Launcher commands never outlive their deadline, and leave nothing they started running."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.launcher import gui
from agent.launcher.common import LauncherError, git_bytes, run
from agent.runtime.process_env import pid_alive

# Starts a long-lived child that inherits stdout and stderr, records its pid,
# then prints "done" and exits, or keeps running when asked to.
STARTER = """
import subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
open(sys.argv[1], "w").write(str(child.pid))
print("done", flush=True)
if sys.argv[2] == "stay":
    time.sleep(60)
"""


def started_pid(marker: Path) -> int:
    deadline = time.monotonic() + 10
    while not marker.is_file() or not marker.read_text():
        assert time.monotonic() < deadline, "the command did not start its child"
        time.sleep(0.05)
    return int(marker.read_text())


def ends(pid: int) -> bool:
    deadline = time.monotonic() + 10
    while pid_alive(pid):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)
    return True


def stop(pid: int) -> None:
    if pid_alive(pid):
        os.kill(pid, signal.SIGTERM)


def test_a_finished_command_returns_while_a_child_it_left_holds_its_output(tmp_path):
    marker = tmp_path / "child.pid"
    try:
        started = time.monotonic()
        assert run([sys.executable, "-c", STARTER, str(marker), "leave"], cwd=tmp_path, timeout=20) == "done"
        assert time.monotonic() - started < 15
        assert pid_alive(started_pid(marker))  # Not ours to stop: the command ended normally.
    finally:
        if marker.is_file():
            stop(int(marker.read_text()))


def test_a_command_past_its_deadline_is_stopped_with_every_process_it_started(tmp_path):
    marker = tmp_path / "child.pid"
    try:
        started = time.monotonic()
        with pytest.raises(LauncherError, match="did not finish within 5 seconds"):
            run([sys.executable, "-c", STARTER, str(marker), "stay"], cwd=tmp_path, timeout=5)
        assert time.monotonic() - started < 20
        assert ends(started_pid(marker))
    finally:
        if marker.is_file():
            stop(int(marker.read_text()))


def test_git_input_and_failure_still_reach_the_caller(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    blob = git_bytes(tmp_path, "hash-object", "-w", "--stdin", data=b"payload\n")
    assert git_bytes(tmp_path, "cat-file", "-p", blob.decode().strip()) == b"payload\n"
    with pytest.raises(LauncherError, match="Git failed"):
        git_bytes(tmp_path, "cat-file", "-p", "0" * 40)


SUPERVISOR = """
import subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
open(sys.argv[1], "w").write(str(child.pid))
time.sleep(60)
"""


def test_a_desktop_that_does_not_open_in_time_is_ended_with_what_it_started(tmp_path, monkeypatch):
    # The supervisor never reports ready; its child stands in for Electron and the backends.
    marker = tmp_path / "electron.pid"
    real = subprocess.Popen

    def popen(args, **kwargs):
        if "agent.launcher.gui" in args:
            args = [sys.executable, "-c", SUPERVISOR, str(marker)]
        return real(args, **kwargs)

    monkeypatch.setattr(gui.subprocess, "Popen", popen)
    monkeypatch.setattr(gui, "STARTUP_SECONDS", 5)
    monkeypatch.setattr(gui.dependencies, "gui_executable", lambda install: "electron")
    monkeypatch.setattr(gui, "runtime_environment", lambda install, workspace: dict(os.environ))
    install = SimpleNamespace(kind="desktop", data=tmp_path / "data", python=Path(sys.executable), root=tmp_path)
    try:
        with pytest.raises(LauncherError, match="Desktop startup did not complete"):
            gui.launch_gui(install)  # type: ignore[arg-type]
        assert ends(started_pid(marker))
    finally:
        if marker.is_file():
            stop(int(marker.read_text()))
