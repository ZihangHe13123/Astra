"""Local decision receipts for permissions whose lifetime starts at consent."""

from __future__ import annotations

import ctypes
import sys
import time
from functools import lru_cache
from typing import Any, TypeGuard


def continuous_now() -> float:
    """Monotonic seconds, including suspend on macOS (the helper's clock) and Linux."""
    if sys.platform == "darwin":
        return time.clock_gettime(time.CLOCK_MONOTONIC_RAW)
    if sys.platform.startswith("linux") and hasattr(time, "CLOCK_BOOTTIME"):
        return time.clock_gettime(time.CLOCK_BOOTTIME)
    return time.monotonic()


class _ProcessSerialNumber(ctypes.Structure):
    _fields_ = [("highLongOfPSN", ctypes.c_uint32), ("lowLongOfPSN", ctypes.c_uint32)]


@lru_cache(maxsize=1)
def _load_process_manager() -> ctypes.CDLL:
    # These deprecated, read-only public SDK calls need neither a PyObjC runtime nor an
    # Apple Event, Accessibility permission, or a spawned process just to identify the front app.
    library = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
    psn = ctypes.POINTER(_ProcessSerialNumber)
    library.GetFrontProcess.argtypes = [psn]
    library.GetFrontProcess.restype = ctypes.c_int16  # OSErr
    library.GetProcessPID.argtypes = [psn, ctypes.POINTER(ctypes.c_int32)]  # pid_t *
    library.GetProcessPID.restype = ctypes.c_int32  # OSStatus
    return library


def _valid_pid(value: object) -> TypeGuard[int]:
    return type(value) is int and 0 < value < 2**31


def frontmost_pid_now() -> int | None:
    """Observe the front process once. Missing symbols or a disappearing process give no witness."""
    if sys.platform != "darwin":
        return None
    try:
        api = _load_process_manager()
        psn, pid = _ProcessSerialNumber(), ctypes.c_int32()
        if api.GetFrontProcess(ctypes.byref(psn)) != 0:
            return None
        if api.GetProcessPID(ctypes.byref(psn), ctypes.byref(pid)) != 0:
            return None
        return pid.value if _valid_pid(pid.value) else None
    except (OSError, AttributeError, ctypes.ArgumentError):
        return None


def _is_allow(answer: str) -> bool:
    return str(answer).strip().lower() in {"once", "task", "session", "always"}


class TimedApprovalDecision(str):
    """The backend's in-process answer; neither time nor frontmost PID comes from the frontend."""

    __slots__ = ("private_decided_at", "private_frontmost_pid")
    private_decided_at: float
    private_frontmost_pid: int | None

    def __new__(cls, decision: str, *, foreground: bool = False) -> TimedApprovalDecision:
        answer = super().__new__(cls, decision)
        # Input during a potentially blocking PID lookup must be later than the decision.
        answer.private_decided_at = continuous_now()
        answer.private_frontmost_pid = frontmost_pid_now() if foreground and _is_allow(decision) else None
        return answer


def _valid_seconds(value: object) -> TypeGuard[float]:
    return isinstance(value, (float, int)) and not isinstance(value, bool) and 0 <= value <= sys.float_info.max


class TimedApprovalRequest(dict[str, Any]):
    """An original permission request that needs local consent facts, never wire fields."""

    private_decided_at: float | None = None
    private_frontmost_pid: int | None = None

    def record_decision_time(self, answer: str) -> bool:
        returned_at = continuous_now()
        self.private_decided_at = None
        self.private_frontmost_pid = None
        # Headless handlers return plain strings. Their return is the first time we see their decision.
        decided_at = (
            getattr(answer, "private_decided_at", None)
            if isinstance(answer, TimedApprovalDecision) else returned_at
        )
        if not _valid_seconds(returned_at) or not _valid_seconds(decided_at) or decided_at > returned_at:
            return False
        if self.get("kind") == "computer_foreground_takeover" and _is_allow(answer):
            # Do not repair a missing live witness using the desktop after database or audit work.
            pid = (
                getattr(answer, "private_frontmost_pid", None)
                if isinstance(answer, TimedApprovalDecision) else frontmost_pid_now()
            )
            if not _valid_pid(pid):
                return False
            self.private_frontmost_pid = pid
        self.private_decided_at = decided_at
        return True
