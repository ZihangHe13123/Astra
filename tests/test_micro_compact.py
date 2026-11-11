from agent.runtime.micro_compact import micro_compact_tool_results, results_at_last_cold_start


def _chain(call_id: str, name: str, content: str):
    return [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": call_id, "name": name, "arguments": "{}"}],
        },
        {"role": "tool", "tool_call_id": call_id, "content": content},
    ]


def test_microcompact_clears_only_old_read_results():
    messages = [{"role": "system", "content": "system"}, {"role": "user", "content": "old"}]
    messages += _chain("old-read", "read_file", "A" * 2_000)
    messages += _chain("recent-old-read", "read_file", "D" * 2_000)
    messages += _chain("old-write", "apply_patch", "B" * 2_000)
    messages += [{"role": "assistant", "content": "done"}, {"role": "user", "content": "current"}]
    messages += _chain("current-read", "read_file", "C" * 2_000)

    compacted, stats = micro_compact_tool_results(
        messages,
        tool_risk=lambda name: "write" if name == "apply_patch" else "read",
        char_budget=1_000,
        keep_recent=1,
        clear_batch=1,
    )

    by_id = {message.get("tool_call_id"): message for message in compacted if message.get("role") == "tool"}
    assert "Old tool result content cleared" in by_id["old-read"]["content"]
    assert by_id["recent-old-read"]["content"] == "D" * 2_000
    assert by_id["old-write"]["content"] == "B" * 2_000
    assert by_id["current-read"]["content"] == "C" * 2_000
    assert stats.cleared_results == 1
    assert stats.shadowed_tokens > stats.replacement_tokens > 0
    assert stats.saved_tokens == stats.shadowed_tokens - stats.replacement_tokens
    assert messages[3]["content"] == "A" * 2_000  # durable input is untouched


def test_microcompact_preserves_error_results_and_protocol():
    messages = [{"role": "user", "content": "old"}]
    messages += _chain("failed", "read_file", "Status: error\nTraceback\n" + "X" * 3_000)
    messages += _chain("safe", "read_file", "Y" * 3_000)
    messages += [{"role": "user", "content": "current"}]

    compacted, stats = micro_compact_tool_results(
        messages,
        char_budget=1_000,
        keep_recent=1,
        clear_batch=1,
    )

    tool_ids = [message.get("tool_call_id") for message in compacted if message.get("role") == "tool"]
    assert tool_ids == ["failed", "safe"]
    assert compacted[2]["content"].startswith("Status: error")
    assert stats.cleared_results == 0  # safe is the recent compactable result


def test_microcompact_ignores_transient_system_user_overlay():
    messages = [{"role": "user", "content": "current"}]
    messages += _chain("current", "read_file", "Z" * 4_000)
    messages.append({"role": "user", "content": "[SYSTEM-SUPPLIED TRANSIENT INSTRUCTION]\nverify"})

    compacted, stats = micro_compact_tool_results(
        messages,
        char_budget=500,
        keep_recent=1,
        clear_batch=1,
    )
    assert compacted[2]["content"] == "Z" * 4_000
    assert stats.cleared_results == 0


def _reads(finished: int, current: int = 0, size: int = 2_000):
    """A conversation with this many finished reads, then the current request and its reads."""
    messages = [{"role": "system", "content": "system"}, {"role": "user", "content": "earlier"}]
    for number in range(finished):
        messages += _chain(f"old-{number}", "read_file", f"{number:03d}" + "o" * size)
    messages.append({"role": "user", "content": "current"})
    for number in range(current):
        messages += _chain(f"now-{number}", "read_file", f"{number:03d}" + "n" * size)
    return messages


def _cleared_ids(messages):
    return [message["tool_call_id"] for message in messages if message.get("role") == "tool"
            and message["content"].startswith("[Old tool result content cleared")]


def test_old_results_wait_until_a_whole_batch_is_due_and_then_go_together():
    """Each clear makes the provider read the conversation from there on again, so a long
    conversation that clears one result a turn pays for that on nearly every turn."""
    cleared_as_it_grew = []
    for finished in range(1, 13):
        compacted, stats = micro_compact_tool_results(
            _reads(finished), char_budget=4_000, keep_recent=2, clear_batch=3)
        cleared = _cleared_ids(compacted)
        assert stats.cleared_results == len(cleared)
        # Oldest first, never the recent tail, and only in whole batches.
        assert cleared == [f"old-{number}" for number in range(len(cleared))]
        assert len(cleared) % 3 == 0 and len(cleared) <= max(0, finished - 2)
        cleared_as_it_grew.append(len(cleared))
    # What was cleared stays cleared as the conversation grows, and results were over the
    # budget for a while before the first batch went.
    assert cleared_as_it_grew == sorted(cleared_as_it_grew)
    assert cleared_as_it_grew[3] == 0 and cleared_as_it_grew[-1] >= 6
    # Three changes of the prompt where one result at a time makes one on almost every step.
    assert len(set(cleared_as_it_grew)) - 1 == cleared_as_it_grew[-1] // 3
    one_at_a_time = [
        micro_compact_tool_results(_reads(finished), char_budget=4_000, keep_recent=2, clear_batch=1)[1]
        .cleared_results for finished in range(1, 13)]
    assert len(set(one_at_a_time)) - 1 > 2 * (len(set(cleared_as_it_grew)) - 1)


def test_what_the_current_request_reads_does_not_clear_older_results():
    """The budget is for the results before the current request. Counting the current ones
    too cleared old results in the middle of a turn, one model request after another."""
    under_budget = _reads(4)
    for current in range(0, 6):
        compacted, stats = micro_compact_tool_results(
            _reads(4, current), char_budget=9_000, keep_recent=2, clear_batch=1)
        assert stats.cleared_results == 0
        assert compacted[:len(under_budget)] == under_budget
    # Once the older results are over it themselves, the same ones go at every step of the turn.
    over_budget = micro_compact_tool_results(_reads(8), char_budget=9_000, keep_recent=2, clear_batch=3)[0]
    assert _cleared_ids(over_budget) == ["old-0", "old-1", "old-2"]
    for current in range(1, 6):
        compacted, _ = micro_compact_tool_results(
            _reads(8, current), char_budget=9_000, keep_recent=2, clear_batch=3)
        assert compacted[:len(over_budget)] == over_budget
        assert compacted[len(over_budget):] == _reads(8, current)[len(over_budget):]


def test_clearing_a_cleared_history_again_changes_nothing():
    for finished in (5, 6, 9, 12):
        once, stats = micro_compact_tool_results(
            _reads(finished), char_budget=4_000, keep_recent=2, clear_batch=3)
        assert stats.cleared_results
        again, again_stats = micro_compact_tool_results(once, char_budget=4_000, keep_recent=2, clear_batch=3)
        assert again == once and again_stats.cleared_results == 0


def test_the_batch_size_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("TOOL_RESULT_PROMPT_BUDGET_CHARS", "4000")
    monkeypatch.setenv("TOOL_RESULT_KEEP_RECENT", "2")
    assert micro_compact_tool_results(_reads(9))[1].cleared_results == 0  # ten at a time by default
    monkeypatch.setenv("TOOL_RESULT_CLEAR_BATCH", "4")
    assert micro_compact_tool_results(_reads(9))[1].cleared_results == 4
    monkeypatch.setenv("TOOL_RESULT_CLEAR_BATCH", "1")
    assert micro_compact_tool_results(_reads(9))[1].cleared_results == 7


def _compact_process(payload, tool="process_read"):
    import json
    raw = json.dumps(payload, ensure_ascii=False)
    messages = [{"role": "user", "content": "old"}]
    messages += _chain("process", tool, raw)
    messages += _chain("recent", "read_file", "Z" * 2000)
    messages += [{"role": "user", "content": "continue"}]
    compacted, stats = micro_compact_tool_results(messages, char_budget=1000, keep_recent=1, clear_batch=1)
    assert messages[2]["content"] == raw
    return compacted[2]["content"], stats


def test_process_stamp_keeps_status_exact_byte_range_and_reread_arguments():
    import json
    text = "开始\n" + "中" * 4000 + "\n12 passed"
    payload = {"process_id": "p1", "status": "completed", "exit_code": 0,
                   "artifact_path": "/tmp/log with spaces", "stream": "stderr",
                   "byte_offset": 123, "next_byte_offset": 123 + len(text.encode()),
                   "content": text, "eof": True}
    result, stats = _compact_process(payload)
    stamp = json.loads(result.split("\n", 1)[1])
    assert stamp["status"] == "completed" and stamp["exit_code"] == 0
    assert stamp["artifact_path"] == "/tmp/log with spaces"
    assert stamp["next_byte_offset"] == payload["next_byte_offset"]
    assert stamp["reread"]["arguments"] == {"process_id": "p1", "stream": "stderr",
                                               "byte_offset": 123, "max_chars": len(text)}
    assert "12 passed" in stamp["output_excerpt"]
    assert stats.saved_chars > 0


def test_process_errors_are_not_cleared_even_when_json_quoted():
    for payload in [
        {"process_id": "p", "status": "failed", "exit_code": 1, "content": "X" * 5000},
        {"process_id": "p", "status": "completed", "exit_code": 2, "content": "X" * 5000},
    ]:
        result, stats = _compact_process(payload)
        assert stats.cleared_results == 0
        assert "X" * 5000 in result


def test_poll_stamp_keeps_observed_running_state_and_does_not_invent_output():
    import json
    result, stats = _compact_process({"process_id": "p", "status": "running",
        "exit_code": None, "artifact_path": "/tmp/out", "metadata": {"large": "x" * 5000}},
        "process_poll")
    stamp = json.loads(result.split("\n", 1)[1])
    assert stamp["status"] == "running" and stamp["exit_code"] is None
    assert "output_excerpt" not in stamp and "reread" not in stamp
    assert stats.saved_chars > 0


def _turn(asked_at: float, *calls: str, answered_after: float = 30.0) -> list[dict]:
    """A request, the results it read, and its answer, stored with the times they were added."""
    messages: list[dict] = [{"role": "user", "content": "read", "timestamp": asked_at}]
    for call in calls:
        chain = _chain(call, "read_file", call[:1] * 2_000)
        chain[0]["timestamp"] = asked_at + answered_after / 2
        messages += chain
    return [*messages, {"role": "assistant", "content": "done", "timestamp": asked_at + answered_after}]


def test_only_the_named_results_count_and_are_cleared():
    messages = [{"role": "user", "content": "old"}]
    for call in ("a", "b", "c", "d", "e", "f"):
        messages += _chain(call, "read_file", call * 2_000)
    messages += [{"role": "assistant", "content": "done"}, {"role": "user", "content": "current"}]

    # Six results of 2,000 against 5,000: left to itself the rule clears until the rest fits.
    compacted, stats = micro_compact_tool_results(messages, char_budget=5_000, keep_recent=1, clear_batch=1)
    assert stats.cleared_results == 5
    # Told that only the first three were there: those three are measured against the budget, and
    # the later ones are neither counted nor touched.
    compacted, stats = micro_compact_tool_results(messages, char_budget=5_000, keep_recent=1, clear_batch=1,
                                                  only=frozenset({"a", "b", "c"}))
    by_id = {message["tool_call_id"]: message["content"] for message in compacted if message.get("role") == "tool"}
    assert [call for call, content in by_id.items() if content.startswith("[Old tool result content cleared")] == ["a"]
    assert all(by_id[call] == call * 2_000 for call in "bcdef") and stats.cleared_results == 1
    compacted, stats = micro_compact_tool_results(messages, char_budget=5_000, keep_recent=1, clear_batch=1,
                                                  only=frozenset())
    assert compacted is messages and stats.cleared_results == 0


def test_the_results_a_cold_start_may_clear_are_those_from_before_the_latest_long_pause():
    hour = 3600
    busy = [*_turn(1_000, "a", "b"), *_turn(1_100, "c")]
    # In use without a long pause: no request has begun with nothing cached.
    assert results_at_last_cold_start(busy, hour) == frozenset()
    # Back after an hour: that request is read whole, and everything before it was there.
    back = [*busy, *_turn(1_130 + hour, "d")]
    assert results_at_last_cold_start(back, hour) == {"a", "b", "c"}
    # What the conversation adds within the hour changes nothing, during the turn or in the next ones.
    assert results_at_last_cold_start(back[:-1], hour) == {"a", "b", "c"}
    later = [*back, *_turn(1_200 + hour, "e"), *_turn(1_300 + hour, "f")]
    assert results_at_last_cold_start(later, hour) == {"a", "b", "c"}
    # The next long pause takes in what was added since the last one.
    assert results_at_last_cold_start([*later, *_turn(1_400 + 3 * hour)], hour) == {"a", "b", "c", "d", "e", "f"}
    # A pause is measured from the last answer, not from the request before it.
    slow = [*_turn(1_000, "a", answered_after=hour - 10), *_turn(1_000 + hour, "b")]
    assert results_at_last_cold_start(slow, hour) == frozenset()


def test_a_conversation_without_times_has_no_cold_start_to_clear_at():
    hour = 3600
    untimed = [{key: value for key, value in message.items() if key != "timestamp"}
               for message in [*_turn(1_000, "a"), *_turn(1_000 + 2 * hour, "b")]]
    assert results_at_last_cold_start(untimed, hour) == frozenset()
    # Neither a clock set back nor a time that is not a number makes one.
    odd = [*_turn(5_000, "a"), *_turn(1_000, "b"), {"role": "user", "content": "x", "timestamp": True}]
    assert results_at_last_cold_start(odd, hour) == frozenset()
    # A notice the runtime delivers as a user message starts a request too.
    notice = [*_turn(1_000, "a"), {"role": "user", "content": "done in the background",
                                   "provenance": "notification", "timestamp": 1_030 + hour}]
    assert results_at_last_cold_start(notice, hour) == {"a"}

