"""An image turn that compacts mid-turn keeps every other message as it was.

The canonical message holds the image placeholder from the start; the full
image reaches the provider only as a request projection. Compaction inside the
turn moves messages, so nothing may later be rewritten by a remembered index.
"""

import asyncio
import base64
import json

import pytest

from agent.core.msg import ContentBlock, Msg
from agent.runtime.react import ReActAgent
from agent.runtime.session_store import SessionStore
from agent.runtime.tools.registry import ToolDef, ToolRegistry

PNG_BASE64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGD4DwABBAEAwS2OUAAAAABJRU5ErkJggg=="
PNG_URL = f"data:image/png;base64,{PNG_BASE64}"
FINAL = "FINAL-ANSWER: the chart shows a single red pixel."
SUMMARY = "## Active Task\nInspect the image.\n## Completed\nEarlier turns summarized."


class ScriptedVisionLLM:
    class config:
        model = "fake"
        capabilities = frozenset({"vision"})

    def __init__(self, calls: list[list[dict]]):
        self.script = list(calls)
        self.requests: list[list[dict]] = []
        self.answers = 0

    @staticmethod
    def estimate_tokens(value):
        return len(json.dumps(value, ensure_ascii=False, default=str)) // 4

    async def chat_stream(self, messages, tools):
        del tools
        self.requests.append(messages)
        if self.script:
            calls = self.script.pop(0)
            yield {
                "type": "tool_calls", "calls": calls, "content": "", "reasoning_content": "",
                "finish_reason": "tool_calls", "usage": None,
            }
            return
        self.answers += 1
        text = FINAL if self.answers == 1 else "SECOND-TURN-OK"
        yield {"type": "chunk", "content": text}
        yield {"type": "done", "content": text, "usage": None}

    async def chat_limited(self, messages, *args, **kwargs):
        del messages, args, kwargs
        return {"content": SUMMARY}


def _call(call_id: str, name: str) -> dict:
    return {"id": call_id, "name": name, "arguments": "{}"}


def _registry(image_path) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(ToolDef(
        name="probe", description="read something", risk="read",
        fn=lambda: "probe-output " + "x" * 24_000,
        parameters={"type": "object", "properties": {}},
    ))
    registry.register(ToolDef(
        name="read_image", description="attach an image", risk="read",
        fn=lambda: json.dumps({"type": "image_attachment", "image_paths": [str(image_path)]}),
        parameters={"type": "object", "properties": {}},
    ))
    return registry


def _agent(tmp_path, llm, prior_turns: int):
    image_path = tmp_path / "chart.png"
    image_path.write_bytes(base64.b64decode(PNG_BASE64))
    agent = ReActAgent("agent", llm, _registry(image_path), system_prompt="base")
    session = tmp_path / "sessions" / "image.json"
    agent.context.set_session(str(session))
    agent.context.max_prompt_tokens = 6000
    for index in range(prior_turns):
        agent.context.add_user(f"prior question {index} " + "q" * 1500)
        agent.context.add_assistant(f"prior answer {index} " + "a" * 1500)
    agent.context.save()
    compactions: list[dict] = []
    agent.context.compaction_observer = lambda event: compactions.append(event)
    return agent, session, compactions


def _turn(agent, content):
    async def run():
        return [event async for event in agent.reply_stream(Msg(content=content))]
    return asyncio.run(run())


def _text(message) -> str:
    content = message.get("content")
    return content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)


def _holds_placeholder(message, placeholder: str) -> bool:
    content = message.get("content")
    return isinstance(content, list) and any(
        isinstance(part, dict) and part.get("text") == placeholder for part in content
    )


def _assert_history_intact(agent, session, placeholder: str, owners: int) -> None:
    messages = agent.context.messages
    holders = [message for message in messages if _holds_placeholder(message, placeholder)]
    # Only the message that carried the image holds its placeholder; no answer,
    # tool result or compaction recap was overwritten with it.
    assert len(holders) == owners
    assert all(message["role"] == "user" for message in holders)
    assert all(
        message["content"] in ("", FINAL) for message in messages
        if message["role"] == "assistant" and not _text(message).startswith("[CONTEXT COMPACTION")
    )
    assert sum(FINAL == message.get("content") for message in messages) == 1
    # What is saved is what is held: the next process replays the same history.
    assert SessionStore(session).load(readonly=True)["messages"] == json.loads(json.dumps(messages))


@pytest.mark.parametrize("prior_turns", [1, 2])
def test_user_image_turn_compacted_mid_turn_rewrites_no_other_message(tmp_path, prior_turns):
    llm = ScriptedVisionLLM([[_call("call-probe", "probe")]])
    agent, session, compactions = _agent(tmp_path, llm, prior_turns=prior_turns)

    events = _turn(agent, [ContentBlock.text("What is in this chart?"), ContentBlock.image_url(PNG_URL)])

    assert not [event for event in events if event.get("type") == "error"]
    assert any(event.get("status") == "completed" for event in compactions)
    # The provider saw the image itself; history keeps only its placeholder.
    assert any(PNG_URL in _text(message) for message in llm.requests[0])
    assert PNG_URL not in json.dumps(agent.context.messages)
    _assert_history_intact(agent, session, "[Image: attached image]", owners=1)

    _turn(agent, [ContentBlock.text("thanks, and the colour?")])
    follow_up = llm.requests[-1]
    assert not any(
        message["role"] != "user" and "[Image: attached image]" in _text(message) for message in follow_up
    )
    assert any(message["role"] == "assistant" and message.get("content") == FINAL for message in follow_up)


@pytest.mark.parametrize("prior_turns", [0, 1])
def test_tool_image_turn_compacted_after_the_image_rewrites_no_other_message(tmp_path, prior_turns):
    # The image message is attached after the first tool round; the second
    # round's compaction then moves (or summarizes) it and its neighbours.
    llm = ScriptedVisionLLM([[_call("call-image", "read_image")], [_call("call-probe", "probe")]])
    agent, session, compactions = _agent(tmp_path, llm, prior_turns=prior_turns)

    events = _turn(agent, [ContentBlock.text("Look at chart.png for me.")])

    assert not [event for event in events if event.get("type") == "error"]
    assert any(event.get("status") == "completed" for event in compactions)
    assert any(PNG_URL in _text(message) for message in llm.requests[1])
    assert PNG_URL not in json.dumps(agent.context.messages)
    tool_images = [m for m in agent.context.messages if m.get("provenance") == "tool_image"]
    assert all(_holds_placeholder(message, "[Image: chart.png]") for message in tool_images)
    _assert_history_intact(agent, session, "[Image: chart.png]", owners=len(tool_images))
