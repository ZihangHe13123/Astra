from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from agent.core.msg import ContentBlock, Msg
from agent.runtime.context_index.broker import ContextIndexBroker
from agent.runtime.context_index.diagnostics import trace_metadata
from agent.runtime.context_index.models import ContextIndexRow, RecommendationCandidate, SourceLocator, SourceResult
from agent.runtime.context_index.query import plan_query
from agent.runtime.context_index.ranking import render_selection
from agent.runtime.context_index.workspace import WorkspaceIdentity
from agent.runtime.react import ReActAgent
from agent.runtime.tools.context_index import register_context_index_tools
from agent.runtime.tools.registry import ToolRegistry

NOW = datetime(2026, 9, 9, 4, tzinfo=UTC)
WORKSPACE = WorkspaceIdentity("workspace", "/private/workspace", "astra")


class Source:
    def __init__(self, result=None):
        self.calls = 0
        self.result = result or SourceResult("absent")

    def recommend(self, *_args):
        self.calls += 1
        return self.result

    def open(self, *_args):
        raise AssertionError("Inspection must not open evidence")


def broker_with_rows():
    descriptions = (
        "Database transaction rollback recovery checkpoint. ",
        "Browser checkbox checked state accessibility snapshot. ",
        "Qwen embedding shared worker cache memory budget. ",
        "Railway manuscript figure caption references pagination. ",
    )
    rows = tuple(RecommendationCandidate(
        source="session", identity=str(index), description=text * 12,
        timestamp=NOW.timestamp(), workspace_tier=0, native_query_rank=float(index),
        topic_key=text, trust_label="historical_context",
        locator=SourceLocator("session_message", "private-session", index),
        channel_ranks=(("session-lexical", index),) if index != 2 else (("session-vector", index),),
    ) for index, text in enumerate(descriptions, 1))
    return ContextIndexBroker("all", 900, Source(SourceResult("available", relevance=rows)), Source())


def build(broker, text="project notes", request="request", session="session"):
    return asyncio.run(broker.build(text, request, session, WORKSPACE, NOW, frozenset()))


@pytest.mark.parametrize("text", [
    "新记忆注入，这次注入什么了？",
    "有新注入吗",
    "Which context actually made it into this reply?",
])
def test_inspection_requests_follow_normal_recommendation_pipeline(text):
    broker = broker_with_rows()
    pack = build(broker, text)
    broker.mark_submitted([{"content": pack.rendered}])
    detail = json.loads(broker.inspect())
    assert pack.rows and pack.rendered
    assert broker.session_source.calls == broker.activity_source.calls == 1
    assert detail["current"]["decision"] == "selected"
    assert detail["current"]["injected_count"] == len(pack.rows)
    assert len(detail["current_rows"]) == len(pack.rows)
    assert detail["limits"]["max_items"] == 4
    assert detail["limits"]["open_handles_per_call"] == 3


@pytest.mark.parametrize("text", [
    "之前记忆注入的测试结果怎么样", "昨天我们怎么修复记忆召回的", "继续优化当前记忆注入算法",
    "查看本轮记忆召回实现并修复", "这次记忆召回帮我找之前的数据库事务方案",
    '修复用户问“这条的召回是什么”时的行为',
    "What did we decide about memory injection last time?",
    "Show the current memory recall implementation and optimize it",
    "有新的推荐吗", "有新的 SQL 注入漏洞吗", "有新注入吗，帮我修复它",
])
def test_memory_work_and_history_requests_still_recall(text):
    plan = plan_query(text, NOW)
    assert plan.should_recall and plan.intent != "inspect"


def test_inspection_explains_budget_and_actual_channels_without_searching_again():
    broker = broker_with_rows()
    events = []
    broker.set_feedback_sink(events.append)
    pack = build(broker)
    broker.mark_submitted([{"content": pack.rendered}])
    before = broker.session_source.calls + broker.activity_source.calls
    detail = json.loads(broker.inspect())
    current = detail["current"]
    assert current["candidate_count"] == current["selected_count"] == 4
    assert 0 < current["displayed_count"] == len(pack.rows) < 4
    assert current["injected_count"] == len(pack.rows)
    assert current["render_omissions"]
    assert all("character_budget" in reasons for reasons in current["render_omissions"].values())
    assert current["channels_by_slot"] == {row.slot: list(row.channels) for row in pack.rows}
    assert any(row["channels"] == ["session-vector"] for row in detail["current_rows"])
    assert all(row["handle"] in pack.rendered and row["preview"] in pack.rendered for row in detail["current_rows"])
    assert broker.session_source.calls + broker.activity_source.calls == before
    assert events[-1]["diagnostics"] == current
    assert events[0]["diagnostics"]["submitted"] is False
    assert "Selected: 4; displayed:" in broker.format_last_trace()
    durable = json.dumps(events)
    assert "Database transaction" not in durable and "private-session" not in durable


def test_render_diagnostics_distinguish_token_and_character_limits():
    row = ContextIndexRow("ctx:s:abcd", "session", "R1", "Memory evidence " * 20, "related")
    omissions = {}
    rendered, visible = render_selection([row], NOW, 2000, token_budget=1, omissions=omissions)
    assert not rendered and not visible and omissions == {"R1": ("token_budget",)}
    omissions = {}
    _, visible = render_selection([replace(row, description="")], NOW, 2000, omissions=omissions)
    assert not visible and omissions == {"R1": ("empty_preview",)}


def test_previous_turn_is_metadata_only_and_handles_still_expire():
    broker = broker_with_rows()
    old_pack = build(broker)
    broker.mark_submitted([{"content": old_pack.rendered}])
    broker.complete_request("request")
    assert json.loads(broker.inspect())["current"] is None
    broker.session_source.result = SourceResult("absent")
    build(broker, "有新注入吗", request="inspect")
    detail = json.loads(broker.inspect())
    assert detail["current"]["decision"] == "no-match"
    assert detail["current"]["injected_count"] == 0
    assert detail["previous_completed_turn"]["injected_count"] == len(old_pack.rows)
    assert detail["current_rows"] == []
    assert "ctx:s:" not in json.dumps(detail)
    assert "Database transaction" not in json.dumps(detail)
    assert broker.open([old_pack.rows[0].handle], 2) == "invalid_or_expired_handle"
    # Starting another session must not disclose the previous session's metadata.
    build(broker, "有新注入吗", request="other", session="different")
    assert json.loads(broker.inspect())["previous_completed_turn"] is None
    broker.end_session()
    assert json.loads(broker.inspect())["previous_completed_turn"] is None


@pytest.mark.parametrize("mode", ["off", "shadow"])
def test_disabled_or_shadow_inspection_never_exposes_handles_or_previews(mode):
    broker = broker_with_rows()
    broker.set_mode(mode)
    build(broker)
    detail = json.loads(broker.inspect())
    assert detail["mode"] == mode
    assert detail["current_rows"] == []
    assert "ctx:s:" not in json.dumps(detail) and "Database transaction" not in json.dumps(detail)


def test_durable_metadata_allowlists_untrusted_diagnostics():
    broker = broker_with_rows()
    build(broker)
    trace = broker.last_trace
    trace.source_status["private-path"] = "private error"
    trace.semantic_status["session"] = "private exception"
    trace.source_errors["session"] = "private exception"
    trace.source_diagnostics["activity"] = ("deadline", "private exception")
    trace.displayed[0] = replace(trace.displayed[0], channels=("private channel", "session-vector"))
    trace.render_omissions = {"R4": ("private reason", "token_budget"), "private slot": ("private",)}
    metadata = trace_metadata(trace)
    assert "private" not in json.dumps(metadata)
    assert metadata["semantic_status"]["session"] == "error"
    assert metadata["channels_by_slot"]["R1"] == ["session-vector"]
    assert metadata["source_diagnostics"]["activity"] == ["deadline"]


@pytest.mark.parametrize(("text", "decision"), [("谢谢", "not-needed"), ("这条的召回是什么", "no-match"), ("no archive", "no-match")])
def test_empty_decisions_are_recorded_once_without_a_submission(text, decision):
    broker = ContextIndexBroker("all", 900, Source(), Source())
    events = []
    broker.set_feedback_sink(events.append)
    pack = build(broker, text)
    assert build(broker, text) is pack
    broker.mark_submitted([{"content": "user message"}])
    assert len(events) == 1
    assert events[0]["diagnostics"]["decision"] == decision
    assert events[0]["diagnostics"]["submitted"] is False
    assert events[0]["rows"] == []


@pytest.mark.parametrize("has_rows", [False, True])
def test_inspection_tool_does_not_rebuild_or_change_recommendations(monkeypatch, tmp_path, has_rows):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Inspection must not initialize a reader or query embedding")

    import agent.runtime.context_index.broker as module
    from agent.runtime.context_index.semantic_reader import SemanticReader

    broker = broker_with_rows()
    if not has_rows:
        broker.session_source.result = SourceResult("absent")
    pack = build(broker)
    broker.mark_submitted([{"content": pack.rendered}])
    before = json.loads(broker.inspect())
    registry = ToolRegistry()
    register_context_index_tools(registry, broker)
    # Guard the tool call after normal recommendation has already completed.
    monkeypatch.setattr(broker, "build", forbidden)
    monkeypatch.setattr(broker, "_read_sources", forbidden)
    monkeypatch.setattr(module, "RecordSource", forbidden)
    monkeypatch.setattr(module, "QueryEmbedding", forbidden)
    monkeypatch.setattr(SemanticReader, "ready", forbidden)
    broker.semantic_reader = SemanticReader(tmp_path / "missing-sessions.db")
    for _ in range(2):
        result = asyncio.run(registry.execute("context_inspect", {}))
        assert json.loads(result["fresh_output"]) == before
        assert json.loads(broker.inspect()) == before
    assert broker.session_source.calls == broker.activity_source.calls == 1
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("handle", ["ctx:a:1234567890123", "R1", "ctx:s:ABC1", "ctx:s:abcd\n", "session_20260909_112351_37225"])
def test_bad_handles_fail_before_opening_evidence(handle):
    broker = broker_with_rows()
    registry = ToolRegistry()
    register_context_index_tools(registry, broker)
    result = asyncio.run(registry.execute("context_open", {"handles": [handle]}))
    # Registry enforces length; the broker also checks exact syntax for direct
    # integrations and runtimes whose JSON-schema subset omits pattern checks.
    if len(handle) != 10:
        assert result["code"] == "invalid_arguments"
    else:
        # A refusal is a real failure, kept in history, not a one-step result shown as a success.
        assert result["code"] == "invalid_or_expired_handle" and result["error"]
        assert "fresh_output" not in result
        assert "context_inspect" in result["recovery_hint"] and "does not establish expiry" in result["recovery_hint"]


def test_inspection_tool_request_local_and_no_argument_data_leak():
    broker = broker_with_rows()
    pack = build(broker)
    registry = ToolRegistry()
    register_context_index_tools(registry, broker)
    tool = registry.get("context_inspect")
    result = asyncio.run(registry.execute("context_inspect", {}))
    assert tool.max_calls_per_turn == 2 and not tool.cache_results
    assert tool.risk == "read" and tool.approval == "never"
    assert pack.rows[0].handle in result["fresh_output"]
    assert pack.rows[0].handle not in repr(registry.persistence_safe_result(tool, result))
    invalid = asyncio.run(registry.execute("context_inspect", {"session_id": "other"}))
    assert invalid["code"] == "invalid_arguments"


@pytest.mark.parametrize("text", ["有新注入吗", "把这次实际收到的辅助上下文给我列一下"])
@pytest.mark.parametrize("has_rows", [False, True])
def test_runtime_model_calls_inspection_tool_and_receives_actual_injection(tmp_path, text, has_rows):
    class InspectorLLM:
        def __init__(self):
            self.calls = []

        async def chat_stream(self, messages, tools, **_kwargs):
            self.calls.append(messages)
            assert any(t["function"]["name"] == "context_inspect" for t in tools)
            if len(self.calls) == 1:
                yield {"type": "tool_calls", "calls": [{"id": "inspect", "name": "context_inspect", "arguments": "{}"}],
                       "content": "", "reasoning_content": "", "usage": None}
            else:
                content = next(message["content"] for message in messages if message.get("role") == "tool")
                output, _end = json.JSONDecoder().raw_decode(content[content.index("{"):])
                assert output["current"]["decision"] == ("selected" if has_rows else "no-match")
                count = output["current"]["injected_count"]
                assert bool(count) is has_rows
                assert count == len(output["current_rows"])
                assert output == json.loads(broker.inspect())
                assert output["previous_completed_turn"] is None
                assert output["limits"]["max_items"] == 4
                assert output["limits"]["open_handles_per_call"] == 3
                yield {"type": "done", "content": f"本轮实际注入{count}条。", "usage": None}

    broker = broker_with_rows()
    if not has_rows:
        broker.session_source.result = SourceResult("absent")
    registry = ToolRegistry()
    register_context_index_tools(registry, broker)
    llm = InspectorLLM()
    agent = ReActAgent("agent", llm, registry, max_iterations=3, context_index_broker=broker)
    agent.context.set_session(str(tmp_path / "inspection.json"))
    reply = asyncio.run(agent.reply(Msg(content=[ContentBlock.text(text)])))
    assert reply is not None and "本轮实际注入" in reply.get_text()
    assert len(llm.calls) == 2 and broker.session_source.calls == broker.activity_source.calls == 1
