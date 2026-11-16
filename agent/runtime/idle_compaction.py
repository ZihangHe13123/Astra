"""Idle compaction: a conversation's summary is written while the provider still has it cached.

A provider that keeps a request for a known time (``prompt_cache_seconds``: Claude Code, one
hour) reads a conversation whole, at the full price, when the person comes back after that.
A summary asked for just before then is read from the cache instead, at a twentieth of it, so
a conversation the person comes back to after the cache has gone starts shorter.
Application rules (docs/execution.md, "Idle compaction"):

- The backend checks the conversations it holds (the desktop's and the chats') between turns.
  A conversation is due when its last turn has finished, no request of it has read the cache
  for the lifetime less six minutes, and the compressor's plan says the summary would take off
  at least ``ASTRA_IDLE_COMPACT_MIN_GAIN`` tokens (default 10,000, the summary's budget already
  taken off): the summary request reads the whole conversation and writes the summary, and is
  wasted if the person comes back within the hour. ``ASTRA_IDLE_COMPACT_MIN_TOKENS`` also asks
  for a last request of at least that size when set; ``0`` or ``off`` turns idle compaction
  off. It is not due once the lifetime is nearly over: the summary request has to reach the
  cache in time.
- The summary is asked for by repeating that conversation's last request, as history compaction
  does, never from a transcript. It is kept beside the history, which does not change. That
  request reads the cache again, so the cache lasts a lifetime from then. It is sent only if the
  model's window holds it with room for the answer, sized as the provider reported the last
  request plus what it adds; the compaction line does not apply to it. When no summary is
  written for that or another refusal, a warning says why.
- The next turn of that conversation uses the summary only if idle compaction and the
  conversation's compaction are still on, the turn starts a whole lifetime or more after the
  last request that read the cache (the summary request included), on a provider with a
  lifetime, and the history still begins with what was summarized. The summary then takes the
  place of the part it covers and everything after that part stays word for word, as in history
  compaction. Otherwise the summary is dropped and the conversation goes on in full.
- One summary per pause, one conversation at a time. A turn that starts meanwhile (any
  conversation's) interrupts it, since the model client may have one request slot; a summary
  that has not come back in ten minutes is given up. The provider refuses to send it another
  way than from the cache (``CACHED_PREFIX_ONLY``), so it is never a transcript.
- The summary is saved with the conversation, with when its request read the cache: after a
  restart the next turn applies the same rules to it, the history compared as a saved
  conversation is loaded. A summary that came back counts as a read of the cache when it was
  asked for: whether the cache has gone when the person comes back is counted from there. A
  restart before the conversation has been quiet that long writes none: the new process does
  not have the last request to repeat, and a repeat that missed the cache would cost more than
  the summary saves.
- What becomes of each summary is logged at INFO (``idle compaction: summary``): ready, with an
  id saved with it, then used or dropped and why under that id; or not written, or cancelled and
  why. A summary is ready once it is kept, even if the attempt is cut short while saving it. One
  dropped because the cache was still there says whether the summary request kept it or the last
  turn did.
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
# What a summary must take off to be written (a starting point, to be checked against live data).
DEFAULT_MIN_GAIN = 10_000
CHECK_INTERVAL_SECONDS = 1.0
# A summary that has not come back by then is given up: it holds one of the model client's request slots.
SUMMARY_TIMEOUT_SECONDS = 600.0


def clock() -> float:
    """The time the rules are applied at (a test may replace it)."""
    return time.time()


def enabled() -> bool:
    return os.getenv("ASTRA_IDLE_COMPACT_MIN_TOKENS", "").strip().lower() not in {"0", "off", "false", "no"}


def min_tokens() -> int:
    """The size the last request must have reached; 0 (the default) asks for none."""
    raw = os.getenv("ASTRA_IDLE_COMPACT_MIN_TOKENS", "").strip()
    return int(raw) if raw.isdigit() else 0


def min_gain() -> int:
    """How many tokens the summary must take off to be written."""
    raw = os.getenv("ASTRA_IDLE_COMPACT_MIN_GAIN", "").strip()
    return int(raw) if raw.isdigit() else DEFAULT_MIN_GAIN


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
        # Why the running summary was cancelled, for its log line.
        self._cancelled_for = ""

    def tick(self, *, busy: bool) -> None:
        running = self._task is not None and not self._task.done()
        if busy and running and self._task is not None:
            # A turn needs the model, and the client may have one request slot: the summary gives way.
            self._cancelled_for = "a turn needs the model"
            self._task.cancel()
        if busy or running:
            return
        now = clock()
        if now - self._checked < CHECK_INTERVAL_SECONDS:
            return
        self._checked = now
        if not enabled():
            return
        tokens, gain = min_tokens(), min_gain()
        for context, attribution in self.conversations():
            llm = getattr(getattr(context, "compressor", None), "llm", None)
            cached_for = cache_seconds_for(llm)
            if cached_for is None:
                continue
            if context.idle_summary_due(now, idle_after=idle_seconds(cached_for), cache_seconds=cached_for,
                                        min_tokens=tokens, min_gain=gain, model=model_identity(llm)):
                self._cancelled_for = ""
                self._task = asyncio.create_task(self._prepare(context, now, attribution), name="idle-compaction")
                return

    async def _prepare(self, context: Any, now: float, attribution: dict) -> None:
        # A summary kept is ready, and logged so, even when the attempt is cut short while saving it.
        kept = getattr(context, "kept_idle_summary_id", None)
        try:
            with usage_context(**attribution):
                ready = await asyncio.wait_for(context.prepare_idle_summary(now), SUMMARY_TIMEOUT_SECONDS)
            if not ready:
                logger.info("idle compaction: summary not written")
        except asyncio.CancelledError:
            if getattr(context, "kept_idle_summary_id", None) == kept:
                logger.info("idle compaction: summary cancelled: %s", self._cancelled_for or "interrupted")
            raise
        except asyncio.TimeoutError:
            if getattr(context, "kept_idle_summary_id", None) == kept:
                logger.info("idle compaction: no summary within %.0f s; given up", SUMMARY_TIMEOUT_SECONDS)
        except Exception:
            logger.exception("idle compaction failed")

    async def close(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            self._cancelled_for = "the backend is closing"
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
