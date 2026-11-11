"""Await blocking persistence without abandoning an in-flight write on cancel."""

from __future__ import annotations

import asyncio
import contextvars
import time
from typing import Callable, ParamSpec, TypeVar

P = ParamSpec("P")
T = TypeVar("T")

# A poll on a timer explains a wait only when it was slow, and an idle backend makes one a second:
# recorded each time, they were 99.8% of the runtime profile, and the 15 MB kept of it covered
# under eight hours (live 2026-11-11; the polls took 2 ms, 4 of 62,016 took this long).
ROUTINE_IO_SLOW_MS = 50.0


async def durable_io(function: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """Run I/O off the loop; settle it before propagating cancellation.

    Cancelling ``to_thread`` cannot stop its OS thread. Shielding and draining
    prevents a late commit from racing with task finalization or session close.
    Repeated cancellation must not interrupt that drain. Callers still own
    domain cleanup (e.g. marking an unconfirmed tool step unknown).
    """
    return await _settled(lambda: function(*args, **kwargs), _label(function), 0.0)


async def routine_io(function: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """durable_io for a poll that repeats on a timer whatever the person does.

    It is in the runtime profile only when it took ROUTINE_IO_SLOW_MS or longer.
    """
    return await _settled(lambda: function(*args, **kwargs), _label(function), ROUTINE_IO_SLOW_MS)


def _label(function: Callable[..., object]) -> str:
    return getattr(function, "__qualname__", "io").replace("<locals>.", "")


async def _settled(call: Callable[[], T], label: str, record_from_ms: float) -> T:
    from .latency import current_profiler, io_identity

    profiler = current_profiler()
    queued = time.perf_counter() if profiler is not None else 0.0
    worker_started = queued
    worker_finished = queued

    def invoke() -> T:
        nonlocal worker_started, worker_finished
        if profiler is not None:
            worker_started = time.perf_counter()
        try:
            return call()
        finally:
            if profiler is not None:
                worker_finished = time.perf_counter()

    # A bare executor Future is not included in asyncio.run's all-task
    # cancellation sweep. A child Task wrapping to_thread could itself be
    # cancelled during shutdown and falsely look settled while its OS thread
    # still writes. Copy context just as asyncio.to_thread does.
    context = contextvars.copy_context()
    operation = asyncio.get_running_loop().run_in_executor(None, context.run, invoke)
    cancellation: asyncio.CancelledError | None = None
    try:
        while True:
            try:
                result = await asyncio.shield(operation)
                break
            except asyncio.CancelledError as exc:
                if operation.cancelled():
                    raise
                cancellation = exc
            except Exception:
                if cancellation is not None:
                    raise cancellation from None
                raise
    finally:
        if profiler is not None:
            total_ms = (time.perf_counter() - queued) * 1000
            if total_ms >= record_from_ms:
                profiler.record("io", {
                    "queue_ms": (worker_started - queued) * 1000,
                    "execution_ms": (worker_finished - worker_started) * 1000,
                    "total_ms": total_ms,
                }, identity=io_identity(), label=label)
    if cancellation is not None:
        raise cancellation
    return result
