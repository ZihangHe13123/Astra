"""In-memory, session-owned wakeups. No daemon, durable resumption or catch-up."""

from __future__ import annotations

import math
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone


class SessionWakeups:
    MIN_DELAY = 60
    MAX_LIFETIME = 12 * 3600

    def __init__(self, *, clock: Callable[[], float] = time.monotonic,
                 wall_clock: Callable[[], float] = time.time):
        self.clock = clock
        self.wall_clock = wall_clock
        self._plan: dict = {"state": "idle"}
        self._due = 0.0
        self._expires = 0.0

    def schedule(self, *, session: str, prompt: str, original_request: str,
                 delay_seconds: float = 300, interval_seconds: float = 0,
                 lifetime_seconds: float = 3600) -> dict:
        values = (delay_seconds, interval_seconds, lifetime_seconds)
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
               for value in values):
            raise ValueError("Wakeup times must be finite numbers of seconds.")
        if (delay_seconds < self.MIN_DELAY or interval_seconds < 0
                or 0 < interval_seconds < self.MIN_DELAY
                or not delay_seconds < lifetime_seconds <= self.MAX_LIFETIME):
            raise ValueError("Delay/interval must be at least 60s; expiry must be after the first wakeup and within 12 hours.")
        if (not session or not prompt.strip() or not original_request.strip()
                or len(prompt) > 4000 or len(original_request) > 8000):
            raise ValueError("A wakeup needs a session, a bounded prompt and the original user request.")
        replaced = self._plan.get("id") if self._plan["state"] in {"scheduled", "running"} else None
        self._due = self.clock() + delay_seconds
        self._expires = self.clock() + lifetime_seconds
        self._plan = {"id": uuid.uuid4().hex, "session": session, "prompt": prompt.strip(),
                      "original_request": original_request, "interval_seconds": interval_seconds,
                      "state": "scheduled", "runs": 0, "replaced_id": replaced,
                      "expires_at": self._iso(lifetime_seconds)}
        return self.status()

    def _iso(self, offset: float) -> str:
        return datetime.fromtimestamp(self.wall_clock() + offset, timezone.utc).isoformat()

    def status(self) -> dict:
        return {**self._plan, "next_at": self._iso(max(0, self._due - self.clock()))
                if self._plan["state"] == "scheduled" else None}

    def cancel(self, reason: str = "cancelled") -> dict:
        if self._plan["state"] in {"scheduled", "running"}:
            self._plan["state"] = reason
        return self.status()

    def claim(self, *, session: str, busy: bool) -> dict | None:
        if self._plan["state"] not in {"scheduled", "running"}:
            return None
        if session != self._plan["session"]:
            self.cancel("session_changed")
            return None
        if self.clock() >= self._expires:
            self.cancel("expired")
            return None
        if busy or self._plan["state"] == "running" or self.clock() < self._due:
            return None
        self._plan["state"] = "running"
        self._plan["runs"] += 1
        return {**self.status(), "remaining_seconds": self._expires - self.clock()}

    def remaining(self, plan_id: str, session: str) -> float:
        """Revalidate ownership after waiting to enter the shared agent turn."""
        if (self._plan.get("id") != plan_id or self._plan["state"] != "running"
                or self._plan["session"] != session):
            return 0
        return max(0, self._expires - self.clock())

    def finish(self, plan_id: str, *, outcome: str, summary: str) -> dict | None:
        if self._plan.get("id") != plan_id or self._plan["state"] != "running":
            return None
        if outcome not in {"unchanged", "changed", "completed", "failed"}:
            raise ValueError("Wakeup outcome must be unchanged, changed, completed or failed.")
        self._plan.update(outcome=outcome, summary=summary[:4000])
        if outcome in {"completed", "failed"}:
            self._plan["state"] = outcome
        elif not self._plan["interval_seconds"]:
            self._plan["state"] = "completed"
        elif self.clock() >= self._expires:
            self._plan["state"] = "expired"
        else:
            self._plan["state"] = "scheduled"
            self._due = self.clock() + self._plan["interval_seconds"]
        return self.status()


def wakeup_prompt(plan: dict) -> str:
    return (
        "[Scheduled session wakeup — not a new user message]\n"
        "Continue only the previously authorized check below. Do one bounded check; do not sleep, "
        "start background work, create another schedule, or infer new permissions. "
        "Call report_wakeup with unchanged, changed, completed or failed before ending. "
        "Use unchanged for no meaningful change; completed when the stop condition is met; "
        "failed when blocked or unable to check. Only a meaningful result needs a notification.\n"
        f"Original user request:\n{plan['original_request']}\n"
        f"Scheduled check:\n{plan['prompt']}"
    )


def visible_wakeup_history(messages: list[dict]) -> list[dict]:
    """Keep scheduled checks auditable without restoring silent chatter as user text."""
    hidden = False
    visible = []
    for message in messages:
        provenance = message.get("provenance", "")
        if (message.get("role") == "user" and provenance in {"", "session_wakeup"}
                and (message.get("_meta") or {}).get("type") != "reasoning_context"):
            hidden = provenance == "session_wakeup"
        if provenance == "wakeup_notification" or not hidden:
            visible.append(message)
    return visible
