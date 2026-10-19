import asyncio
import copy
import time
from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import cast

import pytest

from agent.runtime import llm as llm_module
from agent.runtime.llm import LLMClient, LLMIdleTimeout, LLMOverallTimeout
from agent.runtime.context_compressor import ContextCompressor
from agent.runtime.stream_progress import stream_with_progress
from agent.runtime import turn_budget
from test_llm_network_resilience import (
    SequenceStream,
    content_chunk,
    provider_with_streams,
    reasoning_chunk,
    usage_chunk,
)


IDLE_SECONDS = 0.08
LOCAL_PAUSE_SECONDS = 0.18


class ObservedStream(SequenceStream):
    def __init__(self, items):
        super().__init__(items)
        self.read_count = 0
        self.close_count = 0
        self.third_read = asyncio.Event()

    async def __anext__(self):
        self.read_count += 1
        if self.read_count == 3:
            self.third_read.set()
        return await super().__anext__()

    async def aclose(self):
        self.close_count += 1


def make_provider(stream):
    provider, created = provider_with_streams([stream])
    provider.config.idle_timeout = IDLE_SECONDS
    return provider, created


@pytest.mark.parametrize("kind", ["text", "reasoning", "mixed"])
def test_local_consumer_pause_does_not_timeout_ready_provider(kind):
    async def scenario():
        first = reasoning_chunk("draft") if kind == "reasoning" else content_chunk("first")
        if kind == "mixed":
            first.choices[0].delta.reasoning_content = "draft"
        stream = ObservedStream([first, content_chunk("answer", "stop")])
        provider, created = make_provider(stream)
        output = provider.chat_stream([])
        events = []
        try:
            async for event in output:
                events.append(event)
                if event["type"] in {"reasoning", "chunk"}:
                    await asyncio.sleep(LOCAL_PAUSE_SECONDS)
        finally:
            await output.aclose()
        assert events[-1]["type"] == "done"
        assert events[-1]["content"] == ("answer" if kind == "reasoning" else "firstanswer")
        assert events[-1]["reasoning_content"] == ("" if kind == "text" else "draft")
        assert len(created) == stream.close_count == 1
    asyncio.run(scenario())


def test_real_progress_queue_backpressure_keeps_every_chunk():
    async def scenario():
        stream = ObservedStream([content_chunk(str(i), "stop" if i == 4 else None) for i in range(1, 5)])
        provider, created = make_provider(stream)
        output = cast(AsyncGenerator[dict, None], stream_with_progress(provider.chat_stream([]), interval=1))
        events = []
        try:
            async for event in output:
                events.append(event)
                if event["type"] == "chunk" and event["content"] == "1":
                    await asyncio.wait_for(stream.third_read.wait(), timeout=1)
                    # The reader has one queued event and is blocked delivering
                    # the next one. No provider read runs during this pause.
                    await asyncio.sleep(LOCAL_PAUSE_SECONDS)
                    assert stream.read_count == 3
        finally:
            await output.aclose()
        assert [e["content"] for e in events if e["type"] == "chunk"] == ["1", "2", "3", "4"]
        assert next(e for e in events if e["type"] == "done")["content"] == "1234"
        assert len(created) == stream.close_count == 1
    asyncio.run(scenario())


def test_consumer_pause_after_finish_does_not_discard_usage_trailer():
    async def scenario():
        stream = ObservedStream([content_chunk("answer", "stop"), usage_chunk()])
        provider, _ = make_provider(stream)
        output = provider.chat_stream([])
        try:
            assert (await anext(output))["content"] == "answer"
            await asyncio.sleep(LOCAL_PAUSE_SECONDS)
            events = [event async for event in output]
        finally:
            await output.aclose()
        usage = events[-1]["usage"]
        assert isinstance(usage, dict)
        assert usage["prompt_tokens"] == 1
        assert stream.close_count == 1
    asyncio.run(scenario())


def test_real_stall_after_local_pause_still_gets_full_idle_budget_and_closes():
    class StallingStream(ObservedStream):
        cancelled = False

        async def __anext__(self):
            if self.read_count == 1:
                self.read_count += 1
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.cancelled = True
                    raise
            return await super().__anext__()

    async def scenario():
        stream = StallingStream([content_chunk("partial")])
        provider, created = make_provider(stream)
        output = provider.chat_stream([])
        try:
            assert (await anext(output))["content"] == "partial"
            await asyncio.sleep(LOCAL_PAUSE_SECONDS)
            resumed_at = time.monotonic()
            with pytest.raises(LLMIdleTimeout):
                await asyncio.wait_for(anext(output), timeout=1)
            assert time.monotonic() - resumed_at >= IDLE_SECONDS * 0.75
        finally:
            await output.aclose()
        assert stream.cancelled
        assert len(created) == stream.close_count == 1
    asyncio.run(scenario())


def test_a_read_that_ignores_cancellation_still_ends_at_the_idle_limit(monkeypatch):
    monkeypatch.setattr(llm_module, "_CANCEL_GRACE_SECONDS", 0.1)

    class DeafStream(ObservedStream):
        """A stalled read that ignores cancellation and ends only when its connection closes."""

        def __init__(self, items):
            super().__init__(items)
            self.closed = asyncio.Event()
            self.ignored = 0

        async def __anext__(self):
            if self.read_count == 1:
                self.read_count += 1
                while not self.closed.is_set():
                    try:
                        await self.closed.wait()
                    except asyncio.CancelledError:
                        self.ignored += 1
                raise StopAsyncIteration
            return await super().__anext__()

        async def aclose(self):
            await super().aclose()
            self.closed.set()

    async def scenario():
        stream = DeafStream([content_chunk("partial")])
        provider, created = make_provider(stream)
        output = provider.chat_stream([])
        try:
            assert (await anext(output))["content"] == "partial"
            started = time.monotonic()
            reading = asyncio.ensure_future(anext(output))
            done, _ = await asyncio.wait({reading}, timeout=3)
            if not done:
                stream.closed.set()  # let the old code's stuck read end so the test can report
                await asyncio.wait({reading}, timeout=1)
            assert done, "the stalled read held the stream past its idle limit"
            with pytest.raises(LLMIdleTimeout):
                reading.result()
            assert time.monotonic() - started < 2
        finally:
            await output.aclose()
        assert stream.ignored >= 1
        assert len(created) == stream.close_count == 1
    asyncio.run(scenario())


def test_a_stream_whose_close_hangs_still_returns_its_answer(monkeypatch):
    monkeypatch.setattr(llm_module, "_CLOSE_GRACE_SECONDS", 0.1)
    release = []

    class StuckClose(ObservedStream):
        async def aclose(self):
            await super().aclose()
            gone = asyncio.Event()  # a shutdown waiting on a peer that is gone
            release.append(gone)
            await gone.wait()

    async def scenario():
        stream = StuckClose([content_chunk("answer", "stop")])
        provider, _ = make_provider(stream)
        consumed = asyncio.ensure_future(_collect(provider.chat_stream([])))
        done, _ = await asyncio.wait({consumed}, timeout=3)
        for gone in release:
            gone.set()
        if not done:
            await asyncio.wait({consumed}, timeout=1)
        assert done, "closing the stream held the answer"
        assert "".join(e.get("content", "") for e in consumed.result() if e["type"] == "chunk") == "answer"
        assert stream.close_count == 1
    asyncio.run(scenario())


async def _collect(output):
    return [event async for event in output]


@pytest.mark.parametrize("deadline", ["request", "turn"])
def test_local_pause_still_counts_toward_absolute_deadline(deadline):
    async def scenario():
        stream = ObservedStream([content_chunk("partial"), content_chunk("too late", "stop")])
        provider, created = make_provider(stream)
        token = None
        if deadline == "request":
            provider.config.overall_timeout = IDLE_SECONDS
            error = LLMOverallTimeout
        else:
            token = turn_budget._CURRENT.set(turn_budget.TurnBudget(IDLE_SECONDS))
            error = turn_budget.TurnBudgetExceeded
        output = provider.chat_stream([])
        try:
            assert (await anext(output))["content"] == "partial"
            await asyncio.sleep(LOCAL_PAUSE_SECONDS)
            with pytest.raises(error):
                await anext(output)
        finally:
            await output.aclose()
            if token is not None:
                turn_budget._CURRENT.reset(token)
        assert stream.read_count == 1
        assert len(created) == stream.close_count == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["empty", "usage"])
def test_heartbeats_still_timeout_with_safe_diagnostics_and_fresh_next_request(kind, caplog):
    private = "PRIVATE-message-and-reasoning-sentinel"

    class Heartbeats(ObservedStream):
        async def __anext__(self):
            if self.read_count == 0:
                return await super().__anext__()
            self.read_count += 1
            await asyncio.sleep(0.01)
            return usage_chunk() if kind == "usage" else content_chunk("")

    async def scenario():
        stalled = Heartbeats([reasoning_chunk(private)])
        fresh = ObservedStream([content_chunk("fresh", "stop")])
        provider, created = provider_with_streams([stalled, fresh])
        provider.config.idle_timeout = IDLE_SECONDS
        output = provider.chat_stream([{"role": "user", "content": private}])
        try:
            await anext(output)
            await asyncio.sleep(LOCAL_PAUSE_SECONDS)
            with pytest.raises(LLMIdleTimeout):
                await asyncio.wait_for(anext(output), timeout=1)
        finally:
            await output.aclose()
        assert len(created) == stalled.close_count == 1
        messages = [r.getMessage() for r in caplog.records if "llm stream failed" in r.getMessage()]
        assert len(messages) == 1
        fields = dict(part.split("=", 1) for part in messages[0].split() if "=" in part)
        assert float(fields["read_wait_seconds"]) >= IDLE_SECONDS * 0.75
        assert float(fields["idle_wait_seconds"]) >= IDLE_SECONDS * 0.75
        assert float(fields["local_pause_seconds"]) >= LOCAL_PAUSE_SECONDS * 0.9
        assert int(fields["chunks"]) > 1
        assert fields["meaningful_chunks"] == "1"
        assert fields["last_chunk_kind"] == kind
        assert private not in caplog.text
        # Neither the exhausted idle budget nor the local pause carries over
        # into the next logical request on the same provider instance.
        events = [event async for event in provider.chat_stream([])]
        assert events[-1]["content"] == "fresh"
        assert len(created) == 2
        assert fresh.close_count == 1
    asyncio.run(scenario())


def test_slow_compaction_does_not_leak_idle_time_or_generation_overrides(monkeypatch):
    async def scenario():
        provider, _ = provider_with_streams([])
        provider.config.model = "deepseek-flash"
        provider.config.thinking_mode = "enabled"
        provider.config.reasoning_effort = "max"
        provider.config.context_limit = 1_000_000
        provider.config.max_tokens = 384_000
        provider.config.idle_timeout = IDLE_SECONDS
        before = copy.deepcopy(vars(provider.config))
        requests = []
        stream = ObservedStream([content_chunk("answer", "stop")])

        async def create(**kwargs):
            requests.append(kwargs)
            if kwargs.get("stream"):
                return stream
            await asyncio.sleep(LOCAL_PAUSE_SECONDS)
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content="Earlier work completed.", tool_calls=[], reasoning_content=""),
                finish_reason="stop",
            )], usage=None)

        monkeypatch.setattr(provider._client.chat.completions, "create", create)
        client = LLMClient(provider.config, provider=provider)
        compressor = ContextCompressor(client)
        history = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "old history " * 10_000},
            {"role": "assistant", "content": "done"},
            {"role": "user", "content": "new task"},
        ]
        compressed = await compressor.compress(history, 4000, force=True)
        events = [event async for event in client.chat_stream(compressed)]
        assert compressor.compression_count == 1
        assert vars(provider.config) == before
        assert [event["type"] for event in events] == ["chunk", "done"]
        assert len(requests) == 2
        summary, generation = requests
        assert summary["max_tokens"] < generation["max_tokens"] == provider.config.max_tokens
        assert summary["extra_body"]["thinking"]["type"] == "disabled"
        assert generation["extra_body"]["thinking"]["type"] == "enabled"
        assert stream.close_count == 1
    asyncio.run(scenario())


def test_closing_backpressured_progress_queue_closes_source_and_reader():
    async def scenario():
        stream = ObservedStream([content_chunk(str(i)) for i in range(10)])
        provider, created = make_provider(stream)
        output = cast(AsyncGenerator[dict, None], stream_with_progress(provider.chat_stream([]), interval=1))
        try:
            async for event in output:
                if event["type"] == "chunk":
                    break
            await asyncio.wait_for(stream.third_read.wait(), timeout=1)
        finally:
            await output.aclose()
        assert len(created) == stream.close_count == 1
        assert not any(t.get_name() == "provider-stream-progress" for t in asyncio.all_tasks())
    asyncio.run(scenario())
