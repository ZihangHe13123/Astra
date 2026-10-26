"""Approvals offer once / this task / this session / always / deny the same way everywhere; consequential
requests are decided per call; each conversation has its own grants; standing rules come from the person."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

from agent.channels.approvals import approval_prompt, approval_reply
from agent.runtime.approval_inbox import ApprovalInbox
from agent.runtime.tools.consequence import command_category, draft_from_arguments, label_category, tool_category
from agent.runtime.tools.grants import AllowRules, enter_grant_space, leave_grant_space
from agent.runtime.tools.registry import ToolDef, ToolRegistry
from agent.ui import approval_rules


def registry_with(tmp_path: Path, answers: list[str], *, consequential: str = "", rules: bool = False,
                  advisor: bool = False) -> tuple[ToolRegistry, list[dict], list[str]]:
    """A registry with one tool whose every call needs a grant for the same scope."""
    registry = ToolRegistry()
    asked: list[dict] = []
    granted: list[str] = []

    async def decide(request: dict) -> str:
        asked.append(request)
        return answers.pop(0)

    def check(args: dict) -> dict:
        request = {"kind": "fixture_write", "scope": f"fixture:{args.get('target', 'a')}",
                   "target": str(args.get("target", "a")), "approval_title": "写入示例"}
        if consequential:
            request["consequential"] = consequential
        return request

    def grant(_args: dict, _request: dict, decision: str):
        granted.append(decision)
        return None

    registry.register(ToolDef(name="fixture_write", description="d", parameters={"type": "object", "properties": {
        "target": {"type": "string"}}}, fn=lambda target="a": f"wrote {target}", risk="write",
        permission_check=check, permission_grant=grant))
    registry.set_approval_handler(decide)
    if rules:
        registry.grants.rules = AllowRules(tmp_path / "settings.json")
    if advisor:
        registry.rule_advisor = lambda _key: True
    return registry, asked, granted


def call(registry: ToolRegistry, task_id: str = "task-1", target: str = "a") -> dict:
    return asyncio.run(registry.execute("fixture_write", {"target": target}, task_id=task_id))


def test_the_card_offers_four_answers_and_a_task_grant_ends_with_its_task(tmp_path):
    registry, asked, granted = registry_with(tmp_path, ["task", "once"])
    assert call(registry)["error"] == ""
    assert asked[0]["choices"] == ["once", "task", "session", "deny"]
    # The same request later in the same task is not asked again; a new task asks.
    assert call(registry)["error"] == "" and len(asked) == 1
    assert call(registry, task_id="task-2")["error"] == "" and len(asked) == 2
    # The tool itself only ever grants one call; the broader grant is the registry's.
    assert granted == ["once", "once", "once"]
    # Without a task there is no "this task" to offer.
    registry, asked, _ = registry_with(tmp_path, ["once"])
    call(registry, task_id="")
    assert asked[0]["choices"] == ["once", "session", "deny"]


def test_a_session_grant_belongs_to_its_conversation_alone(tmp_path):
    registry, asked, _ = registry_with(tmp_path, ["session", "session"])
    call(registry)
    call(registry, task_id="task-9")
    assert len(asked) == 1
    # A WeChat chat in the same backend is asked for itself, and its grant stays its own.
    token = enter_grant_space("channel:weixin_private_owner")
    try:
        call(registry, task_id="channel:1")
        call(registry, task_id="channel:2")
    finally:
        leave_grant_space(token)
    assert len(asked) == 2
    # Ending the desktop session ends its grants, not the chat's.
    registry._clear_session_approvals("s", "reset")
    token = enter_grant_space("channel:weixin_private_owner")
    try:
        assert call(registry, task_id="channel:3")["error"] == ""
    finally:
        leave_grant_space(token)
    assert len(asked) == 2


def test_a_consequential_request_is_asked_every_time_and_never_broadened(tmp_path):
    registry, asked, granted = registry_with(tmp_path, ["session", "task", "deny"], consequential="send")
    assert call(registry)["error"] == "" and call(registry)["error"] == ""
    denied = call(registry)
    assert len(asked) == 3 and all(request["choices"] == ["once", "deny"] for request in asked)
    # A frontend answering more than the card offered gets the exact call at most.
    assert granted == ["once", "once"] and denied["error_type"] == "approval_denied"


def test_always_is_offered_only_when_earned_and_is_kept_until_removed(tmp_path, monkeypatch):
    registry, asked, _ = registry_with(tmp_path, ["once"], rules=True)
    call(registry)
    assert "always" not in asked[0]["choices"]
    registry, asked, _ = registry_with(tmp_path, ["always"], rules=True, advisor=True)
    call(registry)
    assert asked[0]["choices"] == ["once", "task", "session", "always", "deny"]
    stored = json.loads((tmp_path / "settings.json").read_text())["approval_rules"]
    assert [rule["key"] for rule in stored] == [asked[0]["grant_key"]] and stored[0]["label"] == "写入示例：a"
    # Another backend, another conversation: the rule covers it without asking.
    fresh, fresh_asked, _ = registry_with(tmp_path, [], rules=True)
    token = enter_grant_space("channel:qq")
    try:
        assert call(fresh, task_id="other")["error"] == ""
    finally:
        leave_grant_space(token)
    assert fresh_asked == []
    # Removed on the desktop, it asks again with the next request.
    monkeypatch.setenv("AGENT_SETTINGS_PATH", str(tmp_path / "settings.json"))
    assert approval_rules.main(["remove", stored[0]["key"]]) == 0
    fresh_answers = ["once"]
    fresh.approval_handler = None

    async def decide(request: dict) -> str:
        fresh_asked.append(request)
        return fresh_answers.pop(0)

    fresh.set_approval_handler(decide)
    assert call(fresh)["error"] == "" and len(fresh_asked) == 1


def test_always_is_earned_by_five_approvals_on_two_days_with_no_refusal(tmp_path):
    inbox = ApprovalInbox(tmp_path / "approvals.db")

    def approve(decision: str, day: str) -> None:
        record = inbox.create({"tool_name": "t", "grant_key": "k" * 32, "choices": ["once", "deny"]},
                              session_id="s", task_id="t", surface="gui")
        inbox.resolve(record.request_id, decision)
        with sqlite3.connect(tmp_path / "approvals.db") as db:
            db.execute("UPDATE approval_requests SET created_at=? WHERE request_id=?", (f"{day}T10:00:00+00:00",
                                                                                      record.request_id))

    for _ in range(5):
        approve("once", "2026-10-20")
    assert not inbox.rule_eligible("k" * 32)
    approve("session", "2026-10-21")
    assert inbox.rule_eligible("k" * 32)
    approve("deny", "2026-10-22")
    assert not inbox.rule_eligible("k" * 32)


def test_what_counts_as_paying_sending_deleting_or_publishing():
    assert [label_category(text) for text in ("提交订单", "Pay now", "删除账号", "Post", "Next", "Load more")] == \
        ["send", "pay", "delete", "publish", "", ""]
    assert command_category(["git push origin main"]) == "publish"
    assert command_category(["cd build", "rm -rf dist"]) == "delete"
    assert command_category(["sudo env X=1 rm file"]) == "delete"
    assert command_category(["git reset --hard HEAD~1"]) == "delete"
    assert command_category(["npm publish"]) == "publish" and command_category(["gh pr merge 12"]) == "publish"
    assert command_category(["pytest -q", "git status", "npm test"]) == ""
    assert [tool_category(name) for name in ("send_email", "postMessage", "delete-file", "list_posts", "get_thread")] \
        == ["send", "publish", "delete", "", ""]
    assert draft_from_arguments({"to": ["a@x.com", "b@x.com"], "body": "hi"}) == {"to": "a@x.com, b@x.com", "text": "hi"}
    assert draft_from_arguments({"query": "x"}) == {}


def test_a_chat_answers_with_the_four_words_and_reads_a_draft():
    assert [approval_reply(word) for word in ("同意", "本任务", "本会话", "以后都允许", "拒绝", "丢弃", "发送", "也许")] == \
        ["once", "task", "session", "always", "deny", "deny", "once", None]
    prompt = approval_prompt({"approval_title": "发邮件", "choices": ["once", "deny"], "consequential": "send",
                              "draft": {"to": "tan@example.edu", "text": "明天见"}}, 300)
    assert "发送类操作：每次都会单独问你" in prompt and "发给：tan@example.edu\n明天见" in prompt
    assert prompt.endswith("回复“发送”发出，“丢弃”不发；5 分钟内没回复就不执行。")
    ordinary = approval_prompt({"approval_title": "写文件", "choices": ["once", "task", "session", "deny"]}, 300)
    assert "回复“同意”执行一次，“本任务”这件事里都允许，“本会话”这个对话里都允许，“拒绝”取消" in ordinary
