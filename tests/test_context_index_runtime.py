import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.runtime import session_recall
from agent.cli.context_index_preferences import ContextIndexPreferences
from agent.core.msg import ContentBlock, Msg
from agent.runtime import activity_store
from agent.runtime.context import AgentContext
from agent.runtime.context_index import create_context_index_broker
from agent.runtime.context_index.broker import ContextIndexBroker
from agent.runtime.context_index.models import ContextIndexPack, SourceResult
from agent.runtime.context_index.session_source import SessionRecommendationSource
from agent.runtime.react import ReActAgent
from agent.runtime.tools.registry import ToolDef, ToolRegistry
from agent.runtime.session_recall import SessionRecall


def run(coro):
    return asyncio.run(coro)


class RecordingBroker:
    mode = "all"

    def __init__(self, rendered: str = "<context-index>one</context-index>"):
        self.rendered = rendered
        self.build_calls: list[dict] = []
        self.completed_requests: list[str] = []
        self.session_ends = 0

    async def build(self, user_text, **kwargs):
        self.build_calls.append({"user_text": user_text, **kwargs})
        return ContextIndexPack(
            request_id=kwargs["request_id"], rendered=self.rendered
        )

    def complete_request(self, request_id):
        self.completed_requests.append(str(request_id))

    def end_session(self):
        self.session_ends += 1


class ExplodingBroker(RecordingBroker):
    async def build(self, user_text, **kwargs):
        self.build_calls.append({"user_text": user_text, **kwargs})
        raise RuntimeError("source failed")


class ToolThenDoneLLM:
    def __init__(self):
        self.calls = []

    async def chat_stream(self, messages, tools, **kwargs):
        self.calls.append(SimpleNamespace(messages=messages, tools=tools))
        if len(self.calls) == 1:
            yield {
                "type": "tool_calls",
                "calls": [
                    {"id": "call-1", "name": "lookup", "arguments": "{}"}
                ],
                "content": "",
                "reasoning_content": "",
                "usage": None,
            }
        else:
            yield {"type": "done", "content": "ok", "usage": None}


class DoneLLM:
    def __init__(self):
        self.calls = []

    async def chat_stream(self, messages, tools, **kwargs):
        self.calls.append(SimpleNamespace(messages=messages, tools=tools))
        yield {"type": "done", "content": "ok", "usage": None}


class ExplodingLLM:
    async def chat_stream(self, messages, tools, **kwargs):
        raise RuntimeError("provider failed")
        yield  # pragma: no cover - makes this an async generator for the protocol


def _agent(llm, broker, *, max_iterations=2):
    registry = ToolRegistry()
    registry.register(
        ToolDef(
            "lookup",
            "lookup",
            {"type": "object", "properties": {}},
            lambda: "result",
            group="core",
        )
    )
    return ReActAgent(
        "agent",
        llm,
        registry,
        system_prompt="stable-system",
        max_iterations=max_iterations,
        context_index_broker=broker,
    )


def test_react_injects_one_frozen_index_outside_system_prefix():
    broker = RecordingBroker()
    llm = ToolThenDoneLLM()
    agent = _agent(llm, broker)
    agent.context.set_session("/tmp/current-session.json")

    reply = run(
        agent.reply(
            Msg(content=[ContentBlock.text("check history")], id="req-1")
        )
    )

    assert reply is not None and reply.get_text() == "ok"
    assert len(broker.build_calls) == 1
    assert broker.build_calls[0]["request_id"].startswith("context-index:")
    assert broker.build_calls[0]["session_id"] == "current-session"
    assert broker.build_calls[0]["workspace"].root == str(Path.cwd().resolve())
    first, second = llm.calls
    assert "<context-index" not in first.messages[0]["content"]
    assert first.messages[1] == second.messages[1]
    assert "<context-index" in first.messages[2]["content"]
    assert second.messages[:len(first.messages)] == first.messages
    assert agent.context.messages[0]["content"] == "check history"
    assert broker.completed_requests == [broker.build_calls[0]["request_id"]]
    assert agent._context_index_request_id == ""


def test_first_turn_persists_user_before_source_key_exclusion(tmp_path):
    events: list[str] = []
    database_path = tmp_path / "sessions.db"
    session_path = tmp_path / "session_20260901_001333_51259.json"
    source_key = session_path.stem
    current_text = "unique persisted first turn"

    recall = SessionRecall(database_path)
    recall.init_db()
    older_session_id = recall.create_session("Older useful title")
    recall.log_message(older_session_id, "user", current_text)
    current_session_id = recall.get_or_create_session(
        source_key,
        title="Current live title",
    )
    recall.log_message(current_session_id, "user", current_text)
    events.append("session_recall:user_written")
    recall.close()

    class OrderingSessionSource(SessionRecommendationSource):
        def recommend(self, *args, **kwargs):
            events.append("context_index:build")
            return super().recommend(*args, **kwargs)

    class EmptyActivitySource:
        def recommend(self, *_args, **_kwargs):
            return SourceResult(availability="absent")

    broker = ContextIndexBroker(
        mode="session",
        char_budget=900,
        session_source=OrderingSessionSource(database_path),
        activity_source=EmptyActivitySource(),
        # Not a test of the 200 ms source deadline, which can run out on a busy runner before a source thread starts.
        source_deadline_seconds=5.0,
    )
    llm = DoneLLM()
    agent = _agent(llm, broker, max_iterations=1)
    agent.context.set_session(str(session_path))

    reply = run(agent.reply(Msg(content=[ContentBlock.text(current_text)])))

    assert reply is not None and reply.get_text() == "ok"
    prompt = json.dumps(llm.calls[0].messages, ensure_ascii=False)
    assert "<context-index" in prompt
    assert "Older useful title" in prompt
    assert "Current live title" not in prompt
    assert events.index("session_recall:user_written") < events.index(
        "context_index:build"
    )


def test_feedback_sink_binding_tracks_current_agent_context_session(tmp_path):
    class FeedbackBroker(RecordingBroker):
        def __init__(self):
            super().__init__()
            self.feedback_sink = None

        def set_feedback_sink(self, sink):
            self.feedback_sink = sink

    broker = FeedbackBroker()
    agent = _agent(DoneLLM(), broker, max_iterations=1)
    first_path = tmp_path / "first.json"
    second_path = tmp_path / "second.json"
    event = {
        "type": "context_index_impression",
        "schema_version": 1,
        "request_id": "context-index:request",
        "rows": [],
    }

    assert callable(broker.feedback_sink)
    agent.context.set_session(str(first_path))
    broker.feedback_sink(event)
    agent.context.set_session(str(second_path))
    broker.feedback_sink({**event, "request_id": "context-index:next"})

    first_feedback = first_path.parent / ".artifacts" / "first.context-index-feedback.jsonl"
    second_feedback = second_path.parent / ".artifacts" / "second.context-index-feedback.jsonl"
    assert len(first_feedback.read_text(encoding="utf-8").splitlines()) == 1
    assert len(second_feedback.read_text(encoding="utf-8").splitlines()) == 1


def test_feedback_sink_binding_tracks_replaced_agent_context(tmp_path):
    class FeedbackBroker(RecordingBroker):
        def __init__(self):
            super().__init__()
            self.feedback_sink = None

        def set_feedback_sink(self, sink):
            self.feedback_sink = sink

    broker = FeedbackBroker()
    agent = _agent(DoneLLM(), broker, max_iterations=1)
    first_path = tmp_path / "first.json"
    second_path = tmp_path / "second.json"
    event = {
        "type": "context_index_impression",
        "schema_version": 1,
        "request_id": "context-index:request",
        "rows": [],
    }
    agent.context.set_session(str(first_path))
    broker.feedback_sink(event)
    replacement = AgentContext(system_prompt="replacement")
    replacement.set_session(str(second_path))
    agent.context = replacement

    broker.feedback_sink({**event, "request_id": "context-index:next"})

    first_feedback = first_path.parent / ".artifacts" / "first.context-index-feedback.jsonl"
    second_feedback = second_path.parent / ".artifacts" / "second.context-index-feedback.jsonl"
    assert len(first_feedback.read_text(encoding="utf-8").splitlines()) == 1
    assert len(second_feedback.read_text(encoding="utf-8").splitlines()) == 1


@pytest.mark.parametrize("broker", [RecordingBroker(rendered=""), ExplodingBroker()])
def test_failure_and_empty_pack_leave_reply_unchanged(broker):
    llm = DoneLLM()
    agent = _agent(llm, broker, max_iterations=1)

    assert run(
        agent.reply(Msg(content=[ContentBlock.text("hello")], id="req-off"))
    ).get_text() == "ok"
    assert "<context-index" not in json.dumps(llm.calls[0].messages)


def test_allowlisted_interaction_mode_does_not_build_index():
    broker = RecordingBroker()
    llm = DoneLLM()
    agent = _agent(llm, broker, max_iterations=1)
    agent.tool_allowlist = {"lookup"}

    assert run(
        agent.reply(Msg(content=[ContentBlock.text("bar content")], id="bar-1"))
    ).get_text() == "ok"
    assert broker.build_calls == []


def test_slash_text_does_not_build_index():
    broker = RecordingBroker()
    agent = _agent(DoneLLM(), broker, max_iterations=1)

    assert run(
        agent.reply(Msg(content=[ContentBlock.text("/context-index status")], id="slash-1"))
    ).get_text() == "ok"
    assert broker.build_calls == []


def test_reused_external_id_gets_a_fresh_context_index_turn_key():
    class ChangingBroker(RecordingBroker):
        async def build(self, user_text, **kwargs):
            self.build_calls.append({"user_text": user_text, **kwargs})
            return ContextIndexPack(
                request_id=kwargs["request_id"],
                rendered=(
                    f"<context-index>{len(self.build_calls)}</context-index>"
                ),
            )

    broker = ChangingBroker()
    llm = DoneLLM()
    agent = _agent(llm, broker, max_iterations=1)

    run(agent.reply(Msg(content=[ContentBlock.text("first")], id="reused")))
    run(agent.reply(Msg(content=[ContentBlock.text("second")], id="reused")))

    assert len(broker.build_calls) == 2
    first_key, second_key = [call["request_id"] for call in broker.build_calls]
    assert first_key != second_key
    assert broker.completed_requests == [first_key, second_key]
    assert "<context-index>1</context-index>" in llm.calls[0].messages[-1]["content"]
    assert "<context-index>2</context-index>" in json.dumps(llm.calls[1].messages)


def test_cancelled_then_reused_external_id_gets_fresh_context_index_key():
    class ChunkThenWaitLLM:
        async def chat_stream(self, messages, tools, **kwargs):
            yield {"type": "chunk", "content": "partial"}
            await asyncio.Event().wait()

    async def exercise():
        broker = RecordingBroker()
        agent = _agent(ChunkThenWaitLLM(), broker, max_iterations=1)
        stream = agent._run_react_loop(
            Msg(content=[ContentBlock.text("cancel")], id="reused")
        )
        await anext(stream)
        await stream.aclose()
        agent.llm = DoneLLM()
        await agent.reply(Msg(content=[ContentBlock.text("retry")], id="reused"))
        return broker

    broker = run(exercise())
    build_keys = [call["request_id"] for call in broker.build_calls]
    assert len(build_keys) == 2
    assert len(set(build_keys)) == 2
    assert broker.completed_requests == build_keys


def test_error_then_reused_external_id_gets_fresh_context_index_key():
    async def exercise():
        broker = RecordingBroker()
        agent = _agent(ExplodingLLM(), broker, max_iterations=1)
        with pytest.raises(RuntimeError, match="provider failed"):
            await agent.reply(Msg(content=[ContentBlock.text("error")], id="reused"))
        agent.llm = DoneLLM()
        await agent.reply(Msg(content=[ContentBlock.text("retry")], id="reused"))
        return broker

    broker = run(exercise())
    build_keys = [call["request_id"] for call in broker.build_calls]
    assert len(build_keys) == 2
    assert len(set(build_keys)) == 2
    assert broker.completed_requests == build_keys


def test_completion_reset_and_end_expire_broker_state():
    broker = RecordingBroker()
    agent = _agent(DoneLLM(), broker, max_iterations=1)

    run(agent.reply(Msg(content=[ContentBlock.text("hello")], id="complete")))
    agent.reset_conversation()
    agent.end_session("shutdown")

    assert broker.completed_requests == [broker.build_calls[0]["request_id"]]
    assert broker.session_ends == 2


@pytest.mark.parametrize("after_output", [False, True])
def test_early_generator_close_completes_request(after_output):
    class ChunkThenWaitLLM:
        async def chat_stream(self, messages, tools, **kwargs):
            yield {"type": "chunk", "content": "partial"}
            await asyncio.Event().wait()

    async def exercise():
        broker = RecordingBroker()
        agent = _agent(ChunkThenWaitLLM(), broker, max_iterations=1)
        stream = agent._run_react_loop(
            Msg(content=[ContentBlock.text("cancel me")], id="cancelled")
        )
        first = await anext(stream)
        assert first["type"] == "generation_progress"
        if after_output:
            while first["type"] != "chunk":
                first = await anext(stream)
        assert broker.completed_requests == []
        await stream.aclose()
        return broker

    broker = run(exercise())
    assert broker.completed_requests == [broker.build_calls[0]["request_id"]]


def test_broker_factory_is_lazy_read_only_and_uses_writer_canonical_storage(monkeypatch, tmp_path):
    workdir = tmp_path / "missing-workspace"
    writer_sessions = tmp_path / "writer-root" / ".astra" / "sessions.db"
    writer_activity = tmp_path / "writer-root" / ".astra" / "activity-history.sqlite3"
    monkeypatch.setattr(session_recall, "DB_PATH", writer_sessions)
    monkeypatch.delenv("ASTRA_SESSION_RECALL_DB", raising=False)
    monkeypatch.delenv("ASTRA_CONTEXT_INDEX_SESSIONS_DB", raising=False)
    monkeypatch.setattr(activity_store, "default_activity_db_path", lambda: writer_activity)
    launch_dir = tmp_path / "unrelated-launch-dir"
    launch_dir.mkdir()
    monkeypatch.chdir(launch_dir)

    broker = create_context_index_broker(
        ContextIndexPreferences("off", 777), workdir
    )

    assert broker.mode == "off"
    assert broker.char_budget == 777
    assert broker.session_source._database_path == writer_sessions
    assert broker.activity_source._database_path == writer_activity
    assert not workdir.exists()


def test_broker_factory_follows_session_recall_writer_override(monkeypatch, tmp_path):
    writer_path = tmp_path / "writer" / "sessions.db"
    monkeypatch.setenv("ASTRA_SESSION_RECALL_DB", str(writer_path))
    monkeypatch.delenv("ASTRA_CONTEXT_INDEX_SESSIONS_DB", raising=False)

    broker = create_context_index_broker(
        ContextIndexPreferences("off", 900), tmp_path
    )

    assert broker.session_source._database_path == writer_path
    assert not writer_path.parent.exists()


def test_pytest_isolation_binds_context_index_factory_to_test_archive(tmp_path):
    expected = tmp_path / "session-recall.db"

    broker = create_context_index_broker(
        ContextIndexPreferences("off", 900), tmp_path
    )

    assert os.environ["ASTRA_SESSION_RECALL_DB"] == str(expected)
    assert os.environ["ASTRA_CONTEXT_INDEX_SESSIONS_DB"] == str(expected)
    assert broker.session_source._database_path == expected


def test_context_reader_override_wins_over_writer_override(monkeypatch, tmp_path):
    writer_path = tmp_path / "writer.db"
    reader_path = tmp_path / "reader.db"
    monkeypatch.setenv("ASTRA_SESSION_RECALL_DB", str(writer_path))
    monkeypatch.setenv("ASTRA_CONTEXT_INDEX_SESSIONS_DB", str(reader_path))

    broker = create_context_index_broker(
        ContextIndexPreferences("off", 900), tmp_path
    )

    assert broker.session_source._database_path == reader_path


def test_broker_factory_ignores_noncanonical_session_env_but_honors_activity_writer_env(monkeypatch, tmp_path):
    session_path = tmp_path / "writer override" / "sessions.db"
    activity_path = tmp_path / "writer override" / "activity.sqlite3"
    launch_dir = tmp_path / "other-cwd"
    launch_dir.mkdir()
    monkeypatch.chdir(launch_dir)
    monkeypatch.delenv("ASTRA_SESSION_RECALL_DB", raising=False)
    monkeypatch.delenv("ASTRA_CONTEXT_INDEX_SESSIONS_DB", raising=False)
    monkeypatch.setenv("ASTRA_SESSIONS_DB", str(session_path))
    monkeypatch.setenv("ASTRA_ACTIVITY_DB", str(activity_path))

    broker = create_context_index_broker(ContextIndexPreferences("off", 900), tmp_path / "not-project")

    assert broker.session_source._database_path == session_recall.DB_PATH
    assert broker.activity_source._database_path == activity_path
    assert not session_path.parent.exists()


def test_broker_factory_context_specific_session_override_is_the_only_session_override(monkeypatch, tmp_path):
    session_path = tmp_path / "reader-only" / "sessions.db"
    monkeypatch.setenv("ASTRA_SESSIONS_DB", str(tmp_path / "ignored" / "sessions.db"))
    monkeypatch.setenv("ASTRA_CONTEXT_INDEX_SESSIONS_DB", str(session_path))

    broker = create_context_index_broker(ContextIndexPreferences("off", 900), tmp_path)

    assert broker.session_source._database_path == session_path


def test_broker_factory_uses_explicit_readonly_database_overrides(monkeypatch, tmp_path):
    session_path = tmp_path / "external" / "sessions.db"
    activity_path = tmp_path / "external" / "activity.db"
    monkeypatch.setenv("ASTRA_CONTEXT_INDEX_SESSIONS_DB", str(session_path))
    monkeypatch.setenv("ASTRA_CONTEXT_INDEX_ACTIVITY_DB", str(activity_path))

    broker = create_context_index_broker(
        ContextIndexPreferences("off", 900), tmp_path / "workdir"
    )

    assert broker.session_source._database_path == session_path
    assert broker.activity_source._database_path == activity_path
    assert not session_path.parent.exists()
