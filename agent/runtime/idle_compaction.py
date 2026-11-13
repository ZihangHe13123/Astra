"""Idle compaction: a long conversation's summary is written while the provider still has it cached.

A provider that keeps a request for a known time (``prompt_cache_seconds``: Claude Code, one
hour) reads a conversation whole, at the full price, when the person comes back after that.
A summary asked for just before then is read from the cache instead, at about a tenth of it.
Application rules (docs/execution.md, "Idle compaction"):

- The backend checks the conversations it holds (the desktop's and the chats') between turns.
  A conversation is due when its last turn has finished, its last request carried at least
  ``ASTRA_IDLE_COMPACT_MIN_TOKENS`` (default 100,000; ``0`` or ``off`` turns this off), and no
  request of it has read the cache for the lifetime less six minutes. It is not due once the
  lifetime is nearly over: the summary request has to reach the cache in time.
- The summary is asked for by repeating that conversation's last request, as history compaction
  does, never from a transcript. It is kept beside the history, which does not change. That
  request reads the cache again, so the cache lasts a lifetime from then. It is sent only if the
  model's window holds it with room for the answer, sized as the provider reported the last
  request plus what it adds; the compaction line does not apply to it. When no summary is
  written for that or another refusal, a warning says why.
- The next turn of that conversation uses the summary only if it starts a whole lifetime or more
  after the last request that read the cache (the summary request included), on a provider with
  a lifetime, and the history still begins with what was summarized. The summary then takes the
  place of the part it covers and everything after that part stays word for word, as in history
  compaction. Otherwise the summary is dropped and the conversation goes on in full.
- One summary per pause, one conversation at a time. A turn that starts meanwhile (any
  conversation's) interrupts it, since the model client may have one request slot; a summary
  that has not come back in ten minutes is given up. The provider refuses to send it another
  way than from the cache (``CACHED_PREFIX_ONLY``), so it is never a transcript.
- The summary lives in this process only: a restart drops it, and the conversation is then read
  in full as before. The moment the summary request is handed to the provider counts as a read
  of the cache whether or not the summary comes back (``CACHE_READ_OBSERVER``); the next user
  message records it (``cache_read_at``), so the clearing of old tool results counts the pause
  from there, also after a restart.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import Callable, Iterable
from typing import Any

from .prompt_budget import prompt_cache_seconds
from .usage import usage_context

logger = logging.getLogger(__name__)

# How long before the cache would go the summary is asked for: the request has to reach it in time.
IDLE_MARGIN_SECONDS = 360
DEFAULT_MIN_TOKENS = 100_000
CHECK_INTERVAL_SECONDS = 1.0
# A summary that has not come back by then is given up: it holds one of the model client's request slots.
SUMMARY_TIMEOUT_SECONDS = 600.0


def clock() -> float:
    """The time the rules are applied at (a test may replace it)."""
    return time.time()


def min_tokens() -> int:
    raw = os.getenv("ASTRA_IDLE_COMPACT_MIN_TOKENS", "").strip().lower()
    if raw in {"off", "false", "no"}:
        return 0
    return int(raw) if raw.isdigit() else DEFAULT_MIN_TOKENS


def idle_seconds(cache_seconds: float) -> float:
    """How long a conversation is quiet before its summary is asked for."""
    raw = os.getenv("ASTRA_IDLE_COMPACT_AFTER_SECONDS", "").strip()
    if raw.isdigit() and 0 < int(raw) < cache_seconds - 30:
        return float(raw)
    return max(0.0, cache_seconds - IDLE_MARGIN_SECONDS)


def cache_seconds_for(llm: Any) -> int | None:
    return prompt_cache_seconds(str(getattr(getattr(llm, "config", None), "provider", "") or ""))


def model_identity(llm: Any) -> str:
    config = getattr(llm, "config", None)
    return f"{getattr(config, 'provider', '')}:{getattr(config, 'model', '')}"


class IdleCompactor:
    """Writes the summary of one idle conversation at a time, between turns."""

    def __init__(self, conversations: Callable[[], Iterable[tuple[Any, dict]]]):
        # Every conversation this process holds, each with how its requests are attributed in the
        # usage ledger (``usage_context`` arguments).
        self.conversations = conversations
        self._task: asyncio.Task | None = None
        self._checked = 0.0

    def tick(self, *, busy: bool) -> None:
        running = self._task is not None and not self._task.done()
        if busy and running and self._task is not None:
            # A turn needs the model, and the client may have one request slot: the summary gives way.
            self._task.cancel()
        if busy or running:
            return
        now = clock()
        if now - self._checked < CHECK_INTERVAL_SECONDS:
            return
        self._checked = now
        tokens = min_tokens()
        if tokens <= 0:
            return
        for context, attribution in self.conversations():
            llm = getattr(getattr(context, "compressor", None), "llm", None)
            cached_for = cache_seconds_for(llm)
            if cached_for is None:
                continue
            if context.idle_summary_due(now, idle_after=idle_seconds(cached_for), cache_seconds=cached_for,
                                        min_tokens=tokens, model=model_identity(llm)):
                self._task = asyncio.create_task(self._prepare(context, now, attribution), name="idle-compaction")
                return

    async def _prepare(self, context: Any, now: float, attribution: dict) -> None:
        try:
            with usage_context(**attribution):
                ready = await asyncio.wait_for(context.prepare_idle_summary(now), SUMMARY_TIMEOUT_SECONDS)
            logger.info("idle compaction: summary %s", "ready" if ready else "not written")
        except asyncio.TimeoutError:
            logger.info("idle compaction: no summary within %.0f s; given up", SUMMARY_TIMEOUT_SECONDS)
        except Exception:
            logger.exception("idle compaction failed")

    async def close(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
