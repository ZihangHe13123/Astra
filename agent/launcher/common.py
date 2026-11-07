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
import weakref
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


# What a started command and its descendants belong to, so all of them can be ended later, even one whose
# parent has exited: a Job object on Windows, the command's own process group elsewhere.
_JOBS: weakref.WeakKeyDictionary[subprocess.Popen, int] = weakref.WeakKeyDictionary()
_GROUPS: weakref.WeakSet[subprocess.Popen] = weakref.WeakSet()


def _windows_job(process: subprocess.Popen) -> int:
    """A Job object holding the process and everything it starts; 0 when Windows cannot give one."""
    if sys.platform != "win32":
        return 0
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return 0
    if not kernel32.AssignProcessToJobObject(job, wintypes.HANDLE(int(getattr(process, "_handle")))):
        kernel32.CloseHandle(job)
        return 0
    return int(job)


def _close_job(job: int, *, terminate: bool) -> bool:
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    ended = bool(kernel32.TerminateJobObject(wintypes.HANDLE(job), 1)) if terminate else False
    kernel32.CloseHandle(wintypes.HANDLE(job))
    return ended


def spawn(args: list[str], *, own_group: bool, **options: Any) -> subprocess.Popen:
    """Start a command so that `stop_tree` can end all it starts, even a process whose parent has exited.

    On Windows every command gets a Job object. Elsewhere `own_group` gives it a process group of its own;
    leave it off for a command that may ask at the terminal (an SSH passphrase), which a group in the
    background could not read.
    """
    if os.name != "nt" and own_group and not options.get("start_new_session"):
        options["process_group"] = 0
    process = subprocess.Popen(args, **options)
    if job := _windows_job(process):
        _JOBS[process] = job
    elif os.name != "nt" and own_group:
        _GROUPS.add(process)
    return process


def release(process: subprocess.Popen) -> None:
    """Let go of a command that ended by itself; what it left running is not stopped."""
    if job := _JOBS.pop(process, 0):
        _close_job(job, terminate=False)
    _GROUPS.discard(process)


def stop_tree(process: subprocess.Popen) -> None:
    """End a command and every process it started.

    Killing only the command leaves its children running: Git's HTTPS helper,
    a package build, or the desktop's Electron and backends.
    """
    job = _JOBS.pop(process, 0)
    if job and _close_job(job, terminate=True):
        pass
    elif os.name == "nt":
        # Without a Job object, taskkill follows parent links, so it runs while the command is alive.
        try:
            subprocess.run(["taskkill.exe", "/PID", str(process.pid), "/T", "/F"], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        if process in _GROUPS:
            _GROUPS.discard(process)
            with contextlib.suppress(OSError):
                os.killpg(process.pid, signal.SIGKILL)
        # A process that left the group, or a command without one: what still descends from it.
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
            # A captured command never talks to the terminal, so it can have a process group of its own.
            process = spawn(args, own_group=capture, cwd=cwd, env=env, stdin=stdin,
                            stdout=stdout if capture else sys.stderr, stderr=stderr if capture else None)
        except OSError as exc:
            raise LauncherError(f"Could not run {name}: {exc}") from exc
        deadline, overdue = time.monotonic() + timeout, False
        try:
            # Short waits keep Ctrl+C responsive on Windows, where one long wait is not interruptible.
            while process.poll() is None and not overdue:
                overdue = time.monotonic() >= deadline
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=0.2)
        except BaseException:
            stop_tree(process)
            raise
        if overdue and process.poll() is None:
            stop_tree(process)
            raise LauncherError(f"{name} did not finish within {timeout:g} seconds and was stopped.")
        release(process)
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
