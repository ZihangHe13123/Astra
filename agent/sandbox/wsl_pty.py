"""Owner-scoped persistent Bash shell backed by a pseudo-terminal."""

from __future__ import annotations

import asyncio
import base64
import re
import sys
import uuid
from collections.abc import Callable

from ..runtime.process_env import hidden_process_creationflags


OutputCallback = Callable[[str, str], None]
_ANSI_CONTROL_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_SHELL_RESET_MESSAGE = (
    "The persistent bash shell was reset; the next bash call starts from "
    "the workspace with a fresh current directory and environment."
)
_CANCEL_RESET_MESSAGE = (
    "[An earlier bash command was cancelled and the persistent bash shell was "
    "reset with it: this command ran from the workspace with a fresh current "
    "directory and environment.]"
)
_STARTUP_TIMEOUT_SECONDS = 10


class PersistentWslShell:
    """Keep one interactive Bash process alive and fence each command by a marker.

    The command line may be WSL-backed on Windows or native on POSIX. A command
    runs in the shell itself (not a child shell), which preserves cwd and
    exported variables across calls. The class keeps its WSL name as a
    compatibility alias for existing callers.
    """

    def __init__(
        self,
        command: list[str],
        *,
        workdir: str,
        timeout: int | float | None,
        max_output_bytes: int,
        environment: str = "wsl",
    ) -> None:
        self._command = list(command)
        self._workdir = workdir
        self._timeout = timeout
        self._max_output_bytes = max(1, max_output_bytes)
        self._environment = environment
        self._process: asyncio.subprocess.Process | None = None
        self._pending = b""
        self._lock = asyncio.Lock()
        self._closed = False
        self._output_truncated = False
        # A cancelled call returns nothing, so the next result reports its reset.
        self._cancel_reset_unreported = False

    def _creation_kwargs(self) -> dict:
        if sys.platform == "win32":
            return {"creationflags": hidden_process_creationflags()}
        return {}

    async def _spawn(self) -> None:
        if self._closed:
            raise RuntimeError("persistent bash shell is closed")
        process = await asyncio.create_subprocess_exec(
            *self._command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=self._workdir,
            **self._creation_kwargs(),
        )
        self._process = process
        self._pending = b""
        marker = self._marker()
        await self._write(
            "stty -echo -onlcr 2>/dev/null || true; "
            "PS1=''; PROMPT_COMMAND=''; "
            "bind 'set enable-bracketed-paste off' 2>/dev/null || true; "
            f"printf '\\n{marker}0\\n'\n"
        )
        try:
            startup = self._read_until_marker(marker)
            _, status = await asyncio.wait_for(startup, timeout=_STARTUP_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            await self._terminate_locked()
            # Not the command's time limit: the command was never sent.
            raise RuntimeError(
                f"persistent bash shell did not start within {_STARTUP_TIMEOUT_SECONDS}s; "
                "the command was not run"
            ) from None
        except BaseException:
            await self._terminate_locked()
            raise
        if status != 0:
            await self._terminate_locked()
            raise RuntimeError(f"persistent bash shell initialization failed (exit code {status})")

    @staticmethod
    def _marker() -> str:
        return f"__ASTRA_WSL_PTY_RESULT_{uuid.uuid4().hex}__:"

    @staticmethod
    def _clean_output(raw: bytes) -> str:
        text = raw.decode("utf-8", errors="replace")
        text = _ANSI_CONTROL_RE.sub("", text).replace("\r\n", "\n").replace("\r", "\n")
        return text.strip("\n")

    async def _write(self, script: str) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise RuntimeError("persistent bash shell is not running")
        process.stdin.write(script.encode("utf-8"))
        await process.stdin.drain()

    async def _read_until_marker(
        self,
        marker: str,
        unfinished: list[bytearray] | None = None,
    ) -> tuple[bytes, int]:
        """Read up to the marker line. A caller-owned ``unfinished`` keeps what was read if this is cancelled."""
        process = self._process
        if process is None or process.stdout is None:
            raise RuntimeError("persistent bash shell is not running")
        marker_bytes = marker.encode("ascii")
        pending = bytearray(self._pending)
        output = bytearray()
        if unfinished is not None:
            unfinished[:] = [output, pending]
        while True:
            search_start = 0
            while True:
                index = pending.find(marker_bytes, search_start)
                if index < 0:
                    break
                status_end = pending.find(b"\n", index + len(marker_bytes))
                if status_end < 0:
                    # The marker line itself may be split across reads.
                    break
                status_raw = bytes(pending[index + len(marker_bytes):status_end]).strip()
                try:
                    status = int(status_raw)
                except ValueError:
                    # The first startup marker can appear in the PTY's echoed
                    # input before ``stty -echo`` takes effect. Keep scanning
                    # for the real marker rather than treating the echo as a
                    # command result.
                    search_start = index + len(marker_bytes)
                    continue
                output.extend(pending[:index])
                self._pending = bytes(pending[status_end + 1:])
                if len(output) > self._max_output_bytes:
                    self._output_truncated = True
                    del output[self._max_output_bytes:]
                return bytes(output), status

            chunk = await process.stdout.read(4096)
            if not chunk:
                raise RuntimeError("persistent bash shell exited before returning its result marker")
            pending.extend(chunk)
            # Keep enough tail for a marker split across chunks without allowing
            # an accidentally unbounded startup/prompt buffer.
            if len(pending) > self._max_output_bytes + 4096:
                overflow = len(pending) - (self._max_output_bytes + 4096)
                output.extend(pending[:overflow])
                del pending[:overflow]

    def _render_output(self, raw: bytes) -> str:
        """Clean what a command wrote; say when it was cut at the output limit or ran after a cancel's reset."""
        output = self._clean_output(raw)
        if self._output_truncated:
            suffix = (
                f"[Output truncated: persistent bash output exceeded "
                f"{self._max_output_bytes} bytes]"
            )
            output = f"{output}\n{suffix}" if output else suffix
        if self._cancel_reset_unreported:
            self._cancel_reset_unreported = False
            output = f"{_CANCEL_RESET_MESSAGE}\n{output}" if output else _CANCEL_RESET_MESSAGE
        return output

    async def _terminate_locked(self) -> None:
        process = self._process
        self._process = None
        self._pending = b""
        if process is None:
            return
        if process.stdin is not None:
            try:
                process.stdin.close()
            except (BrokenPipeError, ConnectionResetError, RuntimeError):
                pass
        if process.returncode is None:
            process.kill()
        try:
            await asyncio.wait_for(process.wait(), timeout=2)
        except (asyncio.TimeoutError, ProcessLookupError):
            pass

    @staticmethod
    async def _reap(process: asyncio.subprocess.Process) -> None:
        """Reap a process killed by the synchronous lifecycle bridge."""
        try:
            await asyncio.wait_for(process.wait(), timeout=2)
        except (asyncio.TimeoutError, ProcessLookupError):
            pass

    def close_nowait(self) -> None:
        """Stop the PTY synchronously when Minimal is left/reset."""
        process = self._process
        self._process = None
        self._pending = b""
        # This reset is the caller's own; an earlier cancelled command no longer matters.
        self._cancel_reset_unreported = False
        if process is None:
            return
        if process.stdin is not None:
            try:
                process.stdin.close()
            except (BrokenPipeError, ConnectionResetError, RuntimeError):
                pass
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._reap(process))

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            await self._terminate_locked()

    async def execute(
        self,
        command: str,
        on_output: OutputCallback | None = None,
    ) -> dict:
        if not command.strip():
            raise ValueError("command must be a non-empty string")
        async with self._lock:
            unfinished: list[bytearray] = []
            marker = self._marker()
            try:
                if self._process is None or self._process.returncode is not None:
                    await self._spawn()
                self._output_truncated = False
                encoded = base64.b64encode(command.encode("utf-8")).decode("ascii")
                await self._write(
                    f"__astra_cmd=$(printf %s {encoded} | base64 -d); "
                    f"eval \"$__astra_cmd\"; __astra_status=$?; "
                    f"printf '\\n{marker}%s\\n' \"$__astra_status\"\n"
                )
                reader = self._read_until_marker(marker, unfinished)
                if self._timeout is None:
                    raw_output, exit_code = await reader
                else:
                    raw_output, exit_code = await asyncio.wait_for(reader, timeout=self._timeout)
                output = self._render_output(raw_output)
                if on_output is not None and output:
                    on_output("stdout", output)
                return {
                    "output": output,
                    "error": "",
                    "exit_code": exit_code,
                    "environment": self._environment,
                    "persistent": True,
                }
            except asyncio.TimeoutError:
                await self._terminate_locked()
                # Keep what the command wrote before the limit, cleaned and bounded like a finished one.
                raw_output = b"".join(unfinished).split(marker.encode("ascii"), 1)[0]
                if len(raw_output) > self._max_output_bytes:
                    self._output_truncated = True
                    raw_output = raw_output[:self._max_output_bytes]
                output = self._render_output(raw_output)
                if on_output is not None and output:
                    on_output("stdout", output)
                notice = f"[Timeout] Execution exceeded {self._timeout}s; persistent bash shell was reset"
                if self._environment != "wsl":
                    # Killing the PTY owner hangs up the shell and its jobs.
                    notice += " and its command hung up (a process that ignores the hangup keeps running)"
                return {
                    "output": output,
                    "error": notice,
                    "exit_code": -1,
                    "timed_out": True,
                    "environment": self._environment,
                    "persistent": True,
                    "shell_reset": True,
                }
            except asyncio.CancelledError:
                self._cancel_reset_unreported = True
                await self._terminate_locked()
                raise
            except RuntimeError as exc:
                await self._terminate_locked()
                if "exited before returning its result marker" in str(exc):
                    # This result announces a reset itself.
                    self._cancel_reset_unreported = False
                    return {
                        "output": f"{exc}\n{_SHELL_RESET_MESSAGE}",
                        "error": "",
                        "exit_code": -1,
                        "environment": self._environment,
                        "persistent": True,
                        "shell_reset": True,
                    }
                raise
            except Exception:
                await self._terminate_locked()
                raise
