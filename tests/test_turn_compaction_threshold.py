"""When a turn compacts its history: past the line by a third inside the turn, at the line otherwise.

A compaction rewrites the history, and a provider with a prompt cache reads the request again at the
uncached price from the first message it changed. A long turn of tool calls used to reach the line
again and again and rewrite its history each time (live 2026-11-13: three times in five minutes on
Claude Code, 249k tokens read uncached). The real agent loop, a model that reads pages one at a time,
and a summary that stands in for everything before the current request; the test records at which
request each compaction came.
"""

import asyncio
import time

import pytest

from agent.core.msg import ContentBlock, Msg
from agent.runtime import context as context_module
from agent.runtime.llm import LLMConfig
from agent.runtime.react import ReActAgent
from agent.runtime.tools.registry import ToolDef, ToolRegistry

LINE = 3_000
CEILING = LINE + LINE // 3
PAGE_CHARS = 1_000
EARLIER = "an earlier note for the record. " * 300  # about 2,400 tokens before the turn begins


def size(messages: list[dict]) -> int:
    return sum(len(str(message.get("content") or "")) for message in messages) // 4


class PageReader:
    """Reads the pages the latest request names, one in each step, then answers. Which page is next
    is kept outside the conversation: a compaction takes the earlier calls out of it."""

    def __init__(self, provider: str, window: int | None):
        self.config = LLMConfig(model="pages", provider=provider, base_url="http://127.0.0.1:9",
                                context_limit=window, max_tokens=4_096)
        self.requests: list[list[dict]] = []
        self.request_text = ""
        self.done = 0

    def estimate_tokens(self, messages: list[dict]) -> int:
        return size(messages)

    async def chat_stream(self, messages, tools):
        self.requests.append([dict(message) for message in messages])
        asked = max(index for index, message in enumerate(messages)
                    if message["role"] == "user" and "Look at" in str(message.get("content")))
        text = str(messages[asked]["content"])
        if text != self.request_text:
            self.request_text, self.done = text, 0
        wanted = int(text.rsplit("Look at ", 1)[1].split()[0])
        if self.done < wanted:
            self.done += 1
            yield {"type": "tool_calls", "content": "", "reasoning_content": "", "usage": None,
                   "calls": [{"id": f"page-{len(self.requests)}", "name": "page",
                              "arguments": f'{{"number": {self.done}}}'}]}
        else:
            yield {"type": "done", "content": "looked", "usage": None}


class Summary:
    """Stands in for what came before the request being answered and for all but the turn's last two
    calls; records when it was asked. ``gain`` is what its plan says a compaction would take off."""

    def __init__(self, model: PageReader, gain: int = 0):
        self.model = model
        self.gain = gain
        self.at: list[int] = []

    def planned_gain(self, messages, max_prompt_tokens):
        return self.gain

    async def compress(self, messages, max_prompt_tokens, force=False):
        self.at.append(len(self.model.requests))
        asked = max(index for index, message in enumerate(messages)
                    if message["role"] == "user" and "Look at" in str(message.get("content")))
        return [messages[0], {"role": "user", "content": "SUMMARY of what came before"}, messages[asked],
                *messages[asked + 1:][-4:]]


class Setup:
    def __init__(self, monkeypatch, provider: str, window: int | None, gain: int = 0):
        self.ahead = 0.0
        real_time = time.time
        setup = self

        class Shifted:
            """The clock requests are stamped with, which a slow page moves on."""
            def __getattr__(self, name):
                return getattr(time, name)

            def time(self):
                return real_time() + setup.ahead

        monkeypatch.setattr(context_module, "time", Shifted())
        self.slow_page: int | None = None
        self.model = PageReader(provider, window)

        def page(number: int) -> str:
            if number == self.slow_page:
                self.ahead += 2 * 3600  # this page took two hours: the provider's cache has gone
            return f"page {number} " + "x" * PAGE_CHARS

        registry = ToolRegistry()
        registry.register(ToolDef("page", "Read a page",
                                  {"type": "object", "properties": {"number": {"type": "integer"}}}, page))
        self.agent = ReActAgent("agent", self.model, registry, system_prompt="You are Astra.",  # type: ignore[arg-type]
                                max_iterations=40, progressive_tools=False, query_profile_enabled=False)
        self.agent.context.max_prompt_tokens = LINE
        self.summary = Summary(self.model, gain)
        self.agent.context.compressor = self.summary  # type: ignore[assignment]
        self.agent.context.add_user(EARLIER)
        self.agent.context.add_assistant("Noted.")

    def turn(self, text: str) -> None:
        async def run():
            async for _ in self.agent.reply_stream(Msg(content=[ContentBlock.text(text)])):
                pass
        asyncio.run(run())

    def before_compaction(self) -> list[int]:
        """The size of each request up to the first compaction."""
        first = self.summary.at[0] if self.summary.at else len(self.model.requests)
        return [size(request) for request in self.model.requests[:first]]


def repeats(earlier: list[dict], later: list[dict]) -> bool:
    return later[:len(earlier)] == earlier


@pytest.mark.parametrize("provider", ["claude-code", "deepseek"])
def test_a_long_turn_passes_the_line_and_compacts_once_a_third_further(monkeypatch, provider):
    setup = Setup(monkeypatch, provider, window=1_000_000)
    setup.turn("Look at 12 pages")
    sizes = setup.before_compaction()
    assert len(setup.summary.at) == 1
    # Past the line and up to a third further, nothing was rewritten: each request repeated the last.
    assert max(sizes) >= LINE and all(value < CEILING for value in sizes)
    requests = setup.model.requests[:setup.summary.at[0]]
    assert all(repeats(earlier, later) for earlier, later in zip(requests, requests[1:]))


def test_a_turn_whose_cache_has_gone_compacts_at_the_line(monkeypatch):
    """A tool that ran two hours in the middle of a turn: Claude Code holds a request for an hour, so
    the next request is read whole anyway and compacting it costs nothing more."""
    setup = Setup(monkeypatch, "claude-code", window=1_000_000)
    setup.slow_page = 4
    setup.turn("Look at 12 pages")
    sizes = setup.before_compaction()
    assert setup.summary.at and setup.summary.at[0] == 4, "right after the slow page"
    assert sizes[-1] >= LINE - PAGE_CHARS // 4 and all(value < CEILING for value in sizes)


def test_a_provider_without_a_known_cache_lifetime_waits_for_the_third_also_after_a_pause(monkeypatch):
    setup = Setup(monkeypatch, "deepseek", window=1_000_000)
    setup.slow_page = 4
    setup.turn("Look at 12 pages")
    sizes = setup.before_compaction()
    assert len(setup.summary.at) == 1 and max(sizes) >= LINE and all(value < CEILING for value in sizes)
    assert setup.summary.at[0] > 4


def test_without_a_known_window_a_turn_compacts_at_the_line(monkeypatch):
    """A third past the line may be more than an unknown window takes."""
    setup = Setup(monkeypatch, "claude-code", window=None)
    setup.turn("Look at 12 pages")
    sizes = setup.before_compaction()
    assert setup.summary.at and all(value < LINE for value in sizes)


def test_a_turn_that_ends_past_the_line_compacts_when_the_next_one_begins(monkeypatch):
    setup = Setup(monkeypatch, "claude-code", window=1_000_000)
    setup.turn("Look at 3 pages")
    assert not setup.summary.at
    assert LINE <= size(setup.model.requests[-1]) < CEILING
    turns_so_far = len(setup.model.requests)
    setup.turn("Look at 1 pages")
    # Compacted before the next turn's first request was sent, once.
    assert setup.summary.at == [turns_so_far]
    assert "SUMMARY" in str(setup.model.requests[turns_so_far][1].get("content"))


def test_a_window_that_holds_less_than_a_third_more_is_the_limit(monkeypatch):
    """With an 8K allowance for the answer, this window takes 300 tokens past the line."""
    setup = Setup(monkeypatch, "claude-code", window=LINE + 300 + 8_192)
    setup.turn("Look at 12 pages")
    sizes = setup.before_compaction()
    assert setup.summary.at and max(sizes) >= LINE and all(value < LINE + 300 for value in sizes)


@pytest.mark.parametrize("gain, at_the_line", [(LINE // 4, True), (LINE // 4 - 1, False)])
def test_a_compaction_that_would_take_off_a_quarter_of_the_line_is_done_at_it(monkeypatch, gain, at_the_line):
    """What a compaction takes off is not read again in each later request; carried a third further,
    a large part of it would be (a scripted Claude Code turn over 125k of old notes read 69% more)."""
    setup = Setup(monkeypatch, "claude-code", window=1_000_000, gain=gain)
    setup.turn("Look at 12 pages")
    sizes = setup.before_compaction()
    assert setup.summary.at
    if at_the_line:
        assert max(sizes) < LINE, "compacted as soon as a request would reach the line"
    else:
        assert LINE <= max(sizes) < CEILING and len(setup.summary.at) == 1
