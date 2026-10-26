"""Scheduled jobs run by the desktop's background host, so no terminal has to stay open.

The scheduler ticks in a daemon thread: a check can take minutes and must not hold the event
loop. If the host exits while a check runs, its worker keeps its ownership lock and may finish;
the next tick, here or in ``astra jobs serve``, delivers the result (see the scheduler). Pushes
are sent from the event loop, on their own shorter beat, so a slow check does not delay them.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from contextlib import suppress
from pathlib import Path

from .delivery import DesktopNotify, JobDelivery, ThreadRecord, WeixinPush
from .scheduler import tick
from .store import JobStore
from .triggers import LOOK_SECONDS, TriggerWatcher

logger = logging.getLogger(__name__)

TICK_SECONDS = 30.0
PUSH_SECONDS = 10.0


def _seconds(name: str, default: float) -> float:
    try:
        return max(0.1, float(os.environ.get(name, default)))
    except ValueError:
        return default


class JobsHost:
    def __init__(self, home: Path, *, desktop: DesktopNotify | None, weixin: WeixinPush | None,
                 thread: ThreadRecord | None = None, tick_seconds: float | None = None,
                 push_seconds: float | None = None):
        self.store = JobStore(home)
        self.delivery = JobDelivery(self.store, desktop=desktop, weixin=weixin, thread=thread)
        self.triggers = TriggerWatcher(self.store)
        self._look_seconds = _seconds("ASTRA_JOBS_TRIGGER_SECONDS", LOOK_SECONDS)
        self._stop = threading.Event()
        self._tick_seconds = tick_seconds or _seconds("ASTRA_JOBS_TICK_SECONDS", TICK_SECONDS)
        self._push_seconds = push_seconds or _seconds("ASTRA_JOBS_PUSH_SECONDS", PUSH_SECONDS)
        self._thread = threading.Thread(target=self._ticking, name="astra-jobs-scheduler", daemon=True)
        self._pushes: asyncio.Task | None = None

    def start(self) -> JobsHost:
        self._thread.start()
        self._pushes = asyncio.create_task(self._pushing(), name="astra-job-pushes")
        return self

    def _ticking(self) -> None:
        looked = 0.0
        while not self._stop.is_set():
            if time.monotonic() - looked >= self._look_seconds:
                looked = time.monotonic()
                try:
                    self.triggers.look()
                except Exception:
                    logger.exception("job triggers failed")
            try:
                tick(self.store)
            except Exception:
                logger.exception("scheduled jobs tick failed")
            self._stop.wait(self._tick_seconds)

    async def _pushing(self) -> None:
        while True:
            try:
                await self.delivery.deliver_due()
            except Exception:
                logger.exception("scheduled job pushes failed")
            await asyncio.sleep(self._push_seconds)

    async def close(self) -> None:
        self._stop.set()
        if self._pushes is not None:
            self._pushes.cancel()
            with suppress(asyncio.CancelledError):
                await self._pushes
