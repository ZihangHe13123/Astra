"""Tests for the cheapest-first deterministic compaction layer."""

import asyncio

from agent.runtime.context import AgentContext
from agent.runtime.deterministic_compact import drop_completed_steps


def _risk(name: str) -> str:
    return {"read_file": "read", "search": "network", "write_file": "write"}.get(name, "read")


def _step(name: str, result: str, content: str = "") -> list[dict]:
    return [
        {"role": "assistant", "content": content, "tool_calls": [
            {"id": "call-1", "function": {"name": name, "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "call-1", "content": result},
    ]


def test_drops_completed_read_step():
    messages = [*_step("read_file", "file contents"), {"role": "user", "content": "continue"}]
    out, stats = drop_completed_steps(messages, tool_risk=_risk, keep_recent=0)
    assert stats.dropped_steps == 1
    assert stats.saved_chars > 0
    assert out[0]["role"] == "assistant"
    assert "Completed tool step" in out[0]["content"]
    assert "file contents" not in out[0]["content"]


def test_keeps_side_effecting_write_step():
    messages = [*_step("write_file", "wrote file"), {"role": "user", "content": "continue"}]
    out, stats = drop_completed_steps(messages, tool_risk=_risk)
    assert stats.dropped_steps == 0
    assert out == messages


def test_keeps_failed_step():
    messages = [
        {"role": "assistant", "tool_calls": [
            {"id": "c1", "function": {"name": "read_file", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": "[ToolInputError] read_file: bad args"},
        {"role": "user", "content": "continue"},
    ]
    out, stats = drop_completed_steps(messages, tool_risk=_risk)
    assert stats.dropped_steps == 0
    assert len(out) == 3


def test_keeps_current_turn_steps():
    # The latest user turn is last; the step before it is older and droppable,
    # but a step AFTER the latest user turn must survive.
    messages = [
        *_step("read_file", "old data"),
        {"role": "user", "content": "do it now"},
        *_step("read_file", "fresh data"),
    ]
    out, stats = drop_completed_steps(messages, tool_risk=_risk, keep_recent=0)
    assert stats.dropped_steps == 1
    assert "old data" not in str(out)
    assert "fresh data" in str(out)


def test_keeps_assistant_prose():
    messages = [
        {"role": "assistant", "content": "Looking up the config", "tool_calls": [
            {"id": "c1", "function": {"name": "read_file", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": "config data"},
        {"role": "user", "content": "continue"},
    ]
    out, _ = drop_completed_steps(messages, tool_risk=_risk, keep_recent=0)
    assert "Looking up the config" in out[0]["content"]
    assert "Completed tool step" in out[0]["content"]
    assert "config data" not in out[0]["content"]


def test_no_messages_is_identity():
    out, stats = drop_completed_steps([], tool_risk=_risk)
    assert out == []
    assert stats.dropped_steps == 0


def test_keeps_recent_tool_chain_steps():
    # Old completed steps collapse, but the trailing chain near the latest
    # user turn (snapshot → act style) must survive compaction.
    messages = [
        *_step("read_file", "old data 1"),
        *_step("read_file", "old data 2"),
        *_step("read_file", "old data 3"),
        {"role": "user", "content": "continue the chain"},
        *_step("computer_snapshot", "snapshot_123: fresh tree"),
        *_step("read_file", "next step input"),
    ]
    out, stats = drop_completed_steps(messages, tool_risk=_risk, keep_recent=2)
    assert stats.dropped_steps == 1  # only the oldest step collapsed
    assert "old data 1" not in str(out)
    assert "old data 2" in str(out)
    assert "old data 3" in str(out)
    assert "snapshot_123: fresh tree" in str(out)
    assert "next step input" in str(out)


def test_request_local_tool_result_is_never_collapsed():
    # Request-local results are not persisted anywhere; collapsing them loses
    # the information permanently (computer_snapshot → computer_act chain).
    messages = [
        *_step("computer_snapshot", "snapshot_456: ax tree data"),
        {"role": "user", "content": "continue"},
    ]
    out, stats = drop_completed_steps(
        messages,
        tool_risk=_risk,
        tool_request_local=lambda name: name == "computer_snapshot",
    )
    assert stats.dropped_steps == 0
    assert "snapshot_456: ax tree data" in str(out)


def test_default_keep_recent_protects_tail_when_many_old_steps():
    messages = []
    for index in range(20):
        messages.extend(_step("read_file", f"data {index}"))
    messages.append({"role": "user", "content": "continue"})
    out, stats = drop_completed_steps(messages, tool_risk=_risk)
    assert stats.dropped_steps == 20 - 4
    assert f"data {19}" in str(out)


def test_compress_if_needed_prefers_deterministic_layers():
    """When deterministic layers bring a rebuilt context under budget, the LLM
    summary is never invoked."""
    ctx = AgentContext(system_prompt="sys", max_prompt_tokens=150)
    ctx.tool_risk_provider = lambda name: "read"
    ctx.compact_keep_recent = 0
    # Many completed read steps push the estimate far over budget.
    for index in range(5):
        ctx.messages.append({
            "role": "assistant",
            "tool_calls": [{"id": f"c{index}", "function": {"name": "read_file", "arguments": "{}"}}],
        })
        ctx.messages.append({"role": "tool", "tool_call_id": f"c{index}", "content": "x" * 120})
    ctx.messages.append({"role": "user", "content": "continue"})

    calls: list[bool] = []

    class FakeCompressor:
        async def compress(self, prompt, max_tokens, force=False):
            calls.append(True)
            return prompt

    ctx.compressor = FakeCompressor()  # type: ignore[assignment]
    assert ctx.estimate_prompt_tokens() > ctx.max_prompt_tokens
    asyncio.run(ctx.compress_if_needed(deterministic=True))
    assert calls == []
    assert ctx.estimate_prompt_tokens() <= ctx.max_prompt_tokens
