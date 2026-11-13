import asyncio
from types import SimpleNamespace

import pytest

from agent.runtime.context import AgentContext
from agent.runtime.llm import LLMConfig, OpenAICompatibleProvider


def context_with_old_step():
    context = AgentContext(system_prompt="system", max_prompt_tokens=1000)
    context.compact_keep_recent = 0
    context.tool_risk_provider = lambda name: "read"
    context.messages = [
        {"role": "user", "content": "earlier request"},
        {"role": "assistant", "content": "reading", "tool_calls": [
            {"id": "old", "function": {"name": "read_file", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "old", "content": "old result " * 1000},
        {"role": "user", "content": "continue the current task"},
    ]
    return context


class Summary:
    def __init__(self, cleanup_tokens):
        self.calls = []
        self.llm = SimpleNamespace(estimate_tokens=self.estimate)
        self.cleanup_tokens = cleanup_tokens

    def estimate(self, messages):
        if any(m.get("content") == "history summary" for m in messages):
            return 400
        return 1100 if any(m["role"] == "tool" for m in messages) else self.cleanup_tokens

    async def compress(self, messages, max_prompt_tokens, force=False):
        self.calls.append((max_prompt_tokens, force))
        return [messages[0], {"role": "assistant", "content": "history summary"}, messages[-1]]


@pytest.mark.parametrize("cleanup_tokens", [930, 1200])
def test_cleanup_must_meet_calibrated_target_before_skipping_summary(cleanup_tokens):
    context = context_with_old_step()
    summary = Summary(cleanup_tokens)
    context.compressor = summary
    events = []
    context.compaction_observer = events.append
    asyncio.run(context.compress_if_needed(deterministic=True))
    assert summary.calls == [(900, False)]
    assert [event["status"] for event in events] == ["started", "completed"]
    final = events[-1]
    assert final["method"] == "summary"
    assert final["tokens_before"] == 1100
    assert final["tokens_after"] == 400
    assert final["target_tokens"] == 900
    assert final["dropped_steps"] == 1
    assert context.last_prompt_tokens == 400


def test_sufficient_cleanup_keeps_cheap_path_and_reports_same_estimate_basis():
    context = context_with_old_step()
    summary = Summary(800)
    context.compressor = summary
    context.last_prompt_tokens = 1300  # Previous API usage is not the before estimate.
    events = []
    context.compaction_observer = events.append
    asyncio.run(context.compress_if_needed(deterministic=True))
    assert not summary.calls
    assert events[-1]["method"] == "cleanup"
    assert (events[-1]["tokens_before"], events[-1]["tokens_after"]) == (1100, 800)
    assert (events[-1]["messages_before"], events[-1]["messages_after"]) == (4, 3)


def test_a_conversation_in_use_is_compacted_by_its_summary_alone():
    """Dropping a completed step, clearing an old result or moving a runtime snapshot rewrites an
    earlier message for a few thousand tokens, and the provider then reads everything after it
    again uncached (live, to 2026-11-13: 4.9M tokens read that way for 0.7M taken off). Outside a
    rebuild, only the summary rewrites the history; when it declines, nothing is rewritten."""
    context = context_with_old_step()
    history = [dict(message) for message in context.messages]
    summary = Summary(800)  # what dropping the old step alone would have come to: under the target
    context.compressor = summary
    events = []
    context.compaction_observer = events.append
    asyncio.run(context.compress_if_needed())
    assert summary.calls == [(900, False)]
    final = events[-1]
    assert (final["method"], final["dropped_steps"], final["cleared_results"]) == ("summary", 0, 0)

    declined = context_with_old_step()
    summary = Summary(800)

    async def unchanged(messages, max_prompt_tokens, force=False):
        return messages
    summary.compress = unchanged
    declined.compressor = summary
    asyncio.run(declined.compress_if_needed())
    assert declined.messages == history


def test_caller_measurement_keeps_runtime_overhead_in_after_budget():
    context = context_with_old_step()
    summary = Summary(100)
    context.compressor = summary
    # The actual runtime prompt has extra context absent from canonical history.
    def measure():
        return 700 if any(m.get("content") == "history summary" for m in context.messages) else 1200
    asyncio.run(context.compress_if_needed(measure_tokens=measure))
    assert summary.calls == [(900, False)]
    assert context.last_prompt_tokens == 700


def test_micro_cleanup_reports_savings_when_message_count_is_unchanged():
    context = context_with_old_step()
    context.compact_keep_recent = 100
    context.max_prompt_tokens = 8500
    context.messages = [{"role": "user", "content": "old request"}]
    for i in range(7):
        context.messages.extend([
            {"role": "assistant", "tool_calls": [
                {"id": f"c{i}", "function": {"name": "read_file", "arguments": "{}"}},
            ]},
            {"role": "tool", "tool_call_id": f"c{i}", "content": "x" * 5000},
        ])
    context.messages.append({"role": "user", "content": "current task"})
    def measure():
        return max(1, sum(len(m.get("content", "")) for m in context.messages) // 4)
    events = []
    context.compaction_observer = events.append
    asyncio.run(context.compress_if_needed(measure_tokens=measure, deterministic=True))
    final = events[-1]
    assert final["method"] == "cleanup"
    assert final["messages_before"] == final["messages_after"]
    assert final["cleared_results"] == 2
    assert final["tokens_after"] < final["tokens_before"]


def test_declined_summary_does_not_claim_sufficient_compaction():
    context = context_with_old_step()
    summary = Summary(1200)
    async def unchanged(messages, max_prompt_tokens, force=False):
        return messages
    summary.compress = unchanged
    context.compressor = summary
    events = []
    context.compaction_observer = events.append
    asyncio.run(context.compress_if_needed(deterministic=True))
    final = events[-1]
    assert final["status"] == "completed"  # Cleanup happened, but the target was not met.
    assert final["method"] == "cleanup"
    assert final["tokens_after"] > final["target_tokens"]
    assert context.last_prompt_tokens == 1200


def test_no_change_reports_failure_and_preserves_saved_prefix():
    context = context_with_old_step()
    context.compact_keep_recent = 100
    context._saved_message_count = len(context.messages)
    summary = Summary(1200)
    async def unchanged(messages, max_prompt_tokens, force=False):
        return messages
    summary.compress = unchanged
    context.compressor = summary
    events = []
    context.compaction_observer = events.append
    asyncio.run(context.compress_if_needed())
    assert events[-1]["status"] == "failed"
    assert events[-1]["tokens_before"] == events[-1]["tokens_after"] == 1100
    assert context._saved_message_count == len(context.messages)


def test_readback_failure_does_not_mask_cancellation():
    context = context_with_old_step()
    summary = Summary(1200)
    cancelled = False
    async def cancel(messages, max_prompt_tokens, force=False):
        nonlocal cancelled
        cancelled = True
        raise asyncio.CancelledError()
    summary.compress = cancel
    context.compressor = summary
    def measure():
        if cancelled:
            raise ValueError("fixture readback failure")
        return 1200
    events = []
    context.compaction_observer = events.append
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(context.compress_if_needed(measure_tokens=measure))
    assert events[-1]["status"] == "cancelled"
    assert "tokens_after" not in events[-1]
    assert context._saved_message_count == 0


@pytest.mark.parametrize("async_save", [False, True])
def test_cleanup_followed_by_new_messages_persists_rewritten_prefix(tmp_path, async_save):
    context = context_with_old_step()
    context.set_session(str(tmp_path / "session.json"))
    context.compressor = Summary(800)
    context.save()
    async def scenario():
        await context.compress_if_needed()
        context.add_assistant("new answer")
        context.add_user("follow up")
        if async_save:
            await context.save_async()
        else:
            context.save()
    asyncio.run(scenario())
    restored = AgentContext()
    restored.set_session(context.session_path)
    assert restored.load()
    assert restored.messages == context.messages
    assert "old result " not in str(restored.messages)


@pytest.mark.parametrize("factor", [0.5, 1.5, 2.0])
def test_correct_calibration_is_a_fixed_point(factor):
    provider = object.__new__(OpenAICompatibleProvider)
    provider._estimate_calibration = factor
    for _ in range(10):
        provider.record_prompt_usage(15000, 15000)
    assert provider._estimate_calibration == pytest.approx(factor)


def test_repeated_usage_samples_converge_to_actual_tokens(monkeypatch):
    provider = object.__new__(OpenAICompatibleProvider)
    provider.config = LLMConfig()
    provider._estimate_calibration = 1.0
    monkeypatch.setattr(provider, "_estimate_tokens_with_tokenize", lambda messages: None)
    messages = [{"role": "user", "content": "repeatable text " * 1000}]
    actual = 2 * provider.estimate_tokens(messages)
    for _ in range(40):
        provider.record_prompt_usage(provider.estimate_tokens(messages), actual)
    assert provider.estimate_tokens(messages) == pytest.approx(actual, rel=0.01)
