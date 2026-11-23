"""A registered message operation discloses the whole envelope before a human can decide.

The registry is real; no server exists and the decision sink only refuses calls.
"""

import asyncio
import json
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
    [local] = registry.tool_names  # The registered alias can be escaped or shortened; preflight uses the remote name.
    result = asyncio.run(registry.execute(local, arguments))
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


REPLY_ROUTE = "repos/owner/repo/pulls/pull_number/comments/comment_id/replies.post"


@pytest.mark.parametrize("name", [REPLY_ROUTE, REPLY_ROUTE + ".v2", "repos/{owner}/{repo}/comments/{comment_id}/replies.POST",
                                  "POST /repos/{owner}/{repo}/comments/{comment_id}/replies", "github.comments.createReply",
                                  "create_pull_request_review_comment", "chat.postMessage"])
def test_message_operations_disclose_text_and_exact_thread_on_the_registered_card(tmp_path, name):
    position = {"owner": "team", "repo": "review", "pull_number": 37, "comment_id": 910}
    card = mail_card(tmp_path, {**position, "body": "Reply line 1\nReply line 2"}, name=name)
    assert card.get("consequential") in {"send", "publish"}
    draft = card["draft"]
    assert draft["text"] == "Reply line 1\nReply line 2" and draft["via"] == f"fixture/{name}"
    assert json.loads(draft["destination"]) == position  # Keep the thread separate from To/Cc/Bcc and the tool name.
    assert card["choices"] == ["once", "deny"] and card["yolo_bypass_allowed"] is False
    assert safe_replay_event({**card, "type": "tool_approval_request"})["draft"] == draft


@pytest.mark.parametrize("name", ["repos/owner/repo/comments/comment_id/replies.get", "GET /comments/{comment_id}/replies",
                                  "comments.list", "post.get", "chat.getPost", "get_pull_request_review_comments",
                                  "search_posts", "search.post", "POST /comments/search", "create_email_draft",
                                  "update_message_status", "comments.reactions.create", "add_comment_reaction",
                                  "POST /email-drafts", "messages/{id}/post-reactions.post"])
def test_read_search_and_draft_creation_are_not_message_sends(tmp_path, name):
    card = mail_card(tmp_path, {"query": "fixture"}, name=name)
    assert "draft" not in card and not card.get("consequential")  # Still subject to ordinary MCP policy.


@pytest.mark.parametrize("name,args", [
    ("upload_file", {"file_uri": "file:///fixture/report.pdf"}),
    ("files.upload", {"fileId": "fixture", "overwrite": False, "content_type": "application/pdf"}),
    ("merge_pull_request", {"owner": "acme", "repo": "docs", "pull_number": 27}),
    ("mergePullRequest", {"repository": "acme/docs", "number": 27, "merge_method": "squash"}),
    ("upload_file", {"folder_id": "fixture", "file_uri": "file:///fixture/report", "overwrite": False}),
    ("repos/{owner}/{repo}/pulls/{pull_number}/merge.put", {"owner": "acme", "repo": "docs", "pull_number": 27}),
    ("repos/{owner}/{repo}/pulls/{pull_number}/merge.put", {"path": {"owner": "acme", "repo": "docs", "pull_number": 27},
                                                          "requestBody": {"merge_method": "squash"}}),
])
def test_artifacts_keep_their_own_approval_regardless_of_reference_spelling(tmp_path, name, args):
    card = mail_card(tmp_path, args, name=name)
    assert "draft" not in card and card.get("choices") == ["once", "deny"]
    assert card["consequential"] in {"send", "publish"} and card["target"] == f"fixture/{name}"
    for key, value in args.items():
        assert card["arguments"][key] == str(value)


@pytest.mark.parametrize("name,args", [(REPLY_ROUTE, {"owner": "a", "repo": "b", "comment_id": 3}),
    (REPLY_ROUTE, {"body": "shown", "comment_id": {"internal": "not a scalar reference"}}),
    (REPLY_ROUTE, {"body": "shown", "bcc": {"group": "opaque"}}),
    ("upload_file", {"file_uri": "file:///fixture/file", "envelope": {"body": "a separate message"}}),
    ("send_file", {"file_uri": "file:///fixture/report", "to_address_id": "side-address", "body_ref": "other-body"}),
    ("post_message", {"file_uri": "file:///fixture/file"})])
def test_partial_or_mixed_messages_do_not_become_artifact_approvals(tmp_path, name, args):
    card = mail_card(tmp_path, args, name=name)
    assert card.get("choices") == ["deny"] and card["draft"]["truncated"] is True


def test_openapi_path_and_request_body_are_projected_without_ignoring_siblings(tmp_path):
    path = {"owner": "team", "repo": "repo", "comment_id": 8}
    args = {"path": path, "requestBody": {"body": "text", "cc": ["copy@example.invalid"]}}
    card = mail_card(tmp_path, args, name=REPLY_ROUTE)
    assert card["choices"] == ["once", "deny"] and json.loads(card["draft"]["destination"]) == path
    assert card["draft"]["cc"] == "copy@example.invalid" and card["draft"]["text"] == "text"
    for changes in ({"comment_id": 9}, {"body": "another body"}, {"headers": {"X-Private": "fixture"}},
                    {"requestBody": {"body": "text", "unrepresentable": {"address": "elsewhere"}}}):
        partial = mail_card(tmp_path, {**args, **changes}, name=REPLY_ROUTE)
        assert partial["choices"] == ["deny"] and partial["draft"]["truncated"] is True


def test_message_route_is_not_a_replayable_read_even_if_locally_mislabelled(tmp_path):
    card = mail_card(tmp_path, {"body": "review only"}, name=REPLY_ROUTE, risk="read", mode="locked")
    assert card.get("risk") == "write" and card["draft"]["text"] == "review only"
    assert card["choices"] == ["once", "deny"] and card["yolo_bypass_allowed"] is False


def test_reply_destination_cannot_be_silently_replaced_by_a_malformed_binding():
    from agent.cli.computer_commands import approval_choices
    from agent.channels.approvals import approval_prompt

    base = {"choices": ["once", "deny"], "draft": {"text": "hello", "destination": '{"comment_id":17}'}}
    assert '目标位置：{"comment_id":17}' in approval_prompt(base, 30)
    for value in ["{}", "[]", '{"comment_id":true}', '{"repo":{}}', "not json"]:
        assert approval_choices({**base, "draft": {"text": "hello", "destination": value}}) == ["deny"]


@pytest.mark.parametrize("name,args", [
    ("send_sms", {"number": "12345", "message": "hello"}),
    ("chat.postMessage", {"channel": "team", "message": "hello"}),
    ("fixture_post_message", {"channel": "team", "message": "hello"}),
])
def test_existing_recipient_and_body_aliases_are_not_relabelled_as_thread_ids(tmp_path, name, args):
    card = mail_card(tmp_path, args, name=name)
    assert card["choices"] == ["once", "deny"]
    assert card["draft"] == {"to": args.get("channel", args.get("number")), "text": "hello", "via": "fixture/" + name}


@pytest.mark.parametrize("number", [37, "0037"])
def test_explicit_path_binding_wins_over_ambiguous_mail_alias_only_in_its_own_location(tmp_path, number):
    name = "repos/{repo}/issues/{number}/comments.post"
    for args in ({"repo": "paper", "number": number, "body": "reply"},
                 {"path": {"repo": "paper", "number": number}, "requestBody": {"body": "reply"}}):
        card = mail_card(tmp_path, args, name=name)
        assert card["choices"] == ["once", "deny"] and "to" not in card["draft"]
        assert json.loads(card["draft"]["destination"]) == {"repo": "paper", "number": number}
