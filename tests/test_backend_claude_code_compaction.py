"""Real backend protocol: when a Claude Code conversation is compacted, and that it comes through."""

import json
import os
import stat

import pytest

from test_backend_served_model import FAKE_CLAUDE, send
from test_backend_task_protocol import _start_question_protocol_backend, _stop_protocol_backend

pytestmark = pytest.mark.skipif(os.name == "nt", reason="the fake claude executable is a shebang script")

# Three models of one connection: two 1M windows, one with a limit of its own, and a 200k window.
MODELS = """version: 1
providers:
  claude:
    label: Claude Code
    provider: claude-code
    base_url: claude-code://local
    api_key_env: ''
    context_limit: 1000000
    capabilities: [tools, streaming, reasoning, vision]
models:
  sonnet:
    provider: claude-code
    base_url: claude-code://local
    api_key_env: ''
    context_limit: 1000000
    capabilities: [tools, streaming, reasoning, vision]
  opus:
    provider: claude-code
    base_url: claude-code://local
    api_key_env: ''
    context_window: 1000000
    auto_compact_token_limit: 400000
    capabilities: [tools, streaming, reasoning, vision]
  haiku:
    provider: claude-code
    base_url: claude-code://local
    api_key_env: ''
    context_limit: 200000
    capabilities: [tools, streaming, reasoning, vision]
"""

SUMMARY = """## Active Task
None.

## Completed Actions
1. CHECKPOINT-7391: the person sent two long notes and each was acknowledged.

## Critical Context
The notes were filler text."""

TURN_SUMMARY = """## Original Request
Read the files.

## Early Progress
PROGRESS-5512: the first files were read.

## Context for Retained Suffix
Keep reading."""

# A CLI that keeps what each request gave it and reports about as many prompt tokens as that, as the
# real one does. Asked for a summary it writes one; asked to read the files it calls read_file for
# the next one, a call a request (the real one stops at its calls too), until all are read.
RECORDING_CLAUDE = """#!/usr/bin/env python3
import json, os, sys
if sys.argv[1:3] == ["auth", "status"]:
    print(json.dumps({"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "max"}))
    sys.exit(0)
LOG, SUMMARY, TURN_SUMMARY, WORK = %r, %r, %r, %r
arguments = sys.argv[1:]
with open(arguments[arguments.index("--system-prompt-file") + 1], encoding="utf-8") as file:
    system = file.read()
emit = lambda value: (print(json.dumps(value)), sys.stdout.flush())
emit({"type": "system", "subtype": "init", "model": "claude-sonnet-5"})
turns = []
for line in sys.stdin:
    frame = json.loads(line)
    # Without the cache breakpoint, which moves along with the newest turns.
    turns.append([frame["type"], [{key: value for key, value in block.items() if key != "cache_control"}
                                  for block in frame["message"]["content"]]])
    if frame["type"] == "assistant":
        continue
    if frame.get("shouldQuery") is False:
        emit({"type": "result", "subtype": "success", "is_error": False, "num_turns": 0, "usage": {}})
        continue
    break
with open(LOG, "a", encoding="utf-8") as log:
    log.write(json.dumps({"turns": turns, "tools": "--mcp-config" in arguments,
                          "effort": arguments[arguments.index("--effort") + 1]}) + "\\n")
given = len(system) + sum(len(json.dumps(blocks, ensure_ascii=False)) for _, blocks in turns)
usage = {"input_tokens": given // 4, "output_tokens": 5}
asked = json.dumps(turns[-1][1])
# Which file is next is kept outside the conversation: a compaction takes the earlier calls out of it.
read = int(open(LOG + ".read").read()) if os.path.exists(LOG + ".read") else 0
if "TURNS TO SUMMARIZE:" in asked or "TURN PREFIX:" in asked:
    answer = SUMMARY if "TURNS TO SUMMARIZE:" in asked else TURN_SUMMARY
elif "--mcp-config" in arguments and ("Read the files" in asked or "tool_result" in asked) and os.path.exists(
        os.path.join(WORK, "file-%%d.txt" %% read)):
    with open(LOG + ".read", "w") as file:
        file.write(str(read + 1))
    emit({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "toolu_%%d" %% read, "name": "mcp__astra__read_file",
         "input": {"path": "file-%%d.txt" %% read}}]}})
    emit({"type": "user", "message": {"content": [{"type": "tool_result", "is_error": True, "content": "denied"}]}})
    emit({"type": "result", "subtype": "error_max_turns", "is_error": True, "num_turns": 1, "usage": usage})
    sys.exit(0)
else:
    answer = "pong"
emit({"type": "assistant", "message": {"content": [{"type": "text", "text": answer}]}})
emit({"type": "result", "subtype": "success", "is_error": False, "result": answer, "usage": usage})
"""


def backend(tmp_path, cli: str):
    fake = tmp_path / "claude"
    fake.write_text(cli, encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    models = tmp_path / "claude-models.yaml"
    models.write_text(MODELS, encoding="utf-8")
    return _start_question_protocol_backend(
        tmp_path, 0, "claude_code_compaction",
        settings_overrides={"selected_model": "sonnet"},
        env_overrides={"AGENT_MODELS_FILE": str(models), "ASTRA_CLAUDE_CODE_COMMAND": str(fake)},
        workdir=workspace(tmp_path))


def workspace(tmp_path):
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    return work


def recording(tmp_path):
    """A backend on the recording CLI: the process, its events, `turn(text)` to have a message
    answered, and `requests()` for what each CLI process was given, in order."""
    log = tmp_path / "requests.jsonl"
    cli = RECORDING_CLAUDE % (str(log), SUMMARY, TURN_SUMMARY, str(workspace(tmp_path)))
    proc, _, seen, wait_for = backend(tmp_path, cli)

    def turn(text: str) -> dict:
        send(proc, {"type": "message", "text": text})
        wait_for(lambda event: event.get("type") == "done", timeout=60)
        return wait_for(lambda event: event.get("type") == "model_info")

    def requests() -> list[dict]:
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]

    wait_for(lambda event: event.get("type") == "model_info")
    return proc, seen, turn, requests


def note(name: str, tokens: int) -> str:
    """A message of about that many tokens: plain words, four characters to a token."""
    return f"{name} " + "lorem ipsum dolor sit amet " * (tokens * 4 // 27)


def said(turn: list) -> str:
    """The text of one turn the CLI was given."""
    return "\n".join(block.get("text", "") for block in turn[1])


def compaction(seen: list[dict], *, past: int = 150_000) -> dict:
    """The one compaction the backend reported, as it ended: past that size, by a summary."""
    events = [event for event in seen if event.get("type") == "context_compaction"]
    assert [event["status"] for event in events] == ["started", "completed"], f"one compaction, past {past:,}"
    completed = events[1]
    assert completed["method"] == "summary"
    assert completed["tokens_before"] >= past and 150_000 > completed["target_tokens"] > completed["tokens_after"]
    return completed


def repeats(earlier: dict, later: dict) -> bool:
    """Whether the later request begins with everything the earlier one gave the CLI, word for word."""
    return later["turns"][:len(earlier["turns"])] == earlier["turns"]


def files_to_read(tmp_path, count: int, lines: int = 230) -> None:
    """Files of about 3,200 tokens each (at 230 lines), for a turn that reads them one after another."""
    for index in range(count):
        (workspace(tmp_path) / f"file-{index}.txt").write_text(
            "".join(f"{index:02d}-{line:04d} lorem ipsum dolor sit amet consectetur\n" for line in range(lines)),
            encoding="utf-8")


def test_a_claude_code_model_is_compacted_at_150k_unless_it_has_a_limit_of_its_own(tmp_path):
    """Live Astra 2026-11-11: half of the 1M window was the limit, and the thread that had grown to
    226k prompt tokens waited 11 to 41 s for the first event of every reply. The window Astra shows
    for the model is still the model's; what `CTX` is measured against is where it compacts."""
    proc, _, _, wait_for = backend(tmp_path, FAKE_CLAUDE)
    try:
        first = wait_for(lambda event: event.get("type") == "model_info")
        assert (first["model_key"], first["context_limit"]) == ("claude::sonnet", 150_000)
        windows = {model["key"]: model["context_limit"] for model in first["models"]}
        assert windows == {"claude::sonnet": 1_000_000, "claude::opus": 1_000_000, "claude::haiku": 200_000}
        # Half of a 200k window is less; a limit of the model's own stands, here a later one; and
        # coming back to the first model brings its limit back.
        for key, limit in (("claude::haiku", 100_000), ("claude::opus", 400_000), ("claude::sonnet", 150_000)):
            send(proc, {"type": "command", "cmd": f"/model {key}"})
            switched = wait_for(lambda event, key=key: event.get("type") == "model_info" and event.get("model_key") == key)
            assert switched["context_limit"] == limit
    finally:
        _stop_protocol_backend(proc)


def test_a_conversation_past_150k_is_summarized_through_the_cli_and_goes_on(tmp_path):
    """At half the window no conversation ever reached the limit (the largest prompt seen live was
    303k of 500k), so a compaction had never gone through the CLI: the summary is a request of its
    own there, and what is left has to be a conversation the CLI still takes turn by turn."""
    proc, seen, turn, requests = recording(tmp_path)
    try:
        turn(note("NOTE-ONE", 45_000))
        turn(note("NOTE-TWO", 80_000))
        assert not [event for event in seen if event.get("type") == "context_compaction"], "about 128k is under the limit"
        answered = turn(note("NOTE-THREE", 45_000))
        turn("and now?")
    finally:
        _stop_protocol_backend(proc)

    assert answered["context_limit"] == 150_000 and answered["context_used"] < compaction(seen)["target_tokens"]
    # The summary's text is not a reply: each of the four turns got its own answer and nothing else.
    assert "".join(event["content"] for event in seen if event.get("type") == "chunk") == "pong" * 4

    first, second, summary, compacted, after = requests()
    assert [len(request["turns"]) for request in (first, second)] == [1, 3]
    # The conversation thinks as hard as it is set to. The summary is asked for with thinking off, and
    # the CLI's nearest thing to that is its lowest effort.
    assert [request["effort"] for request in (first, second, compacted, after)] == ["high"] * 4
    assert summary["effort"] == "low"
    # The summary is asked for in a request of its own: one turn, no tools, the earlier notes cut short.
    assert summary["tools"] is False and len(summary["turns"]) == 1
    asked = said(summary["turns"][0])
    assert "NOTE-ONE" in asked and "NOTE-TWO" in asked and "NOTE-THREE" not in asked and len(asked) < 10_000
    # The request that answers the third note: the summary where the first two were, the third whole.
    assert compacted["tools"] is True and [role for role, _ in compacted["turns"]] == ["user", "assistant", "user"]
    assert "CHECKPOINT-7391" in said(compacted["turns"][1])
    assert note("NOTE-THREE", 45_000) in said(compacted["turns"][2])
    assert not any("NOTE-ONE" in said(each) or "NOTE-TWO" in said(each) for each in compacted["turns"])
    # The next turn hands the CLI that history exactly as it was sent, then what came after it.
    assert after["turns"][:3] == compacted["turns"]
    assert said(after["turns"][3]) == "pong" and "and now?" in said(after["turns"][4]) and len(after["turns"]) == 5


def test_a_tool_loop_that_passes_150k_is_summarized_between_two_calls_and_finishes(tmp_path):
    """A turn of many tool calls is where a desktop conversation grows fastest, so the limit is
    reached between two calls. The earlier turns hold 125k tokens a summary takes off, well over a
    quarter of the line, so the history is rewritten there rather than carried a third further: the
    earlier turns and the start of this one are summarized in two requests, and the calls kept with
    their results are still pairs the CLI accepts."""
    files = 18
    for index in range(files):
        (workspace(tmp_path) / f"file-{index}.txt").write_text(
            "".join(f"{index:02d}-{line:04d} lorem ipsum dolor sit amet consectetur\n" for line in range(230)), encoding="utf-8")
    proc, seen, turn, requests = recording(tmp_path)
    try:
        turn(note("NOTE-ONE", 45_000))
        turn(note("NOTE-TWO", 80_000))
        before = len(requests())
        turn("Read the files, one after another.")
    finally:
        _stop_protocol_backend(proc)

    compaction(seen)
    # Every file was read, each once, and the turn ended with its answer.
    results = [event for event in seen if event.get("type") == "tool_result"]
    assert [event["call_id"] for event in results] == [f"toolu_{index}" for index in range(files)]
    assert all(f"{index:02d}-0229 lorem" in event["output"] for index, event in enumerate(results))
    assert "".join(event["content"] for event in seen if event.get("type") == "chunk") == "pong" * 3

    loop = requests()[before:]
    plain = [index for index, request in enumerate(loop) if not request["tools"]]
    assert len(plain) == 2 and plain[1] == plain[0] + 1 and 0 < plain[0] < len(loop) - 3, "between two calls"
    # Two summaries stand between the person and the rest of the turn, so neither thinks at length.
    assert [request["effort"] for request in loop] == [
        "low" if index in plain else "high" for index in range(len(loop))]
    earlier, start = (said(loop[index]["turns"][0]) for index in plain)
    assert "TURNS TO SUMMARIZE:" in earlier and "NOTE-ONE" in earlier and "NOTE-TWO" in earlier
    assert "TURN PREFIX:" in start and "Read the files, one after another." in start and "NOTE-" not in start
    # The request after them: both summaries, the person's request in their own words, and then
    # only calls that still have their results, down to the one the model is now answering.
    compacted = loop[plain[1] + 1]["turns"]
    opening = said(compacted[0])
    assert compacted[0][0] == "user" and "CHECKPOINT-7391" in opening and "PROGRESS-5512" in opening
    assert opening.rstrip().endswith("Read the files, one after another.")
    assert not any("NOTE-ONE" in json.dumps(blocks) or "NOTE-TWO" in json.dumps(blocks) for _, blocks in compacted)
    kept = compacted[1:]
    assert len(kept) >= 6 and [role for role, _ in kept] == ["assistant", "user"] * (len(kept) // 2)
    for (_, calls), (_, answers) in zip(kept[::2], kept[1::2]):
        assert [block["id"] for block in calls] == [block["tool_use_id"] for block in answers if block["type"] == "tool_result"]
    assert kept[-1][1][0]["tool_use_id"] == f"toolu_{plain[0] - 1}", "the call made just before the summaries"


# The person's own request of about 130k tokens opens the turn: it is kept word for word, so there is
# little a summary could take off when the turn reaches 150k (an hour-old conversation's fixed part,
# its system prompt and tool definitions, behaves the same way).
BIG_REQUEST = "Read the files, one after another. " + note("CONTEXT", 130_000)


def test_a_tool_loop_with_little_to_summarize_goes_on_past_150k_and_is_compacted_once(tmp_path):
    """Live 2026-11-13: a Claude Code turn whose history had been summarized already reached 150k three
    times in five minutes and rewrote it each time, reading 249k tokens uncached for 10-20k it took
    off. With little to take off, the turn goes on past the line, and is compacted once, when the
    calls it has made are worth summarizing or a third further, between two calls."""
    files_to_read(tmp_path, 30)
    proc, seen, turn, requests = recording(tmp_path)
    try:
        turn(note("NOTE-ONE", 2_000))
        before = len(requests())
        turn(BIG_REQUEST)
    finally:
        _stop_protocol_backend(proc)

    events = [event for event in seen if event.get("type") == "context_compaction"]
    assert [event["status"] for event in events] == ["started", "completed"], "one compaction"
    done = events[1]
    # Well past the line; the request it keeps word for word leaves it over the line afterwards too.
    assert done["method"] == "summary" and done["tokens_before"] >= 180_000 > done["tokens_after"]
    results = [event for event in seen if event.get("type") == "tool_result"]
    assert [event["call_id"] for event in results] == [f"toolu_{index}" for index in range(30)]
    loop = requests()[before:]
    plain = [index for index, request in enumerate(loop) if not request["tools"]]
    assert plain and plain == list(range(plain[0], plain[0] + len(plain))), "summaries together, between two calls"
    # The turn crossed 150k after about six files; up to the summaries every request repeated the
    # one before it.
    assert plain[0] > 14, "well past the line"
    assert all(repeats(earlier, later) for earlier, later in zip(loop[:plain[0] - 1], loop[1:plain[0]]))
    after = loop[plain[-1] + 1:]
    assert all(repeats(earlier, later) for earlier, later in zip(after, after[1:]))


def test_a_tool_loop_that_ends_past_150k_with_little_to_summarize_is_compacted_when_the_next_turn_begins(tmp_path):
    """Past the line with little to take off, the turn ends as it went: every request repeats the one
    before it. The next turn's first request compacts at the line, once, before it is sent."""
    files_to_read(tmp_path, 12)
    proc, seen, turn, requests = recording(tmp_path)
    try:
        turn(note("NOTE-ONE", 2_000))
        before = len(requests())
        turn(BIG_REQUEST)
        loop = requests()[before:]
        assert not [event for event in seen if event.get("type") == "context_compaction"]
        turn("and now?")
    finally:
        _stop_protocol_backend(proc)

    assert len(loop) == 13 and all(request["tools"] for request in loop)
    assert all(repeats(earlier, later) for earlier, later in zip(loop, loop[1:]))
    compaction(seen)
    rest = requests()[before + len(loop):]
    assert not rest[0]["tools"] and rest[-1]["tools"] and "and now?" in said(rest[-1]["turns"][-1])
    assert "CHECKPOINT-7391" in said(rest[-1]["turns"][0])


def test_a_long_tool_loop_whose_own_calls_are_worth_summarizing_is_compacted_at_150k(tmp_path):
    """A turn that started small and read until it reached the line has its own earlier calls to
    take off: compacted there, between two calls, not carried a third further."""
    files_to_read(tmp_path, 30, lines=460)
    proc, seen, turn, requests = recording(tmp_path)
    try:
        turn(note("NOTE-ONE", 2_000))
        before = len(requests())
        turn("Read the files, one after another.")
    finally:
        _stop_protocol_backend(proc)

    first = [event for event in seen if event.get("type") == "context_compaction"][:2]
    assert [event["status"] for event in first] == ["started", "completed"]
    assert first[1]["method"] == "summary" and 150_000 <= first[1]["tokens_before"] < 160_000
    loop = requests()[before:]
    plain = [index for index, request in enumerate(loop) if not request["tools"]]
    assert plain and "TURN PREFIX:" in said(loop[plain[-1]]["turns"][0])
