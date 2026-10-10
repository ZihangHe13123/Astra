import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from agent.runtime.event_stream import RuntimeEventStream
from agent.runtime import event_writer
from agent.runtime.event_writer import OrderedEventWriter, EventQueueFull


def test_blocked_output_close_is_bounded_and_wakes_producers():
    async def scenario():
        started, release = threading.Event(), threading.Event()

        def blocked(_event):
            started.set()
            release.wait(2)

        writer = OrderedEventWriter(None, blocked, max_items=1)
        writer.send({"type": "done"})
        assert await asyncio.to_thread(started.wait, 1)
        writer.send({"type": "done"})
        waiting = asyncio.create_task(writer.send_async({"type": "done"}))
        await asyncio.sleep(0)
        began = time.monotonic()
        try:
            with pytest.raises(TimeoutError, match="did not drain"):
                await writer.close(timeout=0.02)
            assert time.monotonic() - began < 0.5
            with pytest.raises((TimeoutError, RuntimeError)):
                await waiting
        finally:
            release.set()
            await asyncio.to_thread(writer._thread.join, 1)
        assert not writer.alive

    asyncio.run(scenario())


def test_slow_event_persistence_does_not_block_loop_and_replay_stays_ordered(tmp_path):
    async def scenario():
        stream = RuntimeEventStream(tmp_path / "events.db")
        started, release = threading.Event(), threading.Event()
        output = []

        def delay(stage):
            if stage == "before_commit":
                started.set()
                assert release.wait(2)

        stream._fault_hook = delay
        writer = OrderedEventWriter(stream, output.append)
        try:
            writer.send({"type": "tool_calls", "calls": [{"id": "c1", "name": "read_file", "arguments": "SECRET"}]})
            assert await asyncio.to_thread(started.wait, 1)
            writer.send({"type": "chunk", "content": "live only"})
            writer.replay(0, limit=500)
            writer.send({"type": "done"})
            await asyncio.sleep(0.01)
            assert output == []  # Publish still waits for commit; the loop does not.
        finally:
            release.set()
            await writer.close()
        assert [event["type"] for event in output] == [
            "tool_calls", "chunk", "tool_calls", "event_replay_complete", "done",
        ]
        assert output[0]["event_id"] == output[2]["event_id"]
        assert output[2]["replayed"] is True
        assert "arguments" not in output[2]["calls"][0]
        assert output[3]["next_cursor"] == output[0]["cursor"]
        assert output[4]["cursor"] > output[0]["cursor"]
        assert not writer.alive

    asyncio.run(scenario())


def test_default_close_allows_progress_past_the_stall_budget(monkeypatch):
    # Model a three-second drain without making the assertion depend on CI
    # filesystem latency. Real persistence/replay and pipe tests remain above.
    clock = [0.0]

    class WriterThread:
        def __init__(self, *, target, **_kwargs):
            self.writer = target.__self__

        def start(self):
            pass

        def is_alive(self):
            return bool(self.writer._queue)

        def join(self, timeout):
            deadline = clock[0] + timeout
            while self.writer._queue and clock[0] + 1 <= deadline:
                kind, payload, size, _queued, _request = self.writer._queue.popleft()
                self.writer._bytes -= size
                clock[0] += 1
                self.writer._deliver(kind, payload)
            if self.is_alive():
                clock[0] = deadline

    monkeypatch.setattr(event_writer, "time", SimpleNamespace(
        monotonic=lambda: clock[0], perf_counter=time.perf_counter,
    ))
    monkeypatch.setattr(event_writer, "threading", SimpleNamespace(
        Condition=threading.Condition, Thread=WriterThread,
    ))

    async def scenario():
        output = []
        writer = OrderedEventWriter(None, output.append)
        for index in range(3):
            writer.send({"type": "done", "index": index})
        await writer.close()
        assert [event["index"] for event in output] == list(range(3))
        assert clock[0] == 3
        assert not writer.alive

    asyncio.run(scenario())


def test_replay_delivery_also_advances_the_close_progress_clock(tmp_path):
    async def scenario():
        stream = RuntimeEventStream(tmp_path / "replay-events.db")
        for _ in range(8):
            stream.publish({"type": "done"})
        output = []

        def write(event):
            time.sleep(0.05)
            output.append(event)

        writer = OrderedEventWriter(stream, write)
        writer.replay(0)
        try:
            await writer.close(timeout=3, stall_timeout=0.2)
        finally:
            await asyncio.to_thread(writer._thread.join, 1)
        assert [event["type"] for event in output] == ["done"] * 8 + ["event_replay_complete"]
        assert all(event["replayed"] for event in output[:-1])
        assert not writer.alive

    asyncio.run(scenario())


def test_close_still_bounds_stalled_output_with_a_longer_total_budget():
    async def scenario():
        started, release = threading.Event(), threading.Event()

        def write(_event):
            started.set()
            release.wait(2)

        writer = OrderedEventWriter(None, write)
        writer.send({"type": "done"})
        assert await asyncio.to_thread(started.wait, 1)
        began = time.monotonic()
        try:
            with pytest.raises(TimeoutError, match="did not drain"):
                await writer.close(timeout=1, stall_timeout=0.05)
            assert time.monotonic() - began < 0.75
        finally:
            release.set()
            await asyncio.to_thread(writer._thread.join, 1)
        assert not writer.alive

    asyncio.run(scenario())


def test_delivery_progress_cannot_extend_the_absolute_close_deadline():
    async def scenario():
        release, progressing = threading.Event(), threading.Event()
        output = []

        def write(event):
            release.wait(0.02)
            output.append(event)
            if len(output) == 2:
                progressing.set()

        writer = OrderedEventWriter(None, write)
        for index in range(100):
            writer.send({"type": "done", "index": index})
        # The close is timed once output is being delivered. A writer thread
        # that is slow to start would end the close by its stall bound, with
        # one event written, which is not the case this test is about.
        assert await asyncio.to_thread(progressing.wait, 5)
        began = time.monotonic()
        try:
            with pytest.raises(TimeoutError, match="did not drain"):
                await writer.close(timeout=0.15, stall_timeout=0.1)
            assert time.monotonic() - began < 0.75
        finally:
            release.set()
            await asyncio.to_thread(writer._thread.join, 1)
        assert 1 < len(output) < 100
        assert not writer.alive

    asyncio.run(scenario())


def test_event_queue_is_bounded_and_async_producers_wait(tmp_path):
    async def scenario():
        started, release = threading.Event(), threading.Event()
        output = []

        def write(event):
            started.set()
            assert release.wait(2)
            output.append(event)

        writer = OrderedEventWriter(None, write, max_items=2, max_bytes=4096)
        writer.send({"type": "chunk", "content": "1"})
        assert await asyncio.to_thread(started.wait, 1)
        writer.send({"type": "chunk", "content": "2"})
        writer.send({"type": "chunk", "content": "3"})
        with pytest.raises(EventQueueFull):
            writer.send({"type": "done"})
        pending = asyncio.create_task(writer.send_async({"type": "done"}))
        await asyncio.sleep(0.01)
        assert not pending.done()
        release.set()
        await pending
        await writer.close()
        assert [e.get("content", e["type"]) for e in output] == ["1", "2", "3", "done"]

    asyncio.run(scenario())


def test_event_snapshot_and_writer_failure_are_explicit():
    async def scenario():
        output = []
        writer = OrderedEventWriter(None, output.append)
        event = {"type": "tool_calls", "calls": [{"id": "original"}]}
        writer.send(event)
        event["calls"][0]["id"] = "mutated"
        await writer.close()
        assert output[0]["calls"][0]["id"] == "original"
        with pytest.raises(RuntimeError, match="closed"):
            writer.send({"type": "done"})

        def broken(_event):
            raise BrokenPipeError("reader closed")

        writer = OrderedEventWriter(None, broken)
        writer.send({"type": "done"})
        with pytest.raises(BrokenPipeError):
            await writer.close()
        assert not writer.alive

    asyncio.run(scenario())


def test_oversized_event_fails_without_waiting_forever():
    async def scenario():
        writer = OrderedEventWriter(None, lambda event: None, max_bytes=32)
        try:
            with pytest.raises(EventQueueFull):
                await writer.send_async({"type": "chunk", "content": "x" * 100})
        finally:
            await writer.close()

    asyncio.run(scenario())


def test_failed_persistence_does_not_invent_cursor_or_block_later_live_events(tmp_path):
    async def scenario():
        stream = RuntimeEventStream(tmp_path / "events.db")
        output = []

        def fault(stage):
            if stage == "before_commit":
                stream._fault_hook = None
                raise OSError("injected disk failure")

        stream._fault_hook = fault
        writer = OrderedEventWriter(stream, output.append)
        writer.send({"type": "tool_progress", "stage": "running"})
        writer.send({"type": "done"})
        await writer.close()
        assert output[0]["replay_unavailable"] is True
        assert "cursor" not in output[0]
        assert output[1]["replayable"] is True
        assert [event["type"] for event in stream.replay(0)] == ["done"]

    asyncio.run(scenario())
