"""Conversation-edit tests: the shared undo/retry helpers on any active context."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.runtime.context import AgentContext
from agent.runtime.session_store import SessionStore

from agent.runtime import conversation_edits


def _context(path: Path) -> AgentContext:
    context = AgentContext(system_prompt="test prompt")
    context.set_session(str(path))
    return context


def _tool_call(call_id: str = "call_1") -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": "read_file", "arguments": "{}"},
    }


def _stored_roles(path: Path) -> list[str]:
    return [str(message.get("role")) for message in SessionStore(path).load()["messages"]]


def test_undo_removes_the_whole_trailing_segment_including_tools(tmp_path: Path) -> None:
    path = tmp_path / "session.json"
    context = _context(path)
    context.add_user("fix the bug")
    context.add_assistant("", tool_calls=[_tool_call()])
    context.add_tool("call_1", "file contents")
    context.add_assistant("Here is the fix.")
    context.save()

    ok, message = conversation_edits.undo_last_reply(context)

    assert ok is True
    assert message == "Removed the last model reply. Tool side effects were not rolled back."
    assert [m["role"] for m in context.messages] == ["user"]
    assert _stored_roles(path) == ["user"]


def test_undo_is_a_noop_when_the_tail_is_a_request(tmp_path: Path) -> None:
    context = _context(tmp_path / "session.json")
    context.add_user("only a request")

    ok, message = conversation_edits.undo_last_reply(context)

    assert ok is False
    assert message == "No model reply to remove."
    assert [m["role"] for m in context.messages] == ["user"]


def test_undo_without_any_request_still_removes_the_trailing_reply(tmp_path: Path) -> None:
    context = _context(tmp_path / "session.json")
    context.add_assistant("orphan reply")

    ok, message = conversation_edits.undo_last_reply(context)

    assert ok is True
    assert message == "Removed the last model reply."
    assert context.messages == []


def test_undo_exchanges_counts_and_removes_tool_segments(tmp_path: Path) -> None:
    path = tmp_path / "session.json"
    context = _context(path)
    context.add_user("first request")
    context.add_assistant("first reply")
    context.add_user("second request")
    context.add_assistant("", tool_calls=[_tool_call()])
    context.add_tool("call_1", "file contents")
    context.add_assistant("second reply")
    context.save()

    ok, message = conversation_edits.undo_last_exchanges(context, 1)

    assert ok is True
    assert message == "Removed 1 exchange (4 messages). Tool side effects were not rolled back."
    assert [m["role"] for m in context.messages] == ["user", "assistant"]
    assert _stored_roles(path) == ["user", "assistant"]


def test_undo_exchanges_clamps_to_available_history(tmp_path: Path) -> None:
    path = tmp_path / "session.json"
    context = _context(path)
    context.add_user("first request")
    context.add_assistant("first reply")
    context.save()

    ok, message = conversation_edits.undo_last_exchanges(context, 3)

    assert ok is True
    assert message == "Removed 1 of 3 requested exchanges (2 messages)."
    assert context.messages == []
    assert _stored_roles(path) == []


def test_undo_exchanges_without_history_is_a_noop(tmp_path: Path) -> None:
    context = _context(tmp_path / "session.json")

    ok, message = conversation_edits.undo_last_exchanges(context, 1)

    assert ok is False
    assert message == "No exchanges to remove."


@pytest.mark.parametrize("provenance", ["tool_image", "delegate", "notification"])
@pytest.mark.parametrize("count", [None, 1, 5])
def test_undo_uses_real_requests_as_boundaries(tmp_path, provenance, count) -> None:
    path = tmp_path / "session.json"
    context = _context(path)
    context.add_user("first request")
    context.add_assistant("first reply")
    context.add_user("inspect the screenshot")
    context.add_assistant("", tool_calls=[_tool_call()])
    context.add_tool("call_1", "captured")
    context.add_user([
        {"type": "text", "text": "runtime evidence"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
    ], provenance=provenance)
    context.add_assistant("second reply")

    if count is None:
        ok, message = conversation_edits.undo_last_reply(context)
        expected = ["first request", "first reply", "inspect the screenshot"]
    else:
        ok, message = conversation_edits.undo_last_exchanges(context, count)
        expected = ["first request", "first reply"] if count == 1 else []
        assert ("Removed 1 exchange (5 messages)" if count == 1 else "Removed 2 of 5") in message

    assert ok
    assert "Tool side effects were not rolled back" in message
    assert [m["content"] for m in context.messages] == expected
    assert [m["content"] for m in SessionStore(path).load()["messages"]] == expected


def test_undo_exchanges_does_not_count_orphan_runtime_messages(tmp_path) -> None:
    context = _context(tmp_path / "session.json")
    context.add_user("runtime only", provenance="notification")
    context.add_assistant("ack")
    original = list(context.messages)

    assert conversation_edits.undo_last_exchanges(context, 1) == (False, "No exchanges to remove.")
    assert context.messages == original


def test_undo_exchanges_removes_pending_attachment_request(tmp_path) -> None:
    context = _context(tmp_path / "session.json")
    context.add_user([{"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}])
    context.add_user("runtime evidence", provenance="tool_image")

    assert conversation_edits.undo_last_exchanges(context, 1) == (True, "Removed 1 exchange (2 messages).")
    assert context.messages == []


def test_take_last_request_returns_text_and_clears_the_exchange(tmp_path: Path) -> None:
    path = tmp_path / "session.json"
    context = _context(path)
    context.add_user("please fix the bug")
    context.add_assistant("", tool_calls=[_tool_call()])
    context.add_tool("call_1", "file contents")
    context.add_assistant("I cannot help with that.")
    context.save()

    ok, message, text = conversation_edits.take_last_request(context)

    assert ok is True
    assert message == ""
    assert text == "please fix the bug"
    assert context.messages == []
    assert SessionStore(path).load()["messages"] == []


def test_take_last_request_refuses_without_a_real_request(tmp_path: Path) -> None:
    context = _context(tmp_path / "session.json")
    context.add_user("[通知 someone]: ping", provenance="notification")
    context.add_assistant("acknowledged")

    ok, message, text = conversation_edits.take_last_request(context)

    assert ok is False
    assert message == "No request to retry."
    assert text == ""
    assert [m["role"] for m in context.messages] == ["user", "assistant"]


def test_take_last_request_retries_the_request_behind_a_notification(tmp_path: Path) -> None:
    path = tmp_path / "session.json"
    context = _context(path)
    context.add_user("earlier request")
    context.add_assistant("earlier reply")
    context.add_user("[通知 someone]: ping", provenance="notification")
    context.add_assistant("acknowledged")
    context.save()

    ok, message, text = conversation_edits.take_last_request(context)

    assert ok is True
    assert message == ""
    assert text == "earlier request"
    assert context.messages == []
    assert SessionStore(path).load()["messages"] == []


@pytest.mark.parametrize("provenance", ["tool_image", "delegate", "notification"])
def test_take_last_request_skips_system_messages_to_the_real_request(tmp_path, provenance) -> None:
    path = tmp_path / "session.json"
    context = _context(path)
    context.add_user("first request")
    context.add_assistant("first reply")
    context.add_user("inspect the screenshot")
    context.add_assistant("", tool_calls=[_tool_call()])
    context.add_tool("call_1", "captured")
    context.add_user([
        {"type": "text", "text": "runtime evidence"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
    ], provenance=provenance)
    context.add_assistant("second reply")
    context.save()

    ok, message, text = conversation_edits.take_last_request(context)

    assert ok is True
    assert message == ""
    assert text == "inspect the screenshot"
    expected = ["first request", "first reply"]
    assert [m["content"] for m in context.messages] == expected
    assert [m["content"] for m in SessionStore(path).load()["messages"]] == expected


def test_take_last_request_refuses_attachments_without_mutation(tmp_path: Path) -> None:
    context = _context(tmp_path / "session.json")
    context.add_user([
        {"type": "text", "text": "look at this"},
        {"type": "image_url", "url": "data:image/png;base64,AAAA"},
    ])
    context.add_assistant("nice picture")

    ok, message, text = conversation_edits.take_last_request(context)

    assert ok is False
    assert "attachment" in message
    assert text == ""
    assert [m["role"] for m in context.messages] == ["user", "assistant"]


def test_take_last_request_without_a_request_is_a_noop(tmp_path: Path) -> None:
    context = _context(tmp_path / "session.json")

    ok, message, text = conversation_edits.take_last_request(context)

    assert ok is False
    assert message == "No request to retry."
    assert text == ""


def test_a_removed_message_replaced_before_saving_is_saved_as_a_rewrite(tmp_path: Path) -> None:
    path = tmp_path / "session.json"
    context = _context(path)
    context.add_user("question")
    context.add_assistant("draft answer")
    context.save()

    # A draft gives way to a later message before anything is saved again.
    assert context.remove_last_message(role="assistant")["content"] == "draft answer"
    context.add_user("[SYSTEM-DELIVERED SUBAGENT RESULT] evidence")
    context.add_assistant("final answer")
    context.save()

    stored = SessionStore(path).load()["messages"]
    assert [message.get("content") for message in stored] == [
        message.get("content") for message in context.messages
    ] == ["question", "[SYSTEM-DELIVERED SUBAGENT RESULT] evidence", "final answer"]
