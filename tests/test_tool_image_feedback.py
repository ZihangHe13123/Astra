import asyncio
import base64
import json

import pytest

from agent.core.msg import ContentBlock, Msg
from agent.runtime.react import ReActAgent
from agent.runtime.tools.registry import ToolDef, ToolRegistry


def run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize("tool_name", ["render_diagram", "read_image"])
def test_tool_image_result_is_sent_back_to_llm_as_image(tmp_path, tool_name):
    image = tmp_path / "generated.png"
    image.write_bytes(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
    ))

    class FakeLLM:
        def __init__(self):
            self.calls = 0
            self.second_messages = None

        async def chat_stream(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                yield {
                    "type": "tool_calls",
                    "calls": [{
                        "id": "call-1",
                        "name": tool_name,
                        "arguments": json.dumps({"prompt": "test"}),
                    }],
                    "content": "",
                    "usage": None,
                }
                return

            self.second_messages = messages
            yield {"type": "done", "content": "I inspected the generated image.", "usage": None}

    async def scenario():
        registry = ToolRegistry()

        def fake_draw(prompt: str):
            payload = {"success": True, "paths": [str(image)]}
            if tool_name != "read_image":
                payload["type"] = "image_attachment"
            return json.dumps(payload)

        registry.register(ToolDef(
            name=tool_name,
            description="fake draw",
            parameters={"type": "object", "properties": {"prompt": {"type": "string"}}, "required": ["prompt"]},
            fn=fake_draw,
        ))
        llm = FakeLLM()
        agent = ReActAgent("agent", llm, registry, max_iterations=3)

        events = [event async for event in agent.reply_stream(Msg(content=[ContentBlock.text("draw and inspect")]))]

        assert events[-1]["type"] == "done"
        assert llm.calls == 2
        image_messages = [
            msg for msg in llm.second_messages
            if isinstance(msg.get("content"), list)
            and any(part.get("type") == "image_url" for part in msg["content"])
        ]
        assert image_messages
        image_part = next(part for part in image_messages[0]["content"] if part["type"] == "image_url")
        assert image_part["image_url"]["url"].startswith("data:image/png;base64,")

        stored_image_messages = [
            msg for msg in agent.context.messages
            if isinstance(msg.get("content"), list)
            and any(part.get("text") == "[Image: generated.png]" for part in msg["content"])
        ]
        assert stored_image_messages

    run(scenario())


def test_image_is_still_attached_when_its_result_carries_long_text(tmp_path):
    image = tmp_path / "page.png"
    image.write_bytes(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
    ))

    class FakeLLM:
        def __init__(self):
            self.calls = 0
            self.second_messages = None

        async def chat_stream(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                yield {
                    "type": "tool_calls",
                    "calls": [{"id": "call-1", "name": "capture", "arguments": "{}"}],
                    "content": "",
                    "usage": None,
                }
                return
            self.second_messages = messages
            yield {"type": "done", "content": "I looked at the capture.", "usage": None}

    async def scenario():
        registry = ToolRegistry(artifact_dir=str(tmp_path / "tool-results"))

        def capture():
            # Longer than the inline limit, so the model is given a preview of this text.
            return json.dumps({
                "type": "image_attachment", "image_paths": [str(image)], "text": "word " * 6000,
            })

        registry.register(ToolDef(
            name="capture", description="capture a page with its text",
            parameters={"type": "object", "properties": {}}, fn=capture,
        ))
        llm = FakeLLM()
        agent = ReActAgent("agent", llm, registry, max_iterations=3)

        events = [event async for event in agent.reply_stream(Msg(content=[ContentBlock.text("capture it")]))]

        assert events[-1]["type"] == "done"
        tool_result = next(event for event in events if event["type"] == "tool_result")
        assert tool_result["output_truncated"] is True
        assert any(
            isinstance(message.get("content"), list)
            and any(part.get("type") == "image_url" for part in message["content"])
            for message in llm.second_messages
        )

    run(scenario())
