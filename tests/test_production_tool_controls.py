import asyncio
import json
import os
import stat
from pathlib import Path

import pytest

from agent.core.msg import ContentBlock, Msg
from agent.runtime.context_compressor import SUMMARY_PREFIX
from agent.runtime.react import ReActAgent
from agent.runtime.tools.computer import register_computer_tools
from agent.runtime.tools.registry import ToolDef, ToolRegistry, _bounded_head_tail


def run(coro):
    return asyncio.run(coro)


def test_image_path_exposes_image_tools_without_manual_activation(tmp_path: Path):
    registry = ToolRegistry(artifact_dir=tmp_path / "tool-results")
    registry.register(ToolDef(
        "inspect_image_metadata", "inspect", {"type": "object"}, lambda: "ok", group="image",
    ))

    assert "image" in registry.select_groups(r"检查 \\wsl.localhost\Ubuntu\output\sample.png 的生成参数")


def test_registers_computer_apps_for_bounded_catalog_refresh():
    registry = ToolRegistry()

    register_computer_tools(registry, object())

    definition = registry.get("computer_apps")
    assert definition.repeat_guard is False
    assert definition.max_calls_per_turn == 8


def test_mixed_ascii_chinese_terms_route_tool_groups_without_manual_activation():
    registry = ToolRegistry()
    prompts = {
        "web": "search一下最新消息",
        "memory": "memory里有什么",
        "skills": "skill怎么用",
        "image": "comfyui开的吧",
    }
    for group in prompts:
        registry.register(ToolDef(
            f"{group}_tool", group, {"type": "object"}, lambda: "ok", group=group,
        ))

    for group, prompt in prompts.items():
        assert group in registry.select_groups(prompt)

    for prompt in ("hindsight recall", "reflect一下历史", "回忆之前的决定"):
        assert "memory" in registry.select_groups(prompt)

    assert "image" not in registry.select_groups("drawbridge")


def test_short_followup_routes_tools_from_compacted_active_task():
    class FakeLLM:
        pass

    registry = ToolRegistry()
    registry.register(ToolDef(
        "inspect_image_metadata", "inspect", {"type": "object"}, lambda: "ok", group="image",
    ))
    agent = ReActAgent("agent", FakeLLM(), registry, progressive_tools=True)
    agent.context.add_assistant(
        f"{SUMMARY_PREFIX}\n## Active Task\nUse inspect_image_metadata on /home/user/output/sample.png."
    )
    agent.context.add_user("调用工具进行尝试")

    routing_text = agent._tool_routing_text("调用工具进行尝试")
    schemas = agent._available_tool_schemas(routing_text, {"core"}, {}, set())

    assert "inspect_image_metadata" in routing_text
    assert any(item["function"]["name"] == "inspect_image_metadata" for item in schemas)


def test_large_tool_output_is_spilled_to_artifact(tmp_path: Path):
    registry = ToolRegistry(artifact_dir=tmp_path / "tool-results", max_inline_chars=100)
    full_output = "HEAD-" + ("x" * 400) + "-TAIL"
    registry.register(ToolDef("large_read", "read", {"type": "object"}, lambda: full_output))

    result = run(registry.execute("large_read", {}))

    assert result["output_truncated"] is True
    assert "Tool output truncated" in result["output"]
    assert "HEAD-" in result["output"]
    assert "-TAIL" in result["output"]
    artifact = Path(result["artifact_path"])
    assert artifact.parent == (tmp_path / "tool-results").resolve()
    assert artifact.read_text(encoding="utf-8") == full_output
    assert result["artifact_chars"] == len(full_output)


def test_large_tool_output_stays_successful_when_artifact_write_fails(
    tmp_path: Path, monkeypatch,
):
    registry = ToolRegistry(artifact_dir=tmp_path / "tool-results", max_inline_chars=100)
    full_output = "valuable-" + ("x" * 400)
    registry.register(ToolDef("large_read", "read", {"type": "object"}, lambda: full_output))

    def fail_write(_safe_name: str, _output: str) -> Path:
        raise PermissionError("simulated retention failure")

    monkeypatch.setattr(registry, "_write_private_artifact", fail_write)
    result = run(registry.execute("large_read", {}))

    assert result["error"] == ""
    assert result["output"] == full_output
    assert result["output_truncated"] is False
    assert result["artifact_persist_failed"] is True
    assert result["artifact_error_type"] == "PermissionError"
    assert "artifact_path" not in result


def test_tool_result_artifact_uses_exclusive_private_creation(tmp_path: Path):
    registry = ToolRegistry(artifact_dir=tmp_path / "tool-results", max_inline_chars=100)
    artifact = registry._write_private_artifact("read", "secret")

    assert artifact.read_text(encoding="utf-8") == "secret"
    if os.name != "nt":
        assert stat.S_IMODE(artifact.stat().st_mode) == 0o600
        assert stat.S_IMODE(artifact.parent.stat().st_mode) == 0o700


def test_tool_result_head_tail_slice_is_unicode_safe_and_strictly_bounded():
    source = "头😀" * 100 + "TAIL🧪"
    preview = _bounded_head_tail(source, 80)

    assert len(preview) == 80
    assert preview.startswith("头😀")
    assert preview.endswith("TAIL🧪")
    assert "middle omitted" in preview
    assert "\ufffd" not in preview


def test_large_base64_output_is_not_inlined_as_context_noise(tmp_path: Path):
    registry = ToolRegistry(artifact_dir=tmp_path / "tool-results", max_inline_chars=100)
    full_output = "data:image/png;base64," + ("A" * 10_000)
    registry.register(ToolDef("render", "render", {"type": "object"}, lambda: full_output))

    result = run(registry.execute("render", {}))

    assert result["output_truncated"] is True
    assert "Opaque base64 data URL omitted" in result["output"]
    assert "A" * 100 not in result["output"]
    assert Path(result["artifact_path"]).read_text(encoding="utf-8") == full_output


def test_next_llm_prompt_marks_tool_result_as_completed_and_actionable(tmp_path: Path):
    class ToolThenCaptureLLM:
        def __init__(self):
            self.calls = 0
            self.second_prompt = None

        async def chat_stream(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                yield {
                    "type": "tool_calls",
                    "calls": [{"id": "lookup-1", "name": "lookup", "arguments": "{}"}],
                    "content": "I will check.",
                    "reasoning_content": "",
                    "usage": None,
                }
            else:
                self.second_prompt = messages
                yield {"type": "done", "content": "done", "usage": None}

    full_result = "HEAD-" + ("x" * 8_000) + "-DECISIVE-MIDDLE-" + ("y" * 8_000) + "-TAIL"
    registry = ToolRegistry(artifact_dir=tmp_path / "tool-results", max_inline_chars=100)
    registry.register(ToolDef("lookup", "lookup", {"type": "object"}, lambda: full_result))
    llm = ToolThenCaptureLLM()
    agent = ReActAgent("agent", llm, registry, max_iterations=2)  # type: ignore[arg-type]

    run(agent.reply(Msg(content=[ContentBlock.text("check it")])))

    tool_message = next(item for item in llm.second_prompt if item.get("role") == "tool")
    assert "[Tool result: lookup | status: success]" in tool_message["content"]
    assert "Result completeness: complete." in tool_message["content"]
    assert "DECISIVE-MIDDLE" in tool_message["content"]
    assert "Continue the current user request from this result" in tool_message["content"]
    durable_tool_message = next(item for item in agent.context.messages if item.get("role") == "tool")
    assert "DECISIVE-MIDDLE" not in durable_tool_message["content"]
    assert "bounded preview" in durable_tool_message["content"]


def test_cache_stable_exposure_keeps_one_manifest_across_domains():
    class CaptureLLM:
        def __init__(self):
            self.tool_names = []

        async def chat_stream(self, messages, tools):
            self.tool_names = [item["function"]["name"] for item in tools]
            yield {"type": "done", "content": "ok", "usage": None}

    registry = ToolRegistry()
    registry.register(ToolDef("current_time", "time", {"type": "object"}, lambda: "now"))
    registry.register(ToolDef("search_web", "search", {"type": "object"}, lambda: "result", group="web"))
    registry.register(ToolDef("read_file", "read", {"type": "object"}, lambda: "file", group="files"))

    generic_llm = CaptureLLM()
    generic_agent = ReActAgent("agent", generic_llm, registry, max_iterations=1, progressive_tools=True)  # type: ignore[arg-type]
    run(generic_agent.reply(Msg(content=[ContentBlock.text("你好")])))
    assert generic_llm.tool_names == ["current_time", "read_file", "search_web"]

    web_llm = CaptureLLM()
    web_agent = ReActAgent("agent", web_llm, registry, max_iterations=1, progressive_tools=True)  # type: ignore[arg-type]
    run(web_agent.reply(Msg(content=[ContentBlock.text("搜索今日 AI 新闻")])))
    assert "current_time" in web_llm.tool_names
    assert "search_web" in web_llm.tool_names
    assert "read_file" in web_llm.tool_names
    assert web_llm.tool_names == generic_llm.tool_names


def test_non_core_tool_is_available_without_schema_mutation():
    class ActivationLLM:
        def __init__(self):
            self.calls = 0
            self.second_tools = []

        async def chat_stream(self, messages, tools):
            self.calls += 1
            self.second_tools = [item["function"]["name"] for item in tools]
            yield {"type": "done", "content": "ready", "usage": None}

    registry = ToolRegistry()
    registry.register(ToolDef("core_tool", "core", {"type": "object"}, lambda: "ok"))
    registry.register(ToolDef("skill_view", "skill", {"type": "object"}, lambda: "skill", group="skills"))
    llm = ActivationLLM()
    agent = ReActAgent("agent", llm, registry, max_iterations=3, progressive_tools=True)  # type: ignore[arg-type]

    run(_collect(agent.reply_stream(Msg(content=[ContentBlock.text("帮我处理一下")]))))

    assert "skill_view" in llm.second_tools
    assert "activate_tool_group" not in llm.second_tools


def test_consecutive_failures_open_circuit_and_synthesize_without_tools():
    class FailingThenSynthesisLLM:
        def __init__(self):
            self.calls = 0
            self.final_tools = None

        async def chat_stream(self, messages, tools):
            self.calls += 1
            if tools:
                yield {
                    "type": "tool_calls",
                    "calls": [{"id": f"fail-{self.calls}", "name": "unstable", "arguments": "{}"}],
                    "content": "",
                    "reasoning_content": "",
                    "usage": None,
                }
                return
            self.final_tools = tools
            yield {"type": "chunk", "content": "已保留现有进展，网络工具连续失败，请稍后重试。"}
            yield {"type": "done", "content": "已保留现有进展，网络工具连续失败，请稍后重试。", "usage": None}

    executed = 0

    async def unstable():
        nonlocal executed
        executed += 1
        raise ConnectionError("upstream unavailable")

    registry = ToolRegistry()
    registry.register(ToolDef("unstable", "unstable", {"type": "object"}, unstable))
    llm = FailingThenSynthesisLLM()
    agent = ReActAgent("agent", llm, registry, max_iterations=8, failure_threshold=2)  # type: ignore[arg-type]

    events = run(_collect(agent.reply_stream(Msg(content=[ContentBlock.text("run it")]))))

    assert executed == 2
    assert llm.final_tools == []
    assert any(event.get("recoverable") and "circuit opened" in event.get("message", "") for event in events)
    assert any(event.get("type") == "chunk" and "连续失败" in event.get("content", "") for event in events)
    assert events[-1]["type"] == "done"


def test_different_failed_invocations_do_not_open_tool_circuit():
    class DiagnosticLLM:
        def __init__(self):
            self.calls = 0

        async def chat_stream(self, messages, tools):
            del messages
            self.calls += 1
            if self.calls <= 3:
                yield {
                    "type": "tool_calls",
                    "calls": [{
                        "id": f"probe-{self.calls}",
                        "name": "probe",
                        "arguments": json.dumps({"target": f"candidate-{self.calls}"}),
                    }],
                    "content": "",
                    "reasoning_content": "",
                    "usage": None,
                }
                return
            assert tools
            yield {
                "type": "done",
                "content": "诊断完成",
                "reasoning_content": "",
                "usage": None,
            }

    attempted = []

    async def probe(target: str):
        attempted.append(target)
        raise RuntimeError("probe failed")

    registry = ToolRegistry()
    registry.register(ToolDef(
        "probe",
        "probe a candidate",
        {
            "type": "object",
            "properties": {"target": {"type": "string"}},
            "required": ["target"],
        },
        probe,
    ))
    agent = ReActAgent(
        "agent",
        DiagnosticLLM(),
        registry,
        max_iterations=5,
        failure_threshold=3,
    )  # type: ignore[arg-type]

    events = run(_collect(agent.reply_stream(Msg(content=[ContentBlock.text("diagnose")]))))

    assert attempted == ["candidate-1", "candidate-2", "candidate-3"]
    assert not any("circuit opened" in event.get("message", "") for event in events)
    assert events[-1]["type"] == "done"


def test_a_tool_failing_on_and_on_with_new_arguments_is_flagged_then_stopped():
    class StubbornLLM:
        def __init__(self):
            self.calls = 0
            self.seen: list[str] = []

        async def chat_stream(self, messages, tools):
            self.calls += 1
            self.seen.append(json.dumps(messages, ensure_ascii=False))
            if tools:
                yield {"type": "tool_calls", "calls": [{"id": f"try-{self.calls}", "name": "probe",
                       "arguments": json.dumps({"target": f"candidate-{self.calls}"})}],
                       "content": "", "reasoning_content": "", "usage": None}
                return
            yield {"type": "chunk", "content": "试了几种都不行，卡在 probe 上。"}
            yield {"type": "done", "content": "试了几种都不行，卡在 probe 上。", "usage": None}

    attempted = []

    async def probe(target: str):
        attempted.append(target)
        raise RuntimeError("probe failed")

    registry = ToolRegistry()
    registry.register(ToolDef("probe", "probe a candidate", {"type": "object", "properties": {
        "target": {"type": "string"}}, "required": ["target"]}, probe))
    llm = StubbornLLM()
    agent = ReActAgent("agent", llm, registry, max_iterations=12, failure_threshold=3)  # type: ignore[arg-type]

    events = run(_collect(agent.reply_stream(Msg(content=[ContentBlock.text("diagnose")]))))

    # The third failure in a row is flagged to the model; the fifth ends the attempts.
    assert attempted == [f"candidate-{n}" for n in range(1, 6)]
    assert "Failure streak" not in llm.seen[2] and "failed 3 times in a row" in llm.seen[3]
    assert any("circuit opened" in event.get("message", "") for event in events)
    assert events[-1]["type"] == "done"


async def _collect(stream):
    return [event async for event in stream]
def test_tool_definition_rejects_unknown_replay_policy():
    registry = ToolRegistry()
    with pytest.raises(ValueError, match="replay policy"):
        registry.register(ToolDef(
            name="bad_replay",
            description="invalid replay policy",
            parameters={"type": "object", "properties": {}},
            fn=lambda: "ok",
            replay="sometimes",
        ))
