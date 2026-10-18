"""A compaction summary is asked for after the conversation's own last request.

Repeating that request byte for byte lets the provider bill it at its cached-prefix
rate, and the model reads the original messages instead of a flattened transcript.
"""

import asyncio
import copy

from agent.core.msg import ContentBlock, Msg
from agent.runtime.react import ReActAgent
from agent.runtime.tools.registry import ToolDef, ToolRegistry

SUMMARY = "## Active Task\nFinish the storage migration.\n\n## Completed Actions\n- Surveyed the notes folder."

HISTORY = [
    {"role": "user", "content": "Survey the notes folder"},
    {"role": "assistant", "content": "The folder holds three notes about storage."},
    {"role": "user", "content": "Plan the storage migration"},
    {"role": "assistant", "content": "Plan: copy, verify, switch."},
    {"role": "user", "content": "Start with the copy"},
    {"role": "assistant", "content": "Copy finished; verification is next."},
]


class RecordingLLM:
    def __init__(self, first_summary_answer: dict | None = None):
        self.streamed: list[tuple[list[dict], list[dict]]] = []
        self.limited: list[tuple[list[dict], list[dict] | None]] = []
        self.limited_options: list[dict] = []
        self.first_summary_answer = first_summary_answer or {"content": SUMMARY}
        self.oversized = False

    async def chat_stream(self, messages, tools, **kwargs):
        self.streamed.append((copy.deepcopy(messages), copy.deepcopy(tools)))
        yield {"type": "done", "content": "Verification passed.", "usage": None}

    async def chat_limited(self, messages, tools=None, *, max_tokens, **kwargs):
        self.limited.append((copy.deepcopy(messages), copy.deepcopy(tools)))
        self.limited_options.append(kwargs)
        return self.first_summary_answer if len(self.limited) == 1 else {"content": SUMMARY}

    def estimate_tokens(self, messages):
        return 10_000_000 if self.oversized else 1 + sum(len(str(m.get("content", ""))) for m in messages) // 4


def run_conversation(llm: RecordingLLM) -> ReActAgent:
    async def lookup(q: str = ""):
        return "note"

    registry = ToolRegistry()
    registry.register(ToolDef(name="lookup", description="Look up a note",
                              parameters={"type": "object", "properties": {"q": {"type": "string"}}}, fn=lookup))
    agent = ReActAgent("agent", llm, registry, max_iterations=2)
    agent.context.messages.extend(dict(message) for message in HISTORY)

    async def scenario():
        async for _ in agent.reply_stream(Msg(content=[ContentBlock.text("Verify the copy")])):
            pass
    asyncio.run(scenario())
    return agent


def summarized(agent: ReActAgent) -> bool:
    return any("Finish the storage migration." in str(message.get("content")) for message in agent.context.messages)


def test_the_summary_request_repeats_the_last_request_then_asks_for_the_checkpoint():
    llm = RecordingLLM()
    agent = run_conversation(llm)
    sent, sent_tools = llm.streamed[-1]

    asyncio.run(agent.context.compress_if_needed(force=True))

    assert len(llm.limited) == 1
    request, tools = llm.limited[0]
    assert request[:len(sent)] == sent
    assert tools == sent_tools and [tool["function"]["name"] for tool in tools] == ["lookup"]
    # What the conversation gained after that request comes next, then the instruction.
    assert request[len(sent)]["role"] == "assistant" and request[len(sent)]["content"] == "Verification passed."
    assert request[-1]["role"] == "user" and "Context checkpoint request" in request[-1]["content"]
    assert len(request) == len(sent) + 2
    # Thinking is left as the conversation has it: switching it off renders the whole
    # prompt differently for DeepSeek, so nothing of the prefix would be cached.
    assert not llm.limited_options[0].get("disable_thinking")
    assert summarized(agent)


def test_a_tool_call_instead_of_a_summary_falls_back_to_a_transcript():
    llm = RecordingLLM({"content": "", "tool_calls": [
        {"id": "call-1", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}]})
    agent = run_conversation(llm)

    asyncio.run(agent.context.compress_if_needed(force=True))

    assert len(llm.limited) == 2
    transcript, tools = llm.limited[1]
    assert len(transcript) == 1 and transcript[0]["role"] == "user" and not tools
    assert summarized(agent)


def test_a_replay_that_would_not_fit_is_not_sent():
    llm = RecordingLLM()
    agent = run_conversation(llm)
    llm.oversized = True

    asyncio.run(agent.context.compress_if_needed(force=True))

    assert len(llm.limited) == 1
    transcript, _ = llm.limited[0]
    assert len(transcript) == 1 and "Context checkpoint request" not in str(transcript[0]["content"])
    assert summarized(agent)


def test_a_conversation_changed_since_its_last_request_is_not_replayed():
    llm = RecordingLLM()
    agent = run_conversation(llm)
    # Rewound past the last request (for example a retry): that request no longer describes it.
    del agent.context.messages[-3:]

    asyncio.run(agent.context.compress_if_needed(force=True))

    assert len(llm.limited) == 1
    assert len(llm.limited[0][0]) == 1
