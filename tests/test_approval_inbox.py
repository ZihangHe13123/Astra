import pytest

from agent.runtime.approval_inbox import ApprovalInbox, safe_approval_request
from agent.runtime.event_stream import safe_replay_event


def test_inbox_persists_only_bounded_frontend_request(tmp_path):
    inbox = ApprovalInbox(tmp_path / "approvals.db")
    record = inbox.create(
        {
            "request_id": "approval-1",
            "tool_name": "write_file",
            "risk": "write",
            "reason": "needs permission",
            "agent_reason": "需要读取这份文件来核对当前配置。",
            "reason_source": "agent",
            "target": "allowed.txt",
            "approval_title": "Read host file",
            "approval_summary": "Read the requested file",
            "approval_effect": "Read only",
            "approval_boundary": "This file only",
            "scope_kind": "file",
            "workspace": "C:/workspace",
            "outside_workspace": True,
            "targets": ["allowed.txt"],
            "compound_command_count": 20,
            "arguments": {"path": "allowed.txt", "secret": "x" * 5_000},
            "raw_arguments": {"api_key": "must-not-persist"},
            "preview": "p" * 30_000,
        },
        session_id="session-1",
        task_id="task-1",
        surface="channel",
        channel="qq",
    )

    assert record.state == "pending"
    assert record.request["tool_name"] == "write_file"
    assert record.request["approval_title"] == "Read host file"
    assert record.request["agent_reason"] == "需要读取这份文件来核对当前配置。"
    assert record.request["reason_source"] == "agent"
    assert record.request["approval_boundary"] == "This file only"
    assert record.request["scope_kind"] == "file"
    assert record.request["outside_workspace"] is True
    assert record.request["targets"] == ["allowed.txt"]
    assert record.request["compound_command_count"] == 20
    assert "raw_arguments" not in record.request
    assert len(record.request["arguments"]["secret"]) <= 4_001
    assert len(record.request["preview"]) <= 24_001
    assert ApprovalInbox(tmp_path / "approvals.db").get("approval-1") == record


@pytest.mark.parametrize(("field", "limit"), [("to", 500), ("subject", 500), ("via", 500), ("text", 8_000)])
def test_send_draft_inbox_marks_clipping_not_a_literal_ellipsis(field, limit):
    for value, clipped in [("x" * (limit - 1) + "…", False), ("x" * limit + "…", True),
                           ("x" * limit + "PRIVATE-TAIL", True)]:
        draft = safe_approval_request({"draft": {"text": "body", field: value}})["draft"]
        assert draft[field] == (value[:limit] + "…" if clipped else value)
        assert draft.get("truncated", False) is clipped
        assert safe_approval_request({"draft": draft})["draft"] == draft


def test_send_draft_preserves_upstream_marker_through_inbox_and_replay(tmp_path):
    draft = {"text": "producer's bounded prefix", "truncated": True}
    path = tmp_path / "approvals.db"
    record = ApprovalInbox(path).create({"draft": draft})
    stored = ApprovalInbox(path).get(record.request_id)
    assert stored.request["draft"] == draft
    event = safe_replay_event({"type": "tool_approval_request", **stored.public()})
    assert event["draft"] == draft


def test_send_draft_redacted_takeover_replays_only_an_incomplete_marker(tmp_path):
    base = {"kind": "computer_foreground_takeover", "choices": ["once", "deny"]}
    assert "draft" not in safe_approval_request(base)
    path = tmp_path / "approvals.db"
    record = ApprovalInbox(path).create({**base, "draft": {
        "to": "PRIVATE-TO", "subject": "PRIVATE-SUBJECT", "via": "PRIVATE-APP", "text": "PRIVATE-BODY",
    }})
    stored = ApprovalInbox(path).get(record.request_id)
    assert stored.offered_choices is None
    assert stored.request == {**base, "choices": ["deny"], "draft": {"truncated": True}}
    event = safe_replay_event({"type": "tool_approval_request", **stored.public()})
    assert event["draft"] == {"truncated": True}
    assert "PRIVATE-" not in repr(event)


@pytest.mark.parametrize("draft", [
    {"text": "approved words", "unsupported_recipients": "PRIVATE-EXTRA"},
    {"text": "approved words", "subject": {"internal": "PRIVATE-SUBJECT"}},
    ["PRIVATE-WRONG-SHAPE"],
])
def test_send_draft_replay_cannot_turn_unsupported_disclosure_into_a_complete_draft(draft):
    safe = safe_approval_request({"draft": draft})
    replay = safe_replay_event({"type": "tool_approval_request", **safe})
    assert replay["draft"]["truncated"] is True
    assert "PRIVATE-" not in repr(replay)


def test_resolve_is_atomic_and_pending_only(tmp_path):
    inbox = ApprovalInbox(tmp_path / "approvals.db")
    inbox.create({"tool_name": "shell", "risk": "execute"}, request_id="approval-1")

    approved = inbox.resolve("approval-1", "session")

    assert approved is not None
    assert approved.state == "approved"
    assert approved.decision == "session"
    assert inbox.resolve("approval-1", "deny") is None
    assert inbox.get("approval-1") == approved


def test_restart_marks_waiting_requests_orphaned_without_granting(tmp_path):
    inbox = ApprovalInbox(tmp_path / "approvals.db")
    inbox.create({"tool_name": "write_file", "risk": "write"}, request_id="approval-1")
    inbox.create({"tool_name": "shell", "risk": "execute"}, request_id="approval-2")
    inbox.resolve("approval-2", "deny")

    orphaned = inbox.recover_orphaned()

    assert [item.request_id for item in orphaned] == ["approval-1"]
    assert orphaned[0].state == "orphaned"
    assert orphaned[0].decision == ""
    assert inbox.get("approval-2").state == "denied"


def test_safe_request_drops_unknown_fields():
    safe = safe_approval_request(
        {
            "tool_name": "git_commit",
            "reason": "requested",
            "arguments": {"message": "safe summary"},
            "internal_token": "hidden",
        }
    )

    assert safe == {
        "tool_name": "git_commit",
        "reason": "requested",
        "arguments": {"message": "safe summary"},
    }


def test_takeover_action_classes_survive_durable_roundtrip_without_private_fields(tmp_path):
    path = tmp_path / "approvals.db"
    record = ApprovalInbox(path).create(
        {
            "request_id": "takeover-1",
            "choices": ["once", "session", "deny"],
            "tool_name": "computer_act",
            "risk": "write",
            "kind": "computer_foreground_takeover",
            "reason": "Background mode cannot perform this fragment.",
            "detail": "/Users/private/detail.docx",
            "target": "PID 4242 private target",
            "operation": "Type DO-NOT-PERSIST",
            "preview": "private screenshot ref",
            "scope": "opaque-session-hash",
            "arguments": {
                "application": "WPS Office",
                "window": "Annual report",
                "reason": "Foreground input is required.",
                "action_classes": ["scroll", "click", "unknown-private-class", 7],
                "expected_effect": "Scroll and select one paragraph.",
                "pid": 4242,
                "path": "/Users/private/report.docx",
                "text": "DO-NOT-PERSIST",
                "plan_ref": "private-plan-ref",
                "takeover_ref": "private-takeover-ref",
                "digest": "private-digest",
            },
        },
        request_id="takeover-1",
    )

    replayed = ApprovalInbox(path).get("takeover-1")

    assert replayed == record
    assert replayed.request["choices"] == ["once", "session", "deny"]
    assert replayed.request["arguments"] == {
        "application": "WPS Office",
        "window": "Annual report",
        "reason": "Foreground input is required.",
        "action_classes": ["scroll", "click"],
        "expected_effect": "Scroll and select one paragraph.",
    }
    encoded = repr(replayed.request)
    for private in (
        "opaque-session-hash", "4242", "/Users/private", "DO-NOT-PERSIST", "private screenshot ref",
        "private-plan-ref", "private-takeover-ref", "private-digest", "unknown-private-class",
    ):
        assert private not in encoded


@pytest.mark.parametrize("present_result", [True, False])
def test_takeover_presentation_flag_roundtrips_as_a_boolean(tmp_path, present_result):
    path = tmp_path / "approvals.db"
    record = ApprovalInbox(path).create({
        "kind": "computer_foreground_takeover", "choices": ["once", "deny"],
        "arguments": {"present_result": present_result, "plan_ref": "private"},
    })

    replayed = ApprovalInbox(path).get(record.request_id)
    assert replayed.request["arguments"] == {"present_result": present_result}
    assert replayed.request["arguments"]["present_result"] is present_result
    assert replayed.request["choices"] == ["once", "deny"]


@pytest.mark.parametrize("value", ["true", 1, 0, None, {"text": "private"}])
def test_takeover_drops_non_boolean_presentation_flags(value):
    safe = safe_approval_request({
        "kind": "computer_foreground_takeover", "arguments": {"present_result": value},
    })
    assert safe["arguments"] == {}


def test_takeover_rejects_non_list_action_classes():
    safe = safe_approval_request({
        "kind": "computer_foreground_takeover",
        "arguments": {"application": "WPS Office", "action_classes": "scroll"},
    })

    assert safe["arguments"] == {"application": "WPS Office"}


def test_non_takeover_action_classes_keep_legacy_string_bounding():
    safe = safe_approval_request({
        "kind": "custom_approval",
        "arguments": {"action_classes": "custom-action"},
    })

    assert safe["arguments"] == {"action_classes": "custom-action"}
