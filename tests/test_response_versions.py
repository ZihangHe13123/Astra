from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace

import pytest

from agent.cli.response_versions import ResponseVersions
from agent.runtime.context import AgentContext
from agent.runtime.conversation_branches import ConversationBranches
from agent.runtime.session_store import SessionStore


class Model:
    def __init__(self, events):
        self.events, self.requests = events, []

    def supports_forced_tool_choice(self):
        return True

    async def chat_stream(self, **kwargs):
        self.requests.append(copy.deepcopy(kwargs))
        for event in self.events:
            if isinstance(event, BaseException):
                raise event
            yield event


def fixture(tmp_path, events=None):
    context = AgentContext(system_prompt="You are a helpful assistant.")
    context.set_session(str(tmp_path / "conversation.json"))
    context.add_user("change a file and explain")
    context.add_assistant("", tool_calls=[{"id": "write-once", "type": "function",
        "function": {"name": "write_file", "arguments": '{"path":"once.txt"}'}}])
    context.add_tool("write-once", "Successfully wrote the file")
    context.add_assistant("Original explanation")
    context.add_user("original follow-up")
    context.add_assistant("Original follow-up reply")
    context.save()
    model = Model(events if events is not None else [
        {"type": "chunk", "content": "Alternative"},
        {"type": "done", "content": "Alternative", "usage": {"prompt_tokens": 10, "completion_tokens": 2}},
    ])
    agent = SimpleNamespace(context=context, llm=model, reset_conversation=context.reset)
    sent, changes = [], []

    async def changed():
        changes.append(context.branch_id)

    controller = ResponseVersions(agent, sent.append, changed)
    state = controller.state()
    command = {"type": "response_regenerate", "request_id": "regen", "branch_id": state["branch_id"],
               "revision": state["revision"], "source_ref": state["targets"][0]["source_ref"]}
    return context, model, controller, command, sent, changes


def test_alternative_uses_completed_observations_without_replaying_and_keeps_followups(tmp_path):
    context, model, controller, command, sent, changes = fixture(tmp_path)
    original = copy.deepcopy(context.messages)
    asyncio.run(controller.regenerate(command))
    assert context.branch_id != "main"
    assert len(model.requests) == 1
    request = model.requests[0]
    assert request["tools"] == [] and request["tool_choice"] == "none"
    assert [message["role"] for message in request["messages"]] == ["system", "user", "assistant", "tool"]
    assert request["messages"][-1]["content"] == "Successfully wrote the file"
    assert not any("original follow-up" in str(message) for message in request["messages"])
    assert context.messages[-1]["content"] == "Alternative"
    assert SessionStore(context.session_path, branch_id="main").load(readonly=True)["messages"] == original
    assert changes == [context.branch_id]
    state = controller.state()
    group = state["groups"][0]
    asyncio.run(controller.select({"type": "response_select", "request_id": "select-original",
        "branch_id": state["branch_id"], "revision": state["revision"],
        "group_id": group["id"], "version_id": group["versions"][0]["id"]}))
    assert context.messages == original
    assert len(model.requests) == 1
    assert context.branch_id == "main"


@pytest.mark.parametrize("events,error", [
    ([{"type": "chunk", "content": "partial"}, RuntimeError("network down")], RuntimeError),
    ([{"type": "tool_calls", "calls": [{"name": "write_file", "arguments": {}}]}], ValueError),
    ([{"type": "done", "content": "", "usage": {}}], ValueError),
    ([{"type": "chunk", "content": "partial"}], ValueError),
    ([{"type": "done", "content": "truncated", "finish_reason": "length"}], ValueError),
    ([asyncio.CancelledError()], asyncio.CancelledError),
])
def test_failed_or_cancelled_generation_preserves_original_selection(tmp_path, events, error):
    context, model, controller, command, sent, changes = fixture(tmp_path, events)
    original = copy.deepcopy(context.messages)
    with pytest.raises(error):
        asyncio.run(controller.regenerate(command))
    assert context.messages == original
    assert context.branch_id == "main"
    assert ConversationBranches(context.session_path).state()["active_branch"] == "main"
    assert changes == []
    assert sent[-1]["status"] in {"failed", "cancelled"}


def test_stale_reference_and_revision_make_no_model_request(tmp_path):
    context, model, controller, command, sent, changes = fixture(tmp_path)
    command["source_ref"]["digest"] = "f" * 64
    with pytest.raises(ValueError):
        asyncio.run(controller.regenerate(command))
    assert model.requests == []
    command["revision"] = True
    with pytest.raises(ValueError):
        asyncio.run(controller.regenerate(command))
    assert model.requests == []


def test_media_preflight_failure_does_not_publish_selection(tmp_path, monkeypatch):
    context, model, controller, command, sent, changes = fixture(tmp_path)
    asyncio.run(controller.regenerate(command))
    state = controller.state()
    group = state["groups"][0]
    def fail(*args, **kwargs):
        raise OSError("missing image media")
    monkeypatch.setattr(context, "stage_session", fail)
    with pytest.raises(OSError, match="missing image"):
        asyncio.run(controller.select({"request_id": "select", "branch_id": state["branch_id"],
            "revision": state["revision"], "group_id": group["id"], "version_id": group["versions"][0]["id"]}))
    assert controller.state()["active_branch"] == state["active_branch"]


def test_oversized_historical_reply_is_not_replayed_or_guessed(tmp_path):
    context, model, controller, command, sent, changes = fixture(tmp_path)
    context.max_prompt_tokens = 1
    with pytest.raises(ValueError, match="context limit"):
        asyncio.run(controller.regenerate(command))
    assert context.branch_id == "main" and model.requests == []


def test_error_after_atomic_publication_adopts_the_committed_answer(tmp_path, monkeypatch):
    context, model, controller, command, sent, changes = fixture(tmp_path)
    original = ConversationBranches.finish_candidate
    def publish_then_fail(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise OSError("notification read failed after publication")
    monkeypatch.setattr(ConversationBranches, "finish_candidate", publish_then_fail)
    asyncio.run(controller.regenerate(command))
    assert context.branch_id != "main"
    assert context.messages[-1]["content"] == "Alternative"
    assert controller.state()["active_branch"] == context.branch_id
    assert sent[-1]["status"] == "completed"


def test_notification_failure_cannot_reset_an_already_adopted_branch(tmp_path):
    context, model, controller, command, sent, changes = fixture(tmp_path)
    async def unavailable_view():
        raise OSError("history delivery unavailable")
    controller.changed = unavailable_view
    with pytest.raises(RuntimeError, match="new reply was saved"):
        asyncio.run(controller.regenerate(command))
    selected = copy.deepcopy(context.messages)
    assert selected[-1]["content"] == "Alternative"
    assert len(selected) == 4
    context.add_user("next")
    context.save()
    persisted = SessionStore(context.session_path).load(readonly=True)["messages"]
    assert persisted[:-1] == selected
    assert persisted[-1]["content"] == "next"
