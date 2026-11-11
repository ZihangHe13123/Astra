import asyncio
import json
import threading
import time

from agent.runtime.async_io import durable_io, routine_io
from agent.runtime.latency import RuntimeProfiler
from agent.runtime.tools.registry import ToolDef, ToolRegistry
from test_search_responsiveness import call, make_agent


def test_runtime_profiler_is_opt_in(tmp_path, monkeypatch):
    monkeypatch.delenv("ASTRA_PROFILE_QUERY", raising=False)
    assert RuntimeProfiler.from_env(tmp_path) is None
    assert not list(tmp_path.iterdir())


def test_tool_timing_accounts_for_scheduler_barriers_and_approval(tmp_path):
    async def scenario():
        profiler = RuntimeProfiler(tmp_path / "profile.jsonl")
        registry = ToolRegistry(artifact_dir=tmp_path / "artifacts")

        async def tool():
            await asyncio.sleep(0.025)
            return "PRIVATE OUTPUT"

        async def approve(request):
            await asyncio.sleep(0.02)
            return "once"

        registry.register(ToolDef("first", "first", {"type": "object"}, tool, risk="write", approval="always"))
        registry.register(ToolDef("second", "second", {"type": "object"}, tool, risk="read"))
        registry.set_approval_handler(approve)
        with profiler.activate():
            events = await make_agent(registry)._execute_tool_calls([
                call("first", "PRIVATE CALL ONE"), call("second", "PRIVATE CALL TWO"),
            ])
            assert all(not event.get("error") for event in events)
            assert await durable_io(lambda: "PRIVATE IO") == "PRIVATE IO"
        await profiler.close()
        raw = profiler.path.read_text()
        assert "PRIVATE" not in raw
        tools = [e for e in map(json.loads, raw.splitlines()) if e["kind"] == "tool"]
        assert len(tools) == 2
        assert tools[0]["metrics_ms"]["approval_ms"] >= 15
        assert tools[1]["metrics_ms"]["queue_ms"] >= 35
        assert tools[0]["metrics_ms"]["execute_ms"] >= 20
        assert tools[0]["metrics_ms"]["total_ms"] >= 40
        assert any(e["kind"] == "io" for e in map(json.loads, raw.splitlines()))

    asyncio.run(scenario())


def test_a_poll_on_a_timer_is_recorded_only_when_it_was_slow(tmp_path):
    async def scenario():
        profiler = RuntimeProfiler(tmp_path / "profile.jsonl")

        def quick():
            return "quick"

        def slow():
            time.sleep(0.08)
            return "slow"

        def once():
            return "once"

        with profiler.activate():
            assert [await routine_io(quick), await routine_io(slow), await durable_io(once)] == ["quick", "slow", "once"]
        await profiler.close()
        took = {e["label"].rsplit(".", 1)[-1]: e["metrics_ms"]["total_ms"]
                for e in map(json.loads, profiler.path.read_text().splitlines()) if e["kind"] == "io"}
        assert took["slow"] >= 80 and "once" in took
        # A quick poll is left out; only a machine too busy to run it within 50 ms would record it.
        assert took.get("quick", 50) >= 50

    asyncio.run(scenario())


def test_receipts_are_bounded_validated_and_not_cross_clock_subtractions(tmp_path):
    async def scenario():
        profiler = RuntimeProfiler(tmp_path / "profile.jsonl")
        event = profiler.trace_event({"type": "chunk", "content": "PRIVATE"})
        assert "performance_trace_id" not in profiler.trace_event({"type": "chunk", "content": "next"})
        profiler.acknowledge([{"trace_id": event["performance_trace_id"], "handle_ms": 2,
                               "react_commit_ms": 3, "parse_ms": float("nan"), "secret": "PRIVATE"}])
        profiler.acknowledge([{"trace_id": event["performance_trace_id"], "handle_ms": 999}])
        profiler.acknowledge([{"trace_id": "unknown", "handle_ms": 999}])
        await profiler.close()
        raw = profiler.path.read_text()
        assert "PRIVATE" not in raw
        records = [r for r in map(json.loads, raw.splitlines()) if r["kind"] == "frontend"]
        assert len(records) == 1
        assert records[0]["metrics_ms"]["handle_ms"] == 2
        assert records[0]["metrics_ms"]["react_commit_ms"] == 3
        assert "parse_ms" not in records[0]["metrics_ms"]
        assert records[0]["metrics_ms"]["receipt_roundtrip_ms"] >= 0

    asyncio.run(scenario())


def test_slow_profiling_sink_drops_metrics_without_blocking_task_loop(tmp_path):
    async def scenario():
        profiler = RuntimeProfiler(tmp_path / "profile.jsonl", capacity=2)
        original = profiler._append
        started, release = threading.Event(), threading.Event()

        def slow(records):
            started.set()
            assert release.wait(2)
            original(records)

        profiler._append = slow
        try:
            profiler.record("io", {"total_ms": 1})
            assert await asyncio.to_thread(started.wait, 1)
            for _ in range(20):
                profiler.record("io", {"total_ms": 1})
            await asyncio.sleep(0.01)
            assert profiler.dropped == 18
        finally:
            release.set()
            await profiler.close()
        summary = json.loads(profiler.path.read_text().splitlines()[-1])
        assert summary["dropped_records"] == 18

    asyncio.run(scenario())
