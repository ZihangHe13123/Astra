"""Content-free liveness updates while awaiting a provider event stream."""

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any


async def stream_with_progress(
    events: AsyncIterator[dict[str, Any]], *, interval: float = 5.0, quiet_after: float = 30.0,
) -> AsyncIterator[dict[str, Any]]:
    started = last_output = last_report = time.monotonic()
    received_output = False
    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(maxsize=1)
    reader: asyncio.Task | None = None
    stopping = False

    async def read_stream() -> None:
        # One task owns the iterator for its whole lifetime. Creating a task
        # per anext breaks providers that hold ContextVar tokens across yields.
        try:
            try:
                async for event in events:
                    await queue.put(("event", event))
            finally:
                close = getattr(events, "aclose", None)
                if close is not None:
                    await close()
        except asyncio.CancelledError as error:
            if stopping:
                raise
            # A provider may cancel itself; the consumer still needs a terminal
            # signal instead of receiving display heartbeats indefinitely.
            await queue.put(("error", error))
        except Exception as error:
            await queue.put(("error", error))
        else:
            await queue.put(("end", None))

    def progress(phase: str | None = None) -> dict[str, Any]:
        now = time.monotonic()
        idle = max(0.0, now - last_output)
        return {
            "type": "generation_progress",
            "phase": phase or ("waiting" if idle >= quiet_after else "streaming" if received_output else "requesting"),
            "elapsed_seconds": max(0.0, now - started),
            "idle_seconds": idle,
        }

    try:
        reader = asyncio.create_task(read_stream(), name="provider-stream-progress")
        yield progress("requesting")
        while True:
            try:
                kind, event = await asyncio.wait_for(queue.get(), timeout=interval)
            except TimeoutError:
                last_report = time.monotonic()
                yield progress()
                continue
            if kind == "end":
                break
            if kind == "error":
                raise event
            meaningful = event.get("type") in {"tool_calls", "done"} or (
                event.get("type") in {"chunk", "reasoning"} and bool(event.get("content"))
            ) or (
                event.get("type") == "tool_preparing" and event.get("state") == "preparing" and bool(event.get("calls"))
            )
            now = time.monotonic()
            first_output = meaningful and not received_output
            if meaningful:
                last_output = now
                received_output = True
            if first_output or now - last_report >= interval:
                last_report = now
                yield progress()
            yield event
        yield progress("finished")
    finally:
        stopping = True
        if reader is not None:
            reader.cancel()
            drain = asyncio.gather(reader, return_exceptions=True)
            while not drain.done():
                try:
                    await asyncio.shield(drain)
                except asyncio.CancelledError:
                    reader.cancel()
