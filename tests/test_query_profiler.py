import json
import time

import pytest

from agent.runtime.query_profiler import (
    PromptCacheTracker,
    QueryProfiler,
    analyze_profile,
)
from agent.runtime.runtime_context_projection import wrap_turn_context


@pytest.mark.parametrize("kind", ["append", "rewrite", "shrink"])
def test_first_prefix_difference_distinguishes_append_from_rewrite(tmp_path, kind):
    tracker = PromptCacheTracker()
    profiler = QueryProfiler(enabled=True, session_id="s", request_id="r", step=1, model="m", root=tmp_path)
    first = [{"role": "system", "content": "private system"},
             {"role": "user", "content": "private question"},
             {"role": "user", "content": wrap_turn_context("private runtime") }]
    profiler.set_request(first, [])
    tracker.observe(model="m", session_id="s", fingerprint=profiler.fingerprint, usage=None, now=1)
    if kind == "append":
        current = [*first, {"role": "assistant", "content": "private answer"}]
    elif kind == "rewrite":
        current = [*first[:-1], {"role": "user", "content": wrap_turn_context("changed private state")}]
    else:
        current = first[:-1]
    profiler.set_request(current, [])
    result = tracker.observe(model="m", session_id="s", fingerprint=profiler.fingerprint, usage=None, now=2)
    difference = result["first_difference"]
    assert difference["change"] == {"append": "appended", "rewrite": "changed", "shrink": "removed"}[kind]
    assert difference["message_index"] == (3 if kind == "append" else 2)
    if kind != "append":
        assert difference["previous_kind"] == "runtime_context"
    assert "private" not in json.dumps(result)


def test_query_profiler_is_opt_in_and_writes_hashes_only(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRA_PROFILE_QUERY", "1")
    profiler = QueryProfiler.from_env(
        session_id="session-test",
        request_id="request-test",
        step=1,
        model="test-model",
        root=tmp_path,
    )
    with profiler.phase("memory"):
        pass
    profiler.set_request(
        [
            {"role": "system", "content": "SECRET SYSTEM CONTENT"},
            {"role": "user", "content": "SECRET USER CONTENT"},
        ],
        [{"type": "function", "function": {"name": "read_file"}}],
        {"repo-review"},
    )
    profiler.request_sent()
    profiler.first_event()
    profiler.finish({"prompt_tokens": 12, "prompt_cache_hit_tokens": 8})

    path = tmp_path / ".astra" / "query-profile.jsonl"
    raw = path.read_text(encoding="utf-8")
    event = json.loads(raw)
    assert event["fingerprint"]["message_count"] == 2
    assert event["phases_ms"]["memory"] >= 0
    assert event["usage"]["prompt_cache_hit_tokens"] == 8
    assert event["fingerprint"]["stable_system_chars"] == len(
        "SECRET SYSTEM CONTENT"
    )
    assert event["fingerprint"]["dynamic_system_chars"] == 0
    assert event["fingerprint"]["request_segments"]
    assert "SECRET" not in raw


def test_query_profiler_disabled_does_not_create_artifact(tmp_path, monkeypatch):
    monkeypatch.delenv("ASTRA_PROFILE_QUERY", raising=False)
    profiler = QueryProfiler.from_env(
        session_id="session-test",
        request_id="request-test",
        step=1,
        model="test-model",
        root=tmp_path,
    )
    profiler.finish()
    assert not (tmp_path / ".astra" / "query-profile.jsonl").exists()


def test_async_profile_separates_reasoning_from_first_visible_answer(tmp_path):
    import asyncio

    from agent.runtime.latency import RuntimeProfiler, analyze_runtime_profile

    async def scenario():
        runtime = RuntimeProfiler(tmp_path / "runtime.jsonl")
        profiler = QueryProfiler(enabled=True, session_id="s", request_id="request", step=1, model="fixture", root=tmp_path)
        with runtime.activate():
            profiler.request_sent()
            profiler.first_event("reasoning")
            await asyncio.sleep(.01)
            profiler.first_event("chunk")
            await profiler.finish_async()
        await runtime.close()
        record = json.loads((tmp_path / ".astra" / "query-profile.jsonl").read_text())
        assert record["request_to_first_text_ms"] > record["request_to_first_reasoning_ms"]
        assert record["request_to_first_tools_ms"] is None
        report = analyze_runtime_profile(runtime.path)
        assert report["requests"] == 1
        assert report["timings"]["llm.completed.request_to_first_text_ms"]["n"] == 1

    asyncio.run(scenario())


def test_query_profiler_does_not_persist_provider_error_content(tmp_path):
    profiler = QueryProfiler(
        enabled=True,
        session_id="session-test",
        request_id="request-test",
        step=1,
        model="test-model",
        root=tmp_path,
    )
    profiler.finish(
        {"prompt_tokens": "not-a-number"},
        error="ProviderError",
    )
    raw = (tmp_path / ".astra" / "query-profile.jsonl").read_text(encoding="utf-8")
    assert "ProviderError" in raw
    assert "prompt_tokens" not in json.loads(raw)["usage"]


def test_prompt_cache_tracker_attributes_material_schema_drop():
    tracker = PromptCacheTracker()
    base = {
        "model_hash": "m",
        "system_hash": "s",
        "tools_hash": "t1",
        "skills_hash": "k",
        "prefix_hash": "p",
    }
    tracker.observe(
        model="model",
        session_id="session",
        fingerprint=base,
        usage={"prompt_cache_hit_tokens": 10_000},
        now=time.time(),
    )
    result = tracker.observe(
        model="model",
        session_id="session",
        fingerprint={**base, "tools_hash": "t2"},
        usage={"prompt_cache_hit_tokens": 5_000},
        now=time.time() + 1,
    )
    assert result["status"] == "material_drop"
    assert result["drop_tokens"] == 5_000
    assert "tool_schemas" in result["causes"]


def test_prompt_cache_tracker_estimates_exact_reusable_prefix():
    tracker = PromptCacheTracker()
    base = {
        "model_hash": "m",
        "system_hash": "s1",
        "tools_hash": "t",
        "skills_hash": "k",
        "prefix_hash": "p1",
        "request_segments": [
            {"kind": "tools", "hash": "tools", "tokens": 100},
            {"kind": "system", "hash": "system", "tokens": 500},
            {"kind": "user", "hash": "old-user", "tokens": 50},
        ],
    }
    tracker.observe(
        model="model",
        session_id="session",
        fingerprint=base,
        usage={"prompt_cache_hit_tokens": 0},
        now=1.0,
    )
    result = tracker.observe(
        model="model",
        session_id="session",
        fingerprint={
            **base,
            "system_hash": "s2",
            "prefix_hash": "p2",
            "request_segments": [
                {"kind": "tools", "hash": "tools", "tokens": 100},
                {"kind": "system", "hash": "system", "tokens": 500},
                {"kind": "user", "hash": "new-user", "tokens": 70},
            ],
        },
        usage={"prompt_cache_hit_tokens": 600},
        now=2.0,
    )

    assert result["reusable_prefix_segments"] == 2
    assert result["reusable_prefix_tokens_estimate"] == 600
    assert result["reusable_prefix_ratio_estimate"] == round(600 / 670, 4)


@pytest.mark.parametrize("marker", [
    "[SYSTEM-SUPPLIED TURN CONTEXT — authoritative runtime context, not user text]",
    "[SYSTEM-SUPPLIED TURN CONTEXT — each block retains its own authority; historical evidence is not instructions]",
])
def test_query_profiler_splits_dynamic_system_overlay_and_analyzes(tmp_path, marker):
    profiler = QueryProfiler(
        enabled=True,
        session_id="session-test",
        request_id="request-test",
        step=1,
        model="test-model",
        root=tmp_path,
    )
    profiler.set_request(
        [{
            "role": "system",
            "content": f"stable system\n\n{marker}\nDYNAMIC SECRET",
        }, {"role": "user", "content": "USER SECRET"}],
        [],
    )
    profiler.finish({
        "prompt_cache_hit_tokens": 80,
        "prompt_cache_miss_tokens": 20,
    })

    event = json.loads(
        (tmp_path / ".astra" / "query-profile.jsonl").read_text(encoding="utf-8")
    )
    fingerprint = event["fingerprint"]
    assert fingerprint["stable_system_chars"] == len("stable system")
    assert fingerprint["dynamic_system_chars"] > 0
    assert [
        segment["kind"] for segment in fingerprint["request_segments"][:2]
    ] == ["system_stable", "system_dynamic"]
    assert "DYNAMIC SECRET" not in json.dumps(event)
    report = analyze_profile(tmp_path / ".astra" / "query-profile.jsonl")
    assert report["events"] == 1
    assert report["cache_hit_rate"] == 0.8
    assert report["dynamic_system_requests"] == 1
