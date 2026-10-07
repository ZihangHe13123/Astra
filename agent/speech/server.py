"""Start the configured local speech server on demand and keep it alive while in use."""
from __future__ import annotations

import asyncio
import sys
import time
from contextlib import suppress
from pathlib import Path
from typing import Awaitable, Callable

from .provider import SpeechError

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class LocalServer:
    """A user-configured server command, run under `agent.speech.launcher`.

    Every session touches the same heartbeat file when it speaks, so a server
    started by one session stays up while another is still using it.
    """

    def __init__(self, command: tuple[str, ...], *, heartbeat: Path, log: Path, idle_seconds: float,
                 startup_seconds: float) -> None:
        self.command, self._heartbeat, self._log = command, heartbeat, log
        self._idle_seconds, self._startup_seconds = idle_seconds, startup_seconds
        self._process: asyncio.subprocess.Process | None = None

    def touch(self) -> None:
        with suppress(OSError):
            self._heartbeat.parent.mkdir(parents=True, exist_ok=True)
            self._heartbeat.touch()

    async def start(self, healthy: Callable[[], Awaitable[bool]]) -> None:
        """Launch the server unless one already answers, then wait until it does."""
        self.touch()
        if await healthy():
            return
        if self._process is None or self._process.returncode is not None:
            self._log.parent.mkdir(parents=True, exist_ok=True)
            with self._log.open("ab") as output:
                self._process = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "agent.speech.launcher", "--heartbeat", str(self._heartbeat),
                    "--idle", str(self._idle_seconds), "--", *self.command,
                    stdin=asyncio.subprocess.PIPE, stdout=output, stderr=output, cwd=PROJECT_ROOT)
        deadline = time.monotonic() + self._startup_seconds
        while not await healthy():
            if self._process.returncode is not None:
                raise SpeechError(f"The local speech server exited during startup; see {self._log}.")
            if time.monotonic() >= deadline:
                raise SpeechError(f"The local speech server did not answer within {self._startup_seconds:.0f}s.")
            await asyncio.sleep(0.25)

    async def close(self) -> None:
        process, self._process = self._process, None
        if process is None or process.returncode is not None:
            return
        if process.stdin is not None:
            process.stdin.close()  # the launcher stops the server when its owner goes
        try:
            await asyncio.wait_for(process.wait(), timeout=8)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
