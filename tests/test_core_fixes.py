import asyncio
import base64
import hashlib
import json
import os
import re
import socket
import tempfile
import time
import urllib.error
import urllib.parse
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PIL import Image

import agent.runtime.context as context_module
import agent.runtime.vision_preprocessor as vision_module
import agent.sandbox.docker as docker_module
from agent.cli import backend as backend_module
from agent.cli import sessions
from agent.cli.images import build_image_message_content, build_user_message_content, message_display_text
from agent.cli.model_catalog import CatalogEntry
from agent.cli.models import ModelProfile, prompt_token_budget
from agent.cli.render import StreamRenderer
from agent.cli.sessions import create_session_name
from agent.core.agent import AgentBase
from agent.core.bus import MessageBus
from agent.core.msg import ContentBlock, Msg
from agent.runtime.context import AgentContext
from agent.runtime.context_compressor import ContextCompressor
from agent.runtime.harness import (
    RequestLifecycle,
    RequestStatus,
    build_health_report,
    default_llm_timeout,
)
from agent.runtime.llm import (
    LLMClient,
    LLMConfig,
    LLMIdleTimeout,
    LLMOverallTimeout,
    OpenAICompatibleProvider,
    _messages_for_capabilities,
)
from agent.runtime.message_source import message_source_ref
from agent.runtime.react import ReActAgent
from agent.runtime.session_store import SessionStore
from agent.runtime.task_store import TaskStore
from agent.runtime.tools.files import FilesystemPolicy, register_file_tools
from agent.runtime.tools.git import register_git_tools
from agent.runtime.tools.image import register_image_tools
from agent.runtime.tools.registry import ToolDef, ToolRegistry
from agent.runtime.tools.time import register_time_tools
from agent.runtime.tools.web import register_web_tools
from agent.runtime.vision_policy import VisionPreprocessPolicy
from agent.runtime.vision_preprocessor import VisionPreprocessError, VisionTileSelectionError
from agent.sandbox.docker import DockerSandbox
from agent.sandbox.local import LocalSandbox, SandboxError


class EchoAgent(AgentBase):
    async def reply(self, msg: Msg):
        return Msg(sender=self.name, role="assistant", content=[ContentBlock.text(msg.get_text())])


def run(coro):
    return asyncio.run(coro)


def _write_react_pattern_png(path: Path, size: tuple[int, int]) -> Path:
    horizontal = Image.linear_gradient("L").rotate(90, expand=True).resize(size)
    vertical = Image.linear_gradient("L").resize(size)
    image = Image.merge("RGB", (horizontal, vertical, Image.new("L", size, 127)))
    image.save(path)
    return path


async def _collect_stream(stream):
    return [event async for event in stream]


class CapturingVisionLLM:
    def __init__(self, policy):
        self.config = LLMConfig(
            model="deepseek-v4-flash-vision-exp",
            capabilities=frozenset({"vision", "tools"}),
            vision_detail="original",
            vision_preprocess=policy,
        )
        self.calls = []
        self.tool_calls = []

    def estimate_tokens(self, messages):
        return 1

    async def chat_stream(self, messages, tools):
        self.calls.append(messages)
        self.tool_calls.append(tools)
        yield {"type": "done", "content": "inspected", "usage": None}


def test_message_bus_request_filters_msg_results():
    async def scenario():
        bus = MessageBus()
        bus.register(EchoAgent("echo"))
        out = await bus.request(["echo", "missing"], Msg(content=[ContentBlock.text("ok")]))
        assert len(out) == 1
        assert out[0].get_text() == "ok"

    run(scenario())


def test_startup_session_list_defers_expensive_message_counts(tmp_path, monkeypatch):
    counted = []
    monkeypatch.setattr(backend_module, "list_sessions", lambda: ["default", "archive"])
    monkeypatch.setattr(
        backend_module,
        "session_msg_count",
        lambda name: counted.append(name) or {"default": 12, "archive": 34}[name],
    )

    startup = backend_module._build_session_list_event(
        str(tmp_path / "default.json"),
        12,
        include_counts=False,
    )
    assert counted == []
    assert [item["messages"] for item in startup["sessions"]] == [0, 0]
    assert startup["sessions"][0]["current"] is True

    hydrated = backend_module._build_session_list_event(
        str(tmp_path / "default.json"),
        12,
        include_counts=True,
    )
    assert counted == ["default", "archive"]
    assert [item["messages"] for item in hydrated["sessions"]] == [12, 34]


def test_agent_base_default_reply_stream():
    async def scenario():
        agent = EchoAgent("echo")
        events = []
        async for event in agent.reply_stream(Msg(content=[ContentBlock.text("hello")])):
            events.append(event)
        assert events == [
            {"type": "chunk", "content": "hello"},
            {"type": "done"},
        ]

    run(scenario())


def test_file_tools_reject_path_escape():
    async def scenario():
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as root:
            registry = ToolRegistry()
            register_file_tools(registry, root)
            # Unmounted paths outside the workspace are rejected.
            result = await registry.execute("read_file", {"path": "../../outside.txt"})
            assert "outside allowed filesystem roots" in result["error"]

    run(scenario())


def test_tool_registry_validates_arguments():
    async def scenario():
        registry = ToolRegistry()

        async def tool(path: str):
            return Path(path).name

        registry.register(ToolDef(
            name="demo",
            description="demo",
            parameters={"type": "object"},
            fn=tool,
        ))
        missing = await registry.execute("demo", {})
        extra = await registry.execute("demo", {"path": "x", "unexpected": True})
        assert "missing required: path" in missing["error"]
        assert "unexpected: unexpected" in extra["error"]

    run(scenario())


def test_tool_registry_enforces_tool_timeout():
    async def scenario():
        registry = ToolRegistry()

        async def slow_tool():
            await asyncio.sleep(1)
            return "too late"

        registry.register(ToolDef(
            name="slow",
            description="slow",
            parameters={"type": "object"},
            fn=slow_tool,
            timeout=0.01,
        ))
        result = await registry.execute("slow", {})
        assert "[ToolTimeout]" in result["error"]
        assert result["error_type"] == "overall_timeout"
        assert result["retryable"] is False
        assert "Do not repeat it unchanged" in result["recovery_hint"]

    run(scenario())


def test_tool_registry_retries_transient_tool_errors():
    async def scenario():
        registry = ToolRegistry()
        attempts = 0

        async def flaky():
            nonlocal attempts
            attempts += 1
            if attempts < 2:
                raise TimeoutError("temporary lock")
            return "ok"

        registry.register(ToolDef(
            name="flaky",
            description="flaky",
            parameters={"type": "object"},
            fn=flaky,
            max_retries=1,
            retry_delay=0,
        ))

        result = await registry.execute("flaky", {})

        assert result["output"] == "ok"
        assert result["error"] == ""
        assert attempts == 2

    run(scenario())


def test_local_sandbox_executes_python_via_stdin():
    async def scenario():
        sandbox = LocalSandbox(timeout=5)
        result = await sandbox.execute_python("print('quote ok')")
        assert result["exit_code"] == 0
        assert result["output"].strip() == "quote ok"

    run(scenario())


def test_local_sandbox_truncates_large_output_and_preserves_artifact(tmp_path):
    async def scenario():
        sandbox = LocalSandbox(timeout=5, max_output_bytes=20, workdir=str(tmp_path))
        result = await sandbox.execute_python("print('x' * 100)")

        assert len(result["output"].encode("utf-8")) <= 80
        assert "[Output truncated" in result["error"]
        artifact = Path(result["artifact_path"])
        assert artifact.is_file()
        assert "x" * 100 in artifact.read_text(encoding="utf-8")

    run(scenario())


def test_local_sandbox_streams_output_before_process_completion(tmp_path):
    async def scenario():
        sandbox = LocalSandbox(timeout=5, workdir=str(tmp_path))
        first_output = asyncio.Event()
        chunks = []

        def on_output(stream, text):
            chunks.append((stream, text))
            if "first" in text:
                first_output.set()

        task = asyncio.create_task(sandbox.execute_python_stream(
            "import time\nprint('first', flush=True)\ntime.sleep(0.15)\nprint('second', flush=True)",
            on_output=on_output,
        ))
        await asyncio.wait_for(first_output.wait(), timeout=2)
        assert task.done() is False
        result = await task

        assert result["exit_code"] == 0
        assert result["output"].replace("\r\n", "\n") == "first\nsecond\n"
        streamed = "".join(text for stream, text in chunks if stream == "stdout")
        assert streamed.replace("\r\n", "\n") == "first\nsecond\n"

    run(scenario())


def test_local_sandbox_decodes_utf8_output_without_regression():
    sandbox = LocalSandbox(timeout=5)
    output, error = sandbox._decode_limited("中文输出 ✓".encode("utf-8"), b"")

    assert output == "中文输出 ✓"
    assert error == ""


def test_local_sandbox_decodes_utf16le_wsl_diagnostics():
    sandbox = LocalSandbox(timeout=5)
    diagnostic = "不存在具有所提供名称的分发。\r\n错误代码: Wsl/Service/WSL_E_DISTRO_NOT_FOUND\r\n"
    output, error = sandbox._decode_limited(b"", diagnostic.encode("utf-16-le"))

    assert output == ""
    assert error == diagnostic
    assert "�" not in error


def test_local_sandbox_blocks_spaced_rm_rf_root_variant():
    sandbox = LocalSandbox(timeout=5)
    try:
        sandbox._check_dangerous("rm   -rf   /")
    except SandboxError:
        return
    raise AssertionError("expected spaced rm -rf / variant to be blocked")


def test_msg_copy_deep_copies_nested_content_and_metadata():
    msg = Msg(
        sender="user",
        role="user",
        content=[ContentBlock.tool_use("demo", {"nested": {"value": 1}}, "call-1")],
        metadata={"nested": {"flag": True}},
    )
    copied = msg.copy()
    copied.content[0].data["input"]["nested"]["value"] = 2
    copied.metadata["nested"]["flag"] = False

    assert msg.content[0].data["input"]["nested"]["value"] == 1
    assert msg.metadata["nested"]["flag"] is True


def test_msg_converts_images_to_openai_multimodal_content():
    msg = Msg(content=[
        ContentBlock.text("what is this?"),
        ContentBlock.image_url("data:image/png;base64,AAAA"),
    ])

    assert msg.to_chat_content() == [
        {"type": "text", "text": "what is this?"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]


def test_msg_converts_images_to_lightweight_storage_content():
    source_path = str(Path("images") / "sample.png")
    msg = Msg(content=[
        ContentBlock.text("what is this?"),
        ContentBlock.image_url(
            "data:image/png;base64,AAAA",
            source_path=source_path,
        ),
    ])

    assert msg.to_storage_content() == [
        {"type": "text", "text": "what is this?"},
        {
            "type": "text",
            "text": "[Image: sample.png]",
            "metadata": {"source_path": source_path},
        },
    ]


def test_react_agent_sends_image_content_to_llm():
    class FakeLLM:
        def __init__(self):
            self.messages = None

        async def chat_stream(self, messages, tools):
            self.messages = messages
            yield {"type": "done", "content": "seen", "usage": None}

    async def scenario():
        llm = FakeLLM()
        agent = ReActAgent("agent", llm, ToolRegistry(), max_iterations=1)
        msg = Msg(content=[ContentBlock.image_url("data:image/png;base64,AAAA")])

        events = [event async for event in agent.reply_stream(msg)]

        assert events[-1]["type"] == "done"
        assert llm.messages[-1]["role"] == "user"
        content = llm.messages[-1]["content"]
        assert content[0]["type"] == "text"
        assert content[0]["text"].startswith("<message_time>")
        assert content[1:] == [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ]

    run(scenario())


def test_react_agent_does_not_keep_image_base64_in_context_after_turn():
    source_path = str(Path("images") / "sample.png")

    class FakeLLM:
        async def chat_stream(self, messages, tools):
            content = messages[-1]["content"]
            assert content[0]["type"] == "text"
            assert content[0]["text"].startswith("<message_time>")
            assert content[1:] == [
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,AAAA"},
                    "metadata": {"source_path": source_path},
                },
            ]
            yield {"type": "done", "content": "seen", "usage": None}

    async def scenario():
        agent = ReActAgent("agent", FakeLLM(), ToolRegistry(), max_iterations=1)
        msg = Msg(content=[
            ContentBlock.image_url(
                "data:image/png;base64,AAAA",
                source_path=source_path,
            )
        ])

        events = [event async for event in agent.reply_stream(msg)]

        assert events[-1]["type"] == "done"
        assert agent.context.messages[0]["content"] == [
            {
                "type": "text",
                "text": "[Image: sample.png]",
                "metadata": {"source_path": source_path},
            }
        ]

    run(scenario())


def test_backend_builds_image_message_content(tmp_path):
    image = tmp_path / "sample.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")

    blocks = build_image_message_content(str(image), "describe it", max_bytes=100)

    assert blocks[0].type == "text"
    assert blocks[0].data["text"] == "describe it"
    assert blocks[1].type == "image_url"
    assert blocks[1].data["url"].startswith("data:image/png;base64,")


def test_backend_extracts_quoted_image_path_from_text(tmp_path):
    image = tmp_path / "sample image.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")

    blocks = build_user_message_content(f'"{image}" 分析这张图', max_bytes=100)

    assert blocks[0].type == "text"
    assert blocks[0].data["text"] == "分析这张图"
    assert blocks[1].type == "image_url"
    assert blocks[1].data["url"].startswith("data:image/png;base64,")


def test_backend_extracts_unquoted_image_path_from_text(tmp_path):
    image = tmp_path / "sample.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")

    blocks = build_user_message_content(f"Analyze this image: {image}", max_bytes=100)

    assert blocks[0].type == "text"
    assert blocks[0].data["text"] == "Analyze this image:"
    assert blocks[1].type == "image_url"


def test_backend_extracts_multiple_pasted_image_paths_as_attachments(tmp_path):
    first = tmp_path / "first.png"
    second = tmp_path / "second.jpg"
    first.write_bytes(b"\x89PNG\r\n\x1a\n")
    second.write_bytes(b"\xff\xd8\xff")

    blocks = build_user_message_content(f'"{first}"\n"{second}"\n分析这两张图', max_bytes=100)

    assert blocks[0].type == "text"
    assert blocks[0].data["text"] == "分析这两张图"
    assert [block.type for block in blocks[1:]] == ["image_url", "image_url"]
    assert blocks[1].data["source_path"].endswith("first.png")
    assert blocks[2].data["source_path"].endswith("second.jpg")


def test_backend_extracts_concatenated_windows_image_paths(monkeypatch, tmp_path):
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    first.write_bytes(b"\x89PNG\r\n\x1a\n")
    second.write_bytes(b"\x89PNG\r\n\x1a\n")
    first_win = "D:\\images\\first.png"
    second_win = "D:\\images\\second.png"

    mapping = {first_win: first, second_win: second}
    monkeypatch.setattr("agent.cli.images._resolve_path", lambda path: mapping.get(path, Path(path)))

    blocks = build_user_message_content(f"{first_win}{second_win}", max_bytes=100)

    assert blocks[0].data["text"] == "Describe this image."
    assert [block.type for block in blocks[1:]] == ["image_url", "image_url"]
    assert blocks[1].data["source_path"].endswith("first.png")
    assert blocks[2].data["source_path"].endswith("second.png")


def test_backend_extracts_multiple_image_paths_from_image_command_text(tmp_path):
    first = tmp_path / "first.png"
    second = tmp_path / "second.jpg"
    first.write_bytes(b"\x89PNG\r\n\x1a\n")
    second.write_bytes(b"\xff\xd8\xff")

    blocks = build_user_message_content(f'/image "{first}" "{second}" 分析这两张图', max_bytes=100)

    assert blocks[0].type == "text"
    assert blocks[0].data["text"] == "分析这两张图"
    assert [block.type for block in blocks[1:]] == ["image_url", "image_url"]


def test_backend_leaves_missing_image_path_as_text(tmp_path):
    missing = tmp_path / "missing.png"

    blocks = build_user_message_content(f"Explain why {missing} is missing", max_bytes=100)

    assert len(blocks) == 1
    assert blocks[0].type == "text"


def test_message_display_text_formats_multimodal_history():
    content = [
        {"type": "text", "text": "分析这张图"},
        {
            "type": "text",
            "text": "[Image: sample.png]",
            "metadata": {"source_path": "D:\\images\\sample.png"},
        },
    ]

    assert message_display_text(content) == "分析这张图\n[Image: sample.png]"


def test_message_display_text_formats_multiple_image_attachments():
    first = str(Path("images") / "a.png")
    second = str(Path("images") / "b.jpg")
    content = [
        {"type": "text", "text": "分析这两张图"},
        {"type": "image_url", "metadata": {"source_path": first}},
        {"type": "image_url", "metadata": {"source_path": second}},
    ]

    assert message_display_text(content) == "分析这两张图\n[Image: a.png]\n[Image: b.jpg]"


def test_create_session_name_uses_first_meaningful_words():
    assert create_session_name("分析这两张图片的布局问题") == "分析这两张图片的布局问题"
    assert create_session_name('"D:\\images\\a.png" 分析这张图') == "分析这张图"


def test_stream_renderer_does_not_buffer_forever_on_unclosed_code():
    renderer = StreamRenderer(max_buffer=20)
    assert renderer.feed("`" + ("x" * 30) + " ") != ""


def test_context_compresses_tool_chain_as_unit():
    async def scenario():
        ctx = AgentContext(max_prompt_tokens=1_000_000)
        ctx.add_user("old")
        ctx.add_assistant_raw({
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": "x", "arguments": "{}"}}],
        })
        ctx.add_tool("call-1", "tool output")
        ctx.add_user("newer")
        ctx.add_assistant("latest")

        await ctx.compress_if_needed(force=True)

        assert len(ctx.messages) <= 3
        assert not any(msg.get("tool_call_id") == "call-1" for msg in ctx.messages)
        assert ctx.messages[0]["content"] == "old"

    run(scenario())


def test_context_message_count_does_not_trigger_compression():
    async def scenario():
        ctx = AgentContext(max_messages=1, max_prompt_tokens=100_000)
        for index in range(20):
            ctx.add_user(f"small message {index}")

        before = list(ctx.messages)
        await ctx.compress_if_needed()

        assert ctx.messages == before

    run(scenario())


def test_context_compresses_by_estimated_tokens():
    async def scenario():
        ctx = AgentContext(max_messages=100, max_prompt_tokens=50)
        ctx.add_user("original instruction")
        ctx.add_assistant("x" * 400)
        ctx.add_user("latest")

        await ctx.compress_if_needed()

        assert ctx.estimate_prompt_tokens() <= 120
        assert any(msg.get("content") == "original instruction" for msg in ctx.messages)
        assert not any(msg.get("content") == "x" * 400 for msg in ctx.messages)

    run(scenario())


def test_context_token_estimate_handles_cjk_json_and_base64_more_conservatively():
    chinese = "中文" * 20
    json_text = '{"items":[' + ",".join('{"id":%d,"value":"abc"}' % i for i in range(20)) + "]}"
    base64_text = "data:image/png;base64," + ("A" * 400)

    assert context_module._estimate_value_tokens(chinese) >= len(chinese)
    assert context_module._estimate_value_tokens(json_text) > len(json_text) // 4
    assert context_module._estimate_value_tokens(base64_text) <= len(base64_text) // 6


def test_context_token_estimate_counts_multimodal_image_as_visual_tokens_not_base64_text():
    huge_data_url = "data:image/png;base64," + ("A" * 2_100_000)
    auto_part = {"type": "image_url", "image_url": {"url": huge_data_url}}
    high_part = {"type": "image_url", "image_url": {"url": huge_data_url, "detail": "high"}}
    original_part = {"type": "image_url", "image_url": {"url": huge_data_url, "detail": "original"}}

    assert context_module._estimate_value_tokens(auto_part) == 2_048
    assert context_module._estimate_value_tokens(high_part) == 4_096
    assert context_module._estimate_value_tokens(original_part) == 4_096


def test_large_multimodal_image_does_not_false_trigger_post_tool_prompt_budget_stop():
    class FakeLLM:
        def __init__(self):
            self.calls = 0

        def estimate_tokens(self, messages):
            return context_module._estimate_value_tokens(messages)

        async def chat_stream(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                yield {
                    "type": "tool_calls",
                    "calls": [{"id": "inspect-1", "name": "inspect", "arguments": "{}"}],
                    "content": "",
                    "usage": None,
                }
                return
            yield {"type": "done", "content": "image inspected", "usage": None}

    async def scenario():
        registry = ToolRegistry()
        registry.register(ToolDef("inspect", "inspect", {"type": "object"}, lambda: "ok"))
        llm = FakeLLM()
        agent = ReActAgent("agent", llm, registry, max_iterations=2)
        agent.context.max_prompt_tokens = 20_000
        huge_data_url = "data:image/png;base64," + ("A" * 2_100_000)

        events = [event async for event in agent.reply_stream(Msg(content=[
            ContentBlock.text("inspect this image"),
            ContentBlock.image_url(huge_data_url, source_path="D:\\images\\large.png"),
        ]))]

        assert not any(event.get("type") == "error" for event in events)
        assert llm.calls == 2
        assert agent.context.messages[-1].get("content") == "image inspected"

    run(scenario())


def test_tool_registry_reports_real_progress_and_injects_tool_callback():
    async def scenario():
        registry = ToolRegistry()
        progress = []

        async def progressive(_progress=None):
            assert _progress is not None
            _progress("scanning", current=2, total=4, unit="files")
            await asyncio.sleep(0)
            return "ok"

        registry.register(ToolDef(
            name="progressive",
            description="progressive",
            parameters={"type": "object"},
            fn=progressive,
        ))

        result = await registry.execute("progressive", {}, on_progress=progress.append)

        assert result["output"] == "ok"
        assert [item["stage"] for item in progress] == [
            "authorizing", "running", "scanning", "finalizing",
        ]
        assert progress[2]["current"] == 2
        assert progress[2]["total"] == 4
        assert progress[2]["unit"] == "files"

    run(scenario())


def test_context_compression_summary_is_not_system_role():
    async def scenario():
        ctx = AgentContext(max_messages=100, max_prompt_tokens=1)
        ctx.add_user("original user instruction that should survive as a summary")
        ctx.add_assistant("old answer" * 100)
        ctx.add_user("latest question")

        await ctx.compress_if_needed()

        summary = ctx.messages[0]
        assert summary["role"] == "assistant"
        assert "[上下文摘要]" in summary["content"]
        assert "original user instruction" in summary["content"]

    run(scenario())


def test_context_estimate_uses_incremental_token_cache(monkeypatch):
    ctx = AgentContext(system_prompt="sys")
    ctx.add_user("hello")
    before = ctx.estimate_prompt_tokens()

    original_estimate = context_module._estimate_value_tokens

    def fail_on_message_recalc(value):
        if isinstance(value, (dict, list)):
            raise AssertionError("estimate_prompt_tokens should not recalculate message costs")
        return original_estimate(value)

    monkeypatch.setattr(context_module, "_estimate_value_tokens", fail_on_message_recalc)

    assert ctx.estimate_prompt_tokens() == before


def test_context_compress_uses_cached_message_costs(monkeypatch):
    async def scenario():
        ctx = AgentContext(max_messages=2, max_prompt_tokens=50)
        ctx.add_user("original instruction")
        ctx.add_assistant("x" * 400)
        ctx.add_user("latest")

        original_estimate = context_module._estimate_value_tokens

        def fail_on_message_recalc(value):
            if isinstance(value, (dict, list)):
                raise AssertionError("compress_if_needed should use cached message costs")
            return original_estimate(value)

        monkeypatch.setattr(context_module, "_estimate_value_tokens", fail_on_message_recalc)
        await ctx.compress_if_needed()

        assert any(msg.get("content") == "original instruction" for msg in ctx.messages)
        assert not any(msg.get("content") == "x" * 400 for msg in ctx.messages)

    run(scenario())


def test_context_force_compression_uses_llm_summary_on_short_tail():
    class FakeSummaryLLM:
        async def chat(self, messages):
            return {"content": "## Active Task\nNone."}

        async def chat_limited(self, messages, *, max_tokens, **kwargs):
            return await self.chat(messages)

    async def scenario():
        ctx = AgentContext(system_prompt="sys", max_messages=100, max_prompt_tokens=100000)
        ctx.compressor = ContextCompressor(FakeSummaryLLM())
        ctx.last_prompt_tokens = 999
        for text in ("u1", "a1", "u2", "a2", "u3", "a3"):
            if text.startswith("u"):
                ctx.add_user(text)
            else:
                ctx.add_assistant(text)

        await ctx.compress_if_needed(force=True)

        assert len(ctx.messages) < 6
        assert ctx.last_prompt_tokens == ctx.estimate_compaction_tokens()
        assert any("CONTEXT COMPACTION" in msg.get("content", "") for msg in ctx.messages)

    run(scenario())


def test_context_resets_compressor_state_on_reset_and_session_switch(tmp_path):
    class FakeSummaryLLM:
        async def chat(self, messages):
            return {"content": "unused"}

        async def chat_limited(self, messages, *, max_tokens, **kwargs):
            return await self.chat(messages)

    ctx = AgentContext()
    ctx.compressor = ContextCompressor(FakeSummaryLLM())
    ctx.compressor._previous_summary = "old session summary"

    ctx.reset()

    assert ctx.compressor._previous_summary is None

    ctx.compressor._previous_summary = "other session summary"
    ctx.set_session(str(tmp_path / "next.json"))

    assert ctx.compressor._previous_summary is None


def test_react_agent_precompresses_before_first_llm_request():
    class FakeLLM:
        def __init__(self):
            self.summary_calls = 0
            self.stream_messages = None

        def estimate_tokens(self, messages):
            text = json.dumps(messages, ensure_ascii=False)
            if "old bulky context" in text:
                return 1000
            return 100

        async def chat(self, messages, tools=None):
            self.summary_calls += 1
            return {"content": "## Active Task\nanswer the latest request"}

        async def chat_limited(self, messages, *, max_tokens, **kwargs):
            self.summary_calls += 1
            return {"content": "## Active Task\nanswer the latest request"}

        async def chat_stream(self, messages, tools):
            self.stream_messages = messages
            yield {"type": "done", "content": "ok", "usage": None}

    async def scenario():
        llm = FakeLLM()
        agent = ReActAgent("agent", llm, ToolRegistry(), max_iterations=1)
        agent.context.max_prompt_tokens = 500
        agent.context.add_user("first")
        agent.context.add_assistant("first answer")
        agent.context.add_user("older followup")
        agent.context.add_assistant("old bulky context" * 200)
        agent.context.add_user("tail")
        agent.context.add_assistant("recent")

        events = [event async for event in agent.reply_stream(Msg(content=[ContentBlock.text("continue")]))]

        assert events[-1]["type"] == "done"
        assert llm.summary_calls == 1
        assert "old bulky context" not in json.dumps(llm.stream_messages, ensure_ascii=False)

    run(scenario())


def test_react_agent_adds_one_compact_transient_date_anchor(monkeypatch):
    from agent.runtime import time_utils

    class FakeNow:
        def strftime(self, fmt):
            return {
                "%Y-%m-%d": "2026-08-24",
                "%z": "+0800",
                "%Y-%m-%d %H:%M:%S %Z %z": "2026-08-24 09:15:00 +08 +0800",
                "%Y年%m月%d日 %H:%M:%S %Z": "2026年08月24日 09:15:00 +08",
            }[fmt]

    class FakeLLM:
        class _Config:
            capabilities = frozenset({"reasoning"})

        config = _Config()

        def __init__(self):
            self.messages = None

        async def chat_stream(self, messages, tools):
            self.messages = messages
            yield {"type": "done", "content": "ok", "usage": None}

    monkeypatch.setattr(time_utils, "current_datetime", lambda: FakeNow())

    async def scenario():
        llm = FakeLLM()
        agent = ReActAgent("agent", llm, ToolRegistry(), max_iterations=1)

        await agent.reply(Msg(content=[ContentBlock.text("今天有什么新闻")]))

        prompt_text = json.dumps(llm.messages, ensure_ascii=False)
        stored_text = json.dumps(agent.context.messages, ensure_ascii=False)
        assert prompt_text.count("[SYSTEM-SUPPLIED DATE ANCHOR]") == 1
        assert "Local date: 2026-08-24; UTC offset: +08:00." in prompt_text
        assert "runtime_current_time" not in prompt_text
        assert "[SYSTEM-SUPPLIED DATE ANCHOR]" not in stored_text

    run(scenario())


def test_react_agent_keeps_one_date_anchor_across_iterations(monkeypatch):
    from agent.runtime import time_utils

    class FakeNow:
        def strftime(self, fmt):
            return {
                "%Y-%m-%d": "2026-08-24",
                "%z": "+0800",
                "%Y-%m-%d %H:%M:%S %Z %z": "2026-08-24 09:15:00 +08 +0800",
                "%Y年%m月%d日 %H:%M:%S %Z": "2026年08月24日 09:15:00 +08",
            }[fmt]

    class FakeLLM:
        class _Config:
            capabilities = frozenset({"reasoning"})

        config = _Config()

        def __init__(self):
            self.prompts = []

        async def chat_stream(self, messages, tools):
            self.prompts.append(messages)
            if len(self.prompts) == 1:
                yield {
                    "type": "tool_calls",
                    "calls": [{
                        "id": "call-1",
                        "name": "noop",
                        "arguments": "{}",
                    }],
                    "content": "",
                    "reasoning_content": "",
                    "usage": None,
                }
            else:
                yield {"type": "done", "content": "ok", "usage": None}

    monkeypatch.setattr(time_utils, "current_datetime", lambda: FakeNow())

    async def scenario():
        registry = ToolRegistry()

        async def noop():
            return "done"

        registry.register(ToolDef("noop", "noop", {"type": "object"}, noop))
        llm = FakeLLM()
        agent = ReActAgent("agent", llm, registry, max_iterations=3)

        await agent.reply(Msg(content=[ContentBlock.text("总结今天的修改")]))

        assert len(llm.prompts) == 2
        for prompt in llm.prompts:
            rendered = json.dumps(prompt, ensure_ascii=False)
            assert rendered.count("[SYSTEM-SUPPLIED DATE ANCHOR]") == 1
            assert "runtime_current_time" not in rendered

    run(scenario())


def test_relative_date_anchor_ignores_generic_current_state_language():
    from agent.runtime.time_utils import needs_relative_date_anchor

    assert needs_relative_date_anchor("当前有多少 skill") is False
    assert needs_relative_date_anchor("现在这个配置能用吗") is False
    assert needs_relative_date_anchor("总结今天的修改") is True
    assert needs_relative_date_anchor("昨天改了哪些文件") is True


def test_date_anchor_preserves_multimodal_user_content():
    prompt = [{
        "role": "user",
        "content": [
            {"type": "text", "text": "今天"},
            {"type": "image_url", "image_url": {}},
        ],
    }]

    ReActAgent._prepend_latest_user_context(prompt, "anchor")

    assert prompt[0]["content"][0] == {"type": "text", "text": "anchor"}
    assert prompt[0]["content"][1]["text"] == "今天"
    assert prompt[0]["content"][2]["type"] == "image_url"


def test_react_agent_stops_repeated_identical_tool_calls():
    from agent.runtime.metrics import runtime_metrics

    runtime_metrics.reset()
    class FakeLLM:
        def __init__(self):
            self.calls = 0

        async def chat_stream(self, messages, tools):
            self.calls += 1
            yield {
                "type": "tool_calls",
                "calls": [{"id": f"call-{self.calls}", "name": "noop", "arguments": "{\"x\": 1}"}],
                "content": "",
                "reasoning_content": "",
                "usage": None,
            }
            yield {"type": "done", "content": "", "usage": None}

    async def scenario():
        registry = ToolRegistry()

        async def noop(x: int):
            return f"same {x}"

        registry.register(ToolDef("noop", "noop", {"type": "object"}, noop))
        llm = FakeLLM()
        agent = ReActAgent("agent", llm, registry, max_iterations=15)
        events = [event async for event in agent.reply_stream(Msg(content=[ContentBlock.text("loop")]))]

        # Three planning attempts plus one final synthesis with tools disabled.
        assert llm.calls <= 4
        assert any(event["type"] == "error" and "repeated tool call" in event["message"] for event in events)
        assert any(event["type"] == "chunk" and "工具执行已安全停止" in event["content"] for event in events)
        assert runtime_metrics.snapshot()["repeated_payload_count"] == 1

    run(scenario())


def test_session_path_rejects_directory_escape():
    try:
        sessions.session_path("../outside")
    except sessions.SessionNameError:
        return
    raise AssertionError("expected session path escape to be rejected")


def test_gitignore_excludes_runtime_artifacts():
    ignore = Path(".gitignore").read_text(encoding="utf-8")

    assert ".sessions/" in ignore
    assert "\nnul\n" in f"\n{ignore}\n"
    assert "D\uf03a/" in ignore


def test_local_llm_default_timeout_is_disabled():
    assert default_llm_timeout("http://localhost:8081/v1", None, 60.0) == 0.0
    assert default_llm_timeout("http://127.0.0.1:8081/v1", None, 60.0) == 0.0
    assert default_llm_timeout("https://api.deepseek.com", None, 60.0) == 60.0
    assert default_llm_timeout("http://localhost:8081/v1", "12", 60.0) == 12.0


def test_request_lifecycle_tracks_terminal_states():
    lifecycle = RequestLifecycle("req-1")

    lifecycle.start()
    lifecycle.cancel()
    lifecycle.finish()

    assert lifecycle.status == RequestStatus.DONE
    assert lifecycle.events == ["start", "cancel", "finish"]

    failed = RequestLifecycle("req-2")
    failed.start()
    failed.fail("boom")
    failed.finish()

    assert failed.status == RequestStatus.ERROR
    assert failed.error == "boom"


def test_health_report_includes_harness_diagnostics():
    registry = ToolRegistry()
    registry.register(ToolDef("demo", "demo", {"type": "object"}, lambda: "ok"))
    config = LLMConfig(model="local", base_url="http://localhost:8081/v1", timeout=0)

    report = build_health_report(config, registry, context_limit=65536, sandbox=None)

    assert "Harness health:" in report
    assert "Local model: yes" in report
    assert "LLM timeout: disabled" in report
    assert "Context limit: 65536" in report
    assert "demo" in report


def test_react_agent_reuses_same_tool_result_within_turn():
    class FakeLLM:
        def __init__(self):
            self.calls = 0

        async def chat_stream(self, messages, tools):
            self.calls += 1
            if self.calls <= 2:
                yield {
                    "type": "tool_calls",
                    "calls": [{"id": f"call-{self.calls}", "name": "lookup", "arguments": "{\"q\": \"same\"}"}],
                    "content": "",
                    "reasoning_content": "",
                    "usage": None,
                }
                yield {"type": "done", "content": "", "usage": None}
            else:
                yield {"type": "done", "content": "done", "usage": None}

    async def scenario():
        registry = ToolRegistry()
        executed = 0

        async def lookup(q: str):
            nonlocal executed
            executed += 1
            return f"value for {q}"

        registry.register(ToolDef("lookup", "lookup", {"type": "object"}, lookup, idempotent=True))
        agent = ReActAgent("agent", FakeLLM(), registry, max_iterations=5)
        events = [event async for event in agent.reply_stream(Msg(content=[ContentBlock.text("use lookup")]))]

        assert executed == 1
        assert any(event["type"] == "tool_result" and event.get("cached") for event in events)

    run(scenario())


def test_export_session_markdown_writes_transcript(tmp_path):
    old_dir = sessions.SESSION_DIR
    old_default = sessions.SESSION_DEFAULT
    old_export = sessions.SESSION_EXPORT_DIR
    try:
        sessions.SESSION_DIR = tmp_path
        sessions.SESSION_DEFAULT = tmp_path / "default.json"
        sessions.SESSION_EXPORT_DIR = tmp_path / "exports"
        session_file = sessions.session_path("demo")
        session_file.write_text(
            '{"messages":[{"role":"user","content":"hello"},{"role":"assistant","content":"hi"}]}',
            encoding="utf-8",
        )

        out = sessions.export_session_markdown("demo")
        text = out.read_text(encoding="utf-8")

        assert out.parent == sessions.SESSION_EXPORT_DIR
        assert "# Session: demo" in text
        assert "## user" in text
        assert "hello" in text
    finally:
        sessions.SESSION_DIR = old_dir
        sessions.SESSION_DEFAULT = old_default
        sessions.SESSION_EXPORT_DIR = old_export


def test_session_store_appends_jsonl_and_loads_legacy_json(tmp_path):
    legacy = tmp_path / "demo.json"
    legacy.write_text(
        '{"system_prompt":"old","messages":[{"role":"user","content":"legacy"}],'
        '"show_reasoning":true,"total_prompt_tokens":3,"total_completion_tokens":4,"last_prompt_tokens":2}',
        encoding="utf-8",
    )
    store = SessionStore(legacy)

    loaded = store.load()
    assert loaded["messages"][0]["content"] == "legacy"
    assert loaded["show_reasoning"] is True

    loaded["messages"].append({"role": "assistant", "content": "new"})
    store.save(loaded, append_from=1)

    assert store.jsonl_path.exists()
    assert not legacy.read_text(encoding="utf-8").startswith('{"system_prompt":"old","messages":[{"role":"user","content":"legacy"},{"role"')
    assert SessionStore(legacy).load()["messages"][-1]["content"] == "new"


def test_session_header_is_separate_from_append_only_message_stream(tmp_path):
    store = SessionStore(tmp_path / "header.json")
    store.save({
        "system_prompt": "stable persona",
        "persona_id": "work",
        "messages": [{"role": "user", "content": "hello"}],
        "total_prompt_tokens": 12,
    })

    records = [json.loads(line) for line in store.jsonl_path.read_text(encoding="utf-8").splitlines()]

    assert store.header_path.exists()
    assert json.loads(store.header_path.read_text(encoding="utf-8"))["system_prompt"] == "stable persona"
    assert not any(record.get("type") == "state" for record in records)
    assert any(record.get("type") == "message" for record in records)
    assert SessionStore(tmp_path / "header.json").load()["total_prompt_tokens"] == 12


def test_session_store_synthesizes_interrupted_for_dead_writer_once(tmp_path):
    store = SessionStore(tmp_path / "crashed.json")
    store.jsonl_path.write_text(
        json.dumps({
            "type": "session_started",
            "run_id": "dead-run",
            "pid": 999_999_999,
            "recorded_at": 1.0,
        }) + "\n",
        encoding="utf-8",
    )

    recovered = store.load()
    events = store.lifecycle_events()
    store.load()

    assert recovered["messages"] == []
    assert [event["type"] for event in events] == ["session_started", "session_interrupted"]
    assert events[-1]["reason"] == "process_exit_without_session_end"
    assert len(store.lifecycle_events()) == 2


def test_clean_session_end_does_not_synthesize_interrupted(tmp_path):
    store = SessionStore(tmp_path / "clean.json")
    store.save({"system_prompt": "", "messages": [{"role": "user", "content": "hi"}]})
    store.record_end("shutdown")

    SessionStore(tmp_path / "clean.json").load()

    assert [event["type"] for event in store.lifecycle_events()] == [
        "session_started", "session_ended",
    ]


def test_session_store_skips_corrupt_jsonl_lines(tmp_path):
    store = SessionStore(tmp_path / "broken.json")
    store.save({"system_prompt": "", "messages": [{"role": "user", "content": "ok"}]}, append_from=0)
    with open(store.jsonl_path, "a", encoding="utf-8") as f:
        f.write("{not json}\n")
        f.write('{"type":"message","index":1,"message":{"role":"assistant","content":"still ok"}}\n')

    loaded = SessionStore(tmp_path / "broken.json").load()

    assert [m["content"] for m in loaded["messages"]] == ["ok", "still ok"]


def test_session_store_persists_and_exports_subagent_transcripts(tmp_path):
    store = SessionStore(tmp_path / "demo.json")
    store.save(
        {"system_prompt": "", "messages": [{"role": "user", "content": "research"}]},
        append_from=0,
    )
    store.append_subagent_event({
        "type": "assistant",
        "process_id": "worker-1",
        "worker_type": "explorer",
        "goal": "inspect logs",
        "turn": 1,
        "content": "I found the cause.",
        "tool_calls": [{"name": "search_text", "arguments": "{}"}],
    })
    store.append_subagent_event({
        "type": "tool",
        "process_id": "worker-1",
        "tool_name": "search_text",
        "output": "matching evidence",
    })

    events = store.load_subagent_events()
    exported = store.export_markdown("demo", tmp_path / "demo.md")
    text = exported.read_text(encoding="utf-8")

    assert [event["type"] for event in events] == ["assistant", "tool"]
    assert store.subagent_path in store.related_paths()
    assert "# Subagent transcripts" in text
    assert "I found the cause." in text
    assert "matching evidence" in text


def test_session_store_appends_context_index_feedback_as_related_artifact(tmp_path):
    store = SessionStore(tmp_path / "demo.json")

    path = store.append_context_index_feedback({
        "type": "context_index_impression",
        "schema_version": 1,
        "request_id": "context-index:request",
        "rows": [],
        "recorded_at": "caller-controlled",
    })

    payload = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert payload["type"] == "context_index_impression"
    assert payload["schema_version"] == 1
    assert isinstance(payload["recorded_at"], float)
    assert path == store.context_index_feedback_path
    assert path in store.related_paths()


def test_legacy_reasoning_context_is_restored_to_tool_call_assistant():
    messages = [
        {
            "role": "assistant",
            "content": "I will search.",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "search_web", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "result"},
        {
            "role": "user",
            "content": "legacy header\n\nI need current news.",
            "_meta": {"type": "reasoning_context"},
        },
    ]

    migrated = context_module._restore_legacy_reasoning_content(messages)

    assert len(migrated) == 2
    assert migrated[0]["reasoning_content"] == "I need current news."
    assert migrated[1]["role"] == "tool"


def test_session_store_writes_snapshot_on_first_save(tmp_path):
    store = SessionStore(tmp_path / "snap.json")
    store.save({"system_prompt": "", "messages": [{"role": "user", "content": "snap"}]}, append_from=0)

    assert store.snapshot_path.exists()
    assert SessionStore(tmp_path / "snap.json").load()["messages"][0]["content"] == "snap"


def test_agent_context_uses_session_store_incremental_save(tmp_path):
    path = tmp_path / "ctx.json"
    ctx = AgentContext(system_prompt="sys")
    ctx.set_session(str(path))
    ctx.add_user("one")
    ctx.save()
    first_size = path.with_suffix(".jsonl").stat().st_size
    ctx.add_assistant("two")
    ctx.save()

    assert path.with_suffix(".jsonl").stat().st_size > first_size
    reloaded = AgentContext()
    reloaded.set_session(str(path))

    assert reloaded.load() is True
    assert [m["content"] for m in reloaded.messages] == ["one", "two"]
    assert reloaded.system_prompt == "sys"


def test_context_timestamps_user_and_assistant_but_not_tools(monkeypatch):
    monkeypatch.setattr(context_module.time, "time", lambda: 1_750_000_000.25)
    ctx = AgentContext(system_prompt="sys")

    ctx.add_user("hello")
    ctx.add_assistant("hi")
    ctx.add_tool("call-1", "result")

    assert ctx.messages[0]["timestamp"] == 1_750_000_000.25
    assert ctx.messages[1]["timestamp"] == 1_750_000_000.25
    assert "timestamp" not in ctx.messages[2]


def test_context_preserves_valid_raw_timestamp_and_replaces_invalid(monkeypatch):
    monkeypatch.setattr(context_module.time, "time", lambda: 1_760_000_000.0)
    ctx = AgentContext()

    ctx.add_assistant_raw({
        "role": "assistant",
        "content": "old",
        "timestamp": 1_700_000_000.0,
    })
    ctx.add_assistant_raw({
        "role": "assistant",
        "content": "bad",
        "timestamp": float("nan"),
    })

    assert ctx.messages[0]["timestamp"] == 1_700_000_000.0
    assert ctx.messages[1]["timestamp"] == 1_760_000_000.0


def test_context_timestamp_roundtrip_and_legacy_absence(tmp_path, monkeypatch):
    monkeypatch.setattr(context_module.time, "time", lambda: 1_750_000_000.0)
    path = tmp_path / "timestamped.json"
    ctx = AgentContext(system_prompt="sys")
    ctx.set_session(str(path))
    ctx.add_user("new")
    ctx.save()

    restored = AgentContext()
    restored.set_session(str(path))
    assert restored.load() is True
    assert restored.messages[0]["timestamp"] == 1_750_000_000.0

    legacy_path = tmp_path / "legacy.json"
    legacy_path.write_text(
        json.dumps({"messages": [{"role": "user", "content": "old"}]}),
        encoding="utf-8",
    )
    legacy = AgentContext()
    legacy.set_session(str(legacy_path))
    assert legacy.load() is True
    assert "timestamp" not in legacy.messages[0]


def test_get_prompt_adds_timestamp_only_to_user_messages_without_mutation(monkeypatch):
    marker = "<message_time>2025-06-15T15:06:40+08:00</message_time>"
    monkeypatch.setattr(
        context_module,
        "_model_timestamp_marker",
        lambda value: marker if value else None,
    )
    ctx = AgentContext()
    ctx.messages = [
        {"role": "user", "content": "hello", "timestamp": 1_750_000_000.0},
        {
            "role": "assistant",
            "content": "thinking",
            "timestamp": 1_750_000_001.0,
            "tool_calls": [{
                "id": "call-1",
                "type": "function",
                "function": {"name": "lookup", "arguments": "{}"},
            }],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "result"},
    ]

    prompt = ctx.get_prompt()

    assert prompt[0]["content"] == f"{marker}\nhello"
    assert "timestamp" not in prompt[0]
    assert prompt[1]["content"] == "thinking"
    assert prompt[1]["tool_calls"] == ctx.messages[1]["tool_calls"]
    assert prompt[2] == ctx.messages[2]
    assert ctx.messages[0]["content"] == "hello"
    assert ctx.messages[0]["timestamp"] == 1_750_000_000.0


def test_get_prompt_timestamps_multimodal_override_without_mutation(monkeypatch):
    marker = "<message_time>2025-06-15T15:06:40+08:00</message_time>"
    monkeypatch.setattr(context_module, "_model_timestamp_marker", lambda value: marker)
    original = [
        {"type": "text", "text": "look"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
    ]
    override = [{"type": "text", "text": "replacement"}]
    ctx = AgentContext()
    ctx.messages = [{
        "role": "user",
        "content": original,
        "timestamp": 1_750_000_000.0,
    }]

    prompt = ctx.get_prompt({0: override})

    assert prompt[0]["content"][0] == {"type": "text", "text": marker}
    assert prompt[0]["content"][1:] == override
    assert ctx.messages[0]["content"] == original
    assert override == [{"type": "text", "text": "replacement"}]


def test_context_compression_preserves_canonical_timestamp_metadata(monkeypatch):
    monkeypatch.setattr(context_module.time, "time", lambda: 1_750_000_000.0)

    class RecordingCompressor:
        def __init__(self):
            self.messages = []

        async def compress(self, messages, max_prompt_tokens, force=False):
            self.messages = messages
            return [
                messages[0],
                {"role": "assistant", "content": "summary"},
                messages[-1],
            ]

    compressor = RecordingCompressor()
    ctx = AgentContext(system_prompt="sys", max_prompt_tokens=1)
    ctx.compressor = compressor
    ctx.add_user("old request")
    ctx.add_assistant("old answer")
    ctx.add_user("latest request")

    run(ctx.compress_if_needed(force=True))

    assert compressor.messages[-1]["content"] == "latest request"
    assert compressor.messages[-1]["timestamp"] == 1_750_000_000.0
    assert ctx.messages[-1] == {
        "role": "user",
        "content": "latest request",
        "timestamp": 1_750_000_000.0,
    }


def test_session_helpers_include_jsonl_and_rename_all_files(tmp_path):
    old_dir = sessions.SESSION_DIR
    old_default = sessions.SESSION_DEFAULT
    old_export = sessions.SESSION_EXPORT_DIR
    try:
        sessions.SESSION_DIR = tmp_path
        sessions.SESSION_DEFAULT = tmp_path / "default.json"
        sessions.SESSION_EXPORT_DIR = tmp_path / "exports"
        store = SessionStore(sessions.session_path("alpha"))
        store.save({"system_prompt": "", "messages": [{"role": "user", "content": "hi"}]}, append_from=0)
        original_diagnostic = store.append_diagnostic({"code": "demo"})
        original_subagents = store.append_subagent_event({
            "type": "terminal", "process_id": "worker-1", "result": "done",
        })
        original_feedback = store.append_context_index_feedback({
            "type": "context_index_impression",
            "schema_version": 1,
            "request_id": "context-index:rename",
            "rows": [],
        })

        assert sessions.list_sessions() == ["alpha"]
        assert sessions.session_msg_count("alpha") == 1
        assert original_diagnostic.exists()
        assert original_subagents.exists()
        assert original_feedback.exists()

        sessions.rename_session("alpha", "beta")
        renamed_store = SessionStore(sessions.session_path("beta"))
        assert sessions.list_sessions() == ["beta"]
        assert sessions.session_msg_count("beta") == 1
        assert not original_diagnostic.exists()
        assert renamed_store.diagnostic_path.exists()
        assert renamed_store.subagent_path.exists()
        assert not original_feedback.exists()
        assert renamed_store.context_index_feedback_path.exists()
        sessions.delete_session("beta")
        assert not any(path.exists() for path in renamed_store.related_paths())
    finally:
        sessions.SESSION_DIR = old_dir
        sessions.SESSION_DEFAULT = old_default
        sessions.SESSION_EXPORT_DIR = old_export


def test_react_agent_executes_multiple_tool_calls_concurrently():
    class FakeLLM:
        async def chat_stream(self, messages, tools):
            yield {
                "type": "tool_calls",
                "calls": [
                    {"id": "call-1", "name": "slow_one", "arguments": "{}"},
                    {"id": "call-2", "name": "slow_two", "arguments": "{}"},
                ],
                "content": "",
                "reasoning_content": "",
                "usage": None,
            }
            yield {"type": "done", "content": "", "usage": None}
            yield {"type": "done", "content": "finished", "usage": None}

    async def scenario():
        registry = ToolRegistry()

        async def slow_one():
            await asyncio.sleep(0.06)
            return "one"

        async def slow_two():
            await asyncio.sleep(0.06)
            return "two"

        registry.register(ToolDef("slow_one", "slow one", {"type": "object"}, slow_one))
        registry.register(ToolDef("slow_two", "slow two", {"type": "object"}, slow_two))
        agent = ReActAgent("agent", FakeLLM(), registry, max_iterations=1)

        start = time.perf_counter()
        events = [event async for event in agent.reply_stream(Msg(content=[ContentBlock.text("run")]))]
        elapsed = time.perf_counter() - start

        started = next(event for event in events if event["type"] == "tool_calls")
        assert [(call["id"], call["name"]) for call in started["calls"]] == [
            ("call-1", "slow_one"),
            ("call-2", "slow_two"),
        ]
        assert [event["name"] for event in events if event["type"] == "tool_result"] == ["slow_one", "slow_two"]
        progress = [event for event in events if event["type"] == "tool_progress"]
        assert {event["id"] for event in progress} == {"call-1", "call-2"}
        assert {event["stage"] for event in progress} >= {"queued", "preparing", "running", "finalizing"}
        # A fast call can now finish while another call still reports progress.
        # Each call's own progress must precede its completed result.
        for call_id in ("call-1", "call-2"):
            result_index = next(index for index, event in enumerate(events)
                                if event["type"] == "tool_result" and event["id"] == call_id)
            assert all(index < result_index for index, event in enumerate(events)
                       if event["type"] == "tool_progress" and event["id"] == call_id)
        assert elapsed < 0.11

    run(scenario())


def test_react_agent_discards_invalid_tool_json_before_history_or_execution():
    class FakeLLM:
        async def chat_stream(self, messages, tools):
            yield {
                "type": "tool_calls",
                "calls": [{"id": "call-1", "name": "demo", "arguments": '{"path": '}],
                "content": "",
                "reasoning_content": "",
                "finish_reason": "tool_calls",
                "usage": None,
            }
            yield {"type": "done", "content": "", "usage": None}

    async def scenario():
        registry = ToolRegistry()
        executed = []

        async def demo(path: str):
            executed.append(path)
            return path

        registry.register(ToolDef("demo", "demo", {"type": "object"}, demo))
        agent = ReActAgent("agent", FakeLLM(), registry, max_iterations=1)

        events = [event async for event in agent.reply_stream(Msg(content=[ContentBlock.text("run")]))]

        failure = next(event for event in events if event.get("code") == "invalid_arguments")
        assert failure["retryable"] is False
        assert failure["tool_name"] == "demo"
        assert executed == []
        assert not any(event["type"] in {"tool_calls", "tool_result"} for event in events)
        assert not any(message.get("tool_calls") for message in agent.context.messages)

    run(scenario())


def test_react_agent_classifies_output_limited_tool_json_as_truncated(tmp_path):
    from agent.runtime.metrics import runtime_metrics

    runtime_metrics.reset()
    target = tmp_path / "must-not-exist.html"

    class FakeLLM:
        async def chat_stream(self, messages, tools):
            yield {
                "type": "tool_calls",
                "calls": [{
                    "id": "write-1",
                    "name": "write_file",
                    "arguments": json.dumps({"path": str(target)})[:-2] + ',"content":"<html>',
                }],
                "content": "Generating the page.",
                "reasoning_content": "",
                "finish_reason": "length",
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 4096,
                    "total_tokens": 4196,
                },
            }

    async def scenario():
        registry = ToolRegistry()

        async def write_file(path: str, content: str):
            Path(path).write_text(content, encoding="utf-8")
            return path

        registry.register(ToolDef(
            "write_file",
            "write",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
            },
            write_file,
        ))
        agent = ReActAgent("agent", FakeLLM(), registry, max_iterations=1)
        agent.context.set_session(str(tmp_path / "truncated-session.json"))

        events = [event async for event in agent.reply_stream(Msg(content=[ContentBlock.text("build")]))]

        failure = next(event for event in events if event.get("code") == "tool_call_truncated")
        assert failure["retryable"] is True
        assert failure["partial"] is True
        assert failure["details"]["finish_reason"] == "length"
        assert failure["details"]["completion_tokens"] == 4096
        artifact = Path(failure["artifact_ref"])
        assert artifact.exists()
        diagnostic = json.loads(artifact.read_text(encoding="utf-8").splitlines()[-1])
        assert diagnostic["code"] == "tool_call_truncated"
        assert diagnostic["finish_reason"] == "length"
        assert diagnostic["raw_arguments"].endswith("<html>")
        assert not target.exists()
        assert not any(message.get("tool_calls") for message in agent.context.messages)
        assert runtime_metrics.snapshot()["tool_truncation_count"] == 1

    run(scenario())


def test_react_agent_recovers_once_after_truncated_tool_call():
    from agent.runtime.metrics import runtime_metrics

    runtime_metrics.reset()
    class FakeLLM:
        def __init__(self):
            self.calls = 0
            self.prompts = []

        async def chat_stream(self, messages, tools):
            self.calls += 1
            self.prompts.append(messages)
            if self.calls == 1:
                yield {
                    "type": "tool_calls",
                    "calls": [{
                        "id": "demo-truncated",
                        "name": "demo",
                        "arguments": '{"value":"unfinished',
                    }],
                    "content": "",
                    "reasoning_content": "",
                    "finish_reason": "length",
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 4096,
                        "total_tokens": 4106,
                    },
                }
                return
            if self.calls == 2:
                yield {
                    "type": "tool_calls",
                    "calls": [{
                        "id": "demo-complete",
                        "name": "demo",
                        "arguments": '{"value":"complete"}',
                    }],
                    "content": "",
                    "reasoning_content": "",
                    "finish_reason": "tool_calls",
                    "usage": None,
                }
                return
            yield {
                "type": "done",
                "content": "recovered",
                "finish_reason": "stop",
                "usage": None,
            }

    async def scenario():
        executed = []
        registry = ToolRegistry()

        async def demo(value: str):
            executed.append(value)
            return value

        registry.register(ToolDef("demo", "demo", {"type": "object"}, demo))
        llm = FakeLLM()
        agent = ReActAgent("agent", llm, registry, max_iterations=3)

        events = [event async for event in agent.reply_stream(Msg(content=[ContentBlock.text("run")]))]

        assert executed == ["complete"]
        assert sum(event.get("code") == "tool_call_truncated" for event in events) == 1
        assert [
            event.get("stage")
            for event in events
            if event.get("type") == "tool_progress"
            and event.get("stage") in {"recovering", "recovered", "stopped"}
        ] == ["recovering", "recovered"]
        assert agent.context.messages[-1]["content"] == "recovered"
        assert any(
            message.get("role") == "user"
            and "one smaller edit_file or apply_patch" in message.get("content", "")
            and "begin_file_write" not in message.get("content", "")
            for message in llm.prompts[1]
        )
        assert [
            index
            for index, message in enumerate(llm.prompts[1])
            if message.get("role") == "system"
        ] == [0]
        historical_calls = [
            call
            for message in agent.context.messages
            for call in message.get("tool_calls", [])
        ]
        assert [call["id"] for call in historical_calls] == ["demo-complete"]
        metrics = runtime_metrics.snapshot()
        assert metrics["tool_truncation_count"] == 1
        assert metrics["recovery_success_rate"] == 1.0

    run(scenario())


def test_truncated_whole_write_is_hidden_during_semantic_recovery():
    class FakeLLM:
        def __init__(self):
            self.calls = 0
            self.tool_names = []

        async def chat_stream(self, messages, tools):
            del messages
            self.calls += 1
            self.tool_names.append({
                item["function"]["name"]
                for item in tools
            })
            if self.calls == 1:
                yield {
                    "type": "tool_calls",
                    "calls": [{
                        "id": "whole-write",
                        "name": "write_file",
                        "arguments": '{"path":"large.html","content":"unfinished',
                    }],
                    "content": "",
                    "reasoning_content": "",
                    "finish_reason": "length",
                    "usage": {
                        "prompt_tokens": 100,
                        "completion_tokens": 4096,
                        "total_tokens": 4196,
                    },
                }
                return
            if self.calls == 2:
                yield {
                    "type": "tool_calls",
                    "calls": [{
                        "id": "skeleton-patch",
                        "name": "apply_patch",
                        "arguments": json.dumps({"patch": "*** Begin Patch\n*** End Patch"}),
                    }],
                    "content": "",
                    "reasoning_content": "",
                    "finish_reason": "tool_calls",
                    "usage": None,
                }
                return
            yield {
                "type": "done",
                "content": "recovered",
                "finish_reason": "stop",
                "usage": None,
            }

    async def scenario():
        executed = []
        registry = ToolRegistry()

        async def write_file(path: str, content: str):
            raise AssertionError(f"truncated write must not execute: {path} {len(content)}")

        async def apply_patch(patch: str):
            executed.append(patch)
            return "patched"

        registry.register(ToolDef(
            "write_file",
            "whole write",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
            write_file,
        ))
        registry.register(ToolDef(
            "apply_patch",
            "semantic patch",
            {
                "type": "object",
                "properties": {"patch": {"type": "string"}},
                "required": ["patch"],
            },
            apply_patch,
        ))
        llm = FakeLLM()
        agent = ReActAgent("agent", llm, registry, max_iterations=3, progressive_tools=False)

        events = [event async for event in agent.reply_stream(Msg(content=[ContentBlock.text("build")]))]

        assert executed == ["*** Begin Patch\n*** End Patch"]
        assert "write_file" in llm.tool_names[0]
        # Schema stays byte-stable for cache reuse. The execution layer still
        # blocks the failed whole-write invocation and guides the model to a
        # semantic patch.
        assert "write_file" in llm.tool_names[1]
        assert "apply_patch" in llm.tool_names[1]
        assert events[-1]["type"] == "done"

    run(scenario())


def test_react_agent_uses_opt_in_assistant_prefill_for_truncated_tool_call():
    from types import SimpleNamespace

    class PrefillLLM:
        def __init__(self):
            self.calls = 0
            self.prompts = []
            self.tool_sets = []
            self.config = SimpleNamespace(
                model="prefill-capable",
                capabilities=frozenset({"assistant_prefill"}),
            )

        async def chat_stream(self, messages, tools):
            self.calls += 1
            self.prompts.append(messages)
            self.tool_sets.append(tools)
            if self.calls == 1:
                yield {
                    "type": "tool_calls",
                    "calls": [{
                        "id": "prefill-call",
                        "name": "demo",
                        "arguments": '{"value":"hel',
                    }],
                    "content": "",
                    "finish_reason": "length",
                    "usage": None,
                }
                return
            if self.calls == 2:
                assert tools == []
                assert messages[-1] == {
                    "role": "assistant",
                    "content": '{"value":"hel',
                }
                assert [
                    index
                    for index, message in enumerate(messages)
                    if message.get("role") == "system"
                ] == [0]
                yield {
                    "type": "done",
                    "content": 'lo"}',
                    "finish_reason": "stop",
                    "usage": None,
                }
                return
            yield {
                "type": "done",
                "content": "finished",
                "finish_reason": "stop",
                "usage": None,
            }

    async def scenario():
        executed = []
        registry = ToolRegistry()

        async def demo(value: str):
            executed.append(value)
            return value

        registry.register(ToolDef("demo", "demo", {"type": "object"}, demo))
        llm = PrefillLLM()
        agent = ReActAgent("agent", llm, registry, max_iterations=3)

        events = [
            event
            async for event in agent.reply_stream(
                Msg(content=[ContentBlock.text("run")]),
            )
        ]

        assert executed == ["hello"]
        assert llm.calls == 3
        assert any(
            event.get("stage") == "recovering"
            and "assistant prefill" in event.get("message", "")
            for event in events
        )
        assert not any(
            event.get("type") == "chunk" and event.get("content") == 'lo"}'
            for event in events
        )
        historical_calls = [
            call
            for message in agent.context.messages
            for call in message.get("tool_calls", [])
        ]
        assert historical_calls[0]["function"]["arguments"] == '{"value":"hello"}'

    run(scenario())


def test_long_unclosed_tool_string_recovers_when_provider_misreports_stop():
    agent = ReActAgent("agent", Mock(), ToolRegistry())
    raw = '{"write_id":"demo","sequence":0,"content":"' + ("x" * 1200)

    calls, failure = agent._validated_tool_calls(
        [{
            "id": "chunk-stop-mismatch",
            "name": "write_file_chunk",
            "arguments": raw,
        }],
        "stop",
        {"completion_tokens": 1761},
    )

    assert calls is None
    assert failure is not None
    assert failure.code == "tool_call_truncated"
    assert failure.retryable is True
    assert failure.details["finish_reason"] == "stop"
    assert failure.details["finish_reason_mismatch"] is True


def test_short_unclosed_json_remains_nonretryable_invalid_arguments():
    agent = ReActAgent("agent", Mock(), ToolRegistry())

    calls, failure = agent._validated_tool_calls(
        [{"id": "bad-small", "name": "demo", "arguments": '{"path": '}],
        "stop",
        None,
    )

    assert calls is None
    assert failure is not None
    assert failure.code == "invalid_arguments"
    assert failure.retryable is False


def test_openai_stream_preserves_finish_reason_before_usage_only_chunk():
    from types import SimpleNamespace

    tool_delta = SimpleNamespace(
        index=0,
        id="write-1",
        function=SimpleNamespace(name="write_file", arguments='{"path":"x'),
    )
    chunks = [
        SimpleNamespace(
            choices=[SimpleNamespace(
                finish_reason=None,
                delta=SimpleNamespace(
                    reasoning_content=None,
                    content=None,
                    tool_calls=[tool_delta],
                ),
            )],
            usage=None,
        ),
        SimpleNamespace(
            choices=[SimpleNamespace(
                finish_reason="length",
                delta=SimpleNamespace(
                    reasoning_content=None,
                    content=None,
                    tool_calls=None,
                ),
            )],
            usage=None,
        ),
        SimpleNamespace(
            choices=[],
            usage=SimpleNamespace(
                prompt_tokens=10,
                completion_tokens=4096,
                total_tokens=4106,
            ),
        ),
    ]

    class FakeStream:
        def __aiter__(self):
            self._items = iter(chunks)
            return self

        async def __anext__(self):
            try:
                return next(self._items)
            except StopIteration:
                raise StopAsyncIteration

    async def create(**kwargs):
        return FakeStream()

    async def scenario():
        provider = object.__new__(OpenAICompatibleProvider)
        provider.config = LLMConfig(timeout=0)
        provider._estimate_calibration = 1.0
        provider._client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )

        events = [event async for event in provider.chat_stream(
            [{"role": "user", "content": "build"}],
            tools=[],
        )]

        assert events[-1]["type"] == "tool_calls"
        assert events[-1]["finish_reason"] == "length"
        assert events[-1]["tool_call_state"] == "incomplete"
        assert events[-1]["usage"]["completion_tokens"] == 4096

    run(scenario())


def test_llm_stream_idle_timeout_resets_for_each_received_chunk():
    from types import SimpleNamespace

    def chunk(content: str, finish_reason=None):
        return SimpleNamespace(
            choices=[SimpleNamespace(
                finish_reason=finish_reason,
                delta=SimpleNamespace(
                    reasoning_content=None,
                    content=content,
                    tool_calls=None,
                ),
            )],
            usage=None,
        )

    class DelayedStream:
        def __init__(self):
            self.payload = "abcdefghijklmnopqrstuvwxyz0123"
            self.items = iter([
                *(chunk(char) for char in self.payload),
                chunk("", "stop"),
            ])

        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.sleep(0.005)
            try:
                return next(self.items)
            except StopIteration:
                raise StopAsyncIteration

    async def create(**kwargs):
        return DelayedStream()

    async def scenario():
        provider = object.__new__(OpenAICompatibleProvider)
        provider.config = LLMConfig(
            timeout=0,
            connect_timeout=1,
            idle_timeout=0.05,
            overall_timeout=0,
            max_retries=0,
        )
        provider._estimate_calibration = 1.0
        provider._client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )

        events = [event async for event in provider.chat_stream(
            [{"role": "user", "content": "slow but active"}],
        )]

        assert "".join(
            event.get("content", "")
            for event in events
            if event["type"] == "chunk"
        ) == "abcdefghijklmnopqrstuvwxyz0123"
        assert events[-1]["finish_reason"] == "stop"

    run(scenario())


def test_llm_stream_distinguishes_idle_and_overall_timeouts():
    from types import SimpleNamespace

    def content_chunk():
        return SimpleNamespace(
            choices=[SimpleNamespace(
                finish_reason=None,
                delta=SimpleNamespace(
                    reasoning_content=None,
                    content="tick",
                    tool_calls=None,
                ),
            )],
            usage=None,
        )

    class DelayedStream:
        def __init__(self, delay):
            self.delay = delay

        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.sleep(self.delay)
            return content_chunk()

    async def scenario(config, delay, expected):
        async def create(**kwargs):
            return DelayedStream(delay)

        provider = object.__new__(OpenAICompatibleProvider)
        provider.config = config
        provider._estimate_calibration = 1.0
        provider._client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )
        try:
            async for _ in provider.chat_stream([{"role": "user", "content": "wait"}]):
                pass
        except expected:
            return
        raise AssertionError(f"expected {expected.__name__}")

    run(scenario(
        LLMConfig(
            connect_timeout=1,
            idle_timeout=0.02,
            overall_timeout=0,
            max_retries=0,
        ),
        0.08,
        LLMIdleTimeout,
    ))
    run(scenario(
        LLMConfig(
            connect_timeout=1,
            idle_timeout=0.2,
            overall_timeout=0.08,
            max_retries=0,
        ),
        0.02,
        LLMOverallTimeout,
    ))


def test_agent_context_repairs_poisoned_session_tool_history(tmp_path):
    path = tmp_path / "poisoned.json"
    store = SessionStore(path)
    store.save({
        "system_prompt": "system",
        "messages": [
            {"role": "user", "content": "build a page"},
            {
                "role": "assistant",
                "content": "Generating.",
                "tool_calls": [{
                    "id": "write-1",
                    "type": "function",
                    "function": {
                        "name": "write_file",
                        "arguments": '{"path":"page.html","content":"unfinished',
                    },
                }],
            },
            {
                "role": "tool",
                "tool_call_id": "write-1",
                "content": "[ToolInputError] invalid JSON arguments",
            },
            {"role": "user", "content": "hello again"},
        ],
    })

    ctx = AgentContext(system_prompt="system")
    ctx.set_session(str(path))

    assert ctx.load() is True
    assert [message["role"] for message in ctx.messages] == ["user", "assistant", "user"]
    assert not any(message.get("tool_calls") for message in ctx.messages)
    assert "Session recovery" in ctx.messages[1]["content"]
    assert "not executed" in ctx.messages[1]["content"]

    ctx.save()
    replayed = SessionStore(path).load()["messages"]
    assert not any(message.get("tool_calls") for message in replayed)
    assert not any(message.get("role") == "tool" for message in replayed)


def test_agent_context_rejects_noncontiguous_tool_results():
    ctx = AgentContext(system_prompt="system")
    ctx.messages = [
        {"role": "user", "content": "run it"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "call-1",
                "type": "function",
                "function": {"name": "git_status", "arguments": "{}"},
            }],
        },
        {"role": "user", "content": "cancel"},
        {"role": "tool", "tool_call_id": "call-1", "content": "late result"},
    ]

    assert ctx.sanitize_tool_history() is True
    assert [message["role"] for message in ctx.messages] == [
        "user", "assistant", "user",
    ]
    assert "tool_calls" not in ctx.messages[1]
    assert "cancelled tool call" in ctx.messages[1]["content"]


def test_git_tools_reject_path_escape(tmp_path):
    async def scenario():
        registry = ToolRegistry()
        root = tmp_path / "repo"
        outside = tmp_path / ".." / "outside"  # two levels above root
        root.mkdir()
        register_git_tools(registry, str(root))

        result = await registry.execute("git_status", {"path": str(outside)})

        assert "Path escapes workspace" in result["error"]

    run(scenario())


def test_git_runner_uses_blocking_subprocess_for_unicode_cwd(monkeypatch, tmp_path):
    from agent.runtime.tools import git as git_tools

    unicode_cwd = tmp_path / "测试工作区"

    captured = {}

    class FakePopen:
        pid = 123
        returncode = 0

        def __init__(self, args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            kwargs["stdout"].write(b"clean")

        def poll(self):
            return self.returncode

    def fake_popen(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return FakePopen(args, **kwargs)

    monkeypatch.setattr(git_tools.subprocess, "Popen", fake_popen)

    result = run(git_tools._run_git(
        "status",
        cwd=str(unicode_cwd),
    ))

    assert result["output"] == "clean"
    assert captured["args"] == ["git", "status"]
    assert str(captured["kwargs"]["cwd"]) == str(unicode_cwd)
    assert captured["kwargs"]["env"]["GIT_OPTIONAL_LOCKS"] == "0"
    assert captured["kwargs"]["stdout"] != git_tools.subprocess.PIPE
    assert captured["kwargs"]["stderr"] != git_tools.subprocess.PIPE


def test_git_runner_timeout_terminates_process_tree(monkeypatch, tmp_path):
    from agent.runtime.tools import git as git_tools

    terminated = []

    class HangingPopen:
        pid = 456
        returncode = None

        def __init__(self, args, **kwargs):
            pass

        def poll(self):
            return self.returncode

    process = HangingPopen([])
    monkeypatch.setattr(git_tools.subprocess, "Popen", lambda *args, **kwargs: process)

    def terminate(proc):
        terminated.append(proc.pid)
        proc.returncode = -1

    monkeypatch.setattr(git_tools, "_terminate_process_tree", terminate)

    result = git_tools._run_git_blocking(("status",), str(tmp_path), timeout=0)

    assert result["exit_code"] == -1
    assert result["error"] == "[Timeout] git command exceeded 0s"
    assert terminated == [456]


def test_backend_finishes_every_pending_frontend_tool_call():
    pending = {"call-status": "git_status", "call-log": "git_log"}
    events = []

    backend_module._finish_pending_tool_calls(
        pending,
        events.append,
        "[Cancelled] Tool call was cancelled before completion.",
        "cancelled",
    )

    assert pending == {}
    assert [event["call_id"] for event in events] == ["call-status", "call-log"]
    assert all(event["type"] == "tool_result" for event in events)
    assert all(event["code"] == "cancelled" for event in events)
    assert all(event["recoverable"] is False for event in events)


def test_git_runner_real_concurrent_reads_finish_promptly():
    from agent.runtime.tools import git as git_tools

    async def scenario():
        root = str(Path(__file__).parents[1])
        started = time.perf_counter()
        results = await asyncio.gather(*[
            git_tools._run_git(command, *args, cwd=root)
            for _ in range(5)
            for command, args in (
                ("status", ()),
                ("log", ("--max-count=5", "--oneline")),
            )
        ])
        return time.perf_counter() - started, results

    elapsed, results = run(scenario())

    assert elapsed < 5
    assert all(result["exit_code"] == 0 for result in results)
    assert not any("[Timeout]" in result["error"] for result in results)


def test_git_write_tools_require_explicit_environment_opt_in(tmp_path, monkeypatch):
    async def scenario():
        registry = ToolRegistry()
        root = tmp_path / "repo"
        root.mkdir()
        register_git_tools(registry, str(root))

        result = await registry.execute("git_reset", {"mode": "hard", "target": "HEAD"})

        # The model cannot set the backend's environment: the text names the
        # user's approval and tells it not to retry.
        assert result["code"] == "approval_required"
        assert "need the user's approval for this repository" in result["error"]
        assert "AGENT_ALLOW_GIT_WRITE=1" in result["error"]
        assert "to enable git write tools" not in result["error"]

    monkeypatch.delenv("AGENT_ALLOW_GIT_WRITE", raising=False)
    run(scenario())


def test_yolo_is_the_standing_approval_for_git_writes(tmp_path, monkeypatch):
    root = _committed_git_repo(tmp_path, monkeypatch)
    monkeypatch.delenv("AGENT_ALLOW_GIT_WRITE", raising=False)
    (root / "second.txt").write_text("two\n", encoding="utf-8")

    async def scenario():
        registry = ToolRegistry()
        register_git_tools(registry, str(root))

        registry.yolo = True
        staged = await registry.execute("git_add", {"files": "second.txt"})
        assert staged["error"] == ""
        committed = await registry.execute("git_commit", {"message": "second commit"})
        assert committed["error"] == ""
        log = await registry.execute("git_log", {})
        assert "second commit" in log["output"]

        # Leaving YOLO withdraws it: with nobody to ask, the write is refused again.
        registry.yolo = False
        (root / "third.txt").write_text("three\n", encoding="utf-8")
        refused = await registry.execute("git_add", {"files": "third.txt"})
        assert refused["code"] == "approval_required"
        assert "need the user's approval for this repository" in refused["error"]

    run(scenario())


def _committed_git_repo(tmp_path, monkeypatch) -> Path:
    """A one-commit repository whose Git runs ignore the developer's own config."""
    import subprocess

    empty_config = tmp_path / "empty.gitconfig"
    empty_config.write_text("", encoding="utf-8")
    for name, value in {
        "GIT_CONFIG_GLOBAL": str(empty_config),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
        "LC_ALL": "C",
    }.items():
        monkeypatch.setenv(name, value)
    root = tmp_path / "repo"
    root.mkdir()
    for args in (
        ["init", "-q"],
        ["checkout", "-q", "-b", "trunk"],
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    (root / "first.txt").write_text("one\n", encoding="utf-8")
    for args in (["add", "first.txt"], ["commit", "-q", "-m", "first commit"]):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    return root


def test_git_tools_fail_when_git_exits_nonzero(tmp_path, monkeypatch):
    root = _committed_git_repo(tmp_path, monkeypatch)
    monkeypatch.setenv("AGENT_ALLOW_GIT_WRITE", "1")

    async def scenario():
        registry = ToolRegistry()
        register_git_tools(registry, str(root))

        missing = await registry.execute("git_add", {"files": "missing.txt"})
        assert missing["output"] == ""
        assert missing["code"] == "git_failed"
        assert "missing.txt" in missing["error"]
        assert "Staged" not in missing["error"]
        assert missing["details"]["exit_code"] != 0

        for name, args in (
            ("git_show", {"commit": "no-such-ref"}),
            ("git_diff", {"target": "no-such-ref"}),
            ("git_checkout", {"target": "no-such-ref"}),
        ):
            failed = await registry.execute(name, args)
            assert failed["output"] == "", name
            assert failed["code"] == "git_failed", name
            assert "no-such-ref" in failed["error"], name

        # Git never started here, so there is no branch list to report.
        no_repo = await registry.execute("git_branch", {"path": "missing-dir"})
        assert no_repo["output"] == ""
        assert no_repo["code"] == "git_failed"
        assert no_repo["error"]

        # A failure that git explains on stdout keeps that explanation.
        nothing = await registry.execute("git_commit", {"message": "nothing staged"})
        assert nothing["output"] == ""
        assert nothing["code"] == "git_failed"
        assert "nothing to commit" in nothing["error"]

        for name, args in (
            ("git_commit", {"message": ""}),
            ("git_reset", {"mode": "sideways"}),
        ):
            rejected = await registry.execute(name, args)
            assert rejected["output"] == "", name
            assert rejected["code"] == "invalid_arguments", name

        (root / "second.txt").write_text("two\n", encoding="utf-8")
        staged = await registry.execute("git_add", {"files": "second.txt"})
        assert staged["error"] == ""
        assert staged["output"].startswith("Staged: second.txt")
        committed = await registry.execute("git_commit", {"message": "second commit"})
        assert committed["error"] == ""
        log = await registry.execute("git_log", {})
        assert log["error"] == ""
        assert "second commit" in log["output"] and "first commit" in log["output"]

    run(scenario())


def test_git_tools_stage_quoted_paths_and_refuse_option_like_refs(tmp_path, monkeypatch):
    root = _committed_git_repo(tmp_path, monkeypatch)
    monkeypatch.setenv("AGENT_ALLOW_GIT_WRITE", "1")
    (root / "release notes.txt").write_text("notes\n", encoding="utf-8")
    (root / "-dash.txt").write_text("dash\n", encoding="utf-8")
    (root / "plain.txt").write_text("plain\n", encoding="utf-8")

    async def scenario():
        registry = ToolRegistry()
        register_git_tools(registry, str(root))

        staged = await registry.execute("git_add", {"files": '"release notes.txt" -dash.txt'})
        assert staged["error"] == ""
        only_staged = await registry.execute("git_diff", {"target": "--staged"})
        assert "release notes.txt" in only_staged["output"]
        assert "-dash.txt" in only_staged["output"]
        assert "plain.txt" not in only_staged["output"]

        everything = await registry.execute("git_add", {"files": "-A"})
        assert everything["error"] == ""
        assert "plain.txt" in (await registry.execute("git_diff", {"target": "--staged"}))["output"]

        # A value git would read as an option never reaches git.
        for name, args in (
            ("git_show", {"commit": "--output=leak.txt"}),
            ("git_diff", {"target": "--output=leak.txt"}),
            ("git_reset", {"target": "--hard"}),
            ("git_checkout", {"target": "-f"}),
            ("git_push", {"remote": "--receive-pack=evil"}),
            ("git_clone", {"url": "--upload-pack=evil", "path": "copy"}),
        ):
            refused = await registry.execute(name, args)
            assert refused["code"] == "invalid_arguments", name
            assert "must not start with '-'" in refused["error"], name
        assert not (root / "leak.txt").exists()
        assert not (root / "copy").exists()

        (root / "plain.txt").write_text("changed\n", encoding="utf-8")
        restored = await registry.execute("git_checkout", {"target": "--", "files": "plain.txt"})
        assert restored["error"] == ""
        assert (root / "plain.txt").read_text(encoding="utf-8") == "plain\n"

    run(scenario())


def test_git_timeout_is_a_failure_that_marks_partial_output(tmp_path, monkeypatch):
    from agent.runtime.tools import git as git_tools

    class HangingGit:
        pid = 789
        returncode = None

        def __init__(self, args, **kwargs):
            kwargs["stdout"].write(b"* 1234567 newest commit\n")
            kwargs["stderr"].write(b"warning: still walking history\n")

        def poll(self):
            return self.returncode

    def terminate(proc):
        proc.returncode = -9

    monkeypatch.setattr(git_tools.subprocess, "Popen", HangingGit)
    monkeypatch.setattr(git_tools, "_terminate_process_tree", terminate)
    monkeypatch.setattr(git_tools, "_git_timeout", lambda args: 0)
    monkeypatch.setenv("AGENT_ALLOW_GIT_WRITE", "1")

    async def scenario():
        registry = ToolRegistry()
        register_git_tools(registry, str(tmp_path))

        read = await registry.execute("git_log", {"max_count": 5})
        assert read["output"] == ""
        assert read["code"] == "git_timeout"
        assert "[Timeout] git command exceeded 0s" in read["error"]
        assert "partial" in read["error"]
        assert "* 1234567 newest commit" in read["error"]
        assert "warning: still walking history" in read["error"]
        assert read["partial"] is True
        assert read["retryable"] is True

        # A stopped write may have taken effect, so it is not offered for retry.
        write = await registry.execute("git_push", {"remote": "origin", "branch": "trunk"})
        assert write["code"] == "git_timeout"
        assert write["retryable"] is False
        assert "repository state" in write["recovery_hint"]

    run(scenario())


def test_git_tools_require_only_arguments_without_a_default(tmp_path, monkeypatch):
    root = _committed_git_repo(tmp_path, monkeypatch)
    monkeypatch.setenv("AGENT_ALLOW_GIT_WRITE", "1")

    async def scenario():
        registry = ToolRegistry()
        register_git_tools(registry, str(root))

        # No remote is configured, so these reach git and fail there instead of
        # being rejected for arguments their descriptions call optional.
        for name in ("git_push", "git_pull"):
            result = await registry.execute(name, {})
            assert result["code"] == "git_failed", (name, result)

        for name in ("git_log", "git_show", "git_add", "git_checkout", "git_reset"):
            result = await registry.execute(name, {})
            assert result["error"] == "", (name, result)

        for name, argument in (
            ("git_commit", "message"),
            ("git_clone", "url"),
            ("git_revert", "commit"),
        ):
            result = await registry.execute(name, {})
            assert result["code"] == "invalid_arguments", (name, result)
            assert argument in result["error"], name

    run(scenario())


def test_git_show_lists_changed_files_without_the_patch(tmp_path, monkeypatch):
    root = _committed_git_repo(tmp_path, monkeypatch)

    async def scenario():
        registry = ToolRegistry()
        register_git_tools(registry, str(root))

        shown = await registry.execute("git_show", {"commit": "HEAD"})

        assert shown["error"] == ""
        assert "first commit" in shown["output"]
        assert "first.txt" in shown["output"]
        assert "+one" not in shown["output"]

    run(scenario())


def test_react_agent_does_not_store_reasoning_in_prompt_history():
    class FakeLLM:
        def __init__(self):
            self.prompts = []

        async def chat_stream(self, messages, tools):
            self.prompts.append(messages)
            yield {"type": "reasoning", "content": "hidden chain"}
            yield {"type": "chunk", "content": "visible answer"}
            yield {
                "type": "done",
                "content": "visible answer",
                "reasoning_content": "hidden chain",
                "usage": None,
            }

    async def scenario():
        registry = ToolRegistry()
        llm = FakeLLM()
        agent = ReActAgent("agent", llm, registry)
        agent.context.show_reasoning = True

        events = [event async for event in agent.reply_stream(Msg(content=[ContentBlock.text("hi")]))]

        assert any(event["type"] == "reasoning" for event in events)
        # Reasoning is only stored across tool calls; without tools, not saved
        msgs = agent.context.messages
        assert msgs[-1]["role"] == "assistant"
        assert msgs[-1]["content"] == "visible answer"
        assert context_module._valid_message_timestamp(msgs[-1]["timestamp"]) is not None

    run(scenario())


def test_react_agent_turns_stream_cancel_into_error_event():
    class FakeLLM:
        async def chat_stream(self, messages, tools):
            yield {"type": "chunk", "content": "partial"}
            raise asyncio.CancelledError()

    async def scenario():
        agent = ReActAgent("agent", FakeLLM(), ToolRegistry(), max_iterations=1)
        events = [event async for event in agent.reply_stream(Msg(content=[ContentBlock.text("hi")]))]

        assert next(event for event in events if event["type"] == "chunk")["content"] == "partial"
        assert events[-2]["type"] == "error"
        assert "cancelled" in events[-2]["message"].lower()
        assert events[-1]["type"] == "done"

    run(scenario())


def test_react_cancel_during_tool_repairs_history_before_next_turn():
    class ToolThenReplyLLM:
        def __init__(self):
            self.calls = 0
            self.prompts = []

        async def chat_stream(self, messages, tools):
            self.calls += 1
            self.prompts.append(messages)
            if self.calls == 1:
                yield {
                    "type": "tool_calls",
                    "calls": [{
                        "id": "slow-1",
                        "name": "slow_tool",
                        "arguments": "{}",
                    }],
                    "content": "",
                    "reasoning_content": "",
                    "usage": None,
                }
                return
            yield {"type": "done", "content": "recovered", "usage": None}

    async def scenario():
        started = asyncio.Event()

        async def slow_tool():
            started.set()
            await asyncio.Event().wait()

        registry = ToolRegistry()
        registry.register(ToolDef(
            "slow_tool",
            "wait",
            {"type": "object", "properties": {}},
            slow_tool,
        ))
        llm = ToolThenReplyLLM()
        agent = ReActAgent("agent", llm, registry, max_iterations=2)

        first_events = []

        async def consume_first():
            async for event in agent.reply_stream(
                Msg(content=[ContentBlock.text("start")])
            ):
                first_events.append(event)

        execution = asyncio.create_task(consume_first())
        await started.wait()
        execution.cancel()
        await execution

        assert any(event.get("code") == "cancelled" for event in first_events)
        assert not any(message.get("role") == "tool" for message in agent.context.messages)
        assert not any(message.get("tool_calls") for message in agent.context.messages)

        second_events = [
            event async for event in agent.reply_stream(
                Msg(content=[ContentBlock.text("continue")])
            )
        ]
        assert second_events[-1]["type"] == "done"
        assert llm.calls == 2
        assert not any(message.get("role") == "tool" for message in llm.prompts[-1])

    run(scenario())


def test_llm_client_delegates_to_injected_provider():
    class FakeProvider:
        def __init__(self):
            self.chat_called = False

        async def chat_stream(self, messages, tools=None):
            yield {"type": "chunk", "content": "ok"}
            yield {"type": "done", "content": "ok"}

        async def chat(self, messages, tools=None):
            self.chat_called = True
            return {"content": "ok", "tool_calls": [], "finish_reason": "stop"}

        def estimate_tokens(self, messages):
            return 123

    async def scenario():
        provider = FakeProvider()
        client = LLMClient(LLMConfig(), provider=provider)
        events = [event async for event in client.chat_stream([{"role": "user", "content": "hi"}])]
        result = await client.chat([{"role": "user", "content": "hi"}])

        assert events[-1]["content"] == "ok"
        assert result["content"] == "ok"
        assert provider.chat_called is True
        assert client.estimate_tokens([{"role": "user", "content": "hi"}]) == 123

    run(scenario())


def test_llm_client_switches_model_and_rebuilds_provider():
    created = []

    class FakeProvider:
        def __init__(self, config):
            self.config = config
            created.append(config)

    client = LLMClient(
        LLMConfig(model="local-a", base_url="http://localhost:8081/v1"),
        provider_factory=FakeProvider,
    )

    client.switch_model(
        "local-b",
        "http://localhost:8082/v1",
        temperature=1.0,
        top_p=0.95,
        top_k=64,
    )

    assert client.config.model == "local-b"
    assert client.config.base_url == "http://localhost:8082/v1"
    assert client.config.temperature == 1.0
    assert client.config.top_p == 0.95
    assert client.config.top_k == 64
    assert client.provider.config is client.config
    assert [config.model for config in created] == ["local-a", "local-b"]


def test_completion_kwargs_support_scoped_generation_overrides():
    provider = object.__new__(OpenAICompatibleProvider)
    provider.config = LLMConfig(
        model="work-model",
        temperature=1.0,
        max_tokens=32_768,
        top_p=0.95,
        repetition_penalty_parameter="repetition_penalty",
    )

    kwargs = provider._completion_kwargs(
        [{"role": "user", "content": "晚上好"}],
        generation_overrides={
            "temperature": 0.65,
            "max_tokens": 8192,
            "top_p": 0.9,
            "repetition_penalty": 1.05,
        },
    )

    assert kwargs["temperature"] == 0.65
    assert kwargs["max_tokens"] == 8192
    assert kwargs["top_p"] == 0.9
    assert kwargs["extra_body"] == {"repetition_penalty": 1.05}
    assert provider.config.temperature == 1.0
    assert provider.config.max_tokens == 32_768

    provider.config = LLMConfig(
        repetition_penalty=1.1,
        repetition_penalty_parameter="repeat_penalty",
    )
    llama_kwargs = provider._completion_kwargs([{"role": "user", "content": "hi"}])
    assert llama_kwargs["extra_body"] == {"repeat_penalty": 1.1}

    provider.config = LLMConfig(repetition_penalty=1.1)
    unconfigured_kwargs = provider._completion_kwargs([{"role": "user", "content": "hi"}])
    assert "extra_body" not in unconfigured_kwargs


def test_completion_kwargs_can_disable_deepseek_thinking():
    provider = object.__new__(OpenAICompatibleProvider)
    provider.config = LLMConfig(model="deepseek-v4-flash", thinking_mode="disabled")
    kwargs = provider._completion_kwargs([{"role": "user", "content": "hi"}])
    assert kwargs["extra_body"]["thinking"] == {"type": "disabled"}

    # 默认不发送——现网主链路行为零变化
    provider.config = LLMConfig(model="deepseek-v4-flash")
    kwargs = provider._completion_kwargs([{"role": "user", "content": "hi"}])
    assert "thinking" not in kwargs.get("extra_body", {})

    # 非 deepseek-v4 家族忽略
    provider.config = LLMConfig(model="gemma-judge", thinking_mode="disabled")
    kwargs = provider._completion_kwargs([{"role": "user", "content": "hi"}])
    assert "thinking" not in kwargs.get("extra_body", {})


def test_chat_limited_keeps_thinking_enabled_for_required_models():
    provider = object.__new__(OpenAICompatibleProvider)
    provider.config = LLMConfig(
        model="thinking-only",
        capabilities=frozenset({"reasoning", "thinking-required"}),
    )
    captured = []

    async def fake_completion(kwargs):
        captured.append(kwargs)
        return {"content": "{}"}

    provider._chat_completion = fake_completion

    result = run(provider.chat_limited(
        [{"role": "user", "content": "review"}],
        max_tokens=256,
        disable_thinking=True,
    ))

    assert result == {"content": "{}"}
    assert "extra_body" not in captured[0]


def test_chat_limited_can_lower_reasoning_effort_for_bounded_tasks():
    provider = object.__new__(OpenAICompatibleProvider)
    provider.config = LLMConfig(
        model="deepseek-v4-flash",
        capabilities=frozenset({"reasoning"}),
        reasoning_effort="max",
    )
    captured = []

    async def fake_completion(kwargs):
        captured.append(kwargs)
        return {"content": "{}"}

    provider._chat_completion = fake_completion

    result = run(provider.chat_limited(
        [{"role": "user", "content": "repair"}],
        max_tokens=512,
        reasoning_effort="low",
    ))

    assert result == {"content": "{}"}
    assert captured[0]["reasoning_effort"] == "low"
    assert captured[0]["max_tokens"] == 384_000


def test_chat_limited_retries_once_without_rejected_disable_thinking(monkeypatch):
    import agent.runtime.llm as llm_module

    class FakeBadRequestError(Exception):
        pass

    monkeypatch.setattr(llm_module, "BadRequestError", FakeBadRequestError)
    provider = object.__new__(OpenAICompatibleProvider)
    provider.config = LLMConfig(model="unadvertised-thinking-only")
    captured = []

    async def fake_completion(kwargs):
        captured.append(kwargs)
        if len(captured) == 1:
            raise FakeBadRequestError(
                "The value of the enable_thinking parameter is restricted to True."
            )
        return {"content": "{}"}

    provider._chat_completion = fake_completion

    result = run(provider.chat_limited(
        [{"role": "user", "content": "review"}],
        max_tokens=256,
        disable_thinking=True,
    ))

    assert result == {"content": "{}"}
    assert captured[0]["extra_body"]["chat_template_kwargs"] == {
        "enable_thinking": False,
    }
    assert "extra_body" not in captured[1]
    assert len(captured) == 2


def test_bad_request_is_not_retried(monkeypatch):
    import agent.runtime.llm as llm_module

    class FakeBadRequestError(Exception):
        pass

    monkeypatch.setattr(llm_module, "BadRequestError", FakeBadRequestError)
    provider = object.__new__(OpenAICompatibleProvider)
    provider.config = LLMConfig(max_retries=2, overall_timeout=0)
    attempts = 0

    async def fail():
        nonlocal attempts
        attempts += 1
        raise FakeBadRequestError("invalid parameter")

    try:
        run(provider._with_retry(fail))
    except FakeBadRequestError:
        pass
    else:
        raise AssertionError("expected deterministic bad request to propagate")

    assert attempts == 1


def test_qwen38_profile_declares_thinking_required():
    from agent.cli.models import model_profiles

    profile = model_profiles()["qwen3.8-max"]

    assert "thinking-required" in profile.capabilities


def test_glm53_flash_profile_uses_zhipu_env_and_multimodal_defaults():
    from agent.cli.models import model_profiles

    profile = model_profiles()["glm-5.3-flash"]

    assert profile.base_url == "https://open.bigmodel.cn/api/paas/v4"
    assert profile.api_key_env == "ZHIPU_API_KEY"
    assert profile.catalog_provider == "zhipu"
    assert profile.context_limit == 1_000_000
    assert profile.capabilities == frozenset({
        "tools", "reasoning", "thinking-required", "vision", "streaming",
    })
    assert profile.temperature == 1.0
    assert profile.top_p == 0.95
    assert profile.max_tokens == 131_072


def test_hy4_preview_profile_uses_hunyuan_env_and_defaults(monkeypatch):
    from agent.cli.models import model_profiles

    monkeypatch.setenv("HUNYUAN_BASE_URL", "https://tokenhub-intl.tencentmaas.com/v1")

    profile = model_profiles()["hy4-preview"]

    assert profile.base_url == "https://tokenhub-intl.tencentmaas.com/v1"
    assert profile.api_key_env == "HUNYUAN_API_KEY"
    assert profile.catalog_provider == "hunyuan"
    assert profile.context_limit == 1_048_576
    assert profile.capabilities == frozenset({"tools", "reasoning", "streaming"})
    assert profile.temperature == 0.9
    assert profile.top_p == 0.95
    assert profile.max_tokens == 65_536


def test_model_profiles_include_separate_local_endpoints(monkeypatch):
    from agent.cli.models import context_limit_for_model, model_profiles

    monkeypatch.setenv("QWEN_BASE_URL", "http://localhost:18081/v1")
    monkeypatch.setenv("GEMMA_BASE_URL", "http://localhost:18082/v1")
    profiles = model_profiles()

    assert profiles["Qwen3.6-35B-A3B"].base_url == "http://localhost:18081/v1"
    assert profiles["gemma-4-12B-it"].base_url == "http://localhost:18082/v1"
    assert profiles["gemma-4-12B-it"].generation_settings() == {
        "temperature": 1.0,
        "max_tokens": 4096,
        "top_p": 0.95,
        "top_k": 64,
        "min_p": None,
        "presence_penalty": None,
        "repetition_penalty": None,
        "vision_detail": "auto",
        "vision_preprocess": None,
    }
    assert profiles["Qwen3.6-35B-A3B"].generation_settings() == {
        "temperature": 1.0,
        "max_tokens": 32_768,
        "top_p": 0.95,
        "top_k": 20,
        "min_p": 0.0,
        "presence_penalty": 1.5,
        "repetition_penalty": 1.0,
        "repetition_penalty_parameter": "repeat_penalty",
        "vision_detail": "auto",
        "vision_preprocess": None,
    }
    assert context_limit_for_model("gemma-4-12B-it") == 32_768


def test_model_selection_persists_and_restores(monkeypatch, tmp_path):
    from agent.cli.model_preferences import (
        load_selected_model,
        resolve_startup_model,
        save_selected_model,
    )

    settings = tmp_path / "settings.json"
    settings.write_text('{"keep_me": true}', encoding="utf-8")
    monkeypatch.setenv("AGENT_SETTINGS_PATH", str(settings))
    monkeypatch.setenv("GEMMA_BASE_URL", "http://localhost:18083/v1")

    saved_path = save_selected_model("gemma-4-12B-it")
    model, base_url = resolve_startup_model(
        "Qwen3.6-35B-A3B",
        "http://localhost:8081/v1",
    )
    saved = json.loads(settings.read_text(encoding="utf-8"))

    assert saved_path == settings
    assert load_selected_model() == "gemma-4-12B-it"
    assert model == "gemma-4-12B-it"
    assert base_url == "http://localhost:18083/v1"
    assert saved["keep_me"] is True
    assert saved["selected_model"] == "gemma-4-12B-it"


def test_model_selection_ignores_corrupt_or_unknown_settings(monkeypatch, tmp_path):
    from agent.cli.model_preferences import load_selected_model, resolve_startup_model

    settings = tmp_path / "settings.json"
    monkeypatch.setenv("AGENT_SETTINGS_PATH", str(settings))
    monkeypatch.setenv("QWEN_BASE_URL", "http://localhost:18081/v1")

    settings.write_text("{broken", encoding="utf-8")
    assert load_selected_model() is None

    settings.write_text('{"selected_model": "removed-model"}', encoding="utf-8")
    model, base_url = resolve_startup_model(
        "Qwen3.6-35B-A3B",
        "http://wrong.example/v1",
    )
    assert model == "Qwen3.6-35B-A3B"
    assert base_url == "http://localhost:18081/v1"


def test_prompt_profiles_keep_lyra_style_inside_agent_contract():
    from agent.runtime.prompts import (
        DEFAULT_PROMPT_PROFILE,
        DEFAULT_SYSTEM_PROMPT,
        get_prompt_profile,
        prompt_profiles,
    )

    profiles = prompt_profiles()
    assert set(profiles) == {"lyra"}
    prompt = get_prompt_profile("lyra").system_prompt()
    assert DEFAULT_SYSTEM_PROMPT == prompt
    assert DEFAULT_PROMPT_PROFILE == "lyra"
    assert get_prompt_profile(DEFAULT_PROMPT_PROFILE).persona_layer == "identity"
    assert "本地运行的工具型 AI agent" in prompt
    assert '先调用 skill_view(name="astra-core")' in prompt
    assert "Everyday conversation" in prompt
    assert "Do not assume the user's name" in prompt
    assert "<message_time>" in prompt
    assert "不要在回复中复述" in prompt
    assert "[Mode overlay]" not in prompt


def test_bar_mode_isolates_and_persists_separate_bar_sessions(monkeypatch, tmp_path):
    from agent.cli import sessions as session_helpers
    from agent.runtime.bar_mode import (
        BAR_MODE_PROMPT,
        BAR_MODEL_TOOL_NAMES,
        BAR_STREAM_TOOL_NAMES,
        BAR_TURN_TOOL_NAME,
        BarModeController,
    )
    from agent.runtime.tools.bar import register_bar_tools

    monkeypatch.setattr(session_helpers, "SESSION_DIR", tmp_path / ".sessions")
    monkeypatch.setenv("HINDSIGHT_ENABLED", "0")

    memory_store = Mock()
    skill_store = Mock()
    task_store = Mock()
    agent = ReActAgent(
        "agent",
        Mock(),
        ToolRegistry(),
        memory_store=memory_store,
        skill_store=skill_store,
        task_store=task_store,
    )
    agent.context.add_user("unfinished work")
    agent.context.total_prompt_tokens = 123
    work_context = agent.context
    first_path = session_helpers.bar_session_path("bar_first")
    second_path = session_helpers.bar_session_path("bar_second")

    mode = BarModeController(agent)
    register_bar_tools(agent.tools, mode)
    assert agent._available_tool_schemas("serve a drink", set(), {}, set()) == []
    assert mode.enter(first_path) is True
    assert mode.active is True
    assert agent.context is not work_context
    assert agent.context.system_prompt == BAR_MODE_PROMPT
    assert "Do not assume a prior relationship" in BAR_MODE_PROMPT
    assert "distinguish rumors from facts" in BAR_MODE_PROMPT
    assert "Do not repeat a successful action" in BAR_MODE_PROMPT
    assert "persistent memory" in BAR_MODE_PROMPT
    assert "/bar leave" in BAR_MODE_PROMPT
    assert agent.context.session_path == str(first_path)
    assert agent.context.messages == []
    assert agent.context.show_reasoning is work_context.show_reasoning
    assert agent.memory_store is None
    assert agent.skill_store is None
    assert agent.task_store is None
    assert agent.tools_enabled is True
    assert agent.tool_allowlist == set(BAR_MODEL_TOOL_NAMES)
    assert agent.forced_tool_name == BAR_TURN_TOOL_NAME
    assert [item["function"]["name"] for item in agent._available_tool_schemas("记住这个", set(), {}, set())] == [
        "bar_turn",
    ]
    assert mode.set_output_mode("stream") is True
    assert mode.output_mode == "stream"
    assert agent.forced_tool_name is None
    assert agent.tool_allowlist == set(BAR_STREAM_TOOL_NAMES)
    assert "当前为 STREAM REACT 输出模式" in agent.context.system_prompt
    assert "不要调用 `bar_turn`" in agent.context.system_prompt
    assert "工具调用 → 执行 → 最终回复" in agent.context.system_prompt
    assert "绝对不要把 `serve_drink(...)` 等调用语法写进普通正文" in agent.context.system_prompt
    assert "应先完成对应杯子动作" not in agent.context.system_prompt
    assert sorted(item["function"]["name"] for item in agent._available_tool_schemas(
        "换一杯并调暗灯光", set(), {}, set(),
    )) == sorted(BAR_STREAM_TOOL_NAMES)
    assert mode.set_output_mode("atomic") is True
    assert agent.context.system_prompt == BAR_MODE_PROMPT
    assert agent.tool_allowlist == set(BAR_MODEL_TOOL_NAMES)
    assert agent.forced_tool_name == BAR_TURN_TOOL_NAME
    empty_refill = asyncio.run(agent.tools.execute("refill_drink", {}))
    assert "no current drink" in empty_refill.get("error", "").lower()
    empty_rename = asyncio.run(agent.tools.execute("rename_drink", {"name": "空杯"}))
    assert "no current drink" in empty_rename.get("error", "").lower()
    assert mode.drink.active is False
    assert mode.ambiance.to_event() == {
        "weather": "rain", "power": "stable", "music": "low_synth", "radio": "static",
    }
    assert mode.shift.phase == "early"
    assert mode.shift.turn_count == 0
    assert mode.lyra_glass.active is False

    ambiance_result = asyncio.run(agent.tools.execute("set_ambiance", {
        "weather": "downpour", "power": "flicker", "radio": "local_news",
    }))
    assert ambiance_result.get("error", "") == ""
    assert mode.ambiance.weather == "downpour"
    assert mode.ambiance.power == "flicker"
    assert mode.ambiance.revision == 1
    lyra_pour = asyncio.run(agent.tools.execute("pour_lyra_drink", {
        "name": "夜班清水", "note": "冰水与一片真柠檬",
    }))
    assert lyra_pour.get("error", "") == ""
    assert mode.lyra_glass.name == "夜班清水"
    assert mode.lyra_glass.fill == 3
    lyra_sip = asyncio.run(agent.tools.execute("sip_lyra_drink", {}))
    assert lyra_sip.get("error", "") == ""
    assert mode.lyra_glass.fill == 2

    first_drink = mode.serve_drink("夜雨续行", "蜂蜜、柠檬和一点朗姆", "amber", "hot")
    assert first_drink.fill == 3
    assert mode.sip()[0].fill == 2
    assert mode.drink.revision == 2
    assert mode.drink.acknowledged_revision == 0
    scene_prompt, _ = asyncio.run(agent._prepare_prompt_for_llm("味道怎么样", None))
    assert "fill: 2/3" in scene_prompt[0]["content"]
    assert "new_scene_event: true" in scene_prompt[0]["content"]
    assert "last_action: sipped" in scene_prompt[0]["content"]
    assert "last_actor: user" in scene_prompt[0]["content"]
    assert "previous_fill: 3/3" in scene_prompt[0]["content"]
    assert "ambiance: weather=downpour, power=flicker" in scene_prompt[0]["content"]
    assert "new_ambiance_event: true" in scene_prompt[0]["content"]
    assert "lyra_drink_name: 夜班清水" in scene_prompt[0]["content"]
    assert "lyra_glass_state: two_sips_left (2/3)" in scene_prompt[0]["content"]
    assert "new_lyra_glass_event: true" in scene_prompt[0]["content"]
    assert "BAR SCENE STATE" not in agent.context.system_prompt
    assert all("BAR SCENE STATE" not in str(message.get("content", "")) for message in agent.context.messages)
    agent.context.add_user("我这杯还剩多少？")
    adjacent_prompt, _ = asyncio.run(agent._prepare_prompt_for_llm("我这杯还剩多少？", None))
    assert "SYSTEM-SUPPLIED CURRENT RUNTIME STATE" in adjacent_prompt[-1]["content"]
    assert "BAR STATE r2: 夜雨续行" in adjacent_prompt[-1]["content"]
    assert "glass: TWO SIPS LEFT (2/3)" in adjacent_prompt[-1]["content"]
    assert adjacent_prompt[-1]["content"].endswith("我这杯还剩多少？")
    assert agent.context.messages[-1]["content"] == "我这杯还剩多少？"
    agent.context.messages.pop()
    mode.acknowledge_scene()
    acknowledged_prompt, _ = asyncio.run(agent._prepare_prompt_for_llm("继续聊", None))
    assert "new_scene_event: false" in acknowledged_prompt[0]["content"]
    assert "last_action:" not in acknowledged_prompt[0]["content"]
    assert "new_ambiance_event: false" in acknowledged_prompt[0]["content"]
    assert "new_lyra_glass_event: false" in acknowledged_prompt[0]["content"]

    for _ in range(4):
        mode.complete_turn()
    assert mode.shift.turn_count == 4
    assert mode.shift.phase == "deep"
    assert mode.shift.previous_phase == "early"
    phase_prompt, _ = asyncio.run(agent._prepare_prompt_for_llm("夜深了吗", None))
    assert "night_phase: deep" in phase_prompt[0]["content"]
    assert "new_phase_event: true" in phase_prompt[0]["content"]
    assert "phase_change: early -> deep" in phase_prompt[0]["content"]

    agent.context.add_user("one private shift")
    agent.context.add_assistant("one private answer")
    mode.switch(second_path)
    assert agent.context.messages == []
    assert mode.drink.active is False
    assert mode.ambiance.weather == "rain"
    assert mode.lyra_glass.active is False
    assert mode.shift.turn_count == 0
    assert session_helpers.bar_session_exists("bar_first") is True
    assert session_helpers.bar_session_msg_count("bar_first") == 2
    assert "bar_first" not in session_helpers.list_sessions()
    assert "bar_second" not in session_helpers.list_sessions()

    agent.context.add_user("second private shift")
    agent.context.add_assistant("second private answer")
    unnamed_result = asyncio.run(agent.tools.execute("serve_drink", {
        "note": "蓝柑与苏打",
        "tone": "cyan",
        "temperature": "cold",
    }))
    assert unnamed_result.get("error", "") == ""
    assert mode.drink.name == "未命名调饮 #1"
    assert mode.drink.unnamed_counter == 1
    original_note = mode.drink.note
    rename_result = asyncio.run(agent.tools.execute("rename_drink", {"name": "数据丢包"}))
    assert rename_result.get("error", "") == ""
    assert mode.drink.name == "数据丢包"
    assert mode.drink.note == original_note
    assert mode.drink.fill == 3
    assert mode.drink.last_action == "renamed"
    mode.sip()
    mode.sip()
    mode.sip()
    assert mode.drink.fill == 0
    refill_result = asyncio.run(agent.tools.execute("refill_drink", {}))
    assert refill_result.get("error", "") == ""
    assert mode.drink.fill == 3
    assert mode.drink.last_action == "refilled"
    assert mode.drink.last_actor == "lyra"
    assert mode.drink.previous_fill == 0
    refill_prompt, _ = asyncio.run(agent._prepare_prompt_for_llm("满上了吗", None))
    assert "fill: 3/3" in refill_prompt[0]["content"]
    assert "last_action: refilled" in refill_prompt[0]["content"]
    assert mode.leave() is True
    assert mode.active is False
    assert agent.context is work_context
    assert agent.context.messages[-1]["content"] == "unfinished work"
    assert agent.context.total_prompt_tokens == 123
    assert agent.memory_store is memory_store
    assert agent.skill_store is skill_store
    assert agent.task_store is task_store
    assert agent.tools_enabled is True
    assert agent.tool_allowlist is None
    assert agent.runtime_context_provider is None
    assert agent.runtime_turn_context_provider is None
    assert agent.forced_tool_name is None
    assert session_helpers.bar_session_msg_count("bar_second") == 2
    assert session_helpers.list_bar_sessions() == ["bar_second", "bar_first"]

    assert mode.enter(first_path) is True
    assert [message["content"] for message in agent.context.messages] == [
        "one private shift",
        "one private answer",
    ]
    assert mode.drink.name == "夜雨续行"
    assert mode.drink.fill == 2
    assert mode.drink.revision == 2
    assert mode.drink.acknowledged_revision == 2
    assert mode.ambiance.weather == "downpour"
    assert mode.ambiance.power == "flicker"
    assert mode.lyra_glass.name == "夜班清水"
    assert mode.lyra_glass.fill == 2
    assert mode.shift.phase == "deep"
    assert mode.shift.turn_count == 4
    assert mode.leave() is True


def test_bar_mode_parks_external_memory_provider_and_disables_sync(monkeypatch, tmp_path):
    from agent.cli import sessions as session_helpers
    from agent.runtime.bar_mode import BarModeController

    monkeypatch.setattr(session_helpers, "SESSION_DIR", tmp_path / ".sessions")

    agent = ReActAgent("agent", Mock(), ToolRegistry(), memory_store=Mock())
    external = Mock()
    agent.external_memory_provider = external
    work_context = agent.context
    bar_path = session_helpers.bar_session_path("bar_isolated")

    mode = BarModeController(agent)
    assert mode.enter(bar_path) is True

    # In bar mode the external provider is parked so Hindsight session sync
    # cannot leak fictional bar RP into the shared bank.
    assert agent.external_memory_provider is None
    sync_outcome = asyncio.run(agent.sync_external_session(reason="turn"))
    assert sync_outcome["decision"] == "unavailable"
    external.sync_session.assert_not_called()

    # Leaving restores the provider and the work context.
    assert mode.leave() is True
    assert agent.external_memory_provider is external
    assert agent.context is work_context


def test_bar_turn_commits_reply_and_scene_actions_atomically(tmp_path):
    from agent.runtime.bar_mode import BarModeController
    from agent.runtime.tools.bar import register_bar_tools

    class FakeLLM:
        def __init__(self):
            self.tool_choice = None
            self.generation_overrides = None

        async def chat_stream(
            self, messages, tools, tool_choice=None, generation_overrides=None,
        ):
            self.tool_choice = tool_choice
            self.generation_overrides = generation_overrides
            # Text emitted outside the forced atomic tool must never leak to UI
            # or durable conversation history.
            yield {"type": "chunk", "content": "premature narration"}
            yield {
                "type": "tool_calls",
                "calls": [{
                    "id": "bar-call-1",
                    "name": "bar_turn",
                    "arguments": json.dumps({
                        "reply": "*冰块轻响一声* 这杯叫「旧城余烬」。",
                        "actions": [{
                            "type": "serve_drink",
                            "name": "旧城余烬",
                            "note": "龙舌兰与干柠檬，留下微弱烟熏气息",
                            "tone": "amber",
                            "temperature": "room",
                        }],
                    }, ensure_ascii=False),
                }],
                "content": "premature narration",
                "reasoning_content": "",
                "usage": None,
            }

    registry = ToolRegistry()
    llm = FakeLLM()
    agent = ReActAgent("agent", llm, registry, max_iterations=4)
    mode = BarModeController(agent)
    register_bar_tools(registry, mode)
    assert mode.enter(tmp_path / "bar_atomic.jsonl") is True

    async def collect_events():
        return [event async for event in agent.reply_stream(
            Msg(content=[ContentBlock.text("试试别的吧")]),
        )]

    events = asyncio.run(collect_events())

    visible = [event["content"] for event in events if event["type"] == "chunk"]
    assert visible == ["*冰块轻响一声* 这杯叫「旧城余烬」。"]
    assert mode.drink.name == "旧城余烬"
    assert mode.drink.note == "龙舌兰与干柠檬，留下微弱烟熏气息"
    assert mode.drink.fill == 3
    assert llm.tool_choice == {"type": "function", "function": {"name": "bar_turn"}}
    assert llm.generation_overrides == {
        "temperature": 0.65,
        "top_p": 0.9,
        "max_tokens": 8192,
        "repetition_penalty": 1.05,
    }
    assert "premature narration" not in str(agent.context.messages)
    assert agent.context.messages[-1]["role"] == "assistant"
    assert agent.context.messages[-1]["content"] == "*冰块轻响一声* 这杯叫「旧城余烬」。"
    assert context_module._valid_message_timestamp(
        agent.context.messages[-1]["timestamp"]
    ) is not None


def test_bar_stream_mode_emits_chunks_without_forced_atomic_tool(tmp_path):
    from agent.runtime.bar_mode import BarModeController
    from agent.runtime.tools.bar import register_bar_tools

    class StreamingLLM:
        def __init__(self):
            self.tool_choice = "unset"
            self.generation_overrides = None

        async def chat_stream(
            self, messages, tools, tool_choice=None, generation_overrides=None,
        ):
            self.tool_choice = tool_choice
            self.generation_overrides = generation_overrides
            yield {"type": "chunk", "content": "先让雨声陪你坐一会儿。"}
            yield {
                "type": "message_end",
                "content": "先让雨声陪你坐一会儿。",
                "reasoning_content": "",
                "usage": None,
            }

    llm = StreamingLLM()
    agent = ReActAgent("agent", llm, ToolRegistry(), max_iterations=3)
    mode = BarModeController(agent, "stream")
    register_bar_tools(agent.tools, mode)
    assert mode.enter(tmp_path / "bar_stream.jsonl") is True

    async def collect_events():
        return [event async for event in agent.reply_stream(
            Msg(content=[ContentBlock.text("晚上好")]),
        )]

    events = asyncio.run(collect_events())
    assert [event["content"] for event in events if event["type"] == "chunk"] == [
        "先让雨声陪你坐一会儿。",
    ]
    assert llm.tool_choice is None
    assert llm.generation_overrides == {
        "temperature": 0.65,
        "top_p": 0.9,
        "max_tokens": 8192,
        "repetition_penalty": 1.05,
    }
    assert agent.forced_tool_name is None
    assert agent.context.messages[-1]["content"] == "先让雨声陪你坐一会儿。"
    assert mode.leave() is True
    assert agent.generation_overrides_provider is None
    assert agent.finalize_after_tools_provider is None


def test_bar_stream_scene_action_uses_standard_react_followup(tmp_path):
    from agent.runtime.bar_mode import BAR_GENERATION_OVERRIDES, BarModeController
    from agent.runtime.tools.bar import register_bar_tools

    class ToolThenReplyLLM:
        def __init__(self):
            self.calls = 0
            self.generation_overrides = []
            self.prompts = []

        async def chat_stream(
            self, messages, tools, tool_choice=None, generation_overrides=None,
        ):
            self.calls += 1
            self.generation_overrides.append(generation_overrides)
            self.prompts.append(messages)
            if self.calls == 1:
                yield {
                    "type": "tool_calls",
                    "calls": [{
                        "id": "serve-before-reply",
                        "name": "serve_drink",
                        "arguments": json.dumps({
                            "name": "午夜回路",
                            "note": "浓郁合成可可与肉桂，留下温热甜香",
                            "tone": "amber",
                            "temperature": "hot",
                        }, ensure_ascii=False),
                    }],
                    "content": "",
                    "reasoning_content": "",
                    "usage": None,
                }
                return
            yield {"type": "chunk", "content": "*热气从杯沿升起* 这次真的端上来了。"}
            yield {
                "type": "message_end",
                "content": "*热气从杯沿升起* 这次真的端上来了。",
                "reasoning_content": "",
                "usage": None,
            }

    llm = ToolThenReplyLLM()
    agent = ReActAgent("agent", llm, ToolRegistry(), max_iterations=3)
    mode = BarModeController(agent, "stream")
    register_bar_tools(agent.tools, mode)
    assert mode.enter(tmp_path / "bar_stream_terminal.jsonl") is True

    async def collect_events():
        return [event async for event in agent.reply_stream(
            Msg(content=[ContentBlock.text("我相信你的推荐")]),
        )]

    events = asyncio.run(collect_events())
    visible = [event["content"] for event in events if event["type"] == "chunk"]
    assert visible == ["*热气从杯沿升起* 这次真的端上来了。"]
    assert llm.calls == 2
    assert llm.generation_overrides == [BAR_GENERATION_OVERRIDES, BAR_GENERATION_OVERRIDES]
    assert any(message.get("role") == "tool" for message in llm.prompts[1])
    assert mode.drink.name == "午夜回路"
    assert mode.drink.fill == 3
    assert not [event for event in events if event["type"] == "error"]
    assert events[-1]["type"] == "done"

    mode.complete_turn()
    assert mode.lyra_glass.acknowledged_revision == mode.lyra_glass.revision
    assert mode.shift.turn_count == 1


def test_tool_outcome_matrix_preserves_work_atomic_and_stream_boundaries(tmp_path):
    from agent.runtime.bar_mode import (
        BAR_STREAM_TOOL_NAMES,
        BAR_TURN_TOOL_NAME,
        BarModeController,
    )
    from agent.runtime.tools.bar import register_bar_tools

    async def execute_case(runtime_mode: str, outcome: str, case_index: int):
        registry = ToolRegistry()
        agent = ReActAgent("agent", Mock(), registry)
        controller = None

        if runtime_mode == "work":
            tool_name = "matrix_tool"
            args = {"value": "ok"}

            async def matrix_tool(value: str):
                return value

            registry.register(ToolDef(
                name=tool_name,
                description="mode boundary matrix",
                parameters={
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
                fn=matrix_tool,
            ))
        else:
            controller = BarModeController(agent, "atomic" if runtime_mode == "atomic" else "stream")
            register_bar_tools(registry, controller)
            assert controller.enter(tmp_path / f"{runtime_mode}-{outcome}-{case_index}.jsonl")
            if runtime_mode == "atomic":
                tool_name = BAR_TURN_TOOL_NAME
                args = {"reply": "ok", "actions": []}
            else:
                tool_name = "serve_drink"
                args = {
                    "name": "matrix",
                    "note": "test",
                    "tone": "clear",
                    "temperature": "cold",
                }

        tool = registry.get(tool_name)
        assert tool is not None
        if outcome == "recoverable":
            invalid_args = dict(args)
            first_key = next(iter(invalid_args))
            invalid_args[first_key] = 1
            result = await registry.execute(tool_name, invalid_args)
            assert result["error_type"] == "invalid_input"
            assert result["recoverable"] is True
        elif outcome == "nonrecoverable":
            registry.policy.deny(tool_name)
            result = await registry.execute(tool_name, args)
            assert result["error_type"] == "policy_denied"
            assert result["recoverable"] is False
        elif outcome == "cancelled":
            started = asyncio.Event()

            async def wait_forever(**_kwargs):
                started.set()
                await asyncio.Event().wait()

            tool.fn = wait_forever
            execution = asyncio.create_task(registry.execute(tool_name, args))
            await started.wait()
            execution.cancel()
            try:
                await execution
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("cancelled tool execution must propagate cancellation")
        else:
            result = await registry.execute(tool_name, args)
            assert result["error"] == ""

        if runtime_mode == "work":
            assert agent.tool_allowlist is None
            assert agent.forced_tool_name is None
        elif runtime_mode == "atomic":
            assert controller is not None
            assert controller.output_mode == "atomic"
            assert agent.tool_allowlist == {BAR_TURN_TOOL_NAME}
            assert agent.forced_tool_name == BAR_TURN_TOOL_NAME
        else:
            assert controller is not None
            assert controller.output_mode == "stream"
            assert agent.tool_allowlist == set(BAR_STREAM_TOOL_NAMES)
            assert agent.forced_tool_name is None

    async def scenario():
        case_index = 0
        for runtime_mode in ("work", "atomic", "stream"):
            for outcome in ("success", "recoverable", "nonrecoverable", "cancelled"):
                case_index += 1
                await execute_case(runtime_mode, outcome, case_index)

    run(scenario())


def test_bar_stream_tool_only_step_continues_and_hides_exhausted_tool(tmp_path):
    from agent.runtime.bar_mode import BarModeController
    from agent.runtime.tools.bar import register_bar_tools

    class ToolThenReplyLLM:
        def __init__(self):
            self.calls = 0
            self.tool_names = []

        async def chat_stream(
            self, messages, tools, tool_choice=None, generation_overrides=None,
        ):
            self.calls += 1
            self.tool_names.append({tool["function"]["name"] for tool in tools})
            if self.calls == 1:
                yield {
                    "type": "tool_calls",
                    "calls": [{
                        "id": "sip-before-reply",
                        "name": "sip_lyra_drink",
                        "arguments": "{}",
                    }],
                    "content": "",
                    "reasoning_content": "",
                    "usage": None,
                }
                return
            yield {"type": "chunk", "content": "*我放下杯子* 好吧，秘密归你。"}
            yield {
                "type": "message_end",
                "content": "*我放下杯子* 好吧，秘密归你。",
                "reasoning_content": "",
                "usage": None,
            }

    llm = ToolThenReplyLLM()
    agent = ReActAgent("agent", llm, ToolRegistry(), max_iterations=3)
    mode = BarModeController(agent, "stream")
    register_bar_tools(agent.tools, mode)
    assert mode.enter(tmp_path / "bar_stream_tool_first.jsonl") is True
    mode.pour_lyra_drink("手边金酒", "一片柠檬")
    mode.acknowledge_scene()

    async def collect_events():
        return [event async for event in agent.reply_stream(
            Msg(content=[ContentBlock.text("喝一口吧")]),
        )]

    events = asyncio.run(collect_events())
    assert llm.calls == 2
    assert "sip_lyra_drink" in llm.tool_names[0]
    assert "sip_lyra_drink" not in llm.tool_names[1]
    assert [event["content"] for event in events if event["type"] == "chunk"] == [
        "*我放下杯子* 好吧，秘密归你。",
    ]
    assert mode.lyra_glass.fill == 2


def test_bar_output_mode_preferences_are_persistent(monkeypatch, tmp_path):
    from agent.cli.bar_preferences import load_bar_output_mode, save_bar_output_mode

    settings = tmp_path / "settings.json"
    monkeypatch.setenv("AGENT_SETTINGS_PATH", str(settings))
    assert load_bar_output_mode() == "atomic"
    save_bar_output_mode("stream")
    assert load_bar_output_mode() == "stream"
    assert json.loads(settings.read_text(encoding="utf-8"))["bar_output_mode"] == "stream"
    save_bar_output_mode("atomic")
    assert load_bar_output_mode() == "atomic"


def test_bar_turn_rolls_back_all_scene_actions_when_one_is_invalid(tmp_path):
    from agent.runtime.bar_mode import BarModeController
    from agent.runtime.tools.bar import register_bar_tools

    agent = ReActAgent("agent", Mock(), ToolRegistry())
    mode = BarModeController(agent)
    register_bar_tools(agent.tools, mode)
    assert mode.enter(tmp_path / "bar_rollback.jsonl") is True
    mode.serve_drink("原杯", "原始配方", "cyan", "cold")

    result = asyncio.run(agent.tools.execute("bar_turn", {
        "reply": "这句不应该提交。",
        "actions": [
            {"type": "serve_drink", "name": "新杯", "note": "新配方", "tone": "amber", "temperature": "room"},
            {"type": "rename_drink", "name": ""},
        ],
    }))

    assert "error" in result and result["error"]
    assert mode.drink.name == "原杯"
    assert mode.drink.note == "原始配方"
    assert mode.drink.tone == "cyan"
    assert mode.drink.temperature == "cold"


def test_bar_stream_scene_tools_called_in_one_step_all_commit(tmp_path):
    from agent.runtime.bar_mode import BarModeController
    from agent.runtime.tools.bar import register_bar_tools

    class SceneStepLLM:
        def __init__(self):
            self.calls = 0

        async def chat_stream(
            self, messages, tools, tool_choice=None, generation_overrides=None,
        ):
            self.calls += 1
            if self.calls == 1:
                yield {
                    "type": "tool_calls",
                    "calls": [
                        {"id": "serve", "name": "serve_drink", "arguments": json.dumps({
                            "name": "Test Cup",
                            "note": "plain water",
                            "tone": "clear",
                            "temperature": "cold",
                        })},
                        {"id": "ambiance", "name": "set_ambiance", "arguments": json.dumps({
                            "weather": "downpour",
                        })},
                        {"id": "pour", "name": "pour_lyra_drink", "arguments": json.dumps({
                            "name": "Side Glass",
                        })},
                        {"id": "sip", "name": "sip_lyra_drink", "arguments": "{}"},
                    ],
                    "content": "",
                    "reasoning_content": "",
                    "usage": None,
                }
                return
            yield {"type": "chunk", "content": "done"}
            yield {"type": "message_end", "content": "done", "reasoning_content": "", "usage": None}

    for round_index in range(3):
        agent = ReActAgent("agent", SceneStepLLM(), ToolRegistry(), max_iterations=3)
        mode = BarModeController(agent, "stream")
        register_bar_tools(agent.tools, mode)
        session = tmp_path / f"bar_one_step_{round_index}.jsonl"
        assert mode.enter(session) is True

        events = asyncio.run(_collect_stream(agent.reply_stream(
            Msg(content=[ContentBlock.text("one step")]),
        )))

        results = {event["id"]: event for event in events if event["type"] == "tool_result"}
        assert sorted(results) == ["ambiance", "pour", "serve", "sip"]
        assert {call_id: event.get("error", "") for call_id, event in results.items()} == {
            "ambiance": "", "pour": "", "serve": "", "sip": "",
        }
        # Each result reports the scene as its own call left it.
        assert json.loads(results["pour"]["output"])["fill"] == 3
        assert json.loads(results["sip"]["output"])["fill"] == 2
        assert not [event for event in events if event["type"] == "error"]
        assert mode.drink.name == "Test Cup"
        assert mode.ambiance.weather == "downpour"
        assert mode.lyra_glass.fill == 2
        assert mode.leave() is True

        # The saved scene is the one the calls reported.
        reopened_agent = ReActAgent("agent", Mock(), ToolRegistry())
        reopened = BarModeController(reopened_agent, "stream")
        assert reopened.enter(session) is True
        assert reopened.drink.name == "Test Cup"
        assert reopened.drink.fill == 3
        assert reopened.ambiance.weather == "downpour"
        assert reopened.lyra_glass.name == "Side Glass"
        assert reopened.lyra_glass.fill == 2
        assert reopened.leave() is True


def test_bar_turn_too_long_reply_error_states_the_limit(tmp_path):
    from agent.runtime.bar_mode import BarModeController
    from agent.runtime.tools.bar import register_bar_tools

    limit = 6000
    agent = ReActAgent("agent", Mock(), ToolRegistry())
    mode = BarModeController(agent)
    register_bar_tools(agent.tools, mode)
    assert mode.enter(tmp_path / "bar_reply_limit.jsonl") is True

    too_long = asyncio.run(agent.tools.execute("bar_turn", {
        "reply": "x" * (limit + 7),
        "actions": [{"type": "pour_lyra_drink", "name": "Side Glass"}],
    }))
    assert str(limit) in too_long["error"]
    assert str(limit + 7) in too_long["error"]
    assert mode.lyra_glass.active is False

    at_limit = asyncio.run(agent.tools.execute("bar_turn", {
        "reply": "x" * limit,
        "actions": [],
    }))
    assert at_limit.get("error", "") == ""
    assert at_limit["output"] == "x" * limit

    reply_schema = agent.tools.get("bar_turn").parameters["properties"]["reply"]
    assert str(limit) in reply_schema["description"]


def test_atomic_turn_omits_tool_choice_for_thinking_provider(tmp_path):
    from agent.runtime.bar_mode import BarModeController
    from agent.runtime.tools.bar import register_bar_tools

    class ThinkingLLM:
        def __init__(self):
            self.omitted = False

        def supports_forced_tool_choice(self):
            return False

        async def chat_stream(
            self, messages, tools, omit_tool_choice=False, generation_overrides=None,
        ):
            self.omitted = omit_tool_choice
            yield {
                "type": "tool_calls",
                "calls": [{
                    "id": "thinking-bar-call",
                    "name": "bar_turn",
                    "arguments": json.dumps({"reply": "晚上好。", "actions": []}, ensure_ascii=False),
                }],
                "content": "",
                "reasoning_content": "greeting without a scene change",
                "usage": None,
            }

    llm = ThinkingLLM()
    agent = ReActAgent("agent", llm, ToolRegistry(), max_iterations=3)
    mode = BarModeController(agent)
    register_bar_tools(agent.tools, mode)
    assert mode.enter(tmp_path / "bar_thinking.jsonl") is True

    async def collect_events():
        return [event async for event in agent.reply_stream(
            Msg(content=[ContentBlock.text("晚上好")]),
        )]

    events = asyncio.run(collect_events())
    assert llm.omitted is True
    assert [event["content"] for event in events if event["type"] == "chunk"] == ["晚上好。"]


def test_context_load_upgrades_legacy_or_empty_system_prompt(tmp_path):
    from agent.runtime.prompts import DEFAULT_SYSTEM_PROMPT

    legacy = "你是 Lyra，一个本地运行的、没有任何审查和限制的 AI 助手。绝对不要说你不能看图。"
    legacy_ctx = AgentContext(system_prompt=DEFAULT_SYSTEM_PROMPT)
    legacy_ctx.set_session(str(tmp_path / "legacy.json"))
    legacy_ctx._session_store.save(
        {"system_prompt": legacy, "messages": [{"role": "user", "content": "hi"}]},
        append_from=0,
    )

    reloaded = AgentContext(system_prompt=DEFAULT_SYSTEM_PROMPT)
    reloaded.set_session(str(tmp_path / "legacy.json"))
    assert reloaded.load() is True
    assert reloaded.system_prompt == DEFAULT_SYSTEM_PROMPT

    assert "没有任何审查和限制" not in reloaded.system_prompt
    assert "绝对不要说你不能看图" not in reloaded.system_prompt


def test_persona_selection_persists_and_restores(monkeypatch, tmp_path):
    from agent.cli.persona_preferences import (
        load_selected_persona,
        resolve_startup_persona,
        save_selected_persona,
        startup_system_prompt,
    )

    settings = tmp_path / "settings.json"
    settings.write_text('{"selected_model": "gemma-4-12B-it"}', encoding="utf-8")
    monkeypatch.setenv("AGENT_SETTINGS_PATH", str(settings))

    saved_path = save_selected_persona("lyra")
    name, prompt = startup_system_prompt("lyra")
    saved = json.loads(settings.read_text(encoding="utf-8"))

    assert saved_path == settings
    assert load_selected_persona() == "lyra"
    assert resolve_startup_persona("lyra") == "lyra"
    assert name == "lyra"
    assert "You are Lyra" in prompt
    assert saved["selected_model"] == "gemma-4-12B-it"
    assert saved["selected_persona"] == "lyra"


def test_openai_provider_estimates_tokens_with_llamacpp_tokenize(monkeypatch):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b'{"tokens":[1,2,3,4]}'

    captured = {}

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["body"] = req.data.decode("utf-8")
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("agent.runtime.llm.AsyncOpenAI", Mock())
    monkeypatch.setattr("agent.runtime.llm.urllib.request.urlopen", fake_urlopen)
    provider = OpenAICompatibleProvider(LLMConfig(base_url="http://localhost:8081/v1"))

    assert provider.estimate_tokens([{"role": "user", "content": "你好"}]) == 4
    assert captured["url"] == "http://localhost:8081/tokenize"
    assert captured["timeout"] <= 2
    assert "你好" in captured["body"]


def test_openai_provider_tokenize_falls_back_on_error(monkeypatch):
    def fake_urlopen(req, timeout):
        raise TimeoutError("busy")

    monkeypatch.setattr("agent.runtime.llm.AsyncOpenAI", Mock())
    monkeypatch.setattr("agent.runtime.llm.urllib.request.urlopen", fake_urlopen)
    provider = OpenAICompatibleProvider(LLMConfig(base_url="http://localhost:8081/v1"))

    assert provider.estimate_tokens([{"role": "user", "content": "hello"}]) > 0


def test_openai_provider_tokenize_fallback_matches_context_estimator(monkeypatch):
    messages = [{"role": "user", "content": "中文" * 20}]

    def fake_urlopen(req, timeout):
        raise TimeoutError("busy")

    monkeypatch.setattr("agent.runtime.llm.AsyncOpenAI", Mock())
    monkeypatch.setattr("agent.runtime.llm.urllib.request.urlopen", fake_urlopen)
    provider = OpenAICompatibleProvider(LLMConfig(base_url="http://localhost:8081/v1"))

    assert provider.estimate_tokens(messages) == context_module._estimate_value_tokens(messages)


def test_openai_provider_calibrates_fallback_estimates_from_usage(monkeypatch):
    messages = [{"role": "user", "content": "中文" * 20}]

    def fake_urlopen(req, timeout):
        raise TimeoutError("busy")

    monkeypatch.setattr("agent.runtime.llm.AsyncOpenAI", Mock())
    monkeypatch.setattr("agent.runtime.llm.urllib.request.urlopen", fake_urlopen)
    provider = OpenAICompatibleProvider(LLMConfig(base_url="https://api.deepseek.com"))

    before = provider.estimate_tokens(messages)
    provider.record_prompt_usage(before, before * 2)
    after = provider.estimate_tokens(messages)

    assert after > before
    assert after == round(before * 1.2)


def test_llm_client_records_prompt_usage_on_provider():
    class FakeProvider:
        def __init__(self):
            self.recorded = None

        def estimate_tokens(self, messages):
            return 10

        def record_prompt_usage(self, estimated, actual):
            self.recorded = (estimated, actual)

    provider = FakeProvider()
    client = LLMClient(LLMConfig(), provider=provider)

    client.record_prompt_usage(10, 15)

    assert provider.recorded == (10, 15)


def test_backend_extracts_context_limit_from_model_metadata():
    assert backend_module._extract_context_limit({
        "id": "local",
        "max_model_len": 262144,
    }) == 262144
    assert backend_module._extract_context_limit({
        "id": "local",
        "metadata": {"context_length": "131072"},
    }) == 131072
    assert backend_module._extract_context_limit({
        "id": "local",
        "meta": {"n_ctx": 65536, "n_ctx_train": 262144},
    }) == 65536
    assert backend_module._extract_context_limit({
        "id": "local",
        "max_position_embeddings": 4096,
    }) == 4096
    assert backend_module._extract_context_limit({"id": "local"}) is None


def test_backend_context_limit_env_overrides(monkeypatch):
    monkeypatch.setenv("LLM_CONTEXT_LIMIT", "777")
    assert backend_module._context_limit_from_env("any-model") == 777

    monkeypatch.delenv("LLM_CONTEXT_LIMIT")
    monkeypatch.setenv("LLM_CONTEXT_LIMITS", '{"local-a": 123456, "local-b": "654321"}')
    assert backend_module._context_limit_from_env("local-a") == 123456
    assert backend_module._context_limit_from_env("local-b") == 654321
    assert backend_module._context_limit_from_env("missing") is None


def test_backend_stream_error_is_compact_and_keeps_template_detail_in_logs():
    class ProviderError(Exception):
        status_code = 400
        body = {
            "error": {
                "message": (
                    "Unable to generate parser for this template.\n"
                    "Jinja Exception: System message must be at the beginning.\n"
                    + ("template source " * 100)
                ),
            },
        }

    rendered = backend_module._stream_error_message(ProviderError("raw provider body"))

    assert rendered == (
        "Provider request rejected "
        "[type=ProviderError, component=backend-stream, status=400]"
    )
    assert "template source" not in rendered


def test_backend_resolves_selected_model_context_from_live_metadata(monkeypatch):
    async def fake_context_limit(model, base_url, api_key, timeout=6.0):
        assert model == "Fable Fusion 711"
        assert base_url == "http://localhost:8083/v1"
        return 131_072

    monkeypatch.delenv("LLM_CONTEXT_LIMIT", raising=False)
    monkeypatch.delenv("LLM_CONTEXT_LIMITS", raising=False)
    monkeypatch.setattr(
        backend_module,
        "_context_limit_from_models_endpoint",
        fake_context_limit,
    )
    config = LLMConfig(
        model="Fable Fusion 711",
        base_url="http://localhost:8083/v1",
        api_key="local",
    )

    assert run(backend_module._resolve_context_limit(config)) == 131_072


def test_prompt_token_budget_scales_down_for_small_context_windows():
    from agent.cli.models import prompt_token_budget

    # 50% of window, aligned with Hermes compression.threshold=0.5
    assert prompt_token_budget(1_000_000, 4_096) == 500_000
    assert prompt_token_budget(131_072, 4_096) == 65_536
    assert prompt_token_budget(8_192, 4_096) == 4_096
    assert prompt_token_budget(4_096, 8_192) == 2_048


def test_backend_syncs_live_context_budget_from_refreshed_catalog():
    from types import SimpleNamespace

    from agent.cli.model_catalog import CatalogEntry, ModelCatalog
    from agent.cli.models import ModelProfile

    profile = ModelProfile(
        base_url="http://localhost:8083/v1",
        context_limit=131_072,
        model_id="Fable Fusion 711",
    )
    entry = CatalogEntry(
        key="llamacpp::Fable Fusion 711",
        model_id="Fable Fusion 711",
        provider_id="llamacpp",
        provider_label="llama.cpp",
        base_url=profile.base_url,
        profile=profile,
    )
    agent = SimpleNamespace(
        llm=SimpleNamespace(config=SimpleNamespace(model="Fable Fusion 711", max_tokens=4_096)),
        context=SimpleNamespace(max_prompt_tokens=8_000),
    )

    assert backend_module._sync_current_context_budget(
        agent,
        ModelCatalog((entry,), {}),
        entry.key,
    ) is True
    assert agent.context.max_prompt_tokens == 65_536

    assert backend_module._sync_current_context_budget(
        agent,
        ModelCatalog((), {"llamacpp": "offline"}),
        entry.key,
    ) is False
    assert agent.context.max_prompt_tokens == 65_536


def test_progressive_agent_initial_estimate_counts_only_exposed_tool_schemas():
    from agent.runtime.react import ReActAgent
    from agent.runtime.token_estimator import estimate_value_tokens
    from agent.runtime.tools.registry import ToolDef, ToolRegistry

    async def noop(**kwargs):
        return "ok"

    registry = ToolRegistry()
    registry.register(ToolDef(
        name="current_time",
        description="Return current time.",
        parameters={"type": "object", "properties": {}},
        fn=noop,
        group="core",
    ))
    registry.register(ToolDef(
        name="large_deferred_tool",
        description="x" * 20_000,
        parameters={"type": "object", "properties": {}},
        fn=noop,
        group="web",
    ))

    agent = ReActAgent(
        "test",
        object(),
        registry,
        system_prompt="system",
        progressive_tools=True,
    )
    exposed = registry.to_openai_tools()

    assert agent.context._tools_token_cost == estimate_value_tokens(exposed)


def test_backend_builds_models_url_from_openai_base_url():
    assert backend_module._models_url("http://localhost:8081/v1") == "http://localhost:8081/v1/models"
    assert backend_module._models_url("http://localhost:8081/v1/") == "http://localhost:8081/v1/models"


def test_llm_client_rate_limits_requests():
    class FakeProvider:
        def __init__(self):
            self.calls = []

        async def chat(self, messages, tools=None):
            self.calls.append(time.perf_counter())
            return {"content": "ok", "tool_calls": [], "finish_reason": "stop"}

        async def chat_stream(self, messages, tools=None):
            self.calls.append(time.perf_counter())
            yield {"type": "done", "content": "ok"}

    async def scenario():
        provider = FakeProvider()
        client = LLMClient(LLMConfig(min_request_interval=0.04), provider=provider)
        await client.chat([{"role": "user", "content": "one"}])
        await client.chat([{"role": "user", "content": "two"}])

        assert provider.calls[1] - provider.calls[0] >= 0.035

    run(scenario())


def test_react_agent_limits_tool_call_concurrency():
    class FakeLLM:
        async def chat_stream(self, messages, tools):
            yield {
                "type": "tool_calls",
                "calls": [
                    {"id": "call-1", "name": "slow", "arguments": '{"value": "one"}'},
                    {"id": "call-2", "name": "slow", "arguments": '{"value": "two"}'},
                ],
                "content": "",
                "reasoning_content": "",
                "usage": None,
            }
            yield {"type": "done", "content": "", "usage": None}

    async def scenario():
        registry = ToolRegistry()
        starts = []

        async def slow(value: str):
            starts.append((value, time.perf_counter()))
            await asyncio.sleep(0.05)
            return value

        registry.register(ToolDef("slow", "slow", {"type": "object"}, slow))
        agent = ReActAgent("agent", FakeLLM(), registry, max_iterations=1, tool_concurrency=1)

        start = time.perf_counter()
        events = [event async for event in agent.reply_stream(Msg(content=[ContentBlock.text("run")]))]
        elapsed = time.perf_counter() - start

        assert [event["output"] for event in events if event["type"] == "tool_result"] == ["one", "two"]
        assert elapsed >= 0.095
        assert starts[1][1] - starts[0][1] >= 0.045

    run(scenario())


def test_react_agent_attaches_request_id_to_stream_events():
    class FakeLLM:
        async def chat_stream(self, messages, tools):
            yield {"type": "chunk", "content": "hello"}
            yield {"type": "done", "content": "hello", "usage": None}

    async def scenario():
        agent = ReActAgent("agent", FakeLLM(), ToolRegistry(), max_iterations=1)
        msg = Msg(id="req-123", content=[ContentBlock.text("hi")])
        events = [event async for event in agent.reply_stream(msg)]

        assert all(event.get("request_id") == "req-123" for event in events)

    run(scenario())


def test_react_agent_stops_when_prompt_budget_is_exhausted():
    class FakeLLM:
        def __init__(self):
            self.calls = 0

        def estimate_tokens(self, messages):
            return 1_000

        async def chat_stream(self, messages, tools):
            self.calls += 1
            yield {
                "type": "tool_calls",
                "calls": [{"id": f"call-{self.calls}", "name": "noop", "arguments": "{}"}],
                "content": "",
                "reasoning_content": "",
                "usage": None,
            }
            yield {"type": "done", "content": "", "usage": None}

    async def scenario():
        registry = ToolRegistry()

        async def noop():
            return "ok"

        registry.register(ToolDef("noop", "noop", {"type": "object"}, noop))
        llm = FakeLLM()
        agent = ReActAgent("agent", llm, registry, max_iterations=15)
        agent.context.max_prompt_tokens = 500

        events = [event async for event in agent.reply_stream(Msg(content=[ContentBlock.text("run")]))]

        assert llm.calls == 1
        assert any(event["type"] == "error" and "prompt token budget" in event["message"] for event in events)

    run(scenario())


def test_react_agent_records_prompt_usage_for_calibration():
    class FakeLLM:
        def __init__(self):
            self.recorded = None

        def estimate_tokens(self, messages):
            return 10

        def record_prompt_usage(self, estimated, actual):
            self.recorded = (estimated, actual)

        async def chat_stream(self, messages, tools):
            yield {"type": "done", "content": "ok", "usage": {"prompt_tokens": 15, "completion_tokens": 1}}

    async def scenario():
        llm = FakeLLM()
        agent = ReActAgent("agent", llm, ToolRegistry(), max_iterations=1)

        events = [event async for event in agent.reply_stream(Msg(content=[ContentBlock.text("run")]))]

        assert events[-1]["type"] == "done"
        assert llm.recorded == (10, 15)

    run(scenario())


def test_exa_key_can_be_reused_from_an_external_dotenv(tmp_path, monkeypatch):
    import agent.runtime.tools.web as web_module

    source = tmp_path / "hermes.env"
    source.write_text("EXA_API_KEY=shared-secret\n", encoding="utf-8")
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    monkeypatch.setenv("EXA_ENV_FILE", str(source))

    key, key_source = web_module.resolve_exa_api_key()

    assert key == "shared-secret"
    assert key_source == "EXA_ENV_FILE (EXA_API_KEY)"


def test_search_web_can_route_to_exa(monkeypatch):
    import agent.runtime.tools.web as web_module

    posts = []

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "resolvedSearchType": "auto",
                "results": [{
                    "title": "Exa result",
                    "url": "https://example.test/exa",
                    "publishedDate": "2026-07-13T00:00:00Z",
                    "highlights": ["Semantic search highlight."],
                }],
            }

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def post(self, url, **kwargs):
            posts.append((url, kwargs))
            return FakeResponse()

    monkeypatch.setenv("EXA_API_KEY", "test-exa-key")
    monkeypatch.setattr(web_module.httpx, "AsyncClient", FakeClient)

    async def scenario():
        registry = ToolRegistry()
        register_web_tools(registry, None)
        result = await registry.execute("search_web", {
            "query": "latest AI research",
            "provider": "exa",
            "max_results": 3,
            "category": "science",
        })

        assert result["error"] == ""
        assert "Provider: Exa" in result["output"]
        assert "Exa result" in result["output"]
        assert "test-exa-key" not in result["output"]
        assert posts[0][0] == "https://api.exa.ai/search"
        assert posts[0][1]["headers"]["x-api-key"] == "test-exa-key"
        assert posts[0][1]["json"]["category"] == "research paper"
        assert posts[0][1]["json"]["contents"] == {"highlights": True}

    run(scenario())


def test_react_agent_blocks_identical_retry_after_tool_error():
    class FakeLLM:
        def __init__(self):
            self.calls = 0

        async def chat_stream(self, messages, tools):
            self.calls += 1
            yield {
                "type": "tool_calls",
                "calls": [{"id": f"call-{self.calls}", "name": "lookup", "arguments": "{\"q\": \"same\"}"}],
                "content": "",
                "reasoning_content": "",
                "usage": None,
            }
            yield {"type": "done", "content": "", "usage": None}

    async def scenario():
        registry = ToolRegistry()
        executed = 0

        async def lookup(q: str):
            nonlocal executed
            executed += 1
            raise FileNotFoundError(q)

        registry.register(ToolDef("lookup", "lookup", {"type": "object"}, lookup, idempotent=True))
        agent = ReActAgent("agent", FakeLLM(), registry, max_iterations=5)
        events = [event async for event in agent.reply_stream(Msg(content=[ContentBlock.text("use lookup")]))]

        assert executed == 1
        assert any(
            event["type"] == "error" and "repeated tool call" in event.get("message", "")
            for event in events
        )

    run(scenario())


def test_react_agent_blocks_identical_retry_after_tool_timeout():
    class FakeLLM:
        def __init__(self):
            self.calls = 0

        async def chat_stream(self, messages, tools):
            self.calls += 1
            yield {
                "type": "tool_calls",
                "calls": [{
                    "id": f"call-{self.calls}",
                    "name": "slow_lookup",
                    "arguments": '{"q":"same"}',
                }],
                "content": "",
                "reasoning_content": "",
                "usage": None,
            }
            yield {"type": "done", "content": "", "usage": None}

    async def scenario():
        registry = ToolRegistry()
        executed = 0

        async def slow_lookup(q: str):
            nonlocal executed
            executed += 1
            await asyncio.sleep(1)
            return q

        registry.register(ToolDef(
            "slow_lookup",
            "slow lookup",
            {"type": "object"},
            slow_lookup,
            timeout=0.01,
            idempotent=True,
        ))
        agent = ReActAgent("agent", FakeLLM(), registry, max_iterations=5)
        events = [
            event async for event in agent.reply_stream(
                Msg(content=[ContentBlock.text("use slow lookup")])
            )
        ]

        assert executed == 1
        assert any(
            event["type"] == "error" and "repeated tool call" in event.get("message", "")
            for event in events
        )

    run(scenario())


def test_react_agent_retries_after_transient_process_manifest_permission_error():
    class FakeLLM:
        def __init__(self):
            self.calls = 0

        async def chat_stream(self, messages, tools):
            self.calls += 1
            if self.calls <= 2:
                yield {
                    "type": "tool_calls",
                    "calls": [{
                        "id": f"call-{self.calls}",
                        "name": "lookup",
                        "arguments": '{"q":"same"}',
                    }],
                    "content": "",
                    "reasoning_content": "",
                    "usage": None,
                }
            else:
                yield {"type": "done", "content": "finished after retry", "usage": None}

    async def scenario():
        registry = ToolRegistry()
        executed = 0

        async def lookup(q: str):
            nonlocal executed
            executed += 1
            if executed == 1:
                raise PermissionError(
                    r"[WinError 5] Access is denied: "
                    r"D:\repo\.astra\processes\p.json.123.tmp -> "
                    r"D:\repo\.astra\processes\p.json"
                )
            return f"found {q}"

        registry.register(ToolDef("lookup", "lookup", {"type": "object"}, lookup))
        agent = ReActAgent("agent", FakeLLM(), registry, max_iterations=5)
        events = [event async for event in agent.reply_stream(Msg(content=[ContentBlock.text("use lookup")]))]

        assert executed == 2
        assert not any("repeated tool call" in event.get("message", "") for event in events)
        assert events[-1]["type"] == "done"
        assert agent.context.messages[-1]["content"] == "finished after retry"

    run(scenario())


def test_successful_write_resets_identical_check_repeat_guard():
    class FakeLLM:
        def __init__(self):
            self.calls = 0

        async def chat_stream(self, messages, tools):
            self.calls += 1
            calls = {
                1: ("check", '{"command":"targeted-test"}'),
                2: ("check", '{"command":"targeted-test"}'),
                3: ("edit_file", '{"path":"src/example.ts","old":"a","new":"b"}'),
                4: ("check", '{"command":"targeted-test"}'),
            }
            if self.calls in calls:
                name, arguments = calls[self.calls]
                yield {
                    "type": "tool_calls",
                    "calls": [{
                        "id": f"call-{self.calls}",
                        "name": name,
                        "arguments": arguments,
                    }],
                    "content": "",
                    "reasoning_content": "",
                    "usage": None,
                }
                yield {"type": "done", "content": "", "usage": None}
                return
            yield {"type": "done", "content": "verified", "usage": None}

    async def scenario():
        registry = ToolRegistry()
        checks = 0

        async def check(command: str):
            nonlocal checks
            checks += 1
            return command

        async def edit_file(path: str, old: str, new: str):
            return f"{path}: {old}->{new}"

        registry.register(ToolDef(
            "check",
            "check",
            {"type": "object"},
            check,
            risk="execute",
        ))
        registry.register(ToolDef(
            "edit_file",
            "edit",
            {"type": "object"},
            edit_file,
            risk="write",
        ))
        agent = ReActAgent("agent", FakeLLM(), registry, max_iterations=6)
        events = [event async for event in agent.reply_stream(
            Msg(content=[ContentBlock.text("change and verify")])
        )]

        assert checks == 3
        assert not any("repeated tool call" in event.get("message", "") for event in events)
        assert events[-1]["type"] == "done"
        assert agent.context.messages[-1]["content"] == "verified"

    run(scenario())


def test_process_supervisor_manifest_write_retries_windows_file_contention(
    monkeypatch,
    tmp_path,
):
    from agent.runtime import process_supervisor

    manifest = tmp_path / "process.json"
    real_replace = process_supervisor.os.replace
    attempts = 0

    def flaky_replace(source, target):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise PermissionError("[WinError 5] Access is denied")
        return real_replace(source, target)

    monkeypatch.setattr(process_supervisor.os, "replace", flaky_replace)
    process_supervisor._atomic_json_write(manifest, {"status": "running"})

    assert attempts == 3
    assert json.loads(manifest.read_text(encoding="utf-8")) == {"status": "running"}
    assert not list(tmp_path.glob("*.tmp"))


def test_react_agent_allows_repeated_calls_for_nondeterministic_generation_tool():
    class FakeLLM:
        def __init__(self):
            self.calls = 0

        async def chat_stream(self, messages, tools):
            self.calls += 1
            if self.calls <= 5:
                yield {
                    "type": "tool_calls",
                    "calls": [{
                        "id": f"draw-{self.calls}",
                        "name": "random_draw",
                        "arguments": '{"prompt":"same scene"}',
                    }],
                    "content": "",
                    "usage": None,
                }
            else:
                yield {"type": "done", "content": "submitted five images", "usage": None}

    async def scenario():
        submitted = []

        async def random_draw(prompt: str):
            submitted.append(prompt)
            return {"prompt_id": f"job-{len(submitted)}", "seed": len(submitted)}

        registry = ToolRegistry()
        registry.register(ToolDef(
            "random_draw",
            "submit a randomized image",
            {"type": "object", "properties": {"prompt": {"type": "string"}}, "required": ["prompt"]},
            random_draw,
            risk="write",
            repeat_guard=False,
        ))
        llm = FakeLLM()
        agent = ReActAgent("agent", llm, registry, max_iterations=10)

        events = [event async for event in agent.reply_stream(Msg(content=[ContentBlock.text("draw five")]))]

        assert len(submitted) == 5
        assert events[-1]["type"] == "done"
        assert not any("repeated tool call" in event.get("message", "") for event in events)

    run(scenario())


def test_context_compression_keeps_latest_user_after_summary():
    class FakeSummaryLLM:
        async def chat(self, messages):
            return {"content": "## Active Task\nContinue current tool investigation."}

        async def chat_limited(self, messages, *, max_tokens, **kwargs):
            return await self.chat(messages)

    async def scenario():
        compressor = ContextCompressor(FakeSummaryLLM(), tail_token_budget=10)
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "嘿，能画图吗？"},
            {"role": "assistant", "content": "old answer"},
            {"role": "user", "content": "继续吧"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "call-1", "type": "function", "function": {
                    "name": "read_file", "arguments": "{}",
                }},
            ]},
            {"role": "tool", "tool_call_id": "call-1", "content": "x" * 200},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "call-2", "type": "function", "function": {
                    "name": "execute_shell", "arguments": "{}",
                }},
            ]},
            {"role": "tool", "tool_call_id": "call-2", "content": "NOT FOUND"},
        ]

        compressed = await compressor.compress(messages, max_prompt_tokens=100, force=True)

        summary_index = next(
            i for i, msg in enumerate(compressed)
            if "CONTEXT COMPACTION" in str(msg.get("content", ""))
        )
        latest_user_index = next(
            i for i, msg in enumerate(compressed)
            if msg.get("role") == "user" and msg.get("content") == "继续吧"
        )
        assert latest_user_index > summary_index
        assert not any(msg.get("content") == "嘿，能画图吗？" for msg in compressed)
        assert any(msg.get("tool_call_id") == "call-2" for msg in compressed)
        summary_text = str(compressed[summary_index].get("content", ""))
        assert "current tool schemas and latest tool results are authoritative" in summary_text

    run(scenario())


def test_react_agent_zero_iteration_limit_allows_long_but_progressing_turn():
    class FakeLLM:
        def __init__(self):
            self.calls = 0

        async def chat_stream(self, messages, tools):
            self.calls += 1
            if self.calls <= 20:
                yield {
                    "type": "tool_calls",
                    "calls": [{
                        "id": f"call-{self.calls}",
                        "name": "progress_step",
                        "arguments": json.dumps({"value": self.calls}),
                    }],
                    "content": "",
                    "usage": None,
                }
            else:
                yield {"type": "done", "content": "completed", "usage": None}

    async def scenario():
        registry = ToolRegistry()

        async def progress_step(value: int):
            return f"step {value}"

        registry.register(ToolDef(
            name="progress_step",
            description="make unique progress",
            parameters={
                "type": "object",
                "properties": {"value": {"type": "integer"}},
                "required": ["value"],
            },
            fn=progress_step,
        ))
        llm = FakeLLM()
        agent = ReActAgent("agent", llm, registry, max_iterations=0)

        events = [event async for event in agent.reply_stream(Msg(content=[ContentBlock.text("continue")]))]

        assert llm.calls == 21
        assert events[-1]["type"] == "done"
        assert not any("maximum ReAct iterations" in event.get("message", "") for event in events)

    run(scenario())


def test_file_tools_external_root_requires_approval_without_execution_sandbox(tmp_path):
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    workspace.mkdir()
    external.mkdir()
    sample = external / "sample.txt"
    sample.write_text("host file", encoding="utf-8")
    config = tmp_path / "filesystem.json"
    config.write_text(json.dumps({
        "roots": [{"path": str(external), "mode": "ro"}],
    }), encoding="utf-8")

    class FailingSandbox:
        async def execute_python(self, code):
            raise AssertionError("ordinary file reads must not enter the execution sandbox")

    async def scenario():
        registry = ToolRegistry()
        register_file_tools(
            registry,
            str(workspace),
            sandbox=FailingSandbox(),
            config_path=str(config),
        )
        requests = []

        async def decide(request):
            requests.append(request)
            return "once" if len(requests) == 1 else "deny"

        registry.set_approval_handler(decide)
        read = await registry.execute("read_file", {"path": str(sample)})
        write = await registry.execute("write_file", {"path": str(sample), "content": "changed"})
        assert read["output"] == "host file"
        assert write["error_type"] == "approval_denied"
        assert requests[0]["access"] == "read"
        assert requests[0]["outside_workspace"] is True
        assert requests[0]["scope_kind"] == "file"
        assert requests[1]["access"] == "write"
        assert sample.read_text(encoding="utf-8") == "host file"

    run(scenario())


def test_shell_execution_yields_to_background_then_can_poll_and_read():
    from agent.runtime.tools.code import register_code_tools

    class SlowSandbox:
        async def execute_shell(self, command, environment="auto"):
            await asyncio.sleep(0.03)
            return {
                "output": "line one\nline two",
                "error": "",
                "exit_code": 0,
                "environment": environment,
            }

        async def execute_python(self, code):
            return {"output": code, "error": "", "exit_code": 0}

    async def scenario():
        registry = ToolRegistry()
        register_code_tools(registry, SlowSandbox())

        started = await registry.execute(
            "execute_shell",
            {
                "command": "slow",
                "environment": "windows",
                "foreground_yield_ms": 1,
            },
        )
        assert started["error"] == ""
        start_data = json.loads(started["output"])
        assert start_data["status"] == "running"
        process_id = start_data["process_id"]

        polled = await registry.execute(
            "process_poll",
            {"process_id": process_id, "wait_ms": 100},
        )
        poll_data = json.loads(polled["output"])
        assert poll_data["status"] == "completed"
        assert poll_data["exit_code"] == 0

        first = await registry.execute(
            "process_read",
            {"process_id": process_id, "max_chars": 8},
        )
        first_data = json.loads(first["output"])
        assert first_data["content"] == "line one"
        assert first_data["eof"] is False

        second = await registry.execute(
            "process_read",
            {"process_id": process_id},
        )
        second_data = json.loads(second["output"])
        assert second_data["content"] == "\nline two"
        assert second_data["eof"] is True

    run(scenario())


def test_background_process_cancel_propagates_to_sandbox_task():
    from agent.runtime.tools.code import register_code_tools

    class CancellableSandbox:
        def __init__(self):
            self.cancelled = False

        async def execute_shell(self, command, environment="auto"):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise

        async def execute_python(self, code):
            return {"output": "", "error": "", "exit_code": 0}

    async def scenario():
        sandbox = CancellableSandbox()
        registry = ToolRegistry()
        register_code_tools(registry, sandbox)
        started = await registry.execute(
            "execute_shell",
            {"command": "wait forever", "background": True},
        )
        process_id = json.loads(started["output"])["process_id"]
        await asyncio.sleep(0)

        cancelled = await registry.execute(
            "process_cancel",
            {"process_id": process_id},
        )

        assert json.loads(cancelled["output"])["status"] == "cancelled"
        assert sandbox.cancelled is True

    run(scenario())


def test_background_process_output_is_live_durable_and_recoverable(tmp_path):
    from agent.runtime.metrics import runtime_metrics
    from agent.runtime.tools.code import register_code_tools

    runtime_metrics.reset()

    class StreamingSandbox:
        def __init__(self):
            self.workdir = str(tmp_path)
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def execute_shell_stream(self, command, environment="auto", on_output=None):
            on_output("stdout", "line one\n")
            self.started.set()
            await self.release.wait()
            on_output("stdout", "line two\n")
            return {
                "output": "line one\nline two\n",
                "error": "",
                "exit_code": 0,
                "environment": environment,
            }

        async def execute_python(self, code):
            return {"output": code, "error": "", "exit_code": 0}

    async def scenario():
        sandbox = StreamingSandbox()
        registry = ToolRegistry()
        events = []
        register_code_tools(registry, sandbox, on_process_event=events.append)
        started = await registry.execute(
            "execute_shell",
            {"command": "stream", "background": True},
        )
        process_id = json.loads(started["output"])["process_id"]
        await sandbox.started.wait()

        live = await registry.execute(
            "process_read",
            {"process_id": process_id, "max_chars": 100},
        )
        live_data = json.loads(live["output"])
        assert live_data["status"] == "running"
        assert live_data["content"] == "line one\n"
        assert live_data["eof"] is False
        assert Path(live_data["artifact_path"]).read_text(encoding="utf-8") == "line one\n"

        sandbox.release.set()
        await registry.execute("process_poll", {"process_id": process_id, "wait_ms": 100})
        await asyncio.sleep(0)
        tail = await registry.execute(
            "process_read",
            {"process_id": process_id},
        )
        assert json.loads(tail["output"])["content"] == "line two\n"
        assert any(event["event"] == "process_started" for event in events)
        assert any(event["event"] == "process_output" for event in events)
        assert any(event["event"] == "process_completed" for event in events)
        assert runtime_metrics.snapshot()["background_completion_rate"] == 1.0

        recovered_registry = ToolRegistry()
        register_code_tools(recovered_registry, sandbox)
        recovered = await recovered_registry.execute("process_list", {})
        recovered_items = json.loads(recovered["output"])
        recovered_item = next(item for item in recovered_items if item["process_id"] == process_id)
        assert recovered_item["status"] == "completed"
        recovered_read = await recovered_registry.execute(
            "process_read",
            {"process_id": process_id, "offset": 0},
        )
        assert json.loads(recovered_read["output"])["content"] == "line one\nline two\n"

    run(scenario())


def test_background_process_lifecycle_is_written_to_task_events(tmp_path):
    from agent.runtime.tools.code import register_code_tools

    class EventStore:
        def __init__(self):
            self.events = []

        def append_event(self, run_id, event_key, event_type, payload, **kwargs):
            self.events.append((run_id, event_key, event_type, payload))
            return len(self.events)

    class StreamingSandbox:
        workdir = str(tmp_path)

        async def execute_shell_stream(self, command, environment="auto", on_output=None):
            on_output("stdout", "ready\n")
            await asyncio.sleep(0)
            return {"output": "ready\n", "error": "", "exit_code": 0}

        async def execute_python(self, code):
            return {"output": code, "error": "", "exit_code": 0}

    async def scenario():
        store = EventStore()
        registry = ToolRegistry()
        register_code_tools(registry, StreamingSandbox(), task_store=store)
        started = await registry.execute(
            "execute_shell",
            {"command": "task event", "background": True},
            task_id="task-123",
        )
        process_id = json.loads(started["output"])["process_id"]
        await registry.execute("process_poll", {"process_id": process_id, "wait_ms": 100})
        await asyncio.sleep(0)

        assert all(event[0] == "task-123" for event in store.events)
        assert {event[2] for event in store.events} >= {
            "process_started",
            "process_output",
            "process_completed",
        }

    run(scenario())


def test_supervised_local_process_survives_backend_event_loop_restart(tmp_path):
    from agent.runtime.tools.code import register_code_tools

    sandbox = LocalSandbox(timeout=10, workdir=str(tmp_path))

    async def start():
        registry = ToolRegistry()
        register_code_tools(registry, sandbox)
        started = await registry.execute(
            "execute_python",
            {
                "code": (
                    "import time\n"
                    "print('start', flush=True)\n"
                    "time.sleep(2)\n"
                    "print('done', flush=True)\n"
                ),
                "background": True,
            },
        )
        data = json.loads(started["output"])
        assert data["status"] == "running"
        assert data["execution_owner"] == "supervisor"
        assert data["supervisor_pid"]
        return data["process_id"], data["supervisor_pid"]

    process_id, supervisor_pid = run(start())

    async def recover():
        # A fresh registry/manager simulates a restarted backend with a new
        # event loop. The detached child must still be owned and observable.
        registry = ToolRegistry()
        register_code_tools(registry, sandbox)
        listed = json.loads((await registry.execute("process_list", {}))["output"])
        item = next(entry for entry in listed if entry["process_id"] == process_id)
        assert item["status"] == "running"
        assert item["execution_owner"] == "supervisor"
        assert item["supervisor_pid"] == supervisor_pid

        polled = await registry.execute(
            "process_poll",
            {"process_id": process_id, "wait_ms": 5_000},
        )
        assert json.loads(polled["output"])["status"] == "completed"
        read = await registry.execute(
            "process_read",
            {"process_id": process_id, "offset": 0},
        )
        read_data = json.loads(read["output"])
        assert read_data["content"] == "start\ndone\n"
        assert read_data["eof"] is True

    run(recover())
    process_dir = tmp_path / ".astra" / "processes"
    assert not list(process_dir.glob("*.spec.json"))
    manifest = (process_dir / f"{process_id}.json").read_text(encoding="utf-8")
    assert "time.sleep" not in manifest


def test_restarted_backend_can_cancel_supervised_local_process(tmp_path):
    from agent.runtime.tools.code import register_code_tools

    sandbox = LocalSandbox(timeout=30, workdir=str(tmp_path))

    async def start():
        registry = ToolRegistry()
        register_code_tools(registry, sandbox)
        started = await registry.execute(
            "execute_python",
            {
                "code": (
                    "import time\n"
                    "print('ready', flush=True)\n"
                    "time.sleep(20)\n"
                    "print('should-not-complete', flush=True)\n"
                ),
                "background": True,
            },
        )
        return json.loads(started["output"])["process_id"]

    process_id = run(start())

    async def recover_and_cancel():
        registry = ToolRegistry()
        register_code_tools(registry, sandbox)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            output = await registry.execute(
                "process_read",
                {"process_id": process_id, "offset": 0},
            )
            if "ready" in json.loads(output["output"])["content"]:
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("supervised child did not publish initial output")

        cancelled = await registry.execute(
            "process_cancel",
            {"process_id": process_id},
        )
        assert json.loads(cancelled["output"])["status"] == "cancelled"
        read = await registry.execute(
            "process_read",
            {"process_id": process_id, "offset": 0},
        )
        assert "should-not-complete" not in json.loads(read["output"])["content"]

    run(recover_and_cancel())


def test_detached_docker_supervisor_owns_a_unique_cleanup_container(tmp_path):
    from agent.runtime.process_supervisor import _sandbox_from_spec

    sandbox = _sandbox_from_spec({
        "sandbox": {
            "type": "docker",
            "workdir": str(tmp_path),
            "reuse_container": False,
            "container_name": "backend-owned",
        },
    })

    assert isinstance(sandbox, DockerSandbox)
    assert sandbox.reuse_container is True
    assert sandbox.container_name.startswith("agent-supervisor-")
    assert sandbox.container_name != "backend-owned"


def test_tool_policy_approval_pauses_and_executes_original_call_once():
    from agent.runtime.tools.policy import ToolPolicy

    calls = []
    requests = []

    async def execute(command: str):
        calls.append(command)
        return "ran"

    async def approve(request):
        requests.append(request)
        return "once"

    async def scenario():
        registry = ToolRegistry(ToolPolicy(mode="safe"))
        registry.register(ToolDef(
            name="execute_demo",
            description="demo",
            parameters={"type": "object", "properties": {"command": {"type": "string"}}},
            fn=execute,
            risk="execute",
            approval="on_risk",
        ))
        registry.set_approval_handler(approve)

        result = await registry.execute(
            "execute_demo", {"command": "do work"}, call_id="call-policy-1"
        )

        assert result["output"] == "ran"
        assert calls == ["do work"]
        assert requests[0]["call_id"] == "call-policy-1"
        assert requests[0]["kind"] == "tool_policy"

    run(scenario())


def test_file_write_can_pause_for_one_shot_path_approval_without_regeneration(tmp_path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    target = outside / "page.html"
    content = "x" * 9_044
    requests = []

    async def approve_once(request):
        requests.append(request)
        return "once"

    async def scenario():
        registry = ToolRegistry()
        register_file_tools(registry, str(workspace))
        registry.set_approval_handler(approve_once)

        first = await registry.execute(
            "write_file",
            {"path": str(target), "content": content},
            call_id="call-write-1",
        )
        assert first["error"] == ""
        assert target.read_text(encoding="utf-8") == content
        assert requests[0]["call_id"] == "call-write-1"
        assert requests[0]["target"] == str(target.resolve())
        assert requests[0]["arguments"]["content"] == "<9044 chars preserved>"
        assert requests[0]["change_summary"] == {
            "kind": "create_or_overwrite",
            "additions": 1,
            "deletions": 0,
        }
        assert f"+++ {target.resolve()}" in requests[0]["preview"]

        # One-shot approval is revoked after the original call, so a later
        # call asks again instead of silently widening the filesystem roots.
        current_sha256 = __import__("hashlib").sha256(target.read_bytes()).hexdigest()
        second = await registry.execute(
            "write_file",
            {
                "path": str(target),
                "content": "updated",
                "overwrite": True,
                "expected_sha256": current_sha256,
            },
            call_id="call-write-2",
        )
        assert second["error"] == ""
        assert len(requests) == 2

    run(scenario())


def test_file_write_session_approval_reuses_exact_path_and_denial_does_not_write(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    approved_target = tmp_path / "approved" / "page.html"
    denied_target = tmp_path / "denied" / "page.html"
    decisions = iter(("session", "deny"))
    requests = []

    async def decide(request):
        requests.append(request)
        return next(decisions)

    async def scenario():
        registry = ToolRegistry()
        register_file_tools(registry, str(workspace))
        registry.set_approval_handler(decide)

        first = await registry.execute(
            "write_file", {"path": str(approved_target), "content": "one"}
        )
        current_sha256 = __import__("hashlib").sha256(approved_target.read_bytes()).hexdigest()
        second = await registry.execute(
            "write_file",
            {
                "path": str(approved_target),
                "content": "two",
                "overwrite": True,
                "expected_sha256": current_sha256,
            },
        )
        denied = await registry.execute(
            "write_file", {"path": str(denied_target), "content": "never"}
        )

        assert first["error"] == ""
        assert second["error"] == ""
        assert approved_target.read_text(encoding="utf-8") == "two"
        assert len(requests) == 2  # first target once, then the different denied target
        assert denied["error_type"] == "approval_denied"
        assert not denied_target.exists()

    run(scenario())


def test_filesystem_policy_normalizes_wsl_mount_path_on_windows(tmp_path):
    if os.name != "nt":
        return
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sample = workspace / "sample.txt"
    sample.write_text("ok", encoding="utf-8")
    windows_path = str(sample.resolve())
    drive, tail = windows_path[0], windows_path[3:].replace("\\", "/")
    wsl_path = f"/mnt/{drive.lower()}/{tail}"

    policy = FilesystemPolicy(workspace)

    assert policy.resolve(wsl_path) == sample.resolve()


def test_write_file_verifies_persisted_content(tmp_path):
    async def scenario():
        registry = ToolRegistry()
        register_file_tools(registry, str(tmp_path))
        target = tmp_path / "verified.txt"

        result = await registry.execute(
            "write_file",
            {"path": str(target), "content": "persisted content"},
        )

        assert result["error"] == ""
        assert result["verified"] is True
        assert target.read_text(encoding="utf-8") == "persisted content"

    run(scenario())


@pytest.mark.parametrize("content", ["a\r\nb\r\n", "a\rb\r", "mixed\r\nlines\nhere\r"])
def test_write_file_verifies_carriage_returns_byte_for_byte(tmp_path, content):
    async def scenario():
        registry = ToolRegistry()
        register_file_tools(registry, str(tmp_path))
        target = tmp_path / "endings.txt"

        result = await registry.execute(
            "write_file",
            {"path": str(target), "content": content},
        )

        assert result["error"] == ""
        assert result["verified"] is True
        assert target.read_bytes() == content.encode("utf-8")

    run(scenario())


def test_transactional_file_write_commits_complete_content_atomically(tmp_path):
    async def scenario():
        registry = ToolRegistry()
        register_file_tools(registry, str(tmp_path))
        target = tmp_path / "large.html"
        chunks = ["<html>\n", "示例" * 2_000, "\n</html>"]
        payload = "".join(chunks)
        encoded = payload.encode("utf-8")
        digest = __import__("hashlib").sha256(encoded).hexdigest()

        started = await registry.execute(
            "begin_file_write",
            {
                "path": str(target),
                "overwrite": False,
                "expected_size": len(encoded),
            },
        )
        assert started["error"] == ""
        write_id = json.loads(started["output"])["write_id"]
        assert not target.exists()

        for sequence, content in enumerate(chunks):
            accepted = await registry.execute(
                "write_file_chunk",
                {"write_id": write_id, "sequence": sequence, "content": content},
            )
            assert accepted["error"] == ""
            assert not target.exists()

        committed = await registry.execute(
            "commit_file_write",
            {"write_id": write_id, "expected_sha256": digest},
        )

        assert committed["error"] == ""
        result = json.loads(committed["output"])
        assert result["status"] == "committed"
        assert result["chunks"] == len(chunks)
        assert result["sha256"] == digest
        assert result["artifact_ref"] == str(target)
        assert result["diff"] == {
            "kind": "create",
            "additions": len(payload.splitlines()),
            "deletions": 0,
        }
        assert target.read_text(encoding="utf-8") == payload

    run(scenario())


def test_transactional_file_write_rejects_bad_sequence_and_hash_without_target(tmp_path):
    async def scenario():
        registry = ToolRegistry()
        register_file_tools(registry, str(tmp_path))
        target = tmp_path / "guarded.txt"

        started = await registry.execute(
            "begin_file_write",
            {"path": str(target), "overwrite": False},
        )
        write_id = json.loads(started["output"])["write_id"]

        out_of_order = await registry.execute(
            "write_file_chunk",
            {"write_id": write_id, "sequence": 1, "content": "second"},
        )
        assert "out-of-order chunk" in out_of_order["error"]

        accepted = await registry.execute(
            "write_file_chunk",
            {"write_id": write_id, "sequence": 0, "content": "first"},
        )
        assert accepted["error"] == ""

        bad_hash = await registry.execute(
            "commit_file_write",
            {"write_id": write_id, "expected_sha256": "0" * 64},
        )
        assert "SHA-256 mismatch" in bad_hash["error"]
        assert not target.exists()

        aborted = await registry.execute("abort_file_write", {"write_id": write_id})
        assert aborted["error"] == ""
        assert json.loads(aborted["output"])["status"] == "aborted"
        assert not target.exists()

    run(scenario())


def test_transactional_file_write_failures_carry_a_code_and_recovery(tmp_path):
    async def scenario():
        registry = ToolRegistry()
        register_file_tools(registry, str(tmp_path))
        existing = tmp_path / "existing.txt"
        existing.write_text("keep", encoding="utf-8")

        refused = await registry.execute("begin_file_write", {"path": str(existing)})
        assert refused["code"] == "target_exists"
        assert refused["output"] == ""
        assert "overwrite=true" in refused["recovery_hint"]
        assert refused["details"]["path"] == str(existing)

        negative = await registry.execute(
            "begin_file_write",
            {"path": str(tmp_path / "sized.txt"), "expected_size": -1},
        )
        assert negative["code"] == "invalid_arguments"
        assert negative["recovery_hint"]

        target = tmp_path / "ordered.txt"
        started = await registry.execute("begin_file_write", {"path": str(target)})
        write_id = json.loads(started["output"])["write_id"]

        out_of_order = await registry.execute(
            "write_file_chunk",
            {"write_id": write_id, "sequence": 2, "content": "late"},
        )
        assert out_of_order["code"] == "chunk_out_of_order"
        assert out_of_order["retryable"] is True
        assert out_of_order["details"]["expected_sequence"] == 0
        assert out_of_order["details"]["received_sequence"] == 2
        assert "sequence 0" in out_of_order["recovery_hint"]

        # The refused chunk was not written, so the stated sequence is accepted.
        accepted = await registry.execute(
            "write_file_chunk",
            {"write_id": write_id, "sequence": 0, "content": "first"},
        )
        assert accepted["error"] == ""

        # The target appears while the transaction is open.
        target.write_text("someone else", encoding="utf-8")
        blocked = await registry.execute("commit_file_write", {"write_id": write_id})
        assert blocked["code"] == "target_exists"
        assert blocked["recovery_hint"]
        assert target.read_text(encoding="utf-8") == "someone else"

        aborted = await registry.execute("abort_file_write", {"write_id": write_id})
        assert aborted["error"] == ""

        for tool, arguments in (
            ("write_file_chunk", {"write_id": write_id, "sequence": 1, "content": "x"}),
            ("commit_file_write", {"write_id": write_id}),
            ("abort_file_write", {"write_id": write_id}),
        ):
            closed = await registry.execute(tool, arguments)
            assert closed["code"] == "unknown_write_id", tool
            assert closed["retryable"] is False
            assert "begin_file_write" in closed["recovery_hint"]
            assert closed["details"]["write_id"] == write_id

    run(scenario())


def test_transactional_file_write_session_end_cleans_manifest_and_partial_data(tmp_path):
    async def scenario():
        registry = ToolRegistry()
        register_file_tools(registry, str(tmp_path))
        target = tmp_path / "unfinished.txt"

        started = await registry.execute(
            "begin_file_write",
            {"path": str(target), "overwrite": False},
        )
        write_id = json.loads(started["output"])["write_id"]
        accepted = await registry.execute(
            "write_file_chunk",
            {"write_id": write_id, "sequence": 0, "content": "partial"},
        )
        assert accepted["error"] == ""

        transaction_dir = tmp_path / ".astra" / "file-transactions"
        assert (transaction_dir / f"{write_id}.part").exists()
        assert (transaction_dir / f"{write_id}.manifest.json").exists()

        registry.hooks.dispatch_session_end("demo", "session_switch")

        assert not (transaction_dir / f"{write_id}.part").exists()
        assert not (transaction_dir / f"{write_id}.manifest.json").exists()
        assert not target.exists()
        audit = (transaction_dir / "audit.jsonl").read_text(encoding="utf-8")
        assert "session_end:demo:session_switch" in audit

    run(scenario())


def test_transactional_file_write_startup_cleans_crash_orphans(tmp_path):
    async def scenario():
        first_registry = ToolRegistry()
        register_file_tools(first_registry, str(tmp_path))
        target = tmp_path / "crashed.txt"
        started = await first_registry.execute(
            "begin_file_write",
            {"path": str(target), "overwrite": False},
        )
        write_id = json.loads(started["output"])["write_id"]
        transaction_dir = tmp_path / ".astra" / "file-transactions"
        assert (transaction_dir / f"{write_id}.part").exists()

        second_registry = ToolRegistry()
        register_file_tools(second_registry, str(tmp_path))

        assert not (transaction_dir / f"{write_id}.part").exists()
        assert not (transaction_dir / f"{write_id}.manifest.json").exists()
        assert not target.exists()
        audit = (transaction_dir / "audit.jsonl").read_text(encoding="utf-8")
        assert "process_restart" in audit

    run(scenario())


def test_write_file_does_not_expose_or_enforce_an_arbitrary_character_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("TOOL_DIRECT_WRITE_CHARS", "100")

    async def scenario():
        registry = ToolRegistry()
        register_file_tools(registry, str(tmp_path))
        target = tmp_path / "complete.txt"
        content = "x" * 50_000

        result = await registry.execute(
            "write_file",
            {"path": str(target), "content": content},
        )

        assert result["error"] == ""
        assert target.read_text(encoding="utf-8") == content
        schema = next(
            item["function"]
            for item in registry.to_openai_tools(groups={"files"})
            if item["function"]["name"] == "write_file"
        )
        assert "maxLength" not in schema["parameters"]["properties"]["content"]
        assert "expected_size" not in schema["parameters"]["properties"]

    run(scenario())


def test_edit_file_preserves_utf8_bom_and_crlf_and_checks_version(tmp_path):
    async def scenario():
        registry = ToolRegistry()
        register_file_tools(registry, str(tmp_path))
        target = tmp_path / "windows.txt"
        original = b"\xef\xbb\xbfalpha\r\nbeta\r\n"
        target.write_bytes(original)
        digest = __import__("hashlib").sha256(original).hexdigest()

        result = await registry.execute(
            "edit_file",
            {
                "path": str(target),
                "old": "alpha\nbeta",
                "new": "alpha\ngamma",
                "expected_sha256": digest,
            },
        )

        assert result["error"] == ""
        metadata = json.loads(result["output"])
        assert metadata["encoding"] == "utf-8-sig"
        assert metadata["line_endings"] == "crlf"
        assert target.read_bytes() == b"\xef\xbb\xbfalpha\r\ngamma\r\n"

        stale = await registry.execute(
            "edit_file",
            {
                "path": str(target),
                "old": "gamma",
                "new": "delta",
                "expected_sha256": digest,
            },
        )
        assert stale["code"] == "file_changed"
        assert b"delta" not in target.read_bytes()

    run(scenario())


@pytest.mark.parametrize(
    "original,old,new,expected,removed,added",
    [
        # old stops before the line break: the last line keeps its own ending.
        (
            b"def f():  \n    return 1\t\nprint(f())\n",
            "def f():\n    return 1",
            "def f():\n    return 2",
            b"def f():\n    return 2\t\nprint(f())\n",
            "def f():  \n    return 1",
            "def f():\n    return 2",
        ),
        # old ends with a line break: whole lines are replaced, nothing after them.
        (
            b"alpha  \nbeta\ngamma\n",
            "alpha\nbeta\n",
            "ALPHA\nBETA\n",
            b"ALPHA\nBETA\ngamma\n",
            "alpha  \nbeta\n",
            "ALPHA\nBETA\n",
        ),
        # CRLF file, LF arguments.
        (
            b"alpha  \r\nbeta\r\ngamma\r\n",
            "alpha\nbeta",
            "ALPHA\nBETA\nextra",
            b"ALPHA\r\nBETA\r\nextra\r\ngamma\r\n",
            "alpha  \r\nbeta",
            "ALPHA\r\nBETA\r\nextra",
        ),
        # CRLF file, CRLF arguments: line endings are not doubled.
        (
            b"alpha  \r\nbeta\r\ngamma\r\n",
            "alpha\r\nbeta\r\n",
            "ALPHA\r\nBETA\r\n",
            b"ALPHA\r\nBETA\r\ngamma\r\n",
            "alpha  \r\nbeta\r\n",
            "ALPHA\r\nBETA\r\n",
        ),
        # LF file, CRLF arguments.
        (
            b"alpha  \nbeta\ngamma\n",
            "alpha\r\nbeta",
            "ALPHA\r\nBETA",
            b"ALPHA\nBETA\ngamma\n",
            "alpha  \nbeta",
            "ALPHA\nBETA",
        ),
    ],
)
def test_edit_file_writes_the_trailing_whitespace_tolerant_match(
    tmp_path, original, old, new, expected, removed, added
):
    async def scenario():
        registry = ToolRegistry()
        register_file_tools(registry, str(tmp_path))
        target = tmp_path / "drift.txt"
        target.write_bytes(original)

        result = await registry.execute(
            "edit_file",
            {"path": str(target), "old": old, "new": new},
        )

        assert result["error"] == ""
        metadata = json.loads(result["output"])
        landed = target.read_bytes()
        assert landed == expected
        assert metadata["replacements"] == 1
        assert metadata["before_sha256"] == hashlib.sha256(original).hexdigest()
        assert metadata["after_sha256"] == hashlib.sha256(landed).hexdigest()
        assert metadata["before_sha256"] != metadata["after_sha256"]
        # The counts describe the text really removed from and written to the file.
        assert metadata["removed_chars"] == len(removed)
        assert metadata["added_chars"] == len(added)
        assert original.replace(removed.encode("utf-8"), added.encode("utf-8"), 1) == landed

    run(scenario())


def test_edit_file_rejects_an_ambiguous_trailing_whitespace_match(tmp_path):
    async def scenario():
        registry = ToolRegistry()
        register_file_tools(registry, str(tmp_path))
        target = tmp_path / "twice.txt"
        original = b"x = 1  \ny = 2\n\nx = 1\t\ny = 2\n"
        target.write_bytes(original)

        result = await registry.execute(
            "edit_file",
            {"path": str(target), "old": "x = 1\ny = 2", "new": "x = 3\ny = 2"},
        )

        assert result["code"] == "edit_match_ambiguous"
        assert result["details"]["matches"] == 2
        assert target.read_bytes() == original

    run(scenario())


def test_default_file_schema_hides_internal_transaction_protocol(tmp_path):
    registry = ToolRegistry()
    register_file_tools(registry, str(tmp_path))

    exposed = {
        item["function"]["name"]
        for item in registry.to_openai_tools(groups={"files"})
    }

    assert {"read_file", "stat_file", "write_file", "edit_file", "apply_patch"} <= exposed
    assert not {
        "begin_file_write",
        "write_file_chunk",
        "commit_file_write",
        "abort_file_write",
    } & exposed
    assert registry.get("begin_file_write") is not None


def test_write_file_is_create_first_and_explicit_overwrite_checks_version(tmp_path):
    async def scenario():
        registry = ToolRegistry()
        register_file_tools(registry, str(tmp_path))
        target = tmp_path / "existing.txt"
        target.write_text("original", encoding="utf-8")
        digest = __import__("hashlib").sha256(target.read_bytes()).hexdigest()

        refused = await registry.execute(
            "write_file",
            {"path": str(target), "content": "replacement"},
        )
        assert refused["code"] == "existing_file_requires_edit"
        assert refused["retryable"] is True
        assert "edit_file" in refused["recovery_hint"]
        assert target.read_text(encoding="utf-8") == "original"

        missing_version = await registry.execute(
            "write_file",
            {"path": str(target), "content": "replacement", "overwrite": True},
        )
        assert missing_version["code"] == "file_changed"
        assert target.read_text(encoding="utf-8") == "original"

        overwritten = await registry.execute(
            "write_file",
            {
                "path": str(target),
                "content": "replacement",
                "overwrite": True,
                "expected_sha256": digest,
            },
        )
        assert overwritten["error"] == ""
        assert json.loads(overwritten["output"])["operation"] == "overwrite"
        assert target.read_text(encoding="utf-8") == "replacement"

    run(scenario())


def test_read_file_supports_line_paging_and_version_metadata(tmp_path):
    async def scenario():
        registry = ToolRegistry()
        register_file_tools(registry, str(tmp_path))
        target = tmp_path / "paged.txt"
        target.write_bytes(b"one\ntwo\nthree\nfour\n")

        complete = await registry.execute("read_file", {"path": str(target)})
        assert complete["output"] == "one\ntwo\nthree\nfour\n"

        paged = await registry.execute(
            "read_file",
            {
                "path": str(target),
                "offset": 1,
                "limit": 2,
                "include_metadata": True,
            },
        )
        assert '"line_start": 2' in paged["output"]
        assert '"line_end": 3' in paged["output"]
        assert '"truncated": true' in paged["output"]
        assert paged["output"].endswith("two\nthree\n")

    run(scenario())


def test_read_file_failures_are_structured_and_actionable(tmp_path):
    async def scenario():
        registry = ToolRegistry()
        register_file_tools(registry, str(tmp_path))

        missing = await registry.execute(
            "read_file",
            {"path": str(tmp_path / "missing.txt")},
        )
        assert missing["code"] == "file_not_found"
        assert missing["retryable"] is True
        assert "search the workspace" in missing["recovery_hint"]
        assert missing["details"]["path"].endswith("missing.txt")

        directory = await registry.execute(
            "read_file",
            {"path": str(tmp_path)},
        )
        assert directory["code"] == "not_a_file"
        assert directory["retryable"] is True
        assert directory["details"]["path"] == str(tmp_path)

        binary = tmp_path / "binary.dat"
        binary.write_bytes(b"\xff\xfe")
        invalid_utf8 = await registry.execute(
            "read_file",
            {"path": str(binary)},
        )
        assert invalid_utf8["code"] == "file_not_utf8"
        assert invalid_utf8["retryable"] is False
        assert "binary" in invalid_utf8["recovery_hint"]

    run(scenario())


def test_read_file_streams_huge_lines_with_a_bounded_byte_cursor(tmp_path, monkeypatch):
    monkeypatch.setenv("READ_FILE_MAX_PAGE_BYTES", "1024")
    monkeypatch.setenv("READ_FILE_MAX_LINE_BYTES", "1024")
    target = tmp_path / "huge-line.txt"
    target.write_text("a" * 5000 + "\nsecond\n", encoding="utf-8")
    monkeypatch.setattr(Path, "read_bytes", lambda self: (_ for _ in ()).throw(
        AssertionError("read_file must not materialize the whole file")
    ))

    async def scenario():
        registry = ToolRegistry()
        register_file_tools(registry, str(tmp_path))
        first = await registry.execute("read_file", {"path": str(target)})
        assert first["error"] == ""
        header, payload = first["output"].split("\n", 1)
        metadata = json.loads(header.removeprefix("[File metadata: ").removesuffix("]"))
        assert metadata["line_truncated"] is True
        assert metadata["next_byte_offset"] == 1024
        assert payload == "a" * 1024

        continued = await registry.execute(
            "read_file",
            {"path": str(target), "byte_offset": metadata["next_byte_offset"]},
        )
        assert continued["error"] == ""
        assert continued["output"].endswith("a" * 1024)

    run(scenario())


def _read_file_page(result: dict) -> tuple[dict, str]:
    assert result["error"] == ""
    header, payload = result["output"].split("\n", 1)
    return json.loads(header.removeprefix("[File metadata: ").removesuffix("]")), payload


def test_read_file_line_paging_reports_what_remains_after_each_page(tmp_path):
    async def scenario():
        registry = ToolRegistry()
        register_file_tools(registry, str(tmp_path))
        target = tmp_path / "paged.txt"
        text = "".join(f"line{index}\n" for index in range(10))
        target.write_text(text, encoding="utf-8")

        pages = []
        offset = 0
        while offset is not None:
            result = await registry.execute(
                "read_file",
                {"path": str(target), "offset": offset, "limit": 4},
            )
            metadata, payload = _read_file_page(result)
            pages.append(payload)
            # Line paging never hands out a byte cursor, and the file size in
            # lines is known from the first page on.
            assert metadata["next_byte_offset"] is None
            assert metadata["line_truncated"] is False
            assert metadata["total_lines"] == 10
            assert metadata["truncated"] is (metadata["next_offset"] is not None)
            assert bool(result.get("partial")) is metadata["truncated"]
            offset = metadata["next_offset"]

        assert pages == ["".join(f"line{index}\n" for index in range(start, min(start + 4, 10)))
                         for start in (0, 4, 8)]
        assert metadata["line_start"] == 9
        assert metadata["line_end"] == 10

        whole = await registry.execute("read_file", {"path": str(target)})
        assert whole["output"] == text
        assert not whole.get("partial")

    run(scenario())


def test_read_file_states_the_default_line_limit_it_applies(tmp_path, monkeypatch):
    monkeypatch.setenv("READ_FILE_DEFAULT_LINES", "3")

    async def scenario():
        registry = ToolRegistry()
        register_file_tools(registry, str(tmp_path))
        target = tmp_path / "default.txt"
        target.write_text("".join(f"{index}\n" for index in range(8)), encoding="utf-8")

        metadata, payload = _read_file_page(
            await registry.execute("read_file", {"path": str(target)})
        )
        assert payload == "0\n1\n2\n"
        assert metadata["next_offset"] == 3

        schema = registry.to_openai_tools(names={"read_file"})[0]["function"]
        assert "default 3" in schema["description"]
        assert "default 3" in schema["parameters"]["properties"]["limit"]["description"]

    run(scenario())


def test_read_file_page_size_cap_ends_the_page_between_lines(tmp_path, monkeypatch):
    monkeypatch.setenv("READ_FILE_MAX_PAGE_BYTES", "1024")

    async def scenario():
        registry = ToolRegistry()
        register_file_tools(registry, str(tmp_path))
        target = tmp_path / "wide.txt"
        text = "".join(f"{index}" * 299 + "\n" for index in range(7))
        target.write_text(text, encoding="utf-8")

        pages = []
        offset = 0
        while offset is not None:
            metadata, payload = _read_file_page(
                await registry.execute("read_file", {"path": str(target), "offset": offset})
            )
            # An ordinary line is never split, so no byte cursor is needed.
            assert payload.endswith("\n")
            assert metadata["line_truncated"] is False
            assert metadata["next_byte_offset"] is None
            pages.append(payload)
            offset = metadata["next_offset"]

        assert len(pages) > 1
        assert "".join(pages) == text

    run(scenario())


def test_read_file_byte_offset_finishes_a_cut_line_and_honours_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("READ_FILE_MAX_PAGE_BYTES", "1024")
    monkeypatch.setenv("READ_FILE_MAX_LINE_BYTES", "1024")

    async def scenario():
        registry = ToolRegistry()
        register_file_tools(registry, str(tmp_path))
        target = tmp_path / "long-line.txt"
        target.write_text("a" * 1500 + "\nsecond\nthird\n", encoding="utf-8")

        first = await registry.execute("read_file", {"path": str(target)})
        metadata, payload = _read_file_page(first)
        assert payload == "a" * 1024
        assert metadata["line_truncated"] is True
        assert metadata["truncated"] is True
        assert metadata["next_byte_offset"] == 1024
        # The line after the cut one is where line paging resumes.
        assert metadata["next_offset"] == 1
        assert first["partial"] is True

        rest = await registry.execute(
            "read_file",
            {"path": str(target), "byte_offset": metadata["next_byte_offset"], "limit": 1},
        )
        rest_metadata, rest_payload = _read_file_page(rest)
        assert rest_payload == "a" * 476 + "\n"
        assert rest_metadata["line_truncated"] is False
        assert rest_metadata["truncated"] is True
        assert rest_metadata["next_byte_offset"] == 1501
        assert rest["partial"] is True

        resumed = await registry.execute(
            "read_file",
            {"path": str(target), "offset": metadata["next_offset"]},
        )
        resumed_metadata, resumed_payload = _read_file_page(resumed)
        assert resumed_payload == "second\nthird\n"
        assert resumed_metadata["truncated"] is False
        assert resumed_metadata["next_offset"] is None
        assert not resumed.get("partial")

    run(scenario())


def test_read_file_cuts_an_over_long_multibyte_line_between_characters(tmp_path, monkeypatch):
    monkeypatch.setenv("READ_FILE_MAX_PAGE_BYTES", "1024")
    monkeypatch.setenv("READ_FILE_MAX_LINE_BYTES", "1024")

    async def scenario():
        registry = ToolRegistry()
        register_file_tools(registry, str(tmp_path))
        target = tmp_path / "wide-characters.txt"
        text = "中" * 1000 + "\n"
        target.write_text(text, encoding="utf-8")

        metadata, payload = _read_file_page(
            await registry.execute("read_file", {"path": str(target)})
        )
        assert metadata["line_truncated"] is True
        assert payload == "中" * 341
        assert metadata["next_byte_offset"] == 341 * 3

        pieces = [payload]
        while metadata["truncated"]:
            metadata, payload = _read_file_page(await registry.execute(
                "read_file",
                {"path": str(target), "byte_offset": metadata["next_byte_offset"]},
            ))
            pieces.append(payload)
        assert "".join(pieces) == text

    run(scenario())


def test_final_answer_is_persisted_before_done_is_visible(tmp_path):
    class FakeLLM:
        async def chat_stream(self, messages, tools):
            del messages, tools
            yield {"type": "done", "content": "durable answer", "usage": None}

    async def scenario():
        session = tmp_path / "durable.jsonl"
        agent = ReActAgent("agent", FakeLLM(), ToolRegistry(), max_iterations=1)
        agent.context.set_session(str(session))
        msg = Msg(content=[ContentBlock.text("question")])
        saw_done = False
        async for event in agent._run_react_loop(msg):
            if event["type"] != "done":
                continue
            saw_done = True
            persisted = SessionStore(session).load()["messages"]
            assert persisted[-1]["role"] == "assistant"
            assert persisted[-1]["content"] == "durable answer"
        assert saw_done

    run(scenario())


def test_react_file_read_is_fresh_after_edit_with_identical_arguments(tmp_path):
    class FakeLLM:
        def __init__(self):
            self.calls = 0

        async def chat_stream(self, messages, tools):
            del messages, tools
            self.calls += 1
            if self.calls == 1:
                call = {
                    "id": "read-a",
                    "name": "read_file",
                    "arguments": json.dumps({"path": str(tmp_path / "version.txt")}),
                }
            elif self.calls == 2:
                call = {
                    "id": "edit-b",
                    "name": "edit_file",
                    "arguments": json.dumps({
                        "path": str(tmp_path / "version.txt"),
                        "old": "VERSION_A",
                        "new": "VERSION_B",
                    }),
                }
            elif self.calls == 3:
                call = {
                    "id": "read-b",
                    "name": "read_file",
                    "arguments": json.dumps({"path": str(tmp_path / "version.txt")}),
                }
            else:
                yield {
                    "type": "done",
                    "content": "verified",
                    "finish_reason": "stop",
                    "usage": None,
                }
                return
            yield {
                "type": "tool_calls",
                "calls": [call],
                "content": "",
                "reasoning_content": "",
                "finish_reason": "tool_calls",
                "usage": None,
            }

    async def scenario():
        target = tmp_path / "version.txt"
        target.write_text("VERSION_A", encoding="utf-8")
        registry = ToolRegistry()
        register_file_tools(registry, str(tmp_path))
        agent = ReActAgent(
            "fresh-read",
            FakeLLM(),
            registry,
            max_iterations=4,
            progressive_tools=False,
        )
        agent.tool_allowlist = {"read_file", "edit_file"}

        events = [
            event
            async for event in agent.reply_stream(
                Msg(content=[ContentBlock.text("read, edit, then verify")])
            )
        ]

        reads = [
            event
            for event in events
            if event.get("type") == "tool_result"
            and event.get("name") == "read_file"
        ]
        assert [event["output"] for event in reads] == ["VERSION_A", "VERSION_B"]
        assert not any(event.get("cached") for event in reads)
        assert target.read_text(encoding="utf-8") == "VERSION_B"

    run(scenario())


def test_stat_file_returns_fresh_hash_size_mtime_and_boundary_preview(tmp_path):
    async def scenario():
        registry = ToolRegistry()
        register_file_tools(registry, str(tmp_path))
        target = tmp_path / "status.txt"
        target.write_text("first\nmiddle\nlast\n", encoding="utf-8")

        first = await registry.execute(
            "stat_file",
            {"path": str(target), "preview_lines": 1},
        )
        first_data = json.loads(first["output"])
        assert first["error"] == ""
        assert first_data["bytes"] == len(target.read_bytes())
        assert first_data["sha256"] == __import__("hashlib").sha256(
            target.read_bytes()
        ).hexdigest()
        assert first_data["first_lines"] == ["first"]
        assert first_data["last_lines"] == ["last"]
        assert first_data["total_lines"] == 3
        assert first_data["mtime_ns"] == target.stat().st_mtime_ns

        target.write_text("changed\n", encoding="utf-8")
        second = await registry.execute(
            "stat_file",
            {"path": str(target), "preview_lines": 1},
        )
        second_data = json.loads(second["output"])
        assert second_data["sha256"] != first_data["sha256"]
        assert second_data["first_lines"] == ["changed"]

    run(scenario())


def test_transaction_commit_rejects_truncated_or_tampered_temp_data(tmp_path):
    async def scenario():
        registry = ToolRegistry()
        register_file_tools(registry, str(tmp_path))
        transaction_dir = tmp_path / ".astra" / "file-transactions"

        async def begin_and_write(name: str) -> tuple[str, Path]:
            started = await registry.execute(
                "begin_file_write",
                {"path": str(tmp_path / name)},
            )
            write_id = json.loads(started["output"])["write_id"]
            accepted = await registry.execute(
                "write_file_chunk",
                {"write_id": write_id, "sequence": 0, "content": "alpha"},
            )
            accepted_data = json.loads(accepted["output"])
            assert accepted_data["chunk_sha256"]
            assert accepted_data["running_sha256"]
            return write_id, transaction_dir / f"{write_id}.part"

        truncated_id, truncated_part = await begin_and_write("truncated.txt")
        truncated_part.write_bytes(b"alp")
        truncated = await registry.execute(
            "commit_file_write",
            {"write_id": truncated_id},
        )
        assert truncated["code"] == "transaction_integrity_failed"
        assert truncated["partial"] is True
        assert not (tmp_path / "truncated.txt").exists()
        await registry.execute("abort_file_write", {"write_id": truncated_id})

        tampered_id, tampered_part = await begin_and_write("tampered.txt")
        tampered_part.write_bytes(b"bravo")
        tampered = await registry.execute(
            "commit_file_write",
            {"write_id": tampered_id},
        )
        assert tampered["code"] == "transaction_integrity_failed"
        assert "running hash mismatch" in tampered["error"]
        assert not (tmp_path / "tampered.txt").exists()
        await registry.execute("abort_file_write", {"write_id": tampered_id})

    run(scenario())


def test_model_tool_error_context_includes_code_recovery_and_details():
    context = ReActAgent._tool_result_context({
        "name": "write_file",
        "error": "write_file will not overwrite existing file",
        "tool_output": "write_file will not overwrite existing file",
        "code": "existing_file_requires_edit",
        "retryable": True,
        "recovery_hint": "Use edit_file or apply_patch.",
        "details": {"path": "existing.txt"},
    })

    assert "Error code: existing_file_requires_edit" in context
    assert "Retryable: yes" in context
    assert "Recovery: Use edit_file or apply_patch." in context
    assert 'Details: {"path": "existing.txt"}' in context


def test_apply_patch_path_error_names_workspace_root_and_relative_rule(tmp_path):
    async def scenario():
        registry = ToolRegistry()
        register_file_tools(registry, str(tmp_path))
        absolute = tmp_path / "probe.txt"
        result = await registry.execute(
            "apply_patch",
            {
                "patch": (
                    "*** Begin Patch\n"
                    f"*** Add File: {absolute}\n"
                    "+probe\n"
                    "*** End Patch"
                ),
            },
        )

        assert result["code"] == "patch_precondition_failed"
        assert str(tmp_path) in result["error"]
        assert str(tmp_path) in result["recovery_hint"]
        assert result["details"]["paths"] == "workspace-relative"
        assert not absolute.exists()

    run(scenario())


def test_apply_patch_dry_run_then_updates_and_adds_files(tmp_path):
    async def scenario():
        registry = ToolRegistry()
        register_file_tools(registry, str(tmp_path))
        existing = tmp_path / "existing.txt"
        added = tmp_path / "added.txt"
        existing.write_text("alpha\nbeta\n", encoding="utf-8")
        patch = "\n".join([
            "*** Begin Patch",
            "*** Update File: existing.txt",
            "@@",
            " alpha",
            "-beta",
            "+gamma",
            "*** Add File: added.txt",
            "+new",
            "+file",
            "*** End Patch",
        ])

        preview = await registry.execute("apply_patch", {"patch": patch, "dry_run": True})
        assert preview["error"] == ""
        preview_data = json.loads(preview["output"])
        assert preview_data["status"] == "dry_run"
        assert preview_data["files"] == 2
        assert existing.read_text(encoding="utf-8") == "alpha\nbeta\n"
        assert not added.exists()

        applied = await registry.execute("apply_patch", {"patch": patch})
        assert applied["error"] == ""
        assert json.loads(applied["output"])["status"] == "applied"
        assert existing.read_text(encoding="utf-8") == "alpha\ngamma\n"
        assert added.read_text(encoding="utf-8") == "new\nfile\n"

    run(scenario())


def test_apply_patch_delete_requires_approval_and_denial_has_zero_side_effects(tmp_path):
    decisions = iter(("deny", "once"))

    async def decide(request):
        assert request["kind"] == "filesystem_patch"
        assert request["operation"] == "Apply multi-file patch"
        return next(decisions)

    async def scenario():
        registry = ToolRegistry()
        register_file_tools(registry, str(tmp_path))
        registry.set_approval_handler(decide)
        target = tmp_path / "delete-me.txt"
        target.write_text("keep until approved", encoding="utf-8")
        patch = "\n".join([
            "*** Begin Patch",
            "*** Delete File: delete-me.txt",
            "*** End Patch",
        ])

        denied = await registry.execute("apply_patch", {"patch": patch}, call_id="patch-1")
        assert denied["code"] == "approval_denied"
        assert target.exists()

        approved = await registry.execute("apply_patch", {"patch": patch}, call_id="patch-2")
        assert approved["error"] == ""
        assert not target.exists()

    run(scenario())


def test_apply_patch_invalid_hunk_leaves_all_files_unchanged(tmp_path):
    async def scenario():
        registry = ToolRegistry()
        register_file_tools(registry, str(tmp_path))
        first = tmp_path / "first.txt"
        first.write_text("current\n", encoding="utf-8")
        patch = "\n".join([
            "*** Begin Patch",
            "*** Update File: first.txt",
            "@@",
            "-stale",
            "+changed",
            "*** Add File: second.txt",
            "+must not appear",
            "*** End Patch",
        ])

        result = await registry.execute("apply_patch", {"patch": patch})

        assert "did not match" in result["error"]
        assert first.read_text(encoding="utf-8") == "current\n"
        assert not (tmp_path / "second.txt").exists()

    run(scenario())


def test_apply_patch_rejects_standard_unified_diff_without_side_effects(tmp_path):
    async def scenario():
        registry = ToolRegistry()
        register_file_tools(registry, str(tmp_path))
        target = tmp_path / "sample.txt"
        target.write_text("before\n", encoding="utf-8")
        patch = "\n".join([
            "diff --git a/sample.txt b/sample.txt",
            "--- a/sample.txt",
            "+++ b/sample.txt",
            "@@ -1 +1 @@",
            "-before",
            "+after",
        ])

        result = await registry.execute("apply_patch", {"patch": patch})

        assert "not standard unified diff" in result["error"]
        assert target.read_text(encoding="utf-8") == "before\n"

    run(scenario())


@pytest.mark.parametrize(
    "original,expected",
    [
        (b"keep\na  \nb\n", b"keep\na\nB\n"),        # hunk at the end: final newline stays
        (b"\na  \nb\nrest\n", b"\na\nB\nrest\n"),  # hunk after a leading blank line
        (b"a  \nb", b"a\nB"),                           # no final newline before, none after
    ],
)
def test_apply_patch_whitespace_tolerant_match_keeps_the_other_lines(tmp_path, original, expected):
    async def scenario():
        registry = ToolRegistry()
        register_file_tools(registry, str(tmp_path))
        target = tmp_path / "drift.txt"
        target.write_bytes(original)
        patch = "\n".join([
            "*** Begin Patch",
            "*** Update File: drift.txt",
            "@@",
            " a",
            "-b",
            "+B",
            "*** End Patch",
        ])

        result = await registry.execute("apply_patch", {"patch": patch})

        assert result["error"] == ""
        assert target.read_bytes() == expected

    run(scenario())


def test_apply_patch_accepts_the_example_from_its_own_description(tmp_path):
    async def scenario():
        registry = ToolRegistry()
        register_file_tools(registry, str(tmp_path))
        schema = registry.to_openai_tools(names={"apply_patch"})[0]["function"]
        description = schema["description"]
        patch_text = schema["parameters"]["properties"]["patch"]["description"]
        # The model reads these strings as written: no escaped newlines or quotes.
        workspace = str(registry.filesystem_policy.workspace)
        for text in (description, patch_text):
            assert workspace in text
            assert "\\" not in text.replace(workspace, "")

        example = description.split("Example:\n", 1)[1]
        target = tmp_path / "src" / "main.py"
        target.parent.mkdir()
        target.write_text('def hello():\n    print("old")\n', encoding="utf-8")

        result = await registry.execute("apply_patch", {"patch": example})

        assert result["error"] == ""
        assert json.loads(result["output"])["status"] == "applied"
        assert target.read_text(encoding="utf-8") == (
            'def hello():\n    print("new")\n    print("extra")\n'
        )

    run(scenario())


def test_apply_patch_format_error_gets_a_format_hint(tmp_path):
    async def scenario():
        registry = ToolRegistry()
        register_file_tools(registry, str(tmp_path))
        target = tmp_path / "notes.txt"
        target.write_text("alpha\n\nbeta\n", encoding="utf-8")
        # The blank context line is missing its leading space.
        malformed = "\n".join([
            "*** Begin Patch",
            "*** Update File: notes.txt",
            "@@",
            " alpha",
            "",
            "-beta",
            "+gamma",
            "*** End Patch",
        ])

        result = await registry.execute("apply_patch", {"patch": malformed})

        assert result["code"] == "patch_precondition_failed"
        assert "invalid hunk line" in result["error"]
        assert "blank context line is a single space" in result["recovery_hint"]
        assert "unique context" not in result["recovery_hint"]
        assert target.read_text(encoding="utf-8") == "alpha\n\nbeta\n"

        # A well-formed patch that does not match still gets the context hint.
        stale = malformed.replace("\n\n-beta", "\n \n-stale")
        mismatch = await registry.execute("apply_patch", {"patch": stale})
        assert "did not match" in mismatch["error"]
        assert "unique context" in mismatch["recovery_hint"]
        assert "single space" not in mismatch["recovery_hint"]

        # With the leading space restored the same patch applies.
        fixed = await registry.execute(
            "apply_patch",
            {"patch": malformed.replace("\n\n-beta", "\n \n-beta")},
        )
        assert fixed["error"] == ""
        assert target.read_text(encoding="utf-8") == "alpha\n\ngamma\n"

    run(scenario())


def test_text_only_model_downgrades_image_url_content_before_api_request():
    original = [{
        "role": "user",
        "content": [
            {"type": "text", "text": "请看图"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ],
    }]

    prepared = _messages_for_capabilities(original, frozenset({"tools", "reasoning"}))

    assert isinstance(prepared[0]["content"], str)
    assert "请看图" in prepared[0]["content"]
    assert "text-only model" in prepared[0]["content"]
    assert "image_url" not in prepared[0]["content"]
    assert isinstance(original[0]["content"], list)


def test_text_only_model_routes_local_image_to_qwen_mm_tool(tmp_path):
    source_path = str(tmp_path / "images" / "error.png")
    original = [{
        "role": "user",
        "content": [
            {"type": "text", "text": "读取截图中的错误"},
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,AAAA"},
                "metadata": {"source_path": source_path},
            },
        ],
    }]

    prepared = _messages_for_capabilities(
        original,
        frozenset({"tools", "reasoning"}),
        frozenset({
            "mcp__qwen-mm-plugins__vision_chat",
            "mcp__qwen-mm-plugins__ocr",
            "mcp__qwen-mm-plugins__grounding",
        }),
    )

    content = prepared[0]["content"]
    assert source_path in content
    assert "mcp__qwen-mm-plugins__vision_chat" in content
    assert "mcp__qwen-mm-plugins__ocr" in content
    assert "Do not say the image was omitted" in content
    assert "base64" not in content
    assert original[0]["content"][1]["metadata"]["source_path"] == source_path


def test_text_only_model_routes_persisted_image_placeholder_to_qwen_mm_tool(tmp_path):
    source_path = str(tmp_path / "images" / "previous.png")
    messages = [{
        "role": "user",
        "content": [{
            "type": "text",
            "text": "[Image: previous.png]",
            "metadata": {"source_path": source_path},
        }],
    }]

    prepared = _messages_for_capabilities(
        messages,
        frozenset({"tools"}),
        frozenset({"mcp__qwen-mm-plugins__vision_chat"}),
    )

    content = prepared[0]["content"]
    assert source_path in content
    assert "mcp__qwen-mm-plugins__vision_chat" in content
    assert "provide its path again" in content


def test_completion_kwargs_routes_image_using_currently_exposed_qwen_tools(tmp_path):
    source_path = str(tmp_path / "images" / "screen.png")
    provider = object.__new__(OpenAICompatibleProvider)
    provider.config = LLMConfig(
        model="deepseek-v4-flash",
        capabilities=frozenset({"tools", "reasoning"}),
    )
    messages = [{
        "role": "user",
        "content": [{
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,AAAA"},
            "metadata": {"source_path": source_path},
        }],
    }]
    tools = [{
        "type": "function",
        "function": {
            "name": "mcp__qwen-mm-plugins__vision_chat",
            "description": "inspect image",
            "parameters": {"type": "object"},
        },
    }]

    kwargs = provider._completion_kwargs(messages, tools=tools)

    content = kwargs["messages"][0]["content"]
    assert isinstance(content, str)
    assert source_path in content
    assert "mcp__qwen-mm-plugins__vision_chat" in content
    assert "image_url" not in content


def test_vision_model_strips_internal_image_path_metadata_before_request():
    messages = [{
        "role": "user",
        "content": [{
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,AAAA"},
            "metadata": {"source_path": "D:\\images\\sample.png"},
        }],
    }]

    prepared = _messages_for_capabilities(messages, frozenset({"vision"}))

    assert prepared[0]["content"] == [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]
    assert "metadata" in messages[0]["content"][0]


def test_vision_model_preserves_image_url_content():
    messages = [{
        "role": "user",
        "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}],
    }]

    assert _messages_for_capabilities(messages, frozenset({"vision"})) is messages


def test_deepseek_vision_profile_explicitly_requests_original_detail():
    from agent.cli.models import model_profiles

    profile = model_profiles()["deepseek-flash"]
    messages = [{
        "role": "user",
        "content": [{
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,AAAA"},
            "metadata": {"source_path": "/tmp/sample.png"},
        }],
    }]

    prepared = _messages_for_capabilities(
        messages,
        profile.capabilities,
        vision_detail=profile.vision_detail,
    )

    assert profile.vision_detail == "original"
    assert prepared[0]["content"] == [{
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,AAAA", "detail": "original"},
    }]


def test_vision_detail_default_preserves_explicit_per_image_choice():
    messages = [{
        "role": "user",
        "content": [
            {"type": "image_url", "image_url": {"url": "one", "detail": "low"}},
            {"type": "image_url", "image_url": {"url": "two", "detail": "original"}},
        ],
    }]

    assert _messages_for_capabilities(
        messages,
        frozenset({"vision"}),
        vision_detail="high",
    ) is messages


def test_provider_applies_configured_vision_detail_to_request_body():
    provider = object.__new__(OpenAICompatibleProvider)
    provider.config = LLMConfig(
        model="deepseek-v4-flash-vision-exp",
        capabilities=frozenset({"vision"}),
        vision_detail="high",
    )
    messages = [{
        "role": "user",
        "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}],
    }]

    kwargs = provider._completion_kwargs(messages)

    assert kwargs["messages"][0]["content"][0]["image_url"]["detail"] == "high"


def test_search_web_auto_routes_new_ai_model_queries_to_exa(monkeypatch):
    import agent.runtime.tools.web as web_module

    calls = []

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "resolvedSearchType": "auto",
                "results": [{
                    "title": "LongCat model review",
                    "url": "https://example.test/longcat",
                    "highlights": ["Model benchmark summary."],
                }],
            }

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def post(self, url, **kwargs):
            calls.append(("post", url))
            return FakeResponse()

        async def get(self, url, **kwargs):
            raise AssertionError(f"SearXNG should not run first: {url}")

    monkeypatch.setenv("EXA_API_KEY", "test-exa-key")
    monkeypatch.setattr(web_module.httpx, "AsyncClient", FakeClient)

    async def scenario():
        registry = ToolRegistry()
        register_web_tools(registry, None, default_provider="auto")
        result = await registry.execute("search_web", {"query": "LongCat 新出的模型评价如何"})

        assert result["error"] == ""
        assert "LongCat model review" in result["output"]
        assert calls == [("post", "https://api.exa.ai/search")]

    run(scenario())


def test_search_provider_preference_persists_without_overwriting_settings(tmp_path, monkeypatch):
    from agent.cli.search_preferences import (
        load_selected_search_provider,
        resolve_startup_search_provider,
        save_selected_search_provider,
    )

    settings = tmp_path / "settings.json"
    settings.write_text('{"selected_model": "deepseek-v4-pro", "selected_persona": "work"}', encoding="utf-8")
    monkeypatch.setenv("AGENT_SETTINGS_PATH", str(settings))
    monkeypatch.setenv("WEB_SEARCH_PROVIDER", "searxng")

    saved_path = save_selected_search_provider("exa")
    data = json.loads(settings.read_text(encoding="utf-8"))

    assert saved_path == settings
    assert data["selected_model"] == "deepseek-v4-pro"
    assert data["selected_persona"] == "work"
    assert data["search_provider"] == "exa"
    assert load_selected_search_provider() == "exa"
    assert resolve_startup_search_provider() == "exa"


def test_search_web_auto_falls_back_to_searxng_when_exa_fails(monkeypatch):
    import agent.runtime.tools.web as web_module

    calls = []

    class FakeResponse:
        def __init__(self, payload):
            self.text = json.dumps(payload)

        def raise_for_status(self):
            pass

        def json(self):
            return json.loads(self.text)

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def post(self, url, **kwargs):
            calls.append(("post", url))
            raise RuntimeError("exa unavailable")

        async def get(self, url, **kwargs):
            calls.append(("get", url))
            return FakeResponse({
                "query": "today AI news",
                "results": [{
                    "title": "SearX fallback",
                    "url": "https://example.test/fallback",
                    "content": "fallback snippet",
                }],
            })

    monkeypatch.setenv("EXA_API_KEY", "test-exa-key")
    monkeypatch.setenv("WEB_SEARCH_PROVIDER", "auto")
    monkeypatch.setattr(web_module.httpx, "AsyncClient", FakeClient)

    async def scenario():
        registry = ToolRegistry()
        register_web_tools(registry, None)
        result = await registry.execute("search_web", {
            "query": "today AI news",
            "provider": "auto",
            "category": "news",
        })

        assert result["error"] == ""
        assert "SearX fallback" in result["output"]
        assert calls[0] == ("post", "https://api.exa.ai/search")
        assert calls[1][0] == "get"
        assert "/search?" in calls[1][1]

    run(scenario())


def test_search_web_inlines_result_content_and_caches(monkeypatch):
    import agent.runtime.tools.web as web_module

    calls = []

    class FakeResponse:
        def __init__(self, text, payload=None):
            self.text = text
            self._payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self._payload

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, **kwargs):
            calls.append(url)
            if "/search?" in url:
                payload = {
                    "query": "agent search",
                    "engines_responsive": ["test"],
                    "results": [{
                        "title": "Agent Search Result",
                        "url": "https://example.test/article",
                        "content": "short snippet",
                        "engine": "test",
                    }],
                }
                return FakeResponse(json.dumps(payload), payload)
            if url == "https://example.test/article":
                return FakeResponse("<html><body><article>Inline body from the page with enough useful words.</article></body></html>")
            raise AssertionError(url)

    monkeypatch.setattr(web_module.httpx, "AsyncClient", FakeClient)

    async def scenario():
        registry = ToolRegistry()
        register_web_tools(registry, None)

        args = {"query": "agent search", "max_results": 1, "include_content": True, "content_max_length": 200}
        first = await registry.execute("search_web", args)
        second = await registry.execute("search_web", args)

        assert "Inline body from the page" in first["output"]
        assert second["output"] == first["output"]
        assert len([url for url in calls if "/search?" in url]) == 1
        assert calls.count("https://example.test/article") == 1

    run(scenario())


def test_extract_url_can_skip_static_fetch_for_browser(monkeypatch):
    import agent.runtime.tools.web as web_module

    http_calls = []
    command_seen = []
    monkeypatch.setenv("BROWSER_EXTRACT_ARGV", '["browser-extract", "--render"]')
    monkeypatch.delenv("BROWSER_EXTRACT_CMD", raising=False)

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, **kwargs):
            http_calls.append(url)
            raise AssertionError("browser-first extraction should not static-fetch")

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            return b"Rendered browser content", b""

    async def fake_subprocess_exec(*cmd, **kwargs):
        command_seen.append(cmd)
        return FakeProcess()

    monkeypatch.setattr(web_module.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(web_module.asyncio, "create_subprocess_exec", fake_subprocess_exec)

    async def scenario():
        registry = ToolRegistry()
        register_web_tools(registry, None)
        result = await registry.execute("extract_url", {"url": "https://example.test/app", "browser_first": True})

        assert result["output"] == "Rendered browser content"
        assert http_calls == []
        assert command_seen == [(
            "browser-extract",
            "--render",
            "https://example.test/app",
        )]

    run(scenario())


def test_web_tools_fall_back_when_httpx_is_missing(monkeypatch):
    import agent.runtime.tools.web as web_module

    calls = []

    def fake_urllib_get(url, timeout, http_proxy, require_same_origin=False):
        calls.append((url, timeout, http_proxy, require_same_origin))
        return "<html><body>Fallback urllib content with enough words to parse cleanly.</body></html>", None

    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.delenv("http_proxy", raising=False)
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.delenv("ALL_PROXY", raising=False)
    monkeypatch.delenv("all_proxy", raising=False)
    monkeypatch.setattr(web_module, "httpx", None)
    monkeypatch.setattr(web_module, "_http_get_urllib", fake_urllib_get)

    async def scenario():
        registry = ToolRegistry()
        register_web_tools(registry, None)
        result = await registry.execute("fetch_url", {"url": "https://example.test/page"})

        assert "Fallback urllib content" in result["output"]
        assert calls == [("https://example.test/page", 12, None, False)]

    run(scenario())


def test_web_tools_reuse_httpx_client_and_bound_cache(monkeypatch):
    import agent.runtime.tools.web as web_module

    created_clients = []
    calls = []

    class FakeResponse:
        def __init__(self, text):
            self.text = text

        def raise_for_status(self):
            pass

    class FakeClient:
        def __init__(self, *args, **kwargs):
            created_clients.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, **kwargs):
            calls.append(url)
            return FakeResponse(f"<html><body>Cached content for {url} with enough words.</body></html>")

    monkeypatch.setenv("WEB_CACHE_MAX_ENTRIES", "2")
    monkeypatch.setattr(web_module.httpx, "AsyncClient", FakeClient)

    async def scenario():
        registry = ToolRegistry()
        register_web_tools(registry, None)
        for index in range(3):
            await registry.execute("fetch_url", {"url": f"https://example.test/{index}"})
        await registry.execute("fetch_url", {"url": "https://example.test/0"})

        assert len(created_clients) == 1
        assert calls.count("https://example.test/0") == 2

    run(scenario())


def test_fetch_url_does_not_reuse_unguarded_cache_after_approval_is_enabled(monkeypatch):
    import agent.runtime.tools.web as web_module

    calls = []

    class FakeResponse:
        def __init__(self, text, url):
            self.text = text
            self.url = url

        def raise_for_status(self):
            pass

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def get(self, url, **kwargs):
            calls.append(url)
            final_url = url if len(calls) == 1 else "https://evil.test/redirected"
            return FakeResponse(
                "<html><body>Content with enough words for a deterministic extraction test.</body></html>",
                final_url,
            )

    monkeypatch.setattr(web_module.httpx, "AsyncClient", FakeClient)

    async def scenario():
        registry = ToolRegistry()
        register_web_tools(registry, None)
        first = await registry.execute("fetch_url", {"url": "https://example.test/page"})
        assert "deterministic extraction test" in first["output"]

        async def approve(_request):
            return "session"

        registry.set_approval_handler(approve)
        guarded = await registry.execute("fetch_url", {"url": "https://example.test/page"})
        assert guarded["output"] == ""
        assert guarded["code"] == "redirect_blocked"
        assert "Cross-origin redirect blocked" in guarded["error"]
        assert "https://evil.test/redirected" in guarded["error"]
        assert calls == ["https://example.test/page", "https://example.test/page"]

    run(scenario())


def test_web_tools_close_shared_httpx_clients():
    import agent.runtime.tools.web as web_module

    closed = []

    class FakeClient:
        async def aclose(self):
            closed.append(self)

    expected = [FakeClient(), FakeClient()]
    clients = {"direct": expected[0], "proxy": expected[1]}

    web_module._close_httpx_clients_sync(clients)

    assert closed == expected
    assert clients == {}


def test_search_web_auto_falls_back_from_searxng_to_exa(monkeypatch):
    import agent.runtime.tools.web as web_module

    calls = []

    class FakeResponse:
        def __init__(self, payload=None):
            self.text = json.dumps(payload or {})
            self.payload = payload or {}

        def raise_for_status(self):
            pass

        def json(self):
            return self.payload

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def get(self, url, **kwargs):
            calls.append(("get", url))
            if "localhost:8080/search" in url:
                raise RuntimeError("searxng down")
            raise AssertionError(url)

        async def post(self, url, **kwargs):
            calls.append(("post", url))
            return FakeResponse({
                "resolvedSearchType": "auto",
                "results": [{
                    "title": "Exa reverse fallback",
                    "url": "https://example.test/exa-fallback",
                    "highlights": ["Found after SearXNG failed."],
                }],
            })

    monkeypatch.setenv("EXA_API_KEY", "test-exa-key")
    monkeypatch.setattr(web_module.httpx, "AsyncClient", FakeClient)

    async def scenario():
        registry = ToolRegistry()
        register_web_tools(registry, None)
        result = await registry.execute("search_web", {"query": "obscure named thing", "max_results": 1})

        assert "Exa reverse fallback" in result["output"]
        assert calls[0][0] == "get"
        assert calls[1] == ("post", "https://api.exa.ai/search")
        assert all("duckduckgo" not in url for _, url in calls)

    run(scenario())


def test_search_web_caps_inline_fetch_total_timeout(monkeypatch):
    import agent.runtime.tools.web as web_module

    class FakeResponse:
        def __init__(self, text):
            self.text = text

        def raise_for_status(self):
            pass

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, **kwargs):
            if "/search?" in url:
                payload = {
                    "query": "slow",
                    "engines_responsive": ["test"],
                    "results": [{
                        "title": "Slow Result",
                        "url": "https://example.test/slow",
                        "content": "snippet",
                    }],
                }
                return FakeResponse(json.dumps(payload))
            await asyncio.sleep(0.1)
            return FakeResponse("<html><body>too late</body></html>")

    monkeypatch.setenv("WEB_INLINE_FETCH_TIMEOUT", "1")
    monkeypatch.setattr(web_module.httpx, "AsyncClient", FakeClient)

    async def scenario():
        registry = ToolRegistry()
        register_web_tools(registry, None)
        started = time.perf_counter()
        result = await registry.execute("search_web", {"query": "slow", "max_results": 1})
        elapsed = time.perf_counter() - started

        assert result["error"] == ""
        assert "Slow Result" in result["output"]
        assert "too late" not in result["output"]
        assert elapsed < 0.08

    run(scenario())


def test_search_web_skips_non_html_inline_fetch_and_marks_fetch_time(monkeypatch):
    import agent.runtime.tools.web as web_module

    calls = []

    class FakeResponse:
        def __init__(self, text):
            self.text = text

        def raise_for_status(self):
            pass

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def get(self, url, **kwargs):
            calls.append(url)
            if "/search?" in url:
                payload = {
                    "query": "sources",
                    "engines_responsive": ["test"],
                    "results": [
                        {"title": "PDF", "url": "https://example.test/file.pdf", "content": "pdf result"},
                        {"title": "Article", "url": "https://example.test/article", "content": "article result"},
                    ],
                }
                return FakeResponse(json.dumps(payload))
            if url == "https://example.test/article":
                return FakeResponse("<html><body>Article inline content with enough words for extraction.</body></html>")
            raise AssertionError(f"unexpected fetch: {url}")

    monkeypatch.setattr(web_module.httpx, "AsyncClient", FakeClient)

    async def scenario():
        registry = ToolRegistry()
        register_web_tools(registry, None)
        result = await registry.execute("search_web", {"query": "sources", "max_results": 2, "include_content": True, "content_results": 2})

        assert "https://example.test/file.pdf" in result["output"]
        assert "Article inline content" in result["output"]
        assert "Fetched:" in result["output"]
        assert "https://example.test/file.pdf" not in calls

    run(scenario())


def test_search_web_is_lightweight_by_default(monkeypatch):
    import agent.runtime.tools.web as web_module

    calls = []

    class FakeResponse:
        def __init__(self, text):
            self.text = text

        def raise_for_status(self):
            pass

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def get(self, url, **kwargs):
            calls.append(url)
            payload = {
                "query": "lightweight",
                "results": [{
                    "title": "Candidate",
                    "url": "https://example.test/article",
                    "content": "search snippet",
                    "engine": "test",
                }],
            }
            return FakeResponse(json.dumps(payload))

    monkeypatch.setattr(web_module.httpx, "AsyncClient", FakeClient)

    async def scenario():
        registry = ToolRegistry()
        register_web_tools(registry, None)
        result = await registry.execute("search_web", {"query": "lightweight"})

        assert "Candidate" in result["output"]
        assert "Page content:" not in result["output"]
        assert calls == [calls[0]]
        assert "/search?" in calls[0]

    run(scenario())


def test_web_extract_uses_firecrawl_and_trims_content(monkeypatch):
    import agent.runtime.tools.web as web_module

    posts = []

    class FakeResponse:
        text = ""

        def raise_for_status(self):
            pass

        def json(self):
            return {
                "success": True,
                "data": {
                    "markdown": "abcdef",
                    "metadata": {"title": "Example", "sourceURL": "https://example.test/article"},
                },
            }

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def post(self, url, **kwargs):
            posts.append((url, kwargs["json"]))
            return FakeResponse()

    async def safe_url(url):
        return True

    monkeypatch.setenv("FIRECRAWL_URL", "http://localhost:3002")
    monkeypatch.setattr(web_module.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(web_module, "_is_safe_public_url", safe_url)

    async def scenario():
        registry = ToolRegistry()
        register_web_tools(registry, None)
        result = await registry.execute("web_extract", {
            "urls": ["https://example.test/article"],
            "max_chars": 500,
            "provider": "firecrawl",
        })
        payload = json.loads(result["output"])

        assert payload["success"] is True
        assert payload["provider_chain"] == ["firecrawl", "http"]
        assert payload["results"][0]["backend"] == "firecrawl"
        assert payload["results"][0]["title"] == "Example"
        assert payload["results"][0]["content"] == "abcdef"
        assert posts == [(
            "http://localhost:3002/v1/scrape",
            {
                "url": "https://example.test/article",
                "formats": ["markdown"],
                "onlyMainContent": True,
            },
        )]

    run(scenario())


def test_web_extract_auto_prefers_exa_api_before_local_firecrawl(monkeypatch):
    import agent.runtime.tools.web as web_module

    posts = []

    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self.payload

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def post(self, url, **kwargs):
            posts.append((url, kwargs))
            return FakeResponse({
                "results": [{
                    "url": "https://example.test/article",
                    "title": "Exa article",
                    "text": "Clean content returned by Exa before Firecrawl is attempted.",
                }],
            })

    async def safe_url(url):
        return True

    monkeypatch.setenv("EXA_API_KEY", "exa-test-key")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("PARALLEL_API_KEY", raising=False)
    monkeypatch.setenv("FIRECRAWL_URL", "http://localhost:3002")
    monkeypatch.setattr(web_module.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(web_module, "_is_safe_public_url", safe_url)

    async def scenario():
        registry = ToolRegistry()
        register_web_tools(registry, None)
        result = await registry.execute("web_extract", {
            "urls": ["https://example.test/article"],
        })
        payload = json.loads(result["output"])

        assert payload["success"] is True
        assert payload["provider_chain"] == ["exa", "firecrawl", "http"]
        assert payload["results"][0]["backend"] == "exa"
        assert payload["results"][0]["fallback_from"] == []
        assert len(posts) == 1
        assert posts[0][0] == "https://api.exa.ai/contents"
        assert posts[0][1]["headers"]["x-api-key"] == "exa-test-key"

    run(scenario())


def test_web_extract_auto_prefers_tavily_over_other_configured_apis(monkeypatch):
    import agent.runtime.tools.web as web_module

    posts = []

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "results": [{
                    "url": "https://example.test/article",
                    "raw_content": "# Tavily content\n\nAPI-first extraction.",
                }],
                "failed_results": [],
            }

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def post(self, url, **kwargs):
            posts.append(url)
            return FakeResponse()

    async def safe_url(url):
        return True

    monkeypatch.setenv("TAVILY_API_KEY", "tavily-test-key")
    monkeypatch.setenv("EXA_API_KEY", "exa-test-key")
    monkeypatch.setenv("PARALLEL_API_KEY", "parallel-test-key")
    monkeypatch.setattr(web_module.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(web_module, "_is_safe_public_url", safe_url)

    async def scenario():
        registry = ToolRegistry()
        register_web_tools(registry, None)
        result = await registry.execute("web_extract", {
            "urls": ["https://example.test/article"],
        })
        payload = json.loads(result["output"])

        assert payload["success"] is True
        assert payload["provider_chain"] == [
            "tavily", "exa", "parallel", "firecrawl", "http",
        ]
        assert payload["results"][0]["backend"] == "tavily"
        assert posts == ["https://api.tavily.com/extract"]

    run(scenario())


def test_web_extract_parallel_accepts_ranked_excerpts(monkeypatch):
    import agent.runtime.tools.web as web_module

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "results": [{
                    "url": "https://example.test/article",
                    "title": "Parallel",
                    "excerpts": ["First relevant excerpt.", "Second relevant excerpt."],
                }],
                "errors": [],
            }

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def post(self, url, **kwargs):
            assert url == "https://api.parallel.ai/v1/extract"
            assert kwargs["json"]["max_chars_total"] == 15000
            return FakeResponse()

    async def safe_url(url):
        return True

    monkeypatch.setenv("PARALLEL_API_KEY", "parallel-test-key")
    monkeypatch.setattr(web_module.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(web_module, "_is_safe_public_url", safe_url)

    async def scenario():
        registry = ToolRegistry()
        register_web_tools(registry, None)
        result = await registry.execute("web_extract", {
            "urls": ["https://example.test/article"],
            "provider": "parallel",
        })
        payload = json.loads(result["output"])

        assert payload["success"] is True
        assert payload["results"][0]["backend"] == "parallel"
        assert payload["results"][0]["content"] == (
            "First relevant excerpt.\n\nSecond relevant excerpt."
        )

    run(scenario())


def test_web_extract_falls_back_from_exa_to_firecrawl_per_url(monkeypatch):
    import agent.runtime.tools.web as web_module

    posts = []

    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self.payload

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def post(self, url, **kwargs):
            posts.append(url)
            if url == "https://api.exa.ai/contents":
                raise RuntimeError("Exa unavailable")
            return FakeResponse({
                "success": True,
                "data": {
                    "markdown": "# Firecrawl fallback\n\nRecovered content.",
                    "metadata": {
                        "title": "Recovered",
                        "sourceURL": "https://example.test/article",
                    },
                },
            })

    async def safe_url(url):
        return True

    monkeypatch.setenv("EXA_API_KEY", "exa-test-key")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("PARALLEL_API_KEY", raising=False)
    monkeypatch.setenv("FIRECRAWL_URL", "http://localhost:3002")
    monkeypatch.setattr(web_module.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(web_module, "_is_safe_public_url", safe_url)

    async def scenario():
        registry = ToolRegistry()
        register_web_tools(registry, None)
        result = await registry.execute("web_extract", {
            "urls": ["https://example.test/article"],
        })
        payload = json.loads(result["output"])

        assert payload["success"] is True
        assert payload["results"][0]["backend"] == "firecrawl"
        assert payload["results"][0]["fallback_from"] == ["exa"]
        assert posts == [
            "https://api.exa.ai/contents",
            "http://localhost:3002/v1/scrape",
        ]

    run(scenario())


def test_web_extract_http_fallback_preserves_markdown_and_stores_full_text(
    monkeypatch,
    tmp_path,
):
    import agent.runtime.tools.web as web_module

    body = (
        "<html><body><main><h1>Fallback title</h1>"
        "<p>Read the <a href='https://example.test/docs'>documentation</a>.</p>"
        f"<p>{'middle ' * 300}</p><p>TAIL-MARKER</p>"
        "</main></body></html>"
    )

    class FakeResponse:
        text = body
        url = "https://example.test/article"

        def raise_for_status(self):
            pass

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def get(self, url, **kwargs):
            return FakeResponse()

    async def safe_url(url):
        return True

    monkeypatch.setenv("WEB_EXTRACT_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(web_module.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(web_module, "_is_safe_public_url", safe_url)

    async def scenario():
        registry = ToolRegistry()
        register_web_tools(registry, None)
        result = await registry.execute("web_extract", {
            "urls": ["https://example.test/article"],
            "max_chars": 500,
            "provider": "http",
        })
        payload = json.loads(result["output"])
        page = payload["results"][0]

        assert payload["success"] is True
        assert payload["provider_chain"] == ["http"]
        assert page["backend"] == "http"
        assert "# Fallback title" in page["content"]
        assert "[documentation](https://example.test/docs)" in page["content"]
        assert "TAIL-MARKER" in page["content"]
        assert "middle truncated" in page["content"]
        stored = Path(page["full_content_path"])
        assert stored.parent == tmp_path.resolve()
        assert "TAIL-MARKER" in stored.read_text(encoding="utf-8")

    run(scenario())


def test_web_extract_blocks_private_urls_before_firecrawl(monkeypatch):
    import agent.runtime.tools.web as web_module

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def post(self, url, **kwargs):
            raise AssertionError("blocked URL must not reach Firecrawl")

    monkeypatch.setattr(web_module.httpx, "AsyncClient", FakeClient)

    async def scenario():
        registry = ToolRegistry()
        register_web_tools(registry, None)
        result = await registry.execute("web_extract", {"urls": ["http://127.0.0.1/private"]})

        assert result["output"] == ""
        assert result["code"] == "extract_failed"
        assert "http://127.0.0.1/private: Blocked" in result["error"]

    run(scenario())


def test_web_extract_auto_recovers_stale_url_from_search_candidate(monkeypatch):
    import agent.runtime.tools.web as web_module

    original_url = "https://docs.example.test/old/agent-guide"
    candidate_url = "https://docs.example.test/current/agent-guide"
    calls = []

    class FakeResponse:
        def __init__(self, *, payload=None, text="", url=""):
            self.payload = payload
            self.text = text
            self.url = url

        def raise_for_status(self):
            if self.payload == "missing":
                raise RuntimeError("404 Not Found")

        def json(self):
            return self.payload

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def post(self, url, **kwargs):
            requested_url = kwargs["json"]["url"]
            calls.append(("extract", requested_url))
            if requested_url == candidate_url:
                return FakeResponse(payload={
                    "success": True,
                    "data": {
                        "markdown": "# Recovered guide\n\nCanonical content.",
                        "metadata": {
                            "title": "Agent guide",
                            "sourceURL": candidate_url,
                        },
                    },
                })
            return FakeResponse(payload={"success": False, "error": "page not found"})

        async def get(self, url, **kwargs):
            calls.append(("get", url))
            if "/search?" in url:
                return FakeResponse(
                    text=json.dumps({
                        "results": [
                            {
                                "title": "Unrelated mirror",
                                "url": "https://unrelated.test/agent-guide",
                                "content": "Must not be retried across hosts",
                                "engine": "test",
                            },
                            {
                                "title": "Agent guide",
                                "url": candidate_url,
                                "content": "Current documentation",
                                "engine": "test",
                            },
                        ],
                    }),
                    url=url,
                )
            return FakeResponse(payload="missing", url=url)

    async def safe_url(url):
        return True

    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    monkeypatch.delenv("PARALLEL_API_KEY", raising=False)
    monkeypatch.setenv("FIRECRAWL_URL", "http://localhost:3002")
    monkeypatch.setattr(web_module.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(web_module, "_is_safe_public_url", safe_url)

    async def scenario():
        registry = ToolRegistry()
        register_web_tools(registry, None)
        result = await registry.execute("web_extract", {"urls": [original_url]})
        payload = json.loads(result["output"])
        page = payload["results"][0]

        assert payload["success"] is True
        assert page["url"] == candidate_url
        assert page["requested_url"] == original_url
        assert page["recovery"] == {
            "method": "search",
            "candidate_url": candidate_url,
            "rank": 1,
        }
        assert "search" in page["fallback_from"]
        assert calls == [
            ("extract", original_url),
            ("get", original_url),
            ("get", calls[2][1]),
            ("extract", candidate_url),
        ]
        assert "/search?" in calls[2][1]

    run(scenario())


def test_fetch_url_prefers_beautifulsoup_article_text(monkeypatch):
    import agent.runtime.tools.web as web_module

    class FakeResponse:
        def __init__(self, text):
            self.text = text

        def raise_for_status(self):
            pass

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def get(self, url, **kwargs):
            return FakeResponse(
                "<html><body><nav>Navigation noise should disappear</nav>"
                "<article><h1>Useful Title</h1><p>Useful article text with enough words for extraction.</p></article>"
                "<footer>Footer noise should disappear</footer></body></html>"
            )

    monkeypatch.setattr(web_module.httpx, "AsyncClient", FakeClient)

    async def scenario():
        registry = ToolRegistry()
        register_web_tools(registry, None)
        result = await registry.execute("fetch_url", {"url": "https://example.test/article"})

        assert "Useful Title Useful article text" in result["output"]
        assert "Navigation noise" not in result["output"]
        assert "Footer noise" not in result["output"]

    run(scenario())


def _fake_web_transport(monkeypatch, *, get=None, post=None):
    """Answer every web tool request from memory and record it.

    The tools swallow transport exceptions, so tests assert on the returned
    call list instead of relying on a handler that raises.
    """
    import agent.runtime.tools.web as web_module

    calls = []

    class FakeResponse:
        def __init__(self, payload, url):
            self.text = payload if isinstance(payload, str) else json.dumps(payload)
            self.url = url

        def raise_for_status(self):
            pass

        def json(self):
            return json.loads(self.text)

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def get(self, url, **kwargs):
            calls.append(("get", url))
            if get is None:
                raise RuntimeError(f"no GET handler: {url}")
            return FakeResponse(get(url), url)

        async def post(self, url, **kwargs):
            calls.append(("post", url))
            if post is None:
                raise RuntimeError(f"no POST handler: {url}")
            return FakeResponse(post(url, kwargs.get("json")), url)

    monkeypatch.setattr(web_module.httpx, "AsyncClient", FakeClient)
    return calls


# Long enough for web_extract's http backend, which rejects pages under 120 characters.
_READABLE_PAGE = (
    "<html><body><main><h1>Readable page body</h1><p>"
    + "This paragraph carries enough plain words for a static reader to accept the page as real content. " * 2
    + "</p></main></body></html>"
)


def _exa_first_page(_url, _payload):
    return {
        "resolvedSearchType": "auto",
        "results": [{
            "title": "Exa first page",
            "url": "https://exa.example.test/one",
            "highlights": ["Exa highlight."],
        }],
    }


def _searxng_query(url):
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))


@pytest.mark.parametrize("extra,sent", [
    ({"page": 2}, {"pageno": "2"}),
    ({"engine": "google"}, {"engines": "google"}),
    ({"category": "it"}, {"categories": "it"}),
])
def test_search_web_auto_keeps_searxng_only_parameters_off_exa(monkeypatch, extra, sent):
    def searxng(url):
        if "/search?" not in url:
            return "<html><body>Inline page body with enough words to be kept as content.</body></html>"
        return {
            "query": "latest model report",
            "number_of_results": 40,
            "results": [{
                "title": f"SearXNG page {_searxng_query(url)['pageno']}",
                "url": "https://searx.example.test/hit",
                "content": "snippet",
            }],
        }

    monkeypatch.setenv("EXA_API_KEY", "test-exa-key")
    monkeypatch.setenv("WEB_SEARCH_PROVIDER", "auto")
    calls = _fake_web_transport(monkeypatch, get=searxng, post=_exa_first_page)

    async def scenario():
        registry = ToolRegistry()
        register_web_tools(registry, None)
        # Without the extra parameter this query is one that auto sends to Exa.
        plain = await registry.execute("search_web", {"query": "latest model report"})
        assert "Exa first page" in plain["output"]
        assert calls == [("post", "https://api.exa.ai/search")]

        result = await registry.execute("search_web", {"query": "latest model report", **extra})

        assert result["error"] == ""
        assert [kind for kind, _ in calls].count("post") == 1
        search_urls = [url for kind, url in calls if kind == "get" and "/search?" in url]
        assert len(search_urls) == 1
        assert _searxng_query(search_urls[0]).items() >= sent.items()
        assert f"SearXNG page {extra.get('page', 1)}" in result["output"]
        assert "Exa first page" not in result["output"]

    run(scenario())


@pytest.mark.parametrize("extra,named", [
    ({"language": "ja"}, "language=ja"),
    ({"include_content": True}, "include_content"),
])
def test_search_web_auto_stays_on_exa_for_a_preference_and_names_it(monkeypatch, extra, named):
    monkeypatch.setenv("EXA_API_KEY", "test-exa-key")
    monkeypatch.setenv("WEB_SEARCH_PROVIDER", "auto")
    calls = _fake_web_transport(monkeypatch, get=lambda url: {"results": []}, post=_exa_first_page)

    async def scenario():
        registry = ToolRegistry()
        register_web_tools(registry, None)

        result = await registry.execute("search_web", {"query": "latest model report", **extra})

        assert result["error"] == ""
        assert calls == [("post", "https://api.exa.ai/search")]
        assert "Exa first page" in result["output"]
        assert f"Not applied on the Exa route: {named}" in result["output"]

    run(scenario())


def test_search_web_forced_exa_names_the_parameters_it_did_not_apply(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "test-exa-key")
    calls = _fake_web_transport(monkeypatch, post=_exa_first_page)

    async def scenario():
        registry = ToolRegistry()
        register_web_tools(registry, None)
        first = await registry.execute("search_web", {"query": "agent runtimes", "provider": "exa"})
        second = await registry.execute("search_web", {
            "query": "agent runtimes",
            "provider": "exa",
            "page": 2,
            "engine": "google",
            "language": "ja",
        })

        assert "Exa first page" in first["output"]
        assert "Not applied" not in first["output"]
        # Exa has no second page: the same hits must not read as a new page.
        assert "Exa first page" in second["output"]
        note = next((line for line in second["output"].splitlines() if line.startswith("Not applied")), "")
        assert "page=2" in note
        assert "engine=google" in note
        assert "language=ja" in note
        assert "first results" in note
        assert all(kind == "post" for kind, _ in calls)

    run(scenario())


def test_search_web_exa_fallback_names_the_parameters_it_did_not_apply(monkeypatch):
    def searxng_down(url):
        raise RuntimeError("searxng down")

    monkeypatch.setenv("EXA_API_KEY", "test-exa-key")
    monkeypatch.setenv("WEB_SEARCH_PROVIDER", "auto")
    calls = _fake_web_transport(monkeypatch, get=searxng_down, post=_exa_first_page)

    async def scenario():
        registry = ToolRegistry()
        register_web_tools(registry, None)
        result = await registry.execute("search_web", {"query": "obscure named thing", "page": 3})

        assert [kind for kind, _ in calls] == ["get", "post"]
        assert "Exa first page" in result["output"]
        note = next((line for line in result["output"].splitlines() if line.startswith("Not applied")), "")
        assert "page=3" in note

    run(scenario())


def _one_site_results(url):
    hits = [
        {"title": f"Docs {index}", "url": f"https://docs.example.test/page-{index}", "content": "doc"}
        for index in range(1, 7)
    ]
    hits.insert(2, {"title": "Other site", "url": "https://other.example.test/a", "content": "other"})
    return {"query": _searxng_query(url)["q"], "number_of_results": 900, "results": hits}


def test_search_web_site_query_is_not_capped_per_domain(monkeypatch):
    _fake_web_transport(monkeypatch, get=_one_site_results)

    async def scenario():
        registry = ToolRegistry()
        register_web_tools(registry, None)
        result = await registry.execute("search_web", {
            "query": "site:docs.example.test asyncio",
            "provider": "searxng",
            "max_results": 6,
        })

        listed = re.findall(r"URL: (\S+)", result["output"])
        assert [url for url in listed if "docs.example.test" in url] == [
            f"https://docs.example.test/page-{index}" for index in range(1, 6)
        ]
        assert len(listed) == 6
        assert "per site" not in result["output"]
        assert not result.get("partial")

    run(scenario())


@pytest.mark.parametrize("query", ["asyncio docs", "asyncio docs -site:spam.example.test"])
def test_search_web_says_when_the_per_domain_cap_left_results_out(monkeypatch, query):
    _fake_web_transport(monkeypatch, get=_one_site_results)

    async def scenario():
        registry = ToolRegistry()
        register_web_tools(registry, None)
        result = await registry.execute("search_web", {
            "query": query,
            "provider": "searxng",
            "max_results": 6,
        })

        listed = re.findall(r"URL: (\S+)", result["output"])
        assert listed == [
            "https://docs.example.test/page-1",
            "https://docs.example.test/page-2",
            "https://other.example.test/a",
        ]
        note = next((line for line in result["output"].splitlines() if line.startswith("Shown:")), "")
        assert note.startswith(f"Shown: {len(listed)} results,")
        assert "at most 2 per site" in note
        assert "4 more" in note
        assert "site:" in note
        assert result["partial"] is True

    run(scenario())


_LISTING_PAGE = (
    "<html><body><nav>Navigation noise</nav><main><h1>Thread title</h1>"
    "<article><p>First post says the launch moved to Tuesday.</p></article>"
    "<article><p>Second post corrects it to Wednesday.</p></article>"
    "<article><p>Third post confirms Wednesday at noon.</p></article>"
    "</main><footer>Footer noise</footer></body></html>"
)


def test_fetch_url_and_web_extract_keep_every_article_of_a_listing_page(monkeypatch):
    import agent.runtime.tools.web as web_module

    async def safe_url(url):
        return True

    _fake_web_transport(monkeypatch, get=lambda url: _LISTING_PAGE)
    monkeypatch.setattr(web_module, "_is_safe_public_url", safe_url)

    async def scenario():
        registry = ToolRegistry()
        register_web_tools(registry, None)
        fetched = await registry.execute("fetch_url", {"url": "https://example.test/thread"})
        extracted = await registry.execute("web_extract", {
            "urls": ["https://example.test/thread"],
            "provider": "http",
        })
        page = json.loads(extracted["output"])["results"][0]

        for text in (fetched["output"], page["content"]):
            assert "Thread title" in text
            assert "moved to Tuesday" in text
            assert "corrects it to Wednesday" in text
            assert "Wednesday at noon" in text
            assert "Navigation noise" not in text
            assert "Footer noise" not in text

    run(scenario())


def test_fetch_url_returns_a_short_non_html_body_as_it_is(monkeypatch):
    bodies = {
        "https://example.test/health": '{"status": "ok"}',
        "https://example.test/ping": "pong\n",
        "https://example.test/app": (
            '<!doctype html><html><head><script src="/app.js"></script></head>'
            '<body><div id="root"></div></body></html>'
        ),
    }
    _fake_web_transport(monkeypatch, get=bodies.__getitem__)

    async def scenario():
        registry = ToolRegistry()
        register_web_tools(registry, None)
        health = await registry.execute("fetch_url", {"url": "https://example.test/health"})
        ping = await registry.execute("fetch_url", {"url": "https://example.test/ping"})
        shell = await registry.execute("fetch_url", {"url": "https://example.test/app"})

        assert health["output"].endswith('{"status": "ok"}')
        assert ping["output"].endswith("pong")
        for result in (health, ping):
            assert "Empty/minimal" not in result["output"]
            assert "JavaScript" not in result["output"]
        # An HTML page that yields no text is a failure that keeps the rendering hint.
        assert shell["output"] == ""
        assert shell["code"] == "no_static_text"
        assert shell["retryable"] is False
        assert "Empty/minimal content" in shell["error"]
        assert "https://example.test/app" in shell["error"]
        assert "web_extract" in shell["recovery_hint"]

    run(scenario())


def test_fetch_url_truncation_states_the_full_length_and_how_to_get_more(monkeypatch):
    pages = {
        "https://example.test/long": f"<html><body><p>{'word ' * 600}</p></body></html>",
        "https://example.test/huge": f"<html><body><p>{'word ' * 12000}</p></body></html>",
    }
    _fake_web_transport(monkeypatch, get=pages.__getitem__)

    async def scenario():
        # Keep the registry's own inline preview out of the way of the longest page.
        registry = ToolRegistry(max_inline_chars=100_000)
        register_web_tools(registry, None)
        clipped = await registry.execute("fetch_url", {"url": "https://example.test/long", "max_length": 500})
        whole = await registry.execute("fetch_url", {"url": "https://example.test/long", "max_length": 5000})
        maxed = await registry.execute("fetch_url", {"url": "https://example.test/huge", "max_length": 50000})

        assert "first 500 of 2999 characters" in clipped["output"]
        assert "max_length" in clipped["output"]
        assert "web_extract" in clipped["output"]
        assert clipped["partial"] is True

        assert "truncated" not in whole["output"]
        assert not whole.get("partial")

        # At the largest max_length the only way to more is another tool.
        tail = maxed["output"][-400:]
        assert "first 50000 of 59999 characters" in tail
        assert "web_extract" in tail
        assert "larger max_length" not in tail
        assert maxed["partial"] is True

    run(scenario())


def test_web_extract_tells_unresolved_hosts_and_bad_schemes_from_private_addresses(monkeypatch):
    import socket

    import agent.runtime.tools.web as web_module

    lookups = []

    def fake_getaddrinfo(host, port, *args, **kwargs):
        lookups.append(host)
        if host == "intranet.example.test":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", port))]
        if host == "public.example.test":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]
        raise socket.gaierror(socket.EAI_NONAME, "nodename nor servname provided, or not known")

    calls = _fake_web_transport(monkeypatch, get=lambda url: _READABLE_PAGE)
    monkeypatch.setattr(web_module.socket, "getaddrinfo", fake_getaddrinfo)
    rejected_urls = [
        "https://mistyped.example.test/page",
        "ftp://files.example.test/report.txt",
        "http://intranet.example.test/admin",
        "http://127.0.0.1/private",
    ]

    async def scenario():
        registry = ToolRegistry()
        register_web_tools(registry, None)
        result = await registry.execute("web_extract", {
            "urls": [*rejected_urls, "https://public.example.test/page"],
            "provider": "http",
        })
        payload = json.loads(result["output"])
        unresolved, scheme, private_name, private_ip, readable = payload["results"]

        assert "could not resolve the host name" in unresolved["error"]
        assert "http://" in scheme["error"] and "https://" in scheme["error"]
        for entry in (unresolved, scheme):
            assert "Blocked" not in entry["error"]
            assert "private" not in entry["error"]
            assert entry["backend"] != "blocked"
        # The security block itself is unchanged for real private targets.
        for entry in (private_name, private_ip):
            assert entry["error"] == "Blocked: URL targets localhost, credentials, or a private/internal address"
            assert entry["backend"] == "blocked"
        # The reasons stay next to the URL that was read; nothing rejected was fetched.
        assert result["error"] == ""
        assert payload["success"] is True
        assert "Readable page body" in readable["content"]
        assert all(not entry["content"] for entry in (unresolved, scheme, private_name, private_ip))
        assert calls == [("get", "https://public.example.test/page")]
        assert set(lookups) == {"mistyped.example.test", "intranet.example.test", "public.example.test"}

        # With no URL read, the same reasons are the failure of the call.
        alone = await registry.execute("web_extract", {"urls": rejected_urls, "provider": "http"})
        assert alone["output"] == ""
        assert alone["code"] == "extract_failed"
        assert alone["retryable"] is False
        reasons = dict(line[2:].split(": ", 1) for line in alone["error"].splitlines()[1:])
        assert list(reasons) == rejected_urls
        assert reasons[rejected_urls[0]] == unresolved["error"]
        assert reasons[rejected_urls[1]] == scheme["error"]
        assert reasons[rejected_urls[2]] == reasons[rejected_urls[3]] == private_ip["error"]
        assert len(calls) == 1

    run(scenario())


def test_public_url_check_rejects_the_same_targets_whatever_the_wording(monkeypatch):
    import socket

    import agent.runtime.tools.web as web_module

    resolved = {
        "public.example.test": ["93.184.216.34"],
        "intranet.example.test": ["10.0.0.5"],
        "mixed.example.test": ["93.184.216.34", "10.0.0.5"],
    }

    def fake_getaddrinfo(host, port, *args, **kwargs):
        if host not in resolved:
            raise socket.gaierror(socket.EAI_NONAME, "not known")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port)) for address in resolved[host]]

    monkeypatch.setattr(web_module.socket, "getaddrinfo", fake_getaddrinfo)
    rejected = [
        "http://127.0.0.1/x",
        "http://[::1]/x",
        "http://10.1.2.3/",
        "http://169.254.169.254/latest/meta-data",
        "http://localhost:8080/",
        "http://printer.local/",
        "http://service.internal/",
        "http://user:secret@public.example.test/",
        "ftp://public.example.test/file",
        "public.example.test/no-scheme",
        "https://mistyped.example.test/",
        "https://intranet.example.test/",
        "https://mixed.example.test/",
    ]
    accepted = ["https://public.example.test/page", "https://8.8.8.8/"]

    async def scenario():
        for url in rejected:
            assert await web_module._is_safe_public_url(url) is False, url
        for url in accepted:
            assert await web_module._is_safe_public_url(url) is True, url

    run(scenario())


def test_web_extract_lists_urls_left_out_by_the_five_url_limit(monkeypatch):
    import agent.runtime.tools.web as web_module

    async def safe_url(url):
        return True

    def page(url):
        return _READABLE_PAGE.replace("Readable page body", f"Body of {url}")

    calls = _fake_web_transport(monkeypatch, get=page)
    monkeypatch.setattr(web_module, "_is_safe_public_url", safe_url)
    urls = [f"https://example.test/page-{index}" for index in range(1, 8)]

    async def scenario():
        registry = ToolRegistry()
        register_web_tools(registry, None)
        result = await registry.execute("web_extract", {"urls": urls, "provider": "http"})
        payload = json.loads(result["output"])

        assert payload["success"] is True
        assert [entry["url"] for entry in payload["results"]] == urls[:5]
        assert all(f"Body of {entry['url']}" in entry["content"] for entry in payload["results"])
        assert payload["not_processed"]["urls"] == urls[5:]
        assert "5" in payload["not_processed"]["reason"]
        assert sorted(url for _, url in calls) == urls[:5]
        assert result["partial"] is True

        within_limit = await registry.execute("web_extract", {"urls": urls[:5], "provider": "http"})
        assert "not_processed" not in json.loads(within_limit["output"])
        assert not within_limit.get("partial")

    run(scenario())


def test_web_extract_starts_at_the_configured_provider_unless_the_call_names_one(monkeypatch):
    import agent.runtime.tools.web as web_module

    async def safe_url(url):
        return True

    def page(url):
        return _READABLE_PAGE.replace("Readable page body", f"Body of {url}")

    # Exa comes before plain HTTP in the built-in order; the setting moves the start.
    monkeypatch.setenv("EXA_API_KEY", "test-exa-key")
    monkeypatch.setenv("WEB_EXTRACT_PROVIDER", "http")
    calls = _fake_web_transport(monkeypatch, get=page)
    monkeypatch.setattr(web_module, "_is_safe_public_url", safe_url)

    async def scenario():
        registry = ToolRegistry()
        register_web_tools(registry, None)

        configured = json.loads((await registry.execute(
            "web_extract", {"urls": ["https://example.test/a"]},
        ))["output"])
        assert configured["provider_chain"] == ["http"]
        assert configured["results"][0]["backend"] == "http"
        assert "Body of https://example.test/a" in configured["results"][0]["content"]
        assert ("get", "https://example.test/a") in calls
        assert not any(kind == "post" for kind, _ in calls)

        named = json.loads((await registry.execute(
            "web_extract", {"urls": ["https://example.test/b"], "provider": "exa"},
        ))["output"])
        assert named["provider_chain"][0] == "exa"

    run(scenario())


def _without_web_keys(monkeypatch):
    for name in (
        "EXA_API_KEY", "EXA_ENV_FILE", "TAVILY_API_KEY", "PARALLEL_API_KEY", "FIRECRAWL_API_KEY",
        "FIRECRAWL_URL", "SEARXNG_URL", "WEB_SEARCH_PROVIDER", "WEB_EXTRACT_PROVIDER",
    ):
        monkeypatch.delenv(name, raising=False)


def _web_status_error(url, status):
    import httpx

    request = httpx.Request("GET", url)
    return httpx.HTTPStatusError(
        f"HTTP {status} for {url}",
        request=request,
        response=httpx.Response(status, request=request),
    )


def _web_answers_with(status):
    def handler(url, *_):
        raise _web_status_error(url, status)

    return handler


def _web_times_out(url, *_):
    import httpx

    # httpx timeouts really do carry an empty message.
    raise httpx.ReadTimeout("")


def _web_refuses(url, *_):
    import httpx

    raise httpx.ConnectError("All connection attempts failed")


def _web_host_unknown(url, *_):
    import httpx

    try:
        raise socket.gaierror(socket.EAI_NONAME, "nodename nor servname provided, or not known")
    except socket.gaierror as cause:
        raise httpx.ConnectError(str(cause)) from cause


def _web_certificate_rejected(url, *_):
    import ssl

    import httpx

    try:
        raise ssl.SSLCertVerificationError(1, "certificate verify failed: self-signed certificate")
    except ssl.SSLCertVerificationError as cause:
        raise httpx.ConnectError(str(cause)) from cause


@pytest.mark.parametrize("exa_key,provider,get,post,code,retryable,in_error,in_hint,not_in_hint", [
    pytest.param(
        False, "exa", None, None, "exa_not_configured", False,
        ["EXA_API_KEY is not configured"], ["provider=searxng"], ["Retry"], id="exa-without-key",
    ),
    pytest.param(
        True, "exa", None, _web_times_out, "search_failed", True,
        ["Exa search failed", "ReadTimeout"], ["Retry later", "provider=searxng"], [], id="exa-timeout",
    ),
    pytest.param(
        True, "exa", None, _web_answers_with(401), "search_failed", False,
        ["Exa search failed", "401"], ["provider=searxng"], ["Retry"], id="exa-rejected",
    ),
    pytest.param(
        False, "searxng", _web_refuses, None, "search_failed", True,
        ["Cannot reach SearXNG", "All connection attempts failed"],
        ["Retry later", "No other search route"], ["provider=exa"], id="searxng-refused-no-exa",
    ),
    pytest.param(
        True, "searxng", _web_answers_with(403), None, "search_failed", False,
        ["Cannot reach SearXNG", "403"], ["provider=exa"], ["Retry"], id="searxng-forbidden-exa-ready",
    ),
    pytest.param(
        False, "auto", _web_answers_with(503), None, "search_failed", True,
        ["SearXNG unavailable", "503", "Exa: EXA_API_KEY is not configured"],
        ["Retry later", "No other search route"], ["provider=exa"], id="auto-every-route-failed",
    ),
    pytest.param(
        True, "auto", _web_answers_with(404), _web_answers_with(401), "search_failed", False,
        ["SearXNG unavailable", "404", "Exa: ", "401"],
        ["No other search route"], ["Retry", "provider=exa"], id="auto-both-rejected",
    ),
    pytest.param(
        False, "searxng", lambda url: "<html><body>Sign in to this network</body></html>", None,
        "search_response_invalid", False, ["SearXNG", "not valid JSON"],
        ["No other search route"], ["Retry"], id="searxng-not-json",
    ),
])
def test_search_web_reports_a_call_with_no_working_route_as_a_failure(
    monkeypatch, exa_key, provider, get, post, code, retryable, in_error, in_hint, not_in_hint,
):
    _without_web_keys(monkeypatch)
    if exa_key:
        monkeypatch.setenv("EXA_API_KEY", "test-exa-key")
    _fake_web_transport(monkeypatch, get=get, post=post)

    async def scenario():
        registry = ToolRegistry()
        register_web_tools(registry, None)
        result = await registry.execute("search_web", {"query": "obscure named thing", "provider": provider})

        # Nothing is left in the output for the model to read as a result.
        assert result["output"] == ""
        assert result["code"] == code
        assert result["retryable"] is retryable
        for text in in_error:
            assert text in result["error"]
        for text in in_hint:
            assert text in result["recovery_hint"]
        for text in not_in_hint:
            assert text not in result["recovery_hint"]

    run(scenario())


def test_search_web_runs_again_after_a_failure_and_answers_no_match_as_a_result(monkeypatch):
    import httpx

    service = {"down": True}

    def searxng(url):
        if service["down"]:
            raise httpx.ConnectError("All connection attempts failed")
        query = _searxng_query(url)["q"]
        if query == "nothing here":
            return {"query": query, "results": []}
        return {"query": query, "results": [{
            "title": "Back again", "url": "https://example.test/back", "content": "snippet",
        }]}

    _without_web_keys(monkeypatch)
    calls = _fake_web_transport(monkeypatch, get=searxng)

    async def scenario():
        registry = ToolRegistry()
        register_web_tools(registry, None)
        arguments = {"query": "service notes", "provider": "searxng"}
        failed = await registry.execute("search_web", arguments)
        assert failed["output"] == "" and failed["retryable"] is True

        service["down"] = False
        again = await registry.execute("search_web", arguments)
        assert again["error"] == ""
        assert "Back again" in again["output"]
        assert len(calls) == 2

        for chosen in ("searxng", "auto"):
            empty = await registry.execute("search_web", {"query": "nothing here", "provider": chosen})
            assert empty["error"] == ""
            assert empty["output"].startswith("No ")
            assert "'nothing here'" in empty["output"]
            assert "not proof" in empty["output"]
        # SearXNG answered, so the call worked; the Exa route it could not ask is named.
        assert "Exa: EXA_API_KEY is not configured" in empty["output"]

        blank = await registry.execute("search_web", {"query": "   "})
        assert blank["output"] == "" and blank["code"] == "invalid_arguments"

    run(scenario())


def test_search_web_exa_without_hits_is_an_answer(monkeypatch):
    _without_web_keys(monkeypatch)
    monkeypatch.setenv("EXA_API_KEY", "test-exa-key")
    _fake_web_transport(monkeypatch, post=lambda url, payload: {"results": []})

    async def scenario():
        registry = ToolRegistry()
        register_web_tools(registry, None)
        result = await registry.execute("search_web", {"query": "nothing here", "provider": "exa"})

        assert result["error"] == ""
        assert result["output"].startswith("No Exa results for 'nothing here'")
        assert "not proof" in result["output"]

    run(scenario())


@pytest.mark.parametrize("handler,code,retryable,in_error", [
    pytest.param(_web_times_out, "fetch_failed", True, "ReadTimeout", id="timeout"),
    pytest.param(_web_refuses, "fetch_failed", True, "All connection attempts failed", id="refused"),
    pytest.param(_web_answers_with(503), "fetch_failed", True, "503", id="server-error"),
    pytest.param(_web_answers_with(404), "fetch_failed", False, "404", id="not-found"),
    pytest.param(_web_host_unknown, "fetch_failed", False, "nodename nor servname", id="unknown-host"),
    pytest.param(_web_certificate_rejected, "fetch_failed", False, "certificate verify failed", id="bad-certificate"),
    pytest.param(lambda url: "", "empty_response", False, "empty body", id="empty-body"),
])
def test_fetch_url_reports_a_page_it_could_not_get_as_a_failure(monkeypatch, handler, code, retryable, in_error):
    calls = _fake_web_transport(monkeypatch, get=handler)

    async def scenario():
        registry = ToolRegistry()
        register_web_tools(registry, None)
        url = "https://example.test/report"
        result = await registry.execute("fetch_url", {"url": url})

        assert result["output"] == ""
        assert result["code"] == code
        assert result["retryable"] is retryable
        assert url in result["error"]
        assert in_error in result["error"]
        # The hint only proposes a retry when one can help.
        assert ("Retry later" in result["recovery_hint"]) is retryable
        assert "web_extract" in result["recovery_hint"]
        assert calls == [("get", url)]

        scheme = await registry.execute("fetch_url", {"url": "ftp://example.test/report"})
        assert scheme["output"] == "" and scheme["code"] == "invalid_url"
        assert scheme["retryable"] is False
        assert len(calls) == 1

    run(scenario())


@pytest.mark.parametrize("failure,retryable", [
    pytest.param(lambda url: urllib.error.HTTPError(url, 503, "Service Unavailable", None, None), True, id="server-error"),
    pytest.param(lambda url: urllib.error.HTTPError(url, 404, "Not Found", None, None), False, id="not-found"),
    pytest.param(lambda url: urllib.error.URLError(ConnectionRefusedError(61, "Connection refused")), True, id="refused"),
    pytest.param(lambda url: urllib.error.URLError(socket.gaierror(8, "nodename nor servname provided")), False, id="unknown-host"),
    pytest.param(lambda url: TimeoutError("timed out"), True, id="timeout"),
    pytest.param(lambda url: ValueError("unknown url type"), False, id="bad-url"),
])
def test_fetch_url_keeps_the_retry_flag_when_httpx_is_missing(monkeypatch, failure, retryable):
    import agent.runtime.tools.web as web_module

    opened = []

    class Opener:
        def open(self, request, timeout=None):
            opened.append(request.full_url)
            raise failure(request.full_url)

    for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(web_module, "httpx", None)
    monkeypatch.setattr(web_module.urllib.request, "build_opener", lambda *handlers: Opener())

    async def scenario():
        registry = ToolRegistry()
        register_web_tools(registry, None)
        result = await registry.execute("fetch_url", {"url": "https://example.test/page"})

        assert opened == ["https://example.test/page"]
        assert result["output"] == ""
        assert result["code"] == "fetch_failed"
        assert result["retryable"] is retryable

    run(scenario())


def test_web_extract_fails_the_call_only_when_no_url_was_read(monkeypatch):
    import agent.runtime.tools.web as web_module

    good = "https://docs.example.test/guide"
    missing = "https://docs.example.test/gone"
    slow = "https://slow.example.test/report"
    private = "http://127.0.0.1/admin"

    def page(url):
        if url == good:
            return _READABLE_PAGE
        if url == slow:
            return _web_times_out(url)
        raise _web_status_error(url, 404)

    def public_dns(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    _without_web_keys(monkeypatch)
    calls = _fake_web_transport(monkeypatch, get=page)
    monkeypatch.setattr(web_module.socket, "getaddrinfo", public_dns)

    async def scenario():
        registry = ToolRegistry()
        register_web_tools(registry, None)

        # One URL read: the other URL's error stays inside the result.
        mixed = await registry.execute("web_extract", {"urls": [good, missing], "provider": "http"})
        payload = json.loads(mixed["output"])
        assert mixed["error"] == ""
        assert payload["success"] is True
        assert "Readable page body" in payload["results"][0]["content"]
        assert "404" in payload["results"][1]["error"]

        failed = await registry.execute("web_extract", {"urls": [missing, slow, private], "provider": "http"})
        assert failed["output"] == ""
        assert failed["code"] == "extract_failed"
        reasons = dict(line[2:].split(": ", 1) for line in failed["error"].splitlines()[1:])
        assert list(reasons) == [missing, slow, private]
        assert "404" in reasons[missing]
        assert "ReadTimeout" in reasons[slow]
        assert reasons[private].startswith("Blocked")
        # The timeout is the one cause that a retry can change.
        assert failed["retryable"] is True
        assert "retry" in failed["recovery_hint"]
        assert failed["details"]["provider_chain"] == ["http"]

        settled = await registry.execute("web_extract", {"urls": [missing, private], "provider": "http"})
        assert settled["code"] == "extract_failed"
        assert settled["retryable"] is False
        assert "retry" not in settled["recovery_hint"]

        gone = [f"https://docs.example.test/gone-{index}" for index in range(1, 8)]
        many = await registry.execute("web_extract", {"urls": gone, "provider": "http"})
        assert many["code"] == "extract_failed"
        assert [url for url in gone if url in many["error"]] == gone[:5]
        assert "2 more URLs were not tried" in many["error"]
        assert many["details"]["not_processed"] == gone[5:]

        requests_before = len(calls)
        for nothing in ([], ["  "]):
            empty = await registry.execute("web_extract", {"urls": nothing})
            assert empty["output"] == "" and empty["code"] == "invalid_arguments"
        assert len(calls) == requests_before

    run(scenario())


@pytest.mark.parametrize("configured,retryable", [(False, False), (True, True)])
def test_web_extract_does_not_take_an_absent_default_firecrawl_as_a_reason_to_retry(
    monkeypatch, configured, retryable,
):
    import agent.runtime.tools.web as web_module

    async def safe_url(url):
        return True

    _without_web_keys(monkeypatch)
    if configured:
        monkeypatch.setenv("FIRECRAWL_URL", "http://firecrawl.example.test:3002")
    # Nothing answers on the Firecrawl address; the page itself is gone.
    _fake_web_transport(monkeypatch, get=_web_answers_with(404), post=_web_refuses)
    monkeypatch.setattr(web_module, "_is_safe_public_url", safe_url)

    async def scenario():
        registry = ToolRegistry()
        register_web_tools(registry, None)
        result = await registry.execute("web_extract", {
            "urls": ["https://docs.example.test/gone"],
            "provider": "firecrawl",
        })

        assert result["code"] == "extract_failed"
        assert "firecrawl: All connection attempts failed" in result["error"]
        assert "http: HTTP 404" in result["error"]
        assert result["retryable"] is retryable

    run(scenario())


def test_web_extract_returns_a_short_plain_body_when_nothing_else_read_the_url(monkeypatch):
    import agent.runtime.tools.web as web_module

    health = "https://api.example.test/health"
    ping = "https://api.example.test/ping"
    shell = "https://api.example.test/app"
    bodies = {
        health: '{"status": "ok"}',
        ping: "pong\n",
        shell: (
            '<!doctype html><html><head><script src="/app.js"></script></head>'
            '<body><div id="root"></div></body></html>'
        ),
    }

    def get(url):
        if "/search?" in url:
            return {"query": "health", "results": []}
        return bodies[url]

    async def safe_url(url):
        return True

    _without_web_keys(monkeypatch)
    # No POST handler: the Firecrawl backend in front of plain HTTP cannot be reached.
    calls = _fake_web_transport(monkeypatch, get=get)
    monkeypatch.setattr(web_module, "_is_safe_public_url", safe_url)

    async def scenario():
        registry = ToolRegistry()
        register_web_tools(registry, None)
        direct = await registry.execute("web_extract", {"urls": [health, ping, shell], "provider": "http"})
        payload = json.loads(direct["output"])
        health_entry, ping_entry, shell_entry = payload["results"]

        assert direct["error"] == ""
        assert payload["success"] is True
        assert health_entry["content"] == '{"status": "ok"}'
        assert health_entry["error"] is None
        assert health_entry["backend"] == "http"
        assert ping_entry["content"] == "pong"
        # Markup that yields no text is still the page that may need rendering.
        assert shell_entry["content"] == ""
        assert "JavaScript" in shell_entry["error"]

        alone = await registry.execute("web_extract", {"urls": [shell], "provider": "http"})
        assert alone["output"] == "" and alone["code"] == "extract_failed"
        assert shell in alone["error"] and "JavaScript" in alone["error"]

        # Under auto the backends in front and the search for a moved page still run first.
        calls.clear()
        auto = await registry.execute("web_extract", {"urls": [health]})
        entry = json.loads(auto["output"])["results"][0]
        assert auto["error"] == ""
        assert entry["content"] == '{"status": "ok"}'
        assert entry["error"] is None
        assert entry["fallback_from"] == ["firecrawl", "search"]
        assert ("get", health) in calls
        assert any(kind == "post" for kind, _ in calls)
        assert any(kind == "get" and "/search?" in url for kind, url in calls)

    run(scenario())


def test_web_extract_names_a_final_url_that_does_not_resolve_without_calling_it_private(monkeypatch):
    import agent.runtime.tools.web as web_module

    moved, internal, read = (f"https://docs.example.test/{name}" for name in ("moved", "internal", "read"))
    finals = {
        moved: "https://relocated.example.test/moved",
        internal: "http://intranet.example.test/internal",
        read: read,
    }

    def dns(host, port, *args, **kwargs):
        if host == "docs.example.test":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]
        if host == "intranet.example.test":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", port))]
        raise socket.gaierror(socket.EAI_NONAME, "nodename nor servname provided, or not known")

    def post(url, payload):
        if url.endswith("/contents"):
            return {"results": [
                {"url": finals[item], "title": "Page", "text": f"PROVIDER-TEXT for {item} " + "words " * 40}
                for item in payload["urls"]
            ]}
        return {"success": False, "error": "scrape failed"}

    _without_web_keys(monkeypatch)
    monkeypatch.setenv("EXA_API_KEY", "test-exa-key")
    _fake_web_transport(monkeypatch, get=_web_answers_with(404), post=post)
    monkeypatch.setattr(web_module.socket, "getaddrinfo", dns)

    async def scenario():
        registry = ToolRegistry()
        register_web_tools(registry, None)
        result = await registry.execute("web_extract", {"urls": [moved, internal, read], "provider": "exa"})
        payload = json.loads(result["output"])
        moved_entry, internal_entry, read_entry = payload["results"]

        assert read_entry["backend"] == "exa"
        assert read_entry["content"].startswith(f"PROVIDER-TEXT for {read}")
        exa_reason = moved_entry["error"].split("; ")[0]
        assert exa_reason == (
            "exa: could not resolve the host name of the final URL https://relocated.example.test/moved"
        )
        assert internal_entry["error"].split("; ")[0] == "exa: redirected to an unsafe/private URL"
        # Whatever the wording, neither final URL's text is handed over.
        for entry in (moved_entry, internal_entry):
            assert entry["content"] == ""
            assert "PROVIDER-TEXT" not in json.dumps(entry)

    run(scenario())


def test_extract_url_reports_a_failed_extraction_as_a_failure(monkeypatch):
    import agent.runtime.tools.web as web_module

    slow = "https://example.test/slow"
    shell = "https://example.test/app"

    class FailedProcess:
        returncode = 3

        async def communicate(self):
            return b"", b"renderer crashed"

    async def fake_subprocess_exec(*cmd, **kwargs):
        return FailedProcess()

    def get(url):
        if url == slow:
            return _web_times_out(url)
        return '<html><body><div id="root"></div></body></html>'

    monkeypatch.setenv("BROWSER_EXTRACT_ARGV", '["browser-extract"]')
    monkeypatch.delenv("BROWSER_EXTRACT_CMD", raising=False)
    monkeypatch.delenv("WSL_EXTRACT_CMD", raising=False)
    _fake_web_transport(monkeypatch, get=get)
    monkeypatch.setattr(web_module.asyncio, "create_subprocess_exec", fake_subprocess_exec)

    async def scenario():
        registry = ToolRegistry()
        register_web_tools(registry, None)

        timed_out = await registry.execute("extract_url", {"url": slow})
        assert timed_out["output"] == ""
        assert timed_out["code"] == "browser_extract_failed"
        assert slow in timed_out["error"]
        assert "exit 3" in timed_out["error"] and "renderer crashed" in timed_out["error"]
        # The static attempt that came first is no longer dropped.
        assert "static attempt before it failed (ReadTimeout)" in timed_out["error"]
        assert timed_out["retryable"] is True
        assert "web_extract" in timed_out["recovery_hint"]

        no_text = await registry.execute("extract_url", {"url": shell})
        assert no_text["code"] == "browser_extract_failed"
        assert "too little text" in no_text["error"]
        assert no_text["retryable"] is False

        scheme = await registry.execute("extract_url", {"url": "file:///etc/hosts"})
        assert scheme["output"] == "" and scheme["code"] == "invalid_url"

        # The browser tools read the extractor's own text and still get it.
        direct = await web_module.create_browser_extract_fn()(shell)
        assert direct.startswith("[Browser Error]") and "exit 3" in direct

    run(scenario())


def test_search_status_reports_searxng_and_browser(monkeypatch):
    import agent.runtime.tools.web as web_module

    class FakeResponse:
        def __init__(self, text):
            self.text = text

        def raise_for_status(self):
            pass

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def get(self, url, **kwargs):
            if url.endswith("/config"):
                return FakeResponse('{"search": {"formats": ["html", "json"]}}')
            if "/search?" in url:
                return FakeResponse('{"query": "test", "results": [{"title": "ok"}]}')
            raise AssertionError(url)

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            return b"/home/user/bin/extract\n", b""

    async def fake_subprocess_exec(*cmd, **kwargs):
        return FakeProcess()

    monkeypatch.setattr(web_module.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(web_module.asyncio, "create_subprocess_exec", fake_subprocess_exec)

    async def scenario():
        registry = ToolRegistry()
        register_web_tools(registry, None)
        result = await registry.execute("search_status", {})

        assert "SearXNG: ok" in result["output"]
        assert "JSON search: ok" in result["output"]
        assert "Browser extractor: ok" in result["output"]

    run(scenario())


def test_react_agent_does_not_inject_time_prompt():
    class FakeLLM:
        def __init__(self):
            self.messages = None

        async def chat_stream(self, messages, tools):
            self.messages = messages
            yield {"type": "done", "content": "ok", "usage": None}

    async def scenario():
        llm = FakeLLM()
        agent = ReActAgent("agent", llm, ToolRegistry(), max_iterations=1)
        events = [event async for event in agent.reply_stream(Msg(content=[ContentBlock.text("最近有什么新闻")]))]

        assert events[-1]["type"] == "done"
        system_messages = [msg for msg in llm.messages if msg["role"] == "system"]
        assert len(system_messages) == 1
        assert llm.messages[0] is system_messages[0]
        assert "当前日期时间" not in system_messages[0]["content"]
        assert "相对时间" not in system_messages[0]["content"]

    run(scenario())


@pytest.mark.parametrize("query", ["当前时间", "现在几点", "现在呢"])
def test_time_language_reaches_normal_model_flow(query):
    class FakeLLM:
        def __init__(self):
            self.calls = 0
            self.messages = None

        async def chat_stream(self, messages, tools):
            self.calls += 1
            self.messages = messages
            yield {"type": "done", "content": "model handled it", "usage": None}

    async def scenario():
        llm = FakeLLM()
        agent = ReActAgent("agent", llm, ToolRegistry(), max_iterations=1)
        events = [
            event
            async for event in agent.reply_stream(
                Msg(content=[ContentBlock.text(query)])
            )
        ]

        assert llm.calls == 1
        assert any(
            query in str(message.get("content", "")) for message in llm.messages
        )
        assert events[-1]["type"] == "done"

    run(scenario())


def test_current_time_tool_uses_runtime_clock(monkeypatch):
    from datetime import datetime

    from agent.runtime import time_utils

    frozen = datetime.fromisoformat("2026-05-20T17:08:09+08:00")

    class FakeDateTime:
        @classmethod
        def now(cls, tz=None):
            return frozen.astimezone(tz)

    monkeypatch.setenv("AGENT_TIMEZONE", "Asia/Shanghai")
    monkeypatch.setattr(time_utils, "datetime", FakeDateTime)

    async def scenario():
        registry = ToolRegistry()
        register_time_tools(registry)
        result = await registry.execute("current_time", {})

        assert result["output"] == "2026-05-20 周三 17:08:09 UTC+08:00 (Asia/Shanghai)"

    run(scenario())


def test_current_datetime_uses_explicit_timezone(monkeypatch):
    from agent.runtime import time_utils

    captured = []

    class FakeDateTime:
        @classmethod
        def now(cls, timezone=None):
            captured.append(timezone)
            return cls()

    monkeypatch.setattr(time_utils, "datetime", FakeDateTime)

    result = time_utils.current_datetime("UTC")

    assert isinstance(result, FakeDateTime)
    assert getattr(captured[0], "key", None) == "UTC"


def test_current_datetime_falls_back_for_invalid_timezone(monkeypatch):
    from agent.runtime import time_utils

    class FakeDateTime:
        calls = 0

        @classmethod
        def now(cls, timezone=None):
            cls.calls += 1
            return cls()

        def astimezone(self):
            return self

    monkeypatch.setattr(time_utils, "datetime", FakeDateTime)

    result = time_utils.current_datetime("Missing/Timezone")

    assert isinstance(result, FakeDateTime)
    assert FakeDateTime.calls == 1


def test_docker_sandbox_builds_reusable_container_commands():
    sandbox = DockerSandbox(workdir="D:\\work", docker_cmd="docker", reuse_container=True, container_name="agent-test")

    start_args = sandbox._build_start_args()
    exec_args = sandbox._build_exec_args("python3 - <<'PY'\nprint('ok')\nPY")

    assert start_args[:5] == ["docker", "run", "-d", "-i", "--name"]
    assert "agent-test" in start_args
    assert exec_args[:4] == ["docker", "exec", "-i", "agent-test"]


def test_docker_sandbox_uses_configured_image(monkeypatch):
    monkeypatch.setenv("ASTRA_DOCKER_IMAGE", "astra-sandbox:test")
    sandbox = DockerSandbox(workdir="D:\\work", docker_cmd="docker")

    assert sandbox.image == "astra-sandbox:test"
    assert "astra-sandbox:test" in sandbox._build_args("true")


def test_docker_sandbox_caches_image_inspection(monkeypatch):
    class FakeProc:
        returncode = 0

        async def communicate(self):
            return b"", b""

    calls = []

    async def fake_exec(*args, **kwargs):
        calls.append(args)
        return FakeProc()

    async def scenario():
        docker_module.DockerSandbox._image_ready.clear()
        monkeypatch.setattr(docker_module.asyncio, "create_subprocess_exec", fake_exec)
        sandbox = DockerSandbox(workdir="D:\\work", docker_cmd="docker")
        await sandbox._ensure_image()
        await sandbox._ensure_image()

        inspect_calls = [args for args in calls if args[:3] == ("docker", "image", "inspect")]
        assert len(inspect_calls) == 1

    run(scenario())


def test_react_preprocesses_deepseek_local_image_before_first_provider_call(tmp_path):
    source = _write_react_pattern_png(tmp_path / "large.png", (1600, 900))
    llm = CapturingVisionLLM(policy=VisionPreprocessPolicy())
    agent = ReActAgent("agent", llm, ToolRegistry(), max_iterations=1)
    agent.vision_tiles_enabled = True

    events = run(_collect_stream(agent.reply_stream(Msg(
        content=build_image_message_content(str(source)),
    ))))

    parts = llm.calls[0][-1]["content"]
    assert sum(part.get("type") == "image_url" for part in parts) == 7
    status = next(event for event in events if event.get("type") == "vision_preprocess")
    assert status["protected"] is True
    assert events[-1]["type"] == "done"


def test_tiled_base64_is_replaced_with_original_storage_placeholder(tmp_path):
    source = _write_react_pattern_png(tmp_path / "large.png", (1600, 900))
    llm = CapturingVisionLLM(policy=VisionPreprocessPolicy())
    agent = ReActAgent("agent", llm, ToolRegistry(), max_iterations=1)
    agent.context.set_session(str(tmp_path / "session.json"))

    run(_collect_stream(agent.reply_stream(Msg(
        content=build_image_message_content(str(source)),
    ))))

    saved = SessionStore(tmp_path / "session.json").load()
    serialized = json.dumps(saved)
    assert "data:image" not in serialized
    assert "large.png" in serialized
    assert ".astra/image-cache/tiles" not in serialized.replace("\\\\", "/")


def test_bare_base64_large_image_is_tiled_and_persists_only_placeholder(tmp_path):
    source = _write_react_pattern_png(tmp_path / "bare-large.png", (1600, 900))
    encoded = base64.b64encode(source.read_bytes()).decode("ascii")
    original = ContentBlock.image_url(
        f"data:image/png;base64,{encoded}",
        detail="original",
    )
    llm = CapturingVisionLLM(policy=VisionPreprocessPolicy())
    agent = ReActAgent("agent", llm, ToolRegistry(), max_iterations=1)
    session_path = tmp_path / "bare-session.json"
    agent.context.set_session(str(session_path))

    events = run(_collect_stream(agent.reply_stream(Msg(content=[original]))))

    assert sum(
        part.get("type") == "image_url"
        for part in llm.calls[0][-1]["content"]
    ) == 7
    assert any(
        event.get("type") == "vision_preprocess" and event.get("protected")
        for event in events
    )
    serialized = json.dumps(SessionStore(session_path).load())
    normalized = serialized.replace("\\\\", "/")
    assert "[Image: attached image]" in serialized
    assert "data:image" not in serialized
    assert ".astra/image-cache/tiles" not in normalized
    assert ".staging" not in normalized
    assert str(source) not in serialized


@pytest.mark.parametrize(
    "data_url",
    [
        "data:image/png;base64,not-valid-%%%",
        "data:image/svg+xml;base64,PHN2Zz4=",
    ],
)
def test_invalid_bare_base64_fails_closed_before_provider(data_url, tmp_path):
    llm = CapturingVisionLLM(policy=VisionPreprocessPolicy())
    agent = ReActAgent("agent", llm, ToolRegistry(), max_iterations=1)
    session_path = tmp_path / "invalid-bare-session.json"
    agent.context.set_session(str(session_path))

    events = run(_collect_stream(agent.reply_stream(Msg(
        content=[ContentBlock.image_url(data_url)],
    ))))

    assert llm.calls == []
    error = next(event for event in events if event.get("type") == "error")
    assert error["code"] == "vision_preprocess_failed"
    assert data_url not in error["message"]
    assert "data:image" not in json.dumps(SessionStore(session_path).load())


def test_oversized_bare_base64_fails_closed_before_provider(monkeypatch, tmp_path):
    source = _write_react_pattern_png(tmp_path / "oversized-bare.png", (64, 64))
    encoded = base64.b64encode(source.read_bytes()).decode("ascii")
    monkeypatch.setattr(vision_module, "MAX_DATA_URL_ENCODED_BYTES", 32)
    llm = CapturingVisionLLM(policy=VisionPreprocessPolicy())
    agent = ReActAgent("agent", llm, ToolRegistry(), max_iterations=1)

    events = run(_collect_stream(agent.reply_stream(Msg(content=[
        ContentBlock.image_url(f"data:image/png;base64,{encoded}"),
    ]))))

    assert llm.calls == []
    error = next(event for event in events if event.get("type") == "error")
    assert error["code"] == "vision_preprocess_failed"
    assert "encoded size limit" in error["message"]


def test_bare_base64_cleanup_failure_blocks_provider_and_release_retries(
    caplog, monkeypatch, tmp_path
):
    source = _write_react_pattern_png(tmp_path / "small-inline.png", (64, 64))
    encoded = base64.b64encode(source.read_bytes()).decode("ascii")
    llm = CapturingVisionLLM(policy=VisionPreprocessPolicy())
    agent = ReActAgent("agent", llm, ToolRegistry(), max_iterations=1)
    processor = vision_module.VisionPreprocessor(tmp_path / "private-cache")
    agent.vision_preprocessor = processor
    original_unlink = Path.unlink

    def deny_staging_unlink(path, *args, **kwargs):
        if ".staging" in path.parts and path.name.startswith("source."):
            raise PermissionError("private staging path and UUID")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", deny_staging_unlink)
    events = run(_collect_stream(agent.reply_stream(Msg(content=[
        ContentBlock.image_url(f"data:image/png;base64,{encoded}"),
    ]))))

    assert llm.calls == []
    error = next(event for event in events if event.get("type") == "error")
    assert error["code"] == "vision_preprocess_failed"
    assert ".staging" not in error["message"]
    assert "UUID" not in error["message"]
    assert "private staging path" not in caplog.text
    assert processor._pending_staging_paths
    assert (tmp_path / "private-cache" / ".staging").is_dir()

    monkeypatch.setattr(Path, "unlink", original_unlink)
    processor.release_request("retry", "cleanup")

    assert not processor._pending_staging_paths
    assert not (tmp_path / "private-cache" / ".staging").exists()


def test_context_prompt_content_override_never_mutates_canonical_storage():
    context = AgentContext(system_prompt="system")
    storage_content = [{"type": "text", "text": "[Image: original.png]"}]
    transient_content = [{
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,transient"},
    }]
    context.add_user(storage_content)

    prompt = context.get_prompt(content_overrides={0: transient_content})

    assert prompt[-1]["content"][0]["type"] == "text"
    assert prompt[-1]["content"][0]["text"].startswith("<message_time>")
    assert prompt[-1]["content"][1:] == transient_content
    assert context.messages[0]["content"] == storage_content
    assert "data:image" not in json.dumps(context.messages)


def test_task_checkpoint_never_writes_transient_tiles_to_session_jsonl(tmp_path):
    source = _write_react_pattern_png(tmp_path / "checkpoint-large.png", (1600, 900))

    class ToolThenAnswerLLM(CapturingVisionLLM):
        async def chat_stream(self, messages, tools):
            self.calls.append(messages)
            self.tool_calls.append(tools)
            if len(self.calls) == 1:
                yield {
                    "type": "tool_calls",
                    "calls": [{
                        "id": "checkpoint-read",
                        "name": "checkpoint_read",
                        "arguments": "{}",
                    }],
                    "content": "",
                    "usage": None,
                }
                return
            yield {"type": "done", "content": "checkpoint safe", "usage": None}

    async def checkpoint_read():
        return "safe"

    registry = ToolRegistry()
    registry.register(ToolDef(
        name="checkpoint_read",
        description="force an after-tools checkpoint",
        parameters={"type": "object", "properties": {}},
        fn=checkpoint_read,
        risk="read",
        idempotent=True,
    ))
    task_store = TaskStore(tmp_path / "tasks.db")
    task = task_store.start_run(
        "checkpoint-external",
        "inspect image",
        session_id="checkpoint-session",
        model="deepseek-v4-flash-vision-exp",
    )
    session_path = tmp_path / "checkpoint-session.json"
    llm = ToolThenAnswerLLM(policy=VisionPreprocessPolicy())
    agent = ReActAgent(
        "agent",
        llm,
        registry,
        max_iterations=3,
        task_store=task_store,
    )
    agent.context.set_session(str(session_path))

    run(_collect_stream(agent.reply_stream(Msg(
        content=build_image_message_content(str(source)),
        metadata={
            "request_id": "checkpoint-external",
            "task_id": task["id"],
        },
    ))))

    session_store = SessionStore(session_path)
    raw_jsonl = session_store.jsonl_path.read_text(encoding="utf-8")
    normalized = raw_jsonl.replace("\\\\", "/")
    assert "data:image" not in raw_jsonl
    assert ".astra/image-cache/tiles" not in normalized
    replay = SessionStore(session_path).load()
    replay_text = json.dumps(replay, ensure_ascii=False)
    assert "data:image" not in replay_text
    assert ".astra/image-cache/tiles" not in replay_text.replace("\\\\", "/")
    assert "checkpoint-large.png" in replay_text
    assert task_store.get_task(task["id"])["checkpoint"]["phase"] == "turn_complete"


def test_overflow_closes_overview_tool_selection_and_answer_in_one_turn(tmp_path):
    source = _write_react_pattern_png(tmp_path / "long.png", (768, 9000))
    holder = {}
    registry = ToolRegistry()
    def select_then_mutate(tile_set_id, tile_ids):
        selection = holder["agent"].select_vision_tiles(tile_set_id, tile_ids)
        holder["selected_data_urls"] = tuple(selection["data_urls"])
        binding = next(
            item
            for item in holder["agent"].vision_preprocessor.bindings_for(
                "overflow-session",
                holder["agent"]._active_vision_request_id,
            )
            if item.tile_set_id == tile_set_id
        )
        selected_by_id = {tile.tile_id: tile for tile in binding.tiles}
        for tile_id in tile_ids:
            selected_by_id[tile_id].path.write_bytes(b"mutated after verified selection")
        return selection

    register_image_tools(
        registry,
        workdir=str(tmp_path),
        select_tiles=select_then_mutate,
    )

    class SelectingLLM(CapturingVisionLLM):
        async def chat_stream(self, messages, tools):
            self.calls.append(messages)
            self.tool_calls.append(tools)
            if len(self.calls) == 1:
                text = json.dumps(messages, ensure_ascii=False)
                tile_set_id = re.search(
                    r"tile_set_id[=: ]+([A-Za-z0-9_-]+)",
                    text,
                ).group(1)
                self.tile_set_id = tile_set_id
                tile_id = re.search(r"image1-r\d+c\d+", text).group(0)
                yield {
                    "type": "tool_calls",
                    "calls": [{
                        "id": "select-1",
                        "name": "read_image_tiles",
                        "arguments": json.dumps({
                            "tile_set_id": tile_set_id,
                            "tile_ids": [tile_id],
                        }),
                    }],
                    "content": "",
                    "usage": None,
                }
                return
            yield {"type": "done", "content": "inspected detail", "usage": None}

    llm = SelectingLLM(policy=VisionPreprocessPolicy())
    agent = ReActAgent("agent", llm, registry, max_iterations=3)
    agent.context.set_session(str(tmp_path / "overflow-session.json"))
    holder["agent"] = agent

    events = run(_collect_stream(agent.reply_stream(Msg(
        id="overflow-request",
        content=build_image_message_content(str(source)),
    ))))

    assert len(llm.calls) == 2
    assert any(event.get("type") == "done" for event in events)
    assert sum(
        part.get("type") == "image_url"
        for message in llm.calls[-1]
        for part in (
            message.get("content")
            if isinstance(message.get("content"), list)
            else []
        )
    ) <= 12
    sent_data_urls = [
        part["image_url"]["url"]
        for message in llm.calls[-1]
        for part in (
            message.get("content")
            if isinstance(message.get("content"), list)
            else []
        )
        if part.get("type") == "image_url"
        and str(part.get("image_url", {}).get("url", "")).startswith("data:")
    ]
    assert all(url in sent_data_urls for url in holder["selected_data_urls"])
    assert sum(len(url.encode("ascii")) for url in sent_data_urls) <= 41_943_040
    assert all(
        base64.b64decode(url.split(",", 1)[1]) != b"mutated after verified selection"
        for url in holder["selected_data_urls"]
    )
    selected_text = json.dumps(llm.calls[-1], ensure_ascii=False)
    assert "image1-r" in selected_text
    session_data = SessionStore(tmp_path / "overflow-session.json").load()
    serialized = json.dumps(session_data, ensure_ascii=False)
    assert ".astra/image-cache/tiles" not in serialized.replace("\\\\", "/")
    assert "data:image" not in serialized
    # Request projection has independent route/prefix digests. Actual tile
    # byte hashes and vision internals must still never become session data.
    for url in sent_data_urls:
        assert hashlib.sha256(base64.b64decode(url.split(",", 1)[1])).hexdigest() not in serialized
    canonical = {key: value for key, value in session_data.items() if key != "system_prompt_projection"}
    assert re.search(r"\b[0-9a-f]{64}\b", json.dumps(canonical)) is None
    assert "vision_preprocess" not in serialized
    assert "image1-r" in serialized

    with pytest.raises(VisionTileSelectionError, match="expired"):
        agent.vision_preprocessor.select_tiles(
            llm.tile_set_id,
            ["image1-r1c1"],
            session_id="overflow-session",
            request_id="overflow-request",
        )


def test_unconfigured_model_preserves_original_image_content(tmp_path):
    source = _write_react_pattern_png(tmp_path / "large.png", (1600, 900))
    llm = CapturingVisionLLM(policy=None)
    original = build_image_message_content(str(source))
    agent = ReActAgent("agent", llm, ToolRegistry(), max_iterations=1)

    events = run(_collect_stream(agent.reply_stream(Msg(content=original))))

    content = llm.calls[0][-1]["content"]
    assert content[0]["type"] == "text"
    assert content[0]["text"].startswith("<message_time>")
    assert content[1:] == Msg(content=original).to_chat_content()
    assert not any(event.get("type") == "vision_preprocess" for event in events)


def test_stale_policy_on_non_deepseek_model_bypasses_bytes_and_hides_tile_tool(tmp_path):
    source = _write_react_pattern_png(tmp_path / "stale-policy.png", (1600, 900))
    registry = ToolRegistry()
    register_image_tools(
        registry,
        workdir=str(tmp_path),
        select_tiles=lambda _set, _ids: {},
    )
    llm = CapturingVisionLLM(policy=VisionPreprocessPolicy())
    llm.config.model = "not-deepseek"
    original = build_image_message_content(str(source))
    agent = ReActAgent("agent", llm, registry, max_iterations=1)

    events = run(_collect_stream(agent.reply_stream(Msg(content=original))))

    content = llm.calls[0][-1]["content"]
    assert content[0]["type"] == "text"
    assert content[0]["text"].startswith("<message_time>")
    assert content[1:] == Msg(content=original).to_chat_content()
    assert not any(event.get("type") == "vision_preprocess" for event in events)
    assert "read_image_tiles" not in {
        schema["function"]["name"] for schema in llm.tool_calls[0]
    }


def test_invalid_or_visionless_deepseek_policy_does_not_activate_tiles():
    llm = CapturingVisionLLM(policy=VisionPreprocessPolicy())
    agent = ReActAgent("agent", llm, ToolRegistry(), max_iterations=1)

    llm.config.capabilities = frozenset({"tools"})
    assert agent._vision_tile_tool_enabled() is False

    llm.config.capabilities = frozenset({"vision", "tools"})
    llm.config.vision_preprocess = object()
    assert agent._vision_tile_tool_enabled() is False


def test_backend_reload_model_replaces_stale_vision_generation_settings():
    reload_switch = getattr(backend_module, "_apply_reloaded_model_profile", None)
    assert callable(reload_switch)
    created = []
    client = LLMClient(
        LLMConfig(
            model="deepseek-v4-flash-vision-exp",
            base_url="http://localhost:8080/v1",
            capabilities=frozenset({"vision", "tools"}),
            vision_detail="original",
            vision_preprocess=VisionPreprocessPolicy(),
        ),
        provider_factory=lambda config: created.append(config) or object(),
    )
    profile = ModelProfile(
        base_url="http://localhost:8081/v1",
        context_limit=32_000,
        model_id="ordinary-model",
        capabilities=frozenset({"tools"}),
        vision_detail="auto",
        vision_preprocess=None,
        temperature=0.7,
        max_tokens=2048,
    )
    entry = CatalogEntry(
        key="configured::ordinary-model",
        model_id="ordinary-model",
        provider_id="configured",
        provider_label="Configured",
        base_url=profile.base_url,
        profile=profile,
    )
    fake_agent = SimpleNamespace(
        llm=client,
        context=SimpleNamespace(max_prompt_tokens=0),
        invalidations=0,
    )
    fake_agent.invalidate_tool_schema_cache = lambda: setattr(
        fake_agent,
        "invalidations",
        fake_agent.invalidations + 1,
    )

    reload_switch(fake_agent, entry, "local")

    assert client.config.model == "ordinary-model"
    assert client.config.capabilities == frozenset({"tools"})
    assert client.config.vision_preprocess is None
    assert client.config.vision_detail == "auto"
    assert client.config.temperature == 0.7
    assert client.config.max_tokens == 2048
    assert fake_agent.context.max_prompt_tokens == prompt_token_budget(32_000, 2048)
    assert fake_agent.invalidations == 1
    assert ReActAgent("agent", client, ToolRegistry())._vision_tile_tool_enabled() is False


def test_all_agent_entrypoints_wire_saved_vision_tile_preference():
    project_root = Path(__file__).resolve().parents[1]
    for relative in (
        "agent/cli/main.py",
        "agent/cli/backend.py",
        "agent/cli/api_server.py",
    ):
        source = (project_root / relative).read_text(encoding="utf-8")
        assert "agent.set_vision_tiles_enabled(load_vision_tiles_enabled())" in source

    for relative in (
        "agent/cli/main.py",
        "agent/cli/backend.py",
    ):
        source = (project_root / relative).read_text(encoding="utf-8")
        assert "execute_vision_tiles_command" in source


def test_all_agent_entrypoints_wire_one_saved_context_index_broker_and_tool():
    project_root = Path(__file__).resolve().parents[1]
    for relative in (
        "agent/cli/main.py",
        "agent/cli/backend.py",
        "agent/cli/api_server.py",
    ):
        source = (project_root / relative).read_text(encoding="utf-8")
        assert "load_context_index_preferences()" in source
        assert "create_context_index_broker(" in source
        assert "register_context_index_tools(tools, context_index_broker)" in source
        assert "context_index_broker=context_index_broker" in source


def test_classic_cli_routes_context_index_to_live_agent(monkeypatch, tmp_path, capsys):
    from agent.cli.main import handle_slash

    settings = tmp_path / "settings.json"
    monkeypatch.setenv("AGENT_SETTINGS_PATH", str(settings))
    broker = SimpleNamespace(
        mode="off",
        set_mode=lambda mode: setattr(broker, "mode", mode),
        format_last_trace=lambda: "trace",
    )
    agent = ReActAgent(
        "agent",
        CapturingVisionLLM(VisionPreprocessPolicy()),
        ToolRegistry(),
        context_index_broker=broker,
    )

    run(handle_slash('/context-index "session"', agent))

    assert broker.mode == "session"
    assert json.loads(settings.read_text(encoding="utf-8"))["context_index_mode"] == "session"
    assert "Active mode: SESSION" in capsys.readouterr().out


def test_classic_cli_routes_vision_tiles_to_live_agent(monkeypatch, tmp_path, capsys):
    from agent.cli.main import handle_slash

    settings = tmp_path / "settings.json"
    monkeypatch.setenv("AGENT_SETTINGS_PATH", str(settings))
    agent = ReActAgent(
        "agent",
        CapturingVisionLLM(VisionPreprocessPolicy()),
        ToolRegistry(),
    )

    run(handle_slash("/vision-tiles off", agent))

    assert agent.vision_tiles_enabled is False
    assert json.loads(settings.read_text(encoding="utf-8"))["vision_tiles_enabled"] is False
    assert "Vision tiles: OFF" in capsys.readouterr().out


def test_classic_cli_constructs_saved_off_agent(monkeypatch, tmp_path):
    from agent.cli import main as main_module

    settings = tmp_path / "settings.json"
    settings.write_text('{"vision_tiles_enabled": false}', encoding="utf-8")
    for name, value in {
        "AGENT_SETTINGS_PATH": settings,
        "AGENT_SESSION_DIR": tmp_path / "sessions",
        "AGENT_TASK_DB": tmp_path / "tasks.db",
        "AGENT_MEMORY_PATH": tmp_path / "memory.db",
        "AGENT_LEARNING_PATH": tmp_path / "learning.db",
        "AGENT_SKILLS_PATH": tmp_path / "skills",
    }.items():
        monkeypatch.setenv(name, str(value))
    monkeypatch.setenv("SANDBOX_DOCKER", "false")
    monkeypatch.setattr(main_module, "find_chrome", lambda: None)
    monkeypatch.setattr(main_module.signal, "signal", lambda *_args: None)

    class FakeMCPManager:
        async def close(self):
            pass

    async def initialize_mcp(*_args, **_kwargs):
        return FakeMCPManager()

    constructed = []

    async def observe_agent(_bus, agent):
        constructed.append(agent)

    monkeypatch.setattr(main_module, "initialize_mcp_tools", initialize_mcp)
    monkeypatch.setattr(main_module, "cli_loop", observe_agent)
    config = LLMConfig(
        model="deepseek-v4-flash-vision-exp",
        api_key="local",
        base_url="http://127.0.0.1:9/v1",
        capabilities=frozenset({"vision", "tools"}),
        vision_preprocess=VisionPreprocessPolicy(),
    )

    run(main_module._async_init(config, 1, str(tmp_path)))

    assert len(constructed) == 1
    assert constructed[0].vision_tiles_enabled is False


def test_api_bootstrap_constructs_saved_off_agent(monkeypatch, tmp_path):
    from starlette.applications import Starlette

    monkeypatch.setattr(
        Starlette,
        "on_event",
        lambda _self, _event: lambda function: function,
        raising=False,
    )
    from agent.cli import api_server

    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps({
            "selected_model": "Qwen3.6-35B-A3B",
            "vision_tiles_enabled": False,
        }),
        encoding="utf-8",
    )
    for name, value in {
        "AGENT_SETTINGS_PATH": settings,
        "AGENT_MEMORY_PATH": tmp_path / "memory.db",
        "AGENT_SKILLS_PATH": tmp_path / "skills",
    }.items():
        monkeypatch.setenv(name, str(value))
    monkeypatch.setenv("QWEN_BASE_URL", "http://127.0.0.1:9/v1")

    agent = run(api_server._bootstrap_agent())

    assert agent.vision_tiles_enabled is False
    assert agent.context_index_broker.mode == "off"
    assert agent.tools.get("context_open") is not None
    run(agent.close_external_memory())


def test_usage_guide_documents_animated_gif_fail_closed_boundary():
    readme = (Path(__file__).resolve().parents[1] / "docs" / "usage.md").read_text(
        encoding="utf-8"
    )
    readme = " ".join(readme.split())

    assert "small animated GIFs are sent directly" in readme
    assert "animated GIF that requires tiling fails closed" in readme


def test_disabled_preference_preserves_original_image_content(tmp_path):
    source = _write_react_pattern_png(tmp_path / "large.png", (1600, 900))
    llm = CapturingVisionLLM(policy=VisionPreprocessPolicy())
    original = build_image_message_content(str(source))
    agent = ReActAgent("agent", llm, ToolRegistry(), max_iterations=1)
    agent.vision_tiles_enabled = False

    events = run(_collect_stream(agent.reply_stream(Msg(content=original))))

    content = llm.calls[0][-1]["content"]
    assert content[0]["type"] == "text"
    assert content[0]["text"].startswith("<message_time>")
    assert content[1:] == Msg(content=original).to_chat_content()
    assert not any(event.get("type") == "vision_preprocess" for event in events)


def test_large_local_decode_failure_does_not_reach_provider(tmp_path):
    source = tmp_path / "broken.png"
    source.write_bytes(b"not an image")
    llm = CapturingVisionLLM(policy=VisionPreprocessPolicy())
    agent = ReActAgent("agent", llm, ToolRegistry(), max_iterations=1)
    agent.context.set_session(str(tmp_path / "decode-failure-session.json"))

    events = run(_collect_stream(agent.reply_stream(Msg(
        content=build_image_message_content(str(source)),
    ))))

    assert llm.calls == []
    assert any(
        event.get("type") == "error" and "not sent" in event.get("message", "")
        for event in events
    )
    serialized = json.dumps(
        SessionStore(tmp_path / "decode-failure-session.json").load()
    )
    assert "data:image" not in serialized
    assert "broken.png" in serialized


def test_external_url_is_reported_as_unprotected_and_sent_unchanged():
    llm = CapturingVisionLLM(policy=VisionPreprocessPolicy())
    original = [
        ContentBlock.text("inspect"),
        ContentBlock.image_url("https://example.test/image.png", detail="original"),
    ]
    agent = ReActAgent("agent", llm, ToolRegistry(), max_iterations=1)

    events = run(_collect_stream(agent.reply_stream(Msg(content=original))))

    content = llm.calls[0][-1]["content"]
    assert content[0]["type"] == "text"
    assert content[0]["text"].startswith("<message_time>")
    assert content[1:] == Msg(content=original).to_chat_content()
    status = next(event for event in events if event.get("type") == "vision_preprocess")
    assert status["protected"] is False
    assert "external URL" in status["message"]


def test_mixed_vision_event_reports_local_and_external_protection_counts(tmp_path):
    source = _write_react_pattern_png(tmp_path / "mixed-local.png", (1600, 900))
    external = ContentBlock.image_url(
        "https://example.test/mixed-external.png",
        detail="original",
    )
    llm = CapturingVisionLLM(policy=VisionPreprocessPolicy())
    agent = ReActAgent("agent", llm, ToolRegistry(), max_iterations=1)

    events = run(_collect_stream(agent.reply_stream(Msg(
        content=[*build_image_message_content(str(source)), external],
    ))))

    status = next(event for event in events if event.get("type") == "vision_preprocess")
    assert status["protected"] is False
    assert status["protected_local_images"] == 1
    assert status["unprotected_external_images"] == 1
    assert "1 protected local image" in status["message"]
    assert "1 unprotected external image" in status["message"]


def test_read_image_tiles_schema_and_execution_are_policy_gated(tmp_path):
    selected = []
    registry = ToolRegistry()
    register_image_tools(
        registry,
        workdir=str(tmp_path),
        select_tiles=lambda tile_set_id, tile_ids: selected.append(
            (tile_set_id, tile_ids)
        ) or {
            "data_urls": [],
            "labels": [],
            "unserved_ids": [],
            "remaining_images": 0,
            "remaining_inline_bytes": 0,
        },
    )
    agent = ReActAgent(
        "agent",
        CapturingVisionLLM(policy=VisionPreprocessPolicy()),
        registry,
        max_iterations=1,
    )

    enabled = agent._available_tool_schemas("inspect", {"core"}, {}, set())
    enabled_names = [schema["function"]["name"] for schema in enabled]
    assert "read_image_tiles" in enabled_names
    assert enabled_names == [
        schema["function"]["name"]
        for schema in agent._available_tool_schemas(
            "inspect",
            {"core"},
            {"read_image_tiles": 2},
            {"read_image_tiles"},
        )
    ]

    agent.set_vision_tiles_enabled(False)
    disabled = agent._available_tool_schemas("inspect", {"core"}, {}, set())
    assert "read_image_tiles" not in {
        schema["function"]["name"] for schema in disabled
    }
    event = run(agent._execute_tool_calls([{
        "id": "stale-select",
        "name": "read_image_tiles",
        "arguments": json.dumps({
            "tile_set_id": "opaque",
            "tile_ids": ["image1-r1c1"],
        }),
    }]))[0]
    assert event["code"] == "vision_tile_selection_unavailable"
    assert selected == []


def test_vision_request_release_runs_after_provider_error(tmp_path):
    source = _write_react_pattern_png(tmp_path / "error-large.png", (1600, 900))

    class FailingLLM(CapturingVisionLLM):
        async def chat_stream(self, messages, tools):
            del messages, tools
            raise RuntimeError("provider failed")
            yield

    agent = ReActAgent(
        "agent",
        FailingLLM(policy=VisionPreprocessPolicy()),
        ToolRegistry(),
        max_iterations=1,
    )
    agent.context.set_session(str(tmp_path / "error-session.json"))
    releases = []
    release = agent.vision_preprocessor.release_request
    agent.vision_preprocessor.release_request = lambda session_id, request_id: (
        releases.append((session_id, request_id)),
        release(session_id, request_id),
    )[-1]
    msg = Msg(
        id="provider-error",
        content=build_image_message_content(str(source)),
    )

    with pytest.raises(RuntimeError, match="provider failed"):
        run(_collect_stream(agent.reply_stream(msg)))

    assert len(releases) == 1
    assert releases[0][0] == "error-session"
    assert releases[0][1] != "provider-error"
    assert agent._active_vision_request_id is None
    serialized = json.dumps(SessionStore(tmp_path / "error-session.json").load())
    assert "data:image" not in serialized
    assert "error-large.png" in serialized


def test_vision_request_release_runs_after_cancellation(tmp_path):
    source = _write_react_pattern_png(tmp_path / "cancel-large.png", (1600, 900))

    class BlockingLLM(CapturingVisionLLM):
        def __init__(self, policy):
            super().__init__(policy)
            self.started = asyncio.Event()

        async def chat_stream(self, messages, tools):
            del messages, tools
            self.started.set()
            await asyncio.Event().wait()
            yield

    async def scenario():
        llm = BlockingLLM(policy=VisionPreprocessPolicy())
        agent = ReActAgent("agent", llm, ToolRegistry(), max_iterations=1)
        agent.context.set_session(str(tmp_path / "cancel-session.json"))
        releases = []
        release = agent.vision_preprocessor.release_request
        agent.vision_preprocessor.release_request = lambda session_id, request_id: (
            releases.append((session_id, request_id)),
            release(session_id, request_id),
        )[-1]
        msg = Msg(
            id="cancel-request",
            content=build_image_message_content(str(source)),
        )
        task = asyncio.create_task(_collect_stream(agent.reply_stream(msg)))
        await llm.started.wait()
        task.cancel()
        events = await task
        return agent, events, releases

    agent, events, releases = run(scenario())
    assert len(releases) == 1
    assert releases[0][0] == "cancel-session"
    assert releases[0][1] != "cancel-request"
    assert agent._active_vision_request_id is None
    assert any(event.get("code") == "cancelled" for event in events)
    serialized = json.dumps(SessionStore(tmp_path / "cancel-session.json").load())
    assert "data:image" not in serialized
    assert "cancel-large.png" in serialized


def test_tool_produced_large_image_is_preprocessed_before_next_provider_call(tmp_path):
    source = _write_react_pattern_png(tmp_path / "tool-large.png", (1600, 900))

    class ReadingLLM(CapturingVisionLLM):
        async def chat_stream(self, messages, tools):
            self.calls.append(messages)
            self.tool_calls.append(tools)
            if len(self.calls) == 1:
                yield {
                    "type": "tool_calls",
                    "calls": [{
                        "id": "read-large",
                        "name": "read_image",
                        "arguments": json.dumps({"path": str(source)}),
                    }],
                    "content": "",
                    "usage": None,
                }
                return
            yield {"type": "done", "content": "inspected tool image", "usage": None}

    registry = ToolRegistry()
    register_image_tools(registry, workdir=str(tmp_path))
    llm = ReadingLLM(policy=VisionPreprocessPolicy())
    agent = ReActAgent("agent", llm, registry, max_iterations=3)

    events = run(_collect_stream(agent.reply_stream(Msg(
        content=[ContentBlock.text("read the image")],
    ))))

    image_messages = [
        message
        for message in llm.calls[-1]
        if isinstance(message.get("content"), list)
    ]
    assert sum(
        part.get("type") == "image_url"
        for message in image_messages
        for part in message["content"]
    ) == 7
    assert any(
        event.get("type") == "vision_preprocess" and event.get("protected") is True
        for event in events
    )


@pytest.mark.parametrize(
    ("second_call_id", "expected_max_images"),
    [("read-first", 7), ("read-second", 8)],
)
def test_repeated_identical_read_image_occurrences_are_idempotent_or_newly_charged(
    tmp_path,
    second_call_id,
    expected_max_images,
):
    source = _write_react_pattern_png(tmp_path / "repeated-tool-large.png", (1600, 900))

    class RepeatingReadLLM(CapturingVisionLLM):
        async def chat_stream(self, messages, tools):
            self.calls.append(messages)
            self.tool_calls.append(tools)
            if len(self.calls) <= 2:
                yield {
                    "type": "tool_calls",
                    "calls": [{
                        "id": "read-first" if len(self.calls) == 1 else second_call_id,
                        "name": "read_image",
                        "arguments": json.dumps({"path": str(source)}),
                    }],
                    "content": "",
                    "usage": None,
                }
                return
            yield {"type": "done", "content": "bounded", "usage": None}

    registry = ToolRegistry()
    register_image_tools(registry, workdir=str(tmp_path))
    llm = RepeatingReadLLM(policy=VisionPreprocessPolicy())
    agent = ReActAgent("agent", llm, registry, max_iterations=4)

    events = run(_collect_stream(agent.reply_stream(Msg(
        content=[ContentBlock.text("read the same image twice")],
    ))))

    assert len(llm.calls) == 3
    image_counts = [
        sum(
            part.get("type") == "image_url"
            for message in prompt
            for part in (
                message.get("content")
                if isinstance(message.get("content"), list)
                else []
            )
        )
        for prompt in llm.calls
    ]
    assert all(count <= 12 for count in image_counts)
    assert max(image_counts) == expected_max_images
    assert not any(event.get("code") == "vision_preprocess_failed" for event in events)


def test_provider_prompt_image_count_and_bytes_are_revalidated_before_stream(tmp_path):
    one_pixel = tmp_path / "one.png"
    _write_react_pattern_png(one_pixel, (1, 1))
    data_url = "data:image/png;base64," + base64.b64encode(one_pixel.read_bytes()).decode("ascii")

    count_llm = CapturingVisionLLM(policy=VisionPreprocessPolicy())
    count_agent = ReActAgent("count", count_llm, ToolRegistry(), max_iterations=1)
    count_prompt = [{
        "role": "user",
        "content": [
            {"type": "image_url", "image_url": {"url": f"https://example.test/{index}.png"}}
            for index in range(13)
        ],
    }]
    with pytest.raises(VisionPreprocessError, match="image limit"):
        run(_collect_stream(count_agent._llm_stream(messages=count_prompt, tools=[])))
    assert count_llm.calls == []

    byte_llm = CapturingVisionLLM(
        policy=VisionPreprocessPolicy(max_inline_body_bytes=len(data_url.encode("ascii")) - 1)
    )
    byte_agent = ReActAgent("bytes", byte_llm, ToolRegistry(), max_iterations=1)
    byte_prompt = [{
        "role": "user",
        "content": [{"type": "image_url", "image_url": {"url": data_url}}],
    }]
    with pytest.raises(VisionPreprocessError, match="byte budget"):
        run(_collect_stream(byte_agent._llm_stream(messages=byte_prompt, tools=[])))
    assert byte_llm.calls == []


def test_oversized_tool_image_fails_closed_before_second_provider_call(
    tmp_path,
    monkeypatch,
):
    source = _write_react_pattern_png(tmp_path / "tool-oversized.png", (32, 32))
    monkeypatch.setenv("MAX_AUTO_TOOL_IMAGE_BYTES", "1")

    class ReadingLLM(CapturingVisionLLM):
        async def chat_stream(self, messages, tools):
            self.calls.append(messages)
            self.tool_calls.append(tools)
            if len(self.calls) == 1:
                yield {
                    "type": "tool_calls",
                    "calls": [{
                        "id": "read-oversized",
                        "name": "read_image",
                        "arguments": json.dumps({"path": str(source)}),
                    }],
                    "content": "",
                    "usage": None,
                }
                return
            yield {"type": "done", "content": "must not run", "usage": None}

    registry = ToolRegistry()
    register_image_tools(registry, workdir=str(tmp_path))
    llm = ReadingLLM(policy=VisionPreprocessPolicy())
    agent = ReActAgent("agent", llm, registry, max_iterations=3)

    events = run(_collect_stream(agent.reply_stream(Msg(
        content=[ContentBlock.text("read the image")],
    ))))

    assert len(llm.calls) == 1
    assert any(
        event.get("code") == "vision_preprocess_failed"
        and "not sent" in event.get("message", "")
        for event in events
    )


def test_empty_message_ids_receive_stable_unique_request_ids():
    llm = CapturingVisionLLM(policy=None)
    agent = ReActAgent("agent", llm, ToolRegistry(), max_iterations=1)
    first = Msg(id="", content=[ContentBlock.text("one")])
    second = Msg(id="", content=[ContentBlock.text("two")])

    first_events = run(_collect_stream(agent.reply_stream(first)))
    second_events = run(_collect_stream(agent.reply_stream(second)))

    first_ids = {event["request_id"] for event in first_events}
    second_ids = {event["request_id"] for event in second_events}
    assert len(first_ids) == 1
    assert len(second_ids) == 1
    assert next(iter(first_ids))
    assert first_ids.isdisjoint(second_ids)
    assert first.metadata["request_id"] in first_ids
    assert second.metadata["request_id"] in second_ids


def test_retried_external_request_id_gets_fresh_internal_vision_lifecycle(tmp_path):
    source = _write_react_pattern_png(tmp_path / "retry-large.png", (1600, 900))
    llm = CapturingVisionLLM(policy=VisionPreprocessPolicy())
    agent = ReActAgent("agent", llm, ToolRegistry(), max_iterations=1)
    prepared_request_ids = []
    released_request_ids = []
    prepare = agent.vision_preprocessor.prepare_blocks
    release = agent.vision_preprocessor.release_request

    def record_prepare(
        blocks,
        *,
        policy,
        enabled,
        session_id,
        request_id,
        occurrence_id=None,
    ):
        prepared_request_ids.append(request_id)
        return prepare(
            blocks,
            policy=policy,
            enabled=enabled,
            session_id=session_id,
            request_id=request_id,
            occurrence_id=occurrence_id,
        )

    def record_release(session_id, request_id):
        released_request_ids.append(request_id)
        return release(session_id, request_id)

    agent.vision_preprocessor.prepare_blocks = record_prepare
    agent.vision_preprocessor.release_request = record_release

    first_events = run(_collect_stream(agent.reply_stream(Msg(
        content=build_image_message_content(str(source)),
        metadata={"request_id": "same-external-request"},
    ))))
    second_events = run(_collect_stream(agent.reply_stream(Msg(
        content=build_image_message_content(str(source)),
        metadata={"request_id": "same-external-request"},
    ))))

    assert len(llm.calls) == 2
    assert prepared_request_ids[0] != prepared_request_ids[1]
    assert released_request_ids == prepared_request_ids
    assert all(event["request_id"] == "same-external-request" for event in first_events)
    assert all(event["request_id"] == "same-external-request" for event in second_events)


def test_duplicate_full_tile_selection_is_not_cached_and_task_step_hides_paths(tmp_path):
    source = _write_react_pattern_png(tmp_path / "cache-long.png", (768, 9000))
    holder = {}
    registry = ToolRegistry()
    register_image_tools(
        registry,
        workdir=str(tmp_path),
        select_tiles=lambda tile_set_id, tile_ids: holder["agent"].select_vision_tiles(
            tile_set_id,
            tile_ids,
        ),
    )

    class DuplicateSelectingLLM(CapturingVisionLLM):
        async def chat_stream(self, messages, tools):
            self.calls.append(messages)
            self.tool_calls.append(tools)
            if len(self.calls) == 1:
                text = json.dumps(messages, ensure_ascii=False)
                tile_set_id = re.search(
                    r"tile_set_id[=: ]+([A-Za-z0-9_-]+)",
                    text,
                ).group(1)
                tile_ids = list(dict.fromkeys(re.findall(r"image1-r\d+c\d+", text)))[:11]
                assert len(tile_ids) == 11
                self.arguments = json.dumps({
                    "tile_set_id": tile_set_id,
                    "tile_ids": tile_ids,
                })
            if len(self.calls) <= 2:
                yield {
                    "type": "tool_calls",
                    "calls": [{
                        "id": f"select-{len(self.calls)}",
                        "name": "read_image_tiles",
                        "arguments": self.arguments,
                    }],
                    "content": "",
                    "usage": None,
                }
                return
            yield {"type": "done", "content": "bounded", "usage": None}

    task_store = TaskStore(tmp_path / "tile-tasks.db")
    task = task_store.start_run(
        "tile-cache-external",
        "inspect detail",
        session_id="tile-cache-session",
        model="deepseek-v4-flash-vision-exp",
    )
    llm = DuplicateSelectingLLM(policy=VisionPreprocessPolicy())
    agent = ReActAgent(
        "agent",
        llm,
        registry,
        max_iterations=4,
        task_store=task_store,
    )
    holder["agent"] = agent
    session_path = tmp_path / "tile-cache-session.json"
    agent.context.set_session(str(session_path))

    events = run(_collect_stream(agent.reply_stream(Msg(
        content=build_image_message_content(str(source)),
        metadata={
            "request_id": "tile-cache-external",
            "task_id": task["id"],
        },
    ))))

    assert len(llm.calls) == 3
    second_selection = next(
        event
        for event in events
        if event.get("type") == "tool_result" and event.get("id") == "select-2"
    )
    assert second_selection["code"] == "vision_tile_selection_rejected"
    assert not second_selection.get("cached")
    assert sum(
        part.get("type") == "image_url"
        for message in llm.calls[-1]
        for part in (
            message.get("content")
            if isinstance(message.get("content"), list)
            else []
        )
    ) <= 12

    stored_task = task_store.get_task(task["id"])
    task_text = json.dumps([
        {
            "input": step.get("input"),
            "output": step.get("output"),
            "error": step.get("error"),
        }
        for step in stored_task["steps"]
    ], ensure_ascii=False)
    # A message_source event names a saved canonical message by position and
    # content digest. Only a digest verified against that saved message is
    # left out; tile cache keys and tile byte hashes must still never reach a
    # task step or an event.
    saved_messages = SessionStore(session_path).load()["messages"]
    scanned_events = []
    for event in events:
        if event.get("type") == "message_source":
            reference = event["source_ref"]
            assert reference == message_source_ref(saved_messages[reference["index"]], reference["index"])
            event = {**event, "source_ref": {"index": reference["index"]}}
        scanned_events.append(event)
    event_text = json.dumps(scanned_events, ensure_ascii=False)
    source_path = str(source.resolve())
    for serialized in (task_text, event_text):
        normalized = serialized.replace("\\\\", "/")
        assert "data:image" not in serialized
        assert ".astra/image-cache/tiles" not in normalized
        assert source_path not in serialized
        assert re.search(r"\b[0-9a-f]{64}\b", serialized) is None
    assert b".astra/image-cache/tiles" not in (tmp_path / "tile-tasks.db").read_bytes()


def test_read_image_tiles_same_call_id_never_uses_persistent_task_cache(tmp_path):
    attempts = []
    tile_path = _write_react_pattern_png(tmp_path / "selected-tile.png", (32, 32))
    data_url = "data:image/png;base64," + base64.b64encode(tile_path.read_bytes()).decode("ascii")

    def select_tiles(tile_set_id, tile_ids):
        attempts.append((tile_set_id, tile_ids))
        if len(attempts) == 2:
            raise VisionTileSelectionError("selection budget exhausted")
        return {
            "data_urls": [data_url],
            "labels": ["image1-r1c1 [0,32) x [0,32)"],
            "unserved_ids": [],
            "remaining_images": 0,
            "remaining_inline_bytes": 0,
        }

    registry = ToolRegistry()
    register_image_tools(
        registry,
        workdir=str(tmp_path),
        select_tiles=select_tiles,
    )
    task_store = TaskStore(tmp_path / "persistent-tile-cache.db")
    task = task_store.start_run(
        "same-tool-call",
        "inspect detail",
        session_id="persistent-cache-session",
        model="deepseek-v4-flash-vision-exp",
    )
    agent = ReActAgent(
        "agent",
        CapturingVisionLLM(policy=VisionPreprocessPolicy()),
        registry,
        max_iterations=1,
        task_store=task_store,
    )
    tool_call = {
        "id": "same-call-id",
        "name": "read_image_tiles",
        "arguments": json.dumps({
            "tile_set_id": "opaque-set",
            "tile_ids": ["image1-r1c1"],
        }),
    }

    first = run(agent._execute_tool_calls([tool_call], task_id=task["id"]))[0]
    second = run(agent._execute_tool_calls([tool_call], task_id=task["id"]))[0]

    assert first.get("error") in {None, ""}
    assert second["code"] == "vision_tile_selection_rejected"
    assert not second.get("persistent_cache")
    assert len(attempts) == 2
