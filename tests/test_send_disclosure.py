"""A registered message operation discloses the whole envelope before a human can decide.

The registry is real; no server exists and the decision sink only refuses calls.
"""

import asyncio
from types import SimpleNamespace as NS

import pytest

from agent.runtime.approval_inbox import ApprovalInbox
from agent.runtime.event_stream import safe_replay_event
from agent.runtime.mcp import MCPManager
from agent.runtime.tools.policy import ToolPolicy
from agent.runtime.tools.registry import ToolRegistry


def mail_card(tmp_path, arguments, *, name="send_email", yolo=False, risk="write", mode="permissive", setup=None, invalid=False):
    registry = ToolRegistry(policy=ToolPolicy(mode=mode))
    registry.yolo = yolo
    cards = []

    async def deny(card):
        cards.append(card)
        return "deny"

    class NoTransport:
        async def call_tool(self, *args, **kwargs):
            pytest.fail("a disclosure test must never dispatch")

    schema = {"type": "object", "properties": {key: {} for key in arguments}}
    remote = NS(name=name, description="local message fixture", inputSchema=schema, annotations=None)
    MCPManager(tmp_path / "none.json")._register_tools(registry, "fixture", NoTransport(), [remote], {"risk": risk})
    if setup:
        setup(registry)
    registry.set_approval_handler(deny)
    result = asyncio.run(registry.execute(f"mcp__fixture__{name}", arguments))
    if invalid:
        assert result.get("not_executed") and not cards
        return result
    assert result["code"] == "approval_denied" and len(cards) == 1
    return cards[0]


def test_declared_mail_envelope_goes_to_the_exact_card_and_survives_replay(tmp_path):
    letter = {"to": "main@example.invalid", "cc": ["one@example.invalid", "two@example.invalid"],
              "bcc": "blind@example.invalid", "subject": "Review", "body": "All six lines"}
    card = mail_card(tmp_path, letter)
    envelope = dict(to=letter["to"], cc=", ".join(letter["cc"]), bcc=letter["bcc"], subject="Review",
                    text=letter["body"], via="fixture/send_email")
    assert card.get("draft") == envelope
    assert card["choices"] == ["once", "deny"] and card["yolo_bypass_allowed"] is False
    stored = ApprovalInbox(tmp_path / "inbox.db").create(card)
    replay = safe_replay_event({**stored.request, "type": "tool_approval_request"})
    assert replay and replay["draft"] == envelope


@pytest.mark.parametrize("extra", [
    {"to": "a", "recipients": ["another"]}, {"cc": {"alias": "team"}}, {"bcc": ["one", {"alias": "team"}]},
    {"attachment": "hidden file"}, {"body": "other representation"}, {"_extra": "private parameter"},
])
def test_unrepresentable_mail_fields_cannot_leave_an_approvable_prefix(tmp_path, extra):
    card = mail_card(tmp_path, {"text": "visible message", **extra})
    assert card.get("draft", {}).get("truncated") is True
    assert card["choices"] == ["deny"]


@pytest.mark.parametrize("args,risk", [({"message_id": "opaque-reference"}, "write"), ({"text": "PRIVATE"}, "secret")])
def test_opaque_mail_keeps_a_deny_only_card_without_echoing_hidden_content(tmp_path, args, risk):
    card = mail_card(tmp_path, args, risk=risk)
    assert card.get("draft", {}).get("truncated") is True and "text" not in card["draft"]
    assert card["choices"] == ["deny"]


def test_clipped_copy_list_stays_incomplete_through_replay():
    for key in ("cc", "bcc"):
        replay = safe_replay_event({"type": "tool_approval_request", "draft": {key: "a" * 501, "text": "hello"}})
        assert replay and replay["draft"]["truncated"] is True and len(replay["draft"][key]) == 501


@pytest.mark.parametrize("yolo", [False, True])
def test_locked_mcp_needs_the_exact_envelope_not_a_mode_or_generic_grant(tmp_path, yolo):
    card = mail_card(tmp_path, {"text": "draft", "bcc": "copy@example.invalid"}, mode="locked", yolo=yolo)
    assert card["kind"] == "mcp_side_effect" and card["draft"]["bcc"] == "copy@example.invalid"
    assert not card["yolo_bypass_allowed"]


def test_another_in_flight_call_cannot_cover_a_new_envelope(tmp_path):
    first = mail_card(tmp_path, {"body": "one"})
    second = mail_card(tmp_path, {"body": "two"}, setup=lambda r: r.approved_permission_scopes.add(first["scope"]))
    assert second["draft"]["text"] == "two" and second["request_id"] != first["request_id"]


def test_internal_preflight_keys_cannot_mask_a_remote_argument(tmp_path):
    result = mail_card(tmp_path, {"__permission_call_id": "public input", "text": "message"}, invalid=True)
    assert result["error_type"] == "invalid_input"


@pytest.mark.parametrize("name", ["post_message", "publish", "publish_post"])
def test_a_published_message_never_loses_its_need_for_disclosure(tmp_path, name):
    card = mail_card(tmp_path, {"envelope": {"bcc": "copy@example.invalid", "text": "opaque"}}, name=name)
    assert card.get("draft", {}).get("truncated") is True and card["choices"] == ["deny"]
    assert card["yolo_bypass_allowed"] is False


@pytest.mark.parametrize("name,args", [("publish_release", {"tag": "v1"}), ("upload_file", {"path": "fixture.txt"})])
def test_a_non_message_upload_or_release_does_not_need_an_email_body(tmp_path, name, args):
    card = mail_card(tmp_path, args, name=name)
    assert "draft" not in card and card["choices"] == ["once", "deny"]


@pytest.mark.parametrize("value", [None, {"text": "prefix", "truncated": True}, {"cc": "c" * 501, "text": "hello"}])
def test_replay_cannot_offer_a_positive_choice_for_a_withheld_or_clipped_copy(value):
    from agent.cli.computer_commands import approval_choices

    raw = {"type": "tool_approval_request", "choices": ["once", "session", "deny"], "draft": value}
    replay = safe_replay_event(raw)
    assert replay and replay["draft"]["truncated"] is True
    assert replay["choices"] == approval_choices(replay) == ["deny"]
    assert approval_choices(raw) == (["once", "deny"] if value and "cc" in value else ["deny"])


@pytest.mark.parametrize("offer", [[], ["deny"], "deny"])
def test_a_malformed_or_absent_once_offer_does_not_widen_in_replay(offer):
    replay = safe_replay_event({"type": "tool_approval_request", "draft": {"text": "words"}, "choices": offer})
    assert replay and replay["choices"] == ["deny"]
