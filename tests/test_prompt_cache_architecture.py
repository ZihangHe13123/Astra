import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from agent.core.msg import ContentBlock, Msg
from agent.runtime.context import AgentContext
from agent.runtime.context_compressor import COMPRESSION_RECAP_PREFIX
from agent.runtime.llm import _usage_dict
from agent.runtime.react import ReActAgent
from agent.runtime.tools.registry import ToolDef, ToolRegistry


def run(coro):
    return asyncio.run(coro)


def test_legacy_api_content_sidecar_is_retired_on_restore(tmp_path: Path):
    path = tmp_path / "cache-session.json"
    path.write_text(json.dumps({
        "system_prompt": "stable-system",
        "messages": [{
            "role": "user",
            "content": "clean user text",
            "api_content": "clean user text\n\n[legacy dynamic context]",
        }],
    }), encoding="utf-8")

    restored = AgentContext(system_prompt="different-default")
    restored.set_session(str(path))
    assert restored.load() is True
    assert restored.messages[0]["content"] == "clean user text"
    assert "api_content" not in restored.messages[0]
    assert restored.get_prompt()[1]["content"] == "clean user text"
    assert "api_content" not in restored.get_prompt()[1]


def test_stable_system_suffix_survives_legacy_session_restore(tmp_path: Path):
    path = tmp_path / "legacy-session.json"
    original = AgentContext(system_prompt="persisted persona")
    original.set_session(str(path))
    original.add_user("hello")
    original.save()

    restored = AgentContext(system_prompt="new default")
    restored.set_stable_system_suffix("installed skill catalog")
    restored.set_session(str(path))
    assert restored.load() is True
    assert restored.system_prompt == "persisted persona"
    assert restored.get_prompt()[0]["content"] == (
        "persisted persona\n\ninstalled skill catalog"
    )


def test_react_freezes_turn_context_and_system_prefix_across_tool_iterations():
    class FakePack:
        trace = SimpleNamespace(record_ids=())

        def __init__(self, marker: str):
            self.marker = marker

        def render(self, recall_char_limit: int):
            return f"<agent-memory>{self.marker}</agent-memory>"

    class ChangingRouter:
        recall_char_limit = 1000

        def __init__(self):
            self.calls = 0

        async def build_context(self, *args, **kwargs):
            self.calls += 1
            return FakePack(f"snapshot-{self.calls}")

    class ToolThenDoneLLM:
        def __init__(self):
            self.calls = []

        async def chat_stream(self, messages, tools):
            self.calls.append((messages, tools))
            if len(self.calls) == 1:
                yield {
                    "type": "tool_calls",
                    "calls": [{"id": "call-1", "name": "lookup", "arguments": "{}"}],
                    "content": "",
                    "reasoning_content": "",
                    "usage": None,
                }
            else:
                yield {"type": "done", "content": "done", "usage": None}

    registry = ToolRegistry()
    registry.register(ToolDef(
        "lookup",
        "lookup",
        {"type": "object", "properties": {}},
        lambda: "result",
        group="core",
    ))
    registry.register(ToolDef(
        "web_lookup",
        "web lookup",
        {"type": "object", "properties": {}},
        lambda: "web result",
        group="web",
    ))
    llm = ToolThenDoneLLM()
    router = ChangingRouter()
    agent = ReActAgent(
        "agent",
        llm,  # type: ignore[arg-type]
        registry,
        system_prompt="stable-system",
        max_iterations=2,
        memory_store=object(),  # type: ignore[arg-type]
        memory_router=router,  # type: ignore[arg-type]
        memory_retainer=SimpleNamespace(hooks=None),  # type: ignore[arg-type]
        progressive_tools=True,
    )

    run(agent.reply(Msg(content=[ContentBlock.text("check this")], id="turn-1")))

    assert router.calls == 1
    assert len(llm.calls) == 2
    first_messages, first_tools = llm.calls[0]
    second_messages, second_tools = llm.calls[1]
    assert first_messages[0]["role"] == "system"
    assert first_messages[0]["content"].startswith("stable-system")
    # Runtime snapshots are separate, replayable messages; user text stays clean.
    assert "snapshot-1" not in first_messages[0]["content"]
    assert "snapshot-1" in first_messages[2]["content"]
    assert "check this" in first_messages[1]["content"]
    assert second_messages[:len(first_messages)] == first_messages
    assert first_tools == second_tools
    assert {item["function"]["name"] for item in first_tools} == {
        "lookup",
        "web_lookup",
    }
    assert agent.context.messages[0]["content"] == "check this"
    assert "api_content" not in agent.context.messages[0]

    # A genuine second user turn catches the old disappearing-sidecar bug.
    run(agent.reply(Msg(content=[ContentBlock.text("next question")], id="turn-2")))
    third_messages, third_tools = llm.calls[2]
    assert router.calls == 2
    assert third_messages[:len(second_messages)] == second_messages
    assert "snapshot-2" in third_messages[-1]["content"]
    assert third_tools == first_tools


def test_context_index_coexists_with_memory_router_in_one_frozen_snapshot():
    class Pack:
        trace = SimpleNamespace(record_ids=())

        def render(self, recall_char_limit: int):
            return "<agent-memory>remembered</agent-memory>"

    class Router:
        recall_char_limit = 1000

        async def build_context(self, *args, **kwargs):
            return Pack()

    class Broker:
        mode = "session"

        async def build(self, user_text, **kwargs):
            return SimpleNamespace(rendered="<context-index>suggested</context-index>")

        def complete_request(self, request_id):
            pass

        def end_session(self):
            pass

    class LLM:
        def __init__(self):
            self.messages = []

        async def chat_stream(self, messages, tools):
            self.messages.append(messages)
            yield {"type": "done", "content": "done", "usage": None}

    llm = LLM()
    agent = ReActAgent(
        "agent",
        llm,  # type: ignore[arg-type]
        ToolRegistry(),
        memory_store=object(),  # type: ignore[arg-type]
        memory_router=Router(),  # type: ignore[arg-type]
        memory_retainer=SimpleNamespace(hooks=None),  # type: ignore[arg-type]
        context_index_broker=Broker(),  # type: ignore[arg-type]
    )

    run(agent.reply(Msg(content=[ContentBlock.text("continue")], id="both-1")))

    assert "<agent-memory>remembered</agent-memory>" in llm.messages[0][-1]["content"]
    assert "<context-index>suggested</context-index>" in llm.messages[0][-1]["content"]


def test_deepseek_cache_usage_is_normalized():
    usage = SimpleNamespace(
        prompt_tokens=1000,
        completion_tokens=25,
        total_tokens=1025,
        prompt_cache_hit_tokens=900,
        prompt_cache_miss_tokens=100,
    )

    assert _usage_dict(usage) == {
        "prompt_tokens": 1000,
        "completion_tokens": 25,
        "total_tokens": 1025,
        "prompt_cache_hit_tokens": 900,
        "prompt_cache_miss_tokens": 100,
    }


def test_openai_cached_tokens_fall_back_to_computed_miss():
    usage = SimpleNamespace(
        prompt_tokens=800,
        completion_tokens=10,
        total_tokens=810,
        prompt_tokens_details=SimpleNamespace(cached_tokens=640),
    )

    normalized = _usage_dict(usage)
    assert normalized["prompt_cache_hit_tokens"] == 640
    assert normalized["prompt_cache_miss_tokens"] == 160


def test_provider_without_cache_telemetry_does_not_invent_misses():
    usage = SimpleNamespace(
        prompt_tokens=800,
        completion_tokens=10,
        total_tokens=810,
    )

    normalized = _usage_dict(usage)
    assert "prompt_cache_hit_tokens" not in normalized
    assert "prompt_cache_miss_tokens" not in normalized


def test_null_cache_fields_are_unavailable_but_reported_zero_is_known():
    usage = SimpleNamespace(prompt_tokens=8, completion_tokens=2, total_tokens=10,
                            prompt_cache_hit_tokens=None, prompt_cache_miss_tokens=None,
                            prompt_tokens_details=SimpleNamespace(cached_tokens=None))
    assert "prompt_cache_hit_tokens" not in _usage_dict(usage)
    usage.prompt_tokens_details.cached_tokens = 0
    assert _usage_dict(usage)["prompt_cache_hit_tokens"] == 0
    assert _usage_dict(usage)["prompt_cache_miss_tokens"] == 8
    usage.prompt_cache_hit_tokens = 0
    usage.prompt_tokens_details.cached_tokens = 4
    assert _usage_dict(usage)["prompt_cache_hit_tokens"] == 0


def test_partial_cache_metrics_do_not_fill_unknown_counterpart_with_zero():
    assert _usage_dict(SimpleNamespace(prompt_cache_hit_tokens=2)).get("prompt_cache_miss_tokens") is None
    assert _usage_dict(SimpleNamespace(prompt_cache_miss_tokens=2)).get("prompt_cache_hit_tokens") is None


def test_tool_routing_ignores_synthetic_compression_recap():
    agent = ReActAgent(
        "agent",
        SimpleNamespace(),
        ToolRegistry(),
        system_prompt="system",
    )
    agent.context.messages.extend([
        {"role": "user", "content": "inspect the repository"},
        {
            "role": "user",
            "content": f"{COMPRESSION_RECAP_PREFIX}\nSECRET RECAP ROUTE",
            "provenance": "runtime:compression_recap",
            "metadata": {"synthetic": True},
        },
        {"role": "user", "content": "continue"},
    ])

    routing = agent._tool_routing_text("continue")

    assert "inspect the repository" in routing
    assert "SECRET RECAP ROUTE" not in routing
