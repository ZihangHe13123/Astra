"""Keep the computer from idle sleep while a channel waits for messages.

The display may still turn off and the screen may lock: only system sleep would
cut the connection. The hold is tied to this process, so a crashed backend
never leaves the computer awake.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys

logger = logging.getLogger(__name__)

_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001


class KeepAwake:
    """One idle-sleep hold: ``caffeinate`` on macOS, an execution state on Windows,
    ``systemd-inhibit`` on Linux when present. Without any, it reports inactive."""

    def __init__(self, reason: str = "Astra channel online") -> None:
        self.reason = reason
        self._process: subprocess.Popen | None = None
        self._windows = False

    @property
    def active(self) -> bool:
        if self._windows:
            return True
        return self._process is not None and self._process.poll() is None

    def start(self) -> bool:
        if self.active:
            return True
        try:
            if sys.platform == "darwin":
                # -i: no idle system sleep (the display may still sleep); -w: released when this process exits.
                self._process = subprocess.Popen(
                    ["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            elif os.name == "nt":
                import ctypes

                kernel32 = getattr(ctypes, "windll").kernel32
                if not kernel32.SetThreadExecutionState(_ES_CONTINUOUS | _ES_SYSTEM_REQUIRED):
                    return False
                self._windows = True
            else:
                inhibit = shutil.which("systemd-inhibit")
                if inhibit is None:
                    logger.info("no idle-sleep inhibitor on this system; the channel stops if it sleeps")
                    return False
                self._process = subprocess.Popen(
                    [inhibit, "--what=idle:sleep", "--who=Astra", f"--why={self.reason}", "--mode=block",
                     "sleep", "infinity"],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
        except (OSError, AttributeError, ImportError) as exc:
            logger.warning("could not keep the computer awake: %s", exc)
            self._process = None
            return False
        return self.active

    def stop(self) -> None:
        if self._windows:
            import ctypes

            getattr(ctypes, "windll").kernel32.SetThreadExecutionState(_ES_CONTINUOUS)
            self._windows = False
        process, self._process = self._process, None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
