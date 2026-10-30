"""Small IO primitives shared by setup, diagnostics and the updater."""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


class LauncherError(RuntimeError):
    """An actionable installation error, safe to display without a traceback."""


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("expected an object")
        return value
    except (OSError, ValueError) as exc:
        raise LauncherError(f"Cannot read {path}: {exc}") from exc


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _descendants(root: int) -> list[int]:
    try:
        listing = subprocess.run(["ps", "-axo", "pid=,ppid="], capture_output=True, text=True,
                                 timeout=10, check=False).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    children: dict[int, list[int]] = {}
    for line in listing.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[0].isdigit() and fields[1].isdigit():
            children.setdefault(int(fields[1]), []).append(int(fields[0]))
    found, pending = [], [root]
    while pending:
        for child in children.get(pending.pop(), []):
            found.append(child)
            pending.append(child)
    return found


def stop_tree(process: subprocess.Popen) -> None:
    """End a command and every process it started.

    Killing only the command leaves its children running: Git's HTTPS helper,
    a package build, or the desktop's Electron and backends.
    """
    if os.name == "nt":
        # taskkill follows parent links, so it runs while the command is alive.
        try:
            subprocess.run(["taskkill.exe", "/PID", str(process.pid), "/T", "/F"], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        for pid in _descendants(process.pid):
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)
    if process.poll() is None:
        with contextlib.suppress(OSError):
            process.kill()
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=10)


def _execute(args: list[str], *, cwd: Path, env: dict[str, str] | None, timeout: float,
             capture: bool, data: bytes | None = None) -> tuple[int, bytes, bytes]:
    """Run a command to its end or its deadline.

    Output goes to files, not pipes. Reading a pipe to its end also waits for
    every process that inherited it; on Windows, after a timeout, that wait has
    no limit, so one stalled Git helper kept the updater running forever.
    """
    name = Path(args[0]).name
    with contextlib.ExitStack() as files:
        stdin: Any = subprocess.DEVNULL
        if data is not None:
            stdin = files.enter_context(tempfile.TemporaryFile())
            stdin.write(data)
            stdin.seek(0)
        stdout = files.enter_context(tempfile.TemporaryFile())
        stderr = files.enter_context(tempfile.TemporaryFile())
        try:
            process = subprocess.Popen(args, cwd=cwd, env=env, stdin=stdin, stdout=stdout if capture else sys.stderr,
                                       stderr=stderr if capture else None)
        except OSError as exc:
            raise LauncherError(f"Could not run {name}: {exc}") from exc
        deadline = time.monotonic() + timeout
        try:
            # Short waits keep Ctrl+C responsive on Windows, where one long wait is not interruptible.
            while process.poll() is None:
                if time.monotonic() >= deadline:
                    stop_tree(process)
                    raise LauncherError(f"{name} did not finish within {timeout:g} seconds; "
                                        "it was stopped with every process it started.")
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=0.2)
        except BaseException:
            if process.poll() is None:
                stop_tree(process)
            raise
        stdout.seek(0)
        stderr.seek(0)
        return process.returncode, stdout.read(), stderr.read()


def _text(raw: bytes) -> str:
    return raw.decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n")


def run(
    args: list[str], *, cwd: Path, env: dict[str, str] | None = None,
    timeout: int = 120, capture: bool = True,
) -> str:
    code, stdout, stderr = _execute(args, cwd=cwd, env=env, timeout=timeout, capture=capture)
    if code:
        # Do not echo arguments: remote URLs and environment values may contain secrets.
        raise LauncherError(f"{Path(args[0]).name} failed (exit {code})."
                            + ("\n" + (_text(stderr) or _text(stdout))[-4000:] if capture else ""))
    return _text(stdout).strip()


def git_environment() -> dict[str, str]:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    # An inherited Git override must never redirect an update to another repository.
    for key in tuple(env):
        if key in {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"}:
            env.pop(key)
    return env


def git_bytes(root: Path, *args: str, data: bytes | None = None) -> bytes:
    """Keep NUL-delimited paths and binary content lossless."""
    code, stdout, stderr = _execute(["git", "--literal-pathspecs", "-C", str(root), *args], cwd=root,
                                    env=git_environment(), timeout=120, capture=True, data=data)
    if code:
        message = stderr.decode("utf-8", "replace")[-4000:]
        raise LauncherError(f"Git failed (exit {code}).\n{message}")
    return stdout


def git(root: Path, *args: str) -> str:
    return run(["git", "-C", str(root), *args], cwd=root, env=git_environment())
