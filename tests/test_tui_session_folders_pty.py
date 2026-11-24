"""Exercise the session menu through the real POSIX TUI and its backend."""

import errno
import json
import os
import re
import selectors
import shutil
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

import pytest
import yaml

from test_backend_task_protocol import backend_environment
from test_conversation_folder import projects as projects_fixture

projects = projects_fixture
pytestmark = pytest.mark.skipif(os.name != "posix" or not shutil.which("node"), reason="POSIX PTY and Node required")

_ERASE_FRAME = re.compile(rb"(?:\x1b\[2K\x1b\[1A)*\x1b\[2K\x1b\[G")
_TERMINAL_ESCAPE = re.compile(rb"\x1b\[[0-?]*[ -/]*[@-~]")
_ACTIVE_SESSION = re.compile(r"(?m)^.*\bSTATUS\b[^\n]*\bsession tui-b\b")


def _screen_text(output):
    return _TERMINAL_ESCAPE.sub(b"", output).decode("utf-8", errors="replace")


def test_tui_shows_other_folders_on_request_and_refuses_cross_folder_open(tmp_path, projects):
    import fcntl
    import pty
    import struct
    import termios
    import tty

    projects.seed("conv-a")
    root = Path(__file__).resolve().parents[1]
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"selected_model": "Qwen3.6-35B-A3B"}), encoding="utf-8")
    models = tmp_path / "models.yaml"
    model = {
        "provider": "openai-compatible",
        "base_url": f"http://127.0.0.1:{projects.model.server_port}/v1",
        "api_key_env": "",
        "context_limit": 131072,
        "capabilities": ["tools", "streaming"],
    }
    models.write_text(yaml.safe_dump({
        "version": 1,
        "providers": {"test": {"label": "Test provider", **model}},
        "models": {"Qwen3.6-35B-A3B": model},
    }), encoding="utf-8")
    env = backend_environment(tmp_path)
    env.update({
        "AGENT_SETTINGS_PATH": str(settings),
        "AGENT_TASK_DB": str(tmp_path / "tasks.db"),
        "ASTRA_APPROVAL_DB": str(tmp_path / "approvals.db"),
        "ASTRA_EVENT_DB": str(tmp_path / "events.db"),
        "AGENT_MEMORY_PATH": str(tmp_path / "memory.db"),
        "AGENT_LEARNING_PATH": str(tmp_path / "learning.db"),
        "AGENT_SKILLS_PATH": str(tmp_path / "skills"),
        "AGENT_MODELS_FILE": str(models),
        "AGENT_USER_MODELS_FILE": str(tmp_path / "missing-models.yaml"),
        "LEARNING_REVIEW_AUTO": "0",
        "SANDBOX_DOCKER": "false",
        "AGENT_MCP_CONFIG": str(tmp_path / "missing-mcp.json"),
        "PYTHONUNBUFFERED": "1",
        "AGENT_PYTHON": sys.executable,
        "AGENT_PROJECT_ROOT": str(root),
        "SANDBOX_WORKDIR": str(projects.b),
        "AGENT_SESSION_DIR": str(projects.sessions),
        "AGENT_SESSION": "tui-b",
        "TUI_STARTUP_ANIMATION": "0",
    })

    master, slave = pty.openpty()
    proc = None
    try:
        tty.setraw(slave)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
        proc = subprocess.Popen(
            [shutil.which("node"), "--import", "tsx", "src/index.tsx"],
            cwd=root / "ui-tui", env=env, stdin=slave, stdout=slave, stderr=slave,
            start_new_session=True,
        )
        os.close(slave)
        slave = -1
        output = bytearray()

        with selectors.DefaultSelector() as reader:
            reader.register(master, selectors.EVENT_READ)

            def read_output(timeout):
                if not reader.select(timeout):
                    return False
                try:
                    data = os.read(master, 65536)
                except OSError as error:
                    if error.errno != errno.EIO:
                        raise
                    return False
                output.extend(data)
                return bool(data)

            def wait_for(description, predicate, timeout=15):
                deadline = time.monotonic() + timeout
                while True:
                    # A cheap startup list can be replaced while the menu is open.
                    # Inspect the latest paint so a row erased earlier cannot match.
                    frame = _screen_text(_ERASE_FRAME.split(output)[-1])
                    if predicate(frame):
                        return frame
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not read_output(remaining):
                        break
                pytest.fail(f"Did not see {description} (TUI exit {proc.poll()}). Last frame:\n{frame}")

            wait_for("READY in the status bar", lambda frame: "READY" in frame)
            os.write(master, b"\x0c")
            wait_for("session tui-b in STATUS", _ACTIVE_SESSION.search)
            os.write(master, b"/session ")
            frame = wait_for("--all in the default menu", lambda frame: "--all" in frame and "/session" in frame, 30)
            assert "conv-a" not in frame
            os.write(master, b"--all ")
            wait_for("conv-a in the all-folders menu", lambda frame: "conv-a" in frame and "/session --all" in frame)
            os.write(master, b"conv-a")
            wait_for("conv-a selected", lambda frame: "/session --all conv-a" in frame and frame.count("conv-a") == 2)
            before_submit = len(output)
            os.write(master, b"\r")
            wait_for("the backend's folder refusal", lambda _: "belongs to" in _screen_text(output[before_submit:]))
            wait_for("session tui-b still in STATUS", _ACTIVE_SESSION.search)
            assert projects.header("conv-a")["workspace"] == str(projects.a)

            # Use the TUI's signal shutdown handler and keep the final paint draining.
            proc.terminate()
            deadline = time.monotonic() + 15
            while (remaining := deadline - time.monotonic()) > 0 and read_output(remaining):
                pass
            assert proc.wait(timeout=1) == 0
            # The Node parent is reaped, so an empty process group also verifies backend exit.
            with pytest.raises(ProcessLookupError):
                os.killpg(proc.pid, 0)
    finally:
        os.close(master)
        if slave != -1:
            os.close(slave)
        if proc is not None:
            with suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=3)
