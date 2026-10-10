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


def test_model_is_told_which_images_were_not_attached(tmp_path, monkeypatch):
    good = tmp_path / "small.png"
    good.write_bytes(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
    ))
    big = tmp_path / "huge.png"
    big.write_bytes(good.read_bytes() + b"\0" * 4096)
    monkeypatch.setenv("MAX_AUTO_TOOL_IMAGE_BYTES", "1024")

    class FakeLLM:
        def __init__(self, paths):
            self.paths = paths
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
            yield {"type": "done", "content": "Described what I could see.", "usage": None}

    async def turn(paths):
        registry = ToolRegistry(artifact_dir=str(tmp_path / "tool-results"))
        registry.register(ToolDef(
            name="capture", description="capture pages",
            parameters={"type": "object", "properties": {}},
            fn=lambda: json.dumps({"type": "image_attachment", "image_paths": [str(path) for path in paths]}),
        ))
        llm = FakeLLM(paths)
        agent = ReActAgent("agent", llm, registry, max_iterations=3)
        [event async for event in agent.reply_stream(Msg(content=[ContentBlock.text("capture them")]))]
        # What followed the tool result: a text-only notice is a plain string, a
        # message with images is a list of parts.
        followed = [message for message in llm.second_messages if message.get("role") == "user"][1:]
        parts = [
            part if isinstance(part, dict) else {"type": "text", "text": str(part)}
            for message in followed
            for part in (message["content"] if isinstance(message.get("content"), list) else [message.get("content")])
        ]
        text = "\n".join(str(part.get("text") or "") for part in parts if part.get("type") == "text")
        images = [part for part in parts if part.get("type") == "image_url"]
        return text, images

    # One of two is too large: the small one is attached and the large one is named.
    text, images = run(turn([good, big]))
    assert len(images) == 1
    assert "huge.png" in text and "没有附加" in text
    assert "small.png" not in text.split("没有附加", 1)[1]

    # None can be attached: the model is still told, instead of hearing nothing.
    text, images = run(turn([big]))
    assert images == []
    assert "huge.png" in text and "看不到" in text
