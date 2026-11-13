"""A long Claude Code conversation's summary is written while its cache is still warm.

The CLI keeps a request for an hour. A conversation the person comes back to after that is read
whole, at the full price; a summary asked for just before then is read from the cache. The next
turn uses that summary only if the cache has gone since; otherwise the conversation goes on in
full. A real agent loop on the real Claude Code provider, with a stand-in CLI that records what
each request gave it. Time passes through ``idle_compaction.clock``, the clock the rules read.
"""

import asyncio
import contextlib
import json
import os
import stat
import time

import pytest

from agent.channels.base import ChannelMessage
from agent.channels.router import AgentChannelRouter
from agent.core.msg import ContentBlock, Msg
from agent.runtime import claude_code_provider as ccp
from agent.runtime import idle_compaction
from agent.runtime.claude_code_provider import ClaudeCodeProvider
from agent.runtime.idle_compaction import IdleCompactor
from agent.runtime.llm import LLMClient, LLMConfig
from agent.runtime.react import ReActAgent
from agent.runtime.tools.registry import ToolRegistry

pytestmark = pytest.mark.skipif(os.name == "nt", reason="the stand-in claude executable is a shebang script")

HOUR = 3600
SUMMARY = "## Active Task\nNone.\n\n## Completed Actions\n1. CHECKPOINT-4417: every note was read and acknowledged."
CHECKPOINT = "Context checkpoint request"

# Records each request's turns; writes the summary when asked for a checkpoint, else acknowledges.
STAND_IN = """#!/usr/bin/env python3
import json, os, sys, time
if sys.argv[1:3] == ["auth", "status"]:
    print(json.dumps({"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "max"}))
    sys.exit(0)
LOG, SUMMARY = %r, %r
arguments = sys.argv[1:]
with open(arguments[arguments.index("--system-prompt-file") + 1], encoding="utf-8") as file:
    system = file.read()
emit = lambda value: (print(json.dumps(value)), sys.stdout.flush())
emit({"type": "system", "subtype": "init", "model": "claude-opus-5"})
turns = []
for line in sys.stdin:
    frame = json.loads(line)
    turns.append([frame["type"], [{key: value for key, value in block.items() if key != "cache_control"}
                                  for block in frame["message"]["content"]]])
    if frame["type"] == "assistant":
        continue
    if frame.get("shouldQuery") is False:
        emit({"type": "result", "subtype": "success", "is_error": False, "num_turns": 0, "usage": {}})
        continue
    break
with open(LOG, "a", encoding="utf-8") as log:
    log.write(json.dumps({"turns": turns}) + "\\n")
given = len(system) + sum(len(json.dumps(blocks, ensure_ascii=False)) for _, blocks in turns)
checkpoint = "Context checkpoint request" in json.dumps(turns[-1][1])
while checkpoint and os.path.exists(LOG + ".hold"):
    time.sleep(0.02)
answer = SUMMARY if checkpoint else "Noted."
emit({"type": "assistant", "message": {"content": [{"type": "text", "text": answer}]}})
emit({"type": "result", "subtype": "success", "is_error": False, "result": answer,
      "usage": {"input_tokens": given // 4, "output_tokens": 5}})
"""


def note(n: int) -> str:
    return f"NOTE-{n:02d} " + "".join(f"line {line} of note {n} for the record. " for line in range(60))


class Setup:
    def __init__(self, tmp_path, monkeypatch):
        self.log = tmp_path / "requests.jsonl"
        cli = tmp_path / "claude"
        cli.write_text(STAND_IN % (str(self.log), SUMMARY), encoding="utf-8")
        cli.chmod(cli.stat().st_mode | stat.S_IXUSR)
        monkeypatch.setenv("ASTRA_CLAUDE_CODE_COMMAND", str(cli))
        monkeypatch.setenv("ASTRA_HOME", str(tmp_path / "home"))
        monkeypatch.setenv("ASTRA_IDLE_COMPACT_MIN_TOKENS", "2000")
        workdir = tmp_path / "claude-code"
        workdir.mkdir()
        monkeypatch.setattr(ccp, "workspace", lambda: str(workdir))
        for flag in ("replay", "cache_marker", "thinking_display"):
            monkeypatch.setattr(ClaudeCodeProvider, flag, True)
        # How far the clock is ahead of the moment the test runs.
        self.ahead = 0.0
        monkeypatch.setattr(idle_compaction, "clock", lambda: time.time() + self.ahead)
        self.agent = ReActAgent(
            "agent", LLMClient(LLMConfig(model="opus", provider="claude-code", base_url=ccp.BASE_URL,
                                         capabilities=frozenset({"streaming", "tools", "reasoning"}),
                                         reasoning_effort="high")),
            ToolRegistry(), system_prompt="You are Astra.", max_iterations=2,
            progressive_tools=False, query_profile_enabled=False)
        self.agent.context.set_session(str(tmp_path / "conversation.json"))
        # A tail of about two notes stays word for word; what is older is summarized.
        assert self.agent.context.compressor is not None
        self.agent.context.compressor.tail_token_budget = 1_500
        self.events: list[dict] = []
        self.agent.context.compaction_observer = self.events.append

    def requests(self) -> list[dict]:
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    async def turn(self, text: str) -> None:
        async for _ in self.agent.reply_stream(Msg(content=[ContentBlock.text(text)])):
            pass

    async def notes(self, count: int) -> None:
        for n in range(count):
            await self.turn(note(n))

    def compactor(self) -> IdleCompactor:
        return IdleCompactor(lambda: [(self.agent.context, {})])


async def idle(compactor: IdleCompactor) -> None:
    """One check of the backend's, and the summary it starts, if any."""
    compactor.tick(busy=False)
    task = compactor._task
    if task is not None:
        await task


def text_of(request: dict) -> str:
    return json.dumps(request["turns"], ensure_ascii=False)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    return Setup(tmp_path, monkeypatch)


def test_the_summary_is_written_from_the_cached_request_before_the_hour_is_over(setup):
    async def scenario():
        await setup.notes(6)
        history = [dict(message) for message in setup.agent.context.messages]
        sent = setup.requests()
        compactor = setup.compactor()

        setup.ahead = 50 * 60  # quiet for 50 minutes: not yet
        await idle(compactor)
        assert setup.requests() == sent

        setup.ahead = 55 * 60
        await idle(compactor)
        asked = setup.requests()[len(sent):]
        # One request: the last one word for word, then what came after it and the checkpoint
        # instruction, so the CLI reads all of it from the cache.
        assert len(asked) == 1 and CHECKPOINT in text_of(asked[0])
        assert asked[0]["turns"][:len(sent[-1]["turns"])] == sent[-1]["turns"]
        # The history does not change: the summary waits beside it.
        assert setup.agent.context.messages == history and not setup.events

        # A message Astra sends on its own (a job's report in a chat) joins the history without a
        # turn, and the pause goes on into the next hour of the cache that request kept. The summary
        # still covers what it covered: one summary per pause.
        setup.agent.context.add_assistant("A report.")
        for minutes in (58, 110):
            setup.ahead = minutes * 60
            await idle(compactor)
        assert len(setup.requests()) == len(sent) + 1
    asyncio.run(scenario())


def test_a_turn_after_the_cache_has_gone_starts_from_the_summary(setup):
    async def scenario():
        await setup.notes(6)
        setup.ahead = 55 * 60
        await idle(setup.compactor())
        # An hour after the summary request too: the cache is gone.
        setup.ahead = 55 * 60 + HOUR + 60
        await setup.turn("NOTE-LATE one more thing.")
        request = setup.requests()[-1]
        assert "CHECKPOINT-4417" in text_of(request) and CHECKPOINT not in text_of(request)
        assert "NOTE-00" not in text_of(request) and "NOTE-05" in text_of(request)
        messages = setup.agent.context.messages
        assert any("CHECKPOINT-4417" in str(message.get("content")) for message in messages)
        assert not any("NOTE-00" in str(message.get("content")) for message in messages)
        assert messages[-1]["role"] == "assistant" and messages[-1]["content"] == "Noted."
        assert "NOTE-LATE" in str([m.get("content") for m in messages if m["role"] == "user"][-1])
        completed = [event for event in setup.events if event["status"] == "completed"]
        assert len(completed) == 1 and completed[0]["trigger"] == "idle" and completed[0]["method"] == "summary"
    asyncio.run(scenario())


def test_a_turn_while_the_summary_request_keeps_the_cache_goes_on_in_full(setup):
    async def scenario():
        await setup.notes(6)
        conversation = setup.requests()[-1]
        setup.ahead = 55 * 60
        await idle(setup.compactor())
        # 70 minutes after the conversation's last request, 15 after the summary request, which
        # read the cache again: it is still there, and the conversation goes on in full.
        setup.ahead = 70 * 60
        await setup.turn("NOTE-LATE one more thing.")
        request = setup.requests()[-1]
        assert request["turns"][:len(conversation["turns"])] == conversation["turns"]
        assert "CHECKPOINT-4417" not in text_of(request)
        assert not any("CHECKPOINT-4417" in str(message.get("content")) for message in setup.agent.context.messages)
        # And the summary is gone: the next pause writes a new one.
        setup.ahead = 4 * HOUR
        await setup.turn("NOTE-LATER")
        assert "CHECKPOINT-4417" not in text_of(setup.requests()[-1])
    asyncio.run(scenario())


def test_a_summary_finished_after_the_conversation_moved_on_is_not_kept(setup):
    async def scenario():
        await setup.notes(6)
        compactor = setup.compactor()
        hold = setup.log.with_name(setup.log.name + ".hold")
        hold.touch()
        setup.ahead = 55 * 60
        compactor.tick(busy=False)
        task = compactor._task
        assert task is not None
        while CHECKPOINT not in setup.log.read_text(encoding="utf-8"):
            await asyncio.sleep(0.05)
        # The person writes while the summary is being written.
        setup.agent.context.add_user("NOTE-LATE")
        hold.unlink()
        await task
        setup.agent.context.add_assistant("Noted.")
        setup.ahead = 4 * HOUR
        await setup.turn("NOTE-LATER")
        assert "CHECKPOINT-4417" not in text_of(setup.requests()[-1])
    asyncio.run(scenario())


@pytest.mark.parametrize("minimum, quiet", [
    ("off", 55 * 60),          # turned off
    ("1000000", 55 * 60),      # the conversation is smaller than the minimum
    ("2000", HOUR + 60),       # the cache is already gone: the request would read nothing cached
])
def test_no_summary_is_written(setup, monkeypatch, minimum, quiet):
    monkeypatch.setenv("ASTRA_IDLE_COMPACT_MIN_TOKENS", minimum)

    async def scenario():
        await setup.notes(6)
        sent = len(setup.requests())
        setup.ahead = quiet
        await idle(setup.compactor())
        assert len(setup.requests()) == sent
    asyncio.run(scenario())


def test_a_chat_waiting_for_its_next_message_is_summarized_and_used_the_same_way(setup, tmp_path):
    """WeChat: one long conversation, written to hours apart."""
    agent = setup.agent

    async def scenario():
        router = AgentChannelRouter(agent, asyncio.Lock(), tmp_path / "sessions")
        for n in range(6):
            await router.handle(ChannelMessage("weixin", "owner", "owner", note(n)))
        (name, chat), = router.parked_contexts()
        assert chat is not agent.context
        chat.compressor.tail_token_budget = 1_500
        compactor = IdleCompactor(lambda: [(context, {"source": "channel", "session": session})
                                           for session, context in router.parked_contexts()])
        setup.ahead = 55 * 60
        await idle(compactor)
        assert CHECKPOINT in text_of(setup.requests()[-1])
        setup.ahead = 55 * 60 + HOUR + 60
        await router.handle(ChannelMessage("weixin", "owner", "owner", "NOTE-LATE"))
        request = setup.requests()[-1]
        assert "CHECKPOINT-4417" in text_of(request) and "NOTE-00" not in text_of(request)
        assert any("CHECKPOINT-4417" in str(message.get("content")) for message in chat.messages)
    asyncio.run(scenario())


def test_a_conversation_whose_request_cannot_be_repeated_is_not_summarized_from_a_transcript(setup):
    """A transcript is read whole at the full price: what writing the summary early is meant to save."""
    async def scenario():
        await setup.notes(6)
        sent = len(setup.requests())
        setup.agent.context.max_prompt_tokens = 1_000  # the last request no longer fits
        setup.ahead = 55 * 60
        await idle(setup.compactor())
        assert len(setup.requests()) == sent
    asyncio.run(scenario())


def test_a_conversation_moved_to_another_model_since_its_last_request_is_not_summarized(setup):
    """The other model has nothing of it cached: the summary request would be read in full."""
    async def scenario():
        await setup.notes(6)
        sent = len(setup.requests())
        other = LLMClient(LLMConfig(model="sonnet", provider="claude-code", base_url=ccp.BASE_URL,
                                    capabilities=frozenset({"streaming", "tools", "reasoning"})))
        setup.agent.llm = other
        assert setup.agent.context.compressor is not None
        setup.agent.context.compressor.llm = other
        setup.ahead = 55 * 60
        await idle(setup.compactor())
        assert len(setup.requests()) == sent
    asyncio.run(scenario())


def test_the_backend_writes_its_idle_conversations_summary_and_goes_on_in_full_within_the_hour(tmp_path):
    """The real backend over stdio: its own check, between turns, with the quiet time shortened."""
    from test_backend_claude_code_compaction import MODELS
    from test_backend_served_model import send
    from test_backend_task_protocol import _start_question_protocol_backend, _stop_protocol_backend

    log = tmp_path / "requests.jsonl"
    cli = tmp_path / "claude"
    cli.write_text(STAND_IN % (str(log), SUMMARY), encoding="utf-8")
    cli.chmod(cli.stat().st_mode | stat.S_IXUSR)
    models = tmp_path / "claude-models.yaml"
    models.write_text(MODELS, encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()

    def requests() -> list[dict]:
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []

    proc, _, _, wait_for = _start_question_protocol_backend(
        tmp_path, 0, "idle_compaction", settings_overrides={"selected_model": "sonnet"}, workdir=work,
        env_overrides={"AGENT_MODELS_FILE": str(models), "ASTRA_CLAUDE_CODE_COMMAND": str(cli),
                       "ASTRA_IDLE_COMPACT_AFTER_SECONDS": "3", "ASTRA_IDLE_COMPACT_MIN_TOKENS": "1",
                       "ASTRA_CHANNEL_CONFIG": str(tmp_path / "no-channels.json"),
                       "ASTRA_CONTEXT_INDEX_EMBEDDING": "off"})
    try:
        wait_for(lambda event: event.get("type") == "model_info")
        # Longer than the part a compaction keeps word for word, so there is an older part.
        for n in range(4):
            send(proc, {"type": "message", "text": note(n) * 12})
            wait_for(lambda event: event.get("type") == "done", timeout=60)
        conversation = requests()[-1]
        assert CHECKPOINT not in text_of(conversation)
        deadline = time.monotonic() + 30
        while not any(CHECKPOINT in text_of(request) for request in requests()):
            assert time.monotonic() < deadline, "no summary was asked for"
            time.sleep(0.2)
        asked = requests()[-1]
        assert asked["turns"][:len(conversation["turns"])] == conversation["turns"]

        send(proc, {"type": "message", "text": "NOTE-LATE"})
        wait_for(lambda event: event.get("type") == "done", timeout=60)
        request = requests()[-1]
        assert request["turns"][:len(conversation["turns"])] == conversation["turns"]
        assert "CHECKPOINT-4417" not in text_of(request) and "NOTE-LATE" in text_of(request)
    finally:
        _stop_protocol_backend(proc)


def test_a_turn_that_starts_while_the_summary_is_written_does_not_wait_for_it(setup):
    """The model client has one request slot by default: a summary still being written would hold
    the person's request back until it came back. The backend checks four times a second."""
    async def scenario():
        await setup.notes(6)
        hold = setup.log.with_name(setup.log.name + ".hold")
        hold.touch()
        compactor = setup.compactor()
        setup.ahead = 55 * 60
        compactor.tick(busy=False)
        summary = compactor._task
        assert summary is not None
        try:
            async with asyncio.timeout(10):
                while CHECKPOINT not in setup.log.read_text(encoding="utf-8"):
                    await asyncio.sleep(0.02)
            before = len(setup.requests())
            foreground = asyncio.create_task(setup.turn("NOTE-LATE"))
            async with asyncio.timeout(10):
                while len(setup.requests()) == before:
                    compactor.tick(busy=True)
                    await asyncio.sleep(0.25)
                await foreground
        finally:
            hold.unlink(missing_ok=True)
        assert summary.done()
        assert setup.agent.context.messages[-1]["content"] == "Noted."
        assert "CHECKPOINT-4417" not in text_of(setup.requests()[-1])
    asyncio.run(scenario())


def test_two_returns_within_the_cache_kept_by_summary_requests_both_go_on_from_the_cache(setup, monkeypatch):
    """Summaries at 55 and 125 minutes, returns at 70 and 140: neither return starts cold, and the
    second request repeats the first one's earlier part. Old tool results that would be cleared at
    a cold start stay as they were, also as the stored conversation is read after a restart."""
    from agent.runtime import context as context_module
    from agent.runtime.context import AgentContext
    from agent.runtime.micro_compact import results_at_last_cold_start
    from test_micro_compact import _turn

    real_time = time.time

    class Shifted:
        """The clock messages and requests are stamped with, moved along with the rules' clock."""
        def __getattr__(self, name):
            return getattr(time, name)

        def time(self):
            return real_time() + setup.ahead

    monkeypatch.setattr(context_module, "time", Shifted())
    monkeypatch.setenv("TOOL_RESULT_PROMPT_BUDGET_CHARS", "6000")
    monkeypatch.setenv("TOOL_RESULT_KEEP_RECENT", "1")
    history = _turn(real_time() - 30, *[f"old-{i}" for i in range(14)])
    for message in history:
        for call in message.get("tool_calls", []):
            call["type"] = "function"
            call["function"] = {"name": call.pop("name"), "arguments": call.pop("arguments")}
    setup.agent.context.messages.extend(history)

    async def scenario():
        await setup.notes(6)
        compactor = setup.compactor()
        setup.ahead = 55 * 60
        await idle(compactor)
        setup.ahead = 70 * 60
        await setup.turn("FIRST-WARM-RETURN")
        first = setup.requests()[-1]
        assert "old-0" in text_of(first) and "Old tool result content cleared" not in text_of(first)
        setup.ahead = 125 * 60
        await idle(compactor)
        assert CHECKPOINT in text_of(setup.requests()[-1])
        setup.ahead = 140 * 60
        await setup.turn("SECOND-WARM-RETURN")
        second = setup.requests()[-1]
        assert "Old tool result content cleared" not in text_of(second)
        assert second["turns"][:len(first["turns"])] == first["turns"]
    asyncio.run(scenario())

    live = setup.agent.context
    stored = AgentContext(system_prompt="You are Astra.")
    stored.set_session(live.session_path)
    assert stored.load(readonly=True)
    assert results_at_last_cold_start(stored.messages, HOUR) == results_at_last_cold_start(live.messages, HOUR)
    assert results_at_last_cold_start(stored.messages, HOUR) == frozenset()


def test_a_provider_that_would_send_a_transcript_is_not_asked_for_the_summary(setup, monkeypatch):
    """The CLI's turn-by-turn replay, switched off for the process after a CLI rejected it: the
    request would go as one transcript, read in full."""
    async def scenario():
        await setup.notes(6)
        before = len(setup.requests())
        monkeypatch.setattr(ClaudeCodeProvider, "replay", False)
        setup.ahead = 55 * 60
        await idle(setup.compactor())
        assert setup.requests()[before:] == []
    asyncio.run(scenario())


def test_a_summary_interrupted_after_its_request_read_the_cache_still_counts_as_that_read(setup, monkeypatch):
    """The summary request goes out at 59 minutes and reads the cache; the person comes back at
    60:15, interrupting it. The cache lasts from 59 minutes, so that return is not a cold start
    and old tool results are not cleared there: the request goes on from the cached prefix."""
    from agent.runtime import context as context_module
    from test_micro_compact import _turn

    real_time = time.time

    class Shifted:
        def __getattr__(self, name):
            return getattr(time, name)

        def time(self):
            return real_time() + setup.ahead

    monkeypatch.setattr(context_module, "time", Shifted())
    monkeypatch.setenv("TOOL_RESULT_PROMPT_BUDGET_CHARS", "6000")
    monkeypatch.setenv("TOOL_RESULT_KEEP_RECENT", "1")
    history = _turn(real_time() - 30, *[f"old-{i}" for i in range(14)])
    for message in history:
        for call in message.get("tool_calls", []):
            call["type"] = "function"
            call["function"] = {"name": call.pop("name"), "arguments": call.pop("arguments")}
    setup.agent.context.messages.extend(history)

    async def scenario():
        await setup.notes(6)
        original = setup.requests()[-1]
        assert "old-0" in text_of(original) and "Old tool result content cleared" not in text_of(original)
        hold = setup.log.with_name(setup.log.name + ".hold")
        hold.touch()
        compactor = setup.compactor()
        setup.ahead = 59 * 60  # the last minute in which the summary is still asked for
        compactor.tick(busy=False)
        summary = compactor._task
        assert summary is not None
        foreground = None
        try:
            async with asyncio.timeout(10):
                while CHECKPOINT not in setup.log.read_text(encoding="utf-8"):
                    await asyncio.sleep(0.02)
            setup.ahead = 60 * 60 + 15
            foreground = asyncio.create_task(setup.turn("RETURN-DURING-SUMMARY"))
            async with asyncio.timeout(10):
                while not foreground.done():
                    compactor.tick(busy=True)
                    await asyncio.sleep(0.05)
        finally:
            hold.unlink(missing_ok=True)
            with contextlib.suppress(asyncio.CancelledError):
                await summary
            if foreground is not None:
                await foreground
        assert summary.cancelled()
        request = setup.requests()[-1]
        assert "Old tool result content cleared" not in text_of(request)
        assert request["turns"][:len(original["turns"])] == original["turns"]
    asyncio.run(scenario())
