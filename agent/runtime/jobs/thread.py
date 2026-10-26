"""Scheduled-job results written into the personal thread (specs/personal-thread.md).

The desktop's background host holds that conversation. A result joins it as Astra's own message, marked
with the job, so a later "tell me more about the second one" has it; the window and a paired phone show
it as it arrives. A turn in progress is never interrupted: the result waits until the thread is free.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from .delivery import ThreadRecord


def thread_text(title: str, body: str) -> str:
    """How a result reads in the thread, for the model and on screen alike."""
    return f"【{title}】\n{body}"


def thread_recorder(agent: Any, turn_lock: asyncio.Lock, send: Callable[[dict], None]) -> ThreadRecord:
    async def record(push: dict, title: str, body: str) -> bool:
        if turn_lock.locked():
            return False
        # Held only between turns: a channel turn swaps the agent's conversation under this same lock.
        async with turn_lock:
            agent.context.add_assistant(thread_text(title, body))
            await agent.context.save_async()
        send({"type": "job_result", "title": title, "text": body, "job_id": push["job_id"],
              "run_id": push["run_id"], "state": push["run_state"]})
        return True

    return record
