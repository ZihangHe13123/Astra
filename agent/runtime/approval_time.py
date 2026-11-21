"""Local decision receipts for permissions whose lifetime starts at consent."""

from __future__ import annotations

import sys
import time
from typing import Any, TypeGuard


def continuous_now() -> float:
    """Monotonic seconds, including suspend on macOS (the helper's clock) and Linux."""
    if sys.platform == "darwin":
        return time.clock_gettime(time.CLOCK_MONOTONIC_RAW)
    if sys.platform.startswith("linux") and hasattr(time, "CLOCK_BOOTTIME"):
        return time.clock_gettime(time.CLOCK_BOOTTIME)
    return time.monotonic()


class TimedApprovalDecision(str):
    """The backend's in-process answer; no timestamp is supplied by the frontend."""

    __slots__ = ("private_decided_at",)
    private_decided_at: float

    def __new__(cls, decision: str) -> TimedApprovalDecision:
        answer = super().__new__(cls, decision)
        answer.private_decided_at = continuous_now()
        return answer


def _valid_seconds(value: object) -> TypeGuard[float]:
    return isinstance(value, (float, int)) and not isinstance(value, bool) and 0 <= value <= sys.float_info.max


class TimedApprovalRequest(dict[str, Any]):
    """An original permission request that needs the time as an attribute, not a wire field."""

    private_decided_at: float | None = None

    def record_decision_time(self, answer: str) -> bool:
        returned_at = continuous_now()
        # Headless handlers return plain strings. Their return is the first time we see their decision.
        decided_at = (
            getattr(answer, "private_decided_at", None)
            if isinstance(answer, TimedApprovalDecision) else returned_at
        )
        if not _valid_seconds(returned_at) or not _valid_seconds(decided_at) or decided_at > returned_at:
            self.private_decided_at = None
            return False
        self.private_decided_at = decided_at
        return True
