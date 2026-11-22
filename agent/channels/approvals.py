"""Approving a tool call from the chat that asked for it, the way Hermes Agent does.

A channel turn that needs an approval sends the request to the person whose message started
the turn and waits for their answer: 同意 runs it once, 拒绝 does not. Only that person can
answer, only while a request waits, and only with a message that is nothing but a short yes or
no; anything else is an ordinary message. No answer within the time limit counts as 拒绝, and
the person is told it did not run. A desktop window showing the same request can still answer
first.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from agent.runtime.tools.consequence import DRAFT_TEXT_FIELDS, LABELS, complete_approval_draft
from agent.channels.base import ChannelMessage

logger = logging.getLogger(__name__)

APPROVAL_SECONDS = 300.0
_CLEANUP_SECONDS = 5.0
_chat_presentations: set[asyncio.Task[None]] = set()
_APPROVE = frozenset({"同意", "批准", "允许", "执行", "可以", "好", "好的", "行", "发送", "发", "yes", "y", "ok", "approve",
                      "/approve", "send"})
_TASK = frozenset({"本任务", "这次任务", "本次任务", "这件事都行", "任务内都行", "task"})
_SESSION = frozenset({"本会话", "都同意", "全部同意", "这个对话都行", "会话内都行", "session"})
_ALWAYS = frozenset({"以后都允许", "以后都行", "总是允许", "always"})
_DENY = frozenset({"拒绝", "不同意", "不行", "不要", "不", "别", "取消", "丢弃", "不发", "no", "n", "deny", "/deny", "discard"})
# Credentials hidden from the request the person reads; the rest of the command stays readable.
_HIDE = (
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL), "[私钥]"),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 …"),
    (re.compile(r"\b(sk-[A-Za-z0-9]{5}|ghp_|gho_|github_pat_|xox[abprs]-|AKIA)[A-Za-z0-9_-]{8,}"), r"\1…"),
    (re.compile(r"(?i)([A-Za-z0-9_-]*(?:api[_-]?key|token|secret|password|passwd|pwd))(\s*[:=]\s*[\"']?)[^\s\"';&|]+"),
     r"\1\2…"),
    (re.compile(r"(://[^/\s:@]+:)[^@\s/]+@"), r"\1…@"),
)
_TARGET_CHARS = 300
_DRAFT_CHARS = 800
_DRAFT_LABELS = {"to": "发给", "cc": "抄送", "bcc": "密送", "destination": "目标位置", "subject": "主题", "via": "通过"}
_CHAT_PROMPT_BYTES = 2_000  # Conservative budget for one unsplit plain-text message.
_DESKTOP_DRAFT = "无法在当前聊天中完整展示这份草稿。请在桌面端核对，或回复“丢弃”取消。"


def hide_credentials(text: str) -> str:
    for pattern, replacement in _HIDE:
        text = pattern.sub(replacement, text)
    return text


@dataclass(eq=False)
class ChatApproval:
    """Identity of one registration, even if two calls came from the same message."""

    message: ChannelMessage
    resolve: Callable[[str], object]
    delivered: bool = False
    closed: bool = False
    ending: bool = False

    def __call__(self, choice: str) -> object:
        if self.closed:
            return False
        accepted = self.resolve(choice)
        if accepted is not False:
            self.closed = True
        return accepted


class ChatApprover(Protocol):
    async def ask_approval(self, message: ChannelMessage, prompt: str, answer: ChatApproval) -> bool | None: ...

    async def end_approval(self, owner: ChatApproval, notice: str = "") -> None: ...


def approval_reply(text: str) -> str | None:
    """The decision a chat message gives, if it is nothing but an approval word."""
    word = text.strip().strip("。.!！~～ ").lower()
    for words, decision in ((_APPROVE, "once"), (_TASK, "task"), (_SESSION, "session"), (_ALWAYS, "always"),
                            (_DENY, "deny")):
        if word in words:
            return decision
    return None


def approval_prompt(request: Mapping[str, Any], timeout: float) -> str:
    """What the person reads: the action, its target with credentials hidden, and how to answer."""
    return _chat_offer(request, timeout, private=True, limit=_CHAT_PROMPT_BYTES)[0]


def _chat_offer(request: Mapping[str, Any], timeout: float, *, private: bool, limit: int) -> tuple[str, bool]:
    draft = request.get("draft")
    if "draft" in request:
        if (not private or request.get("risk") == "secret" or not isinstance(draft, Mapping)
                or not complete_approval_draft(draft)):
            return _DESKTOP_DRAFT, False
        for key in DRAFT_TEXT_FIELDS:
            if key not in draft:
                continue
            text = draft[key]
            if (len(text) > (_DRAFT_CHARS if key == "text" else 500) or hide_credentials(text) != text
                    or any(not c.isprintable() and not (c == "\n" and key == "text") for c in text)):
                return _DESKTOP_DRAFT, False
    title = str(request.get("approval_title") or request.get("operation") or f"使用工具 {request.get('tool_name') or ''}")
    target = hide_credentials(str(request.get("target") or "")).strip()
    if len(target) > _TARGET_CHARS:
        target = target[:_TARGET_CHARS] + "…"
    reason = str(request.get("agent_reason") or request.get("reason") or "").strip()
    lines = [f"需要你批准：{title.strip()}"]
    category = str(request.get("consequential") or "")
    if category:
        lines.append(f"{LABELS.get(category, '高影响')}类操作：每次都会单独问你，之前给过的授权不覆盖它。")
    if draft is not None:
        fields = [f"{label}：{draft[key]}" for key, label in _DRAFT_LABELS.items() if key in draft]
        lines.append(f"要发出去的内容（{len(fields)} 项发送信息，随后是正文）：")
        lines.extend(fields)
        lines.append(draft["text"])
    if request.get("action_label"):
        lines.append(f"将点击：{request['action_label']}")
    if target:
        lines.append(target)
    if reason:
        lines.append(f"原因：{reason}")
    minutes = max(1, round(timeout / 60))
    raw_choices = request.get("choices")
    choices = [str(choice) for choice in raw_choices] if isinstance(raw_choices, (list, tuple)) and raw_choices \
        else ["once", "deny"]
    if draft is not None:
        if "once" not in choices:
            return _DESKTOP_DRAFT, False
        choices = ["once", "deny"]
    session_reply = "“本会话”这个对话里都允许"
    if request.get("kind") == "computer_foreground_takeover":
        arguments = request.get("arguments")
        if isinstance(arguments, Mapping) and arguments.get("present_result") is True:
            lines.append("仅限本次操作：成功后将结果窗口保留在前台。")
        elif "session" in choices:
            session_reply = "“本会话”仅限本对话借用同一窗口执行普通操作，最多 2 分钟"
            lines.append("每批操作结束时，若你未切换焦点，会尝试切回之前的应用。")
    replies = [reply for choice, reply in (
        ("once", "“发送”发出" if draft is not None else "“同意”执行一次"),
        ("task", "“本任务”这件事里都允许"),
        ("session", session_reply),
        ("always", "“以后都允许”以后不再问"),
        ("deny", "“丢弃”不发" if draft is not None else "“拒绝”取消"),
    ) if choice in choices]
    lines.append(f"回复{'，'.join(replies)}；{minutes} 分钟内没回复就不执行。")
    prompt = "\n".join(lines)
    if draft is not None:
        if len(prompt) > limit or len(prompt) > _CHAT_PROMPT_BYTES:
            return _DESKTOP_DRAFT, False
        try:
            if len(prompt.encode("utf-8")) > _CHAT_PROMPT_BYTES:
                return _DESKTOP_DRAFT, False
        except UnicodeEncodeError:
            return _DESKTOP_DRAFT, False
    return prompt, True


async def await_chat_approval(
    approver: ChatApprover | None,
    message: ChannelMessage | None,
    request: Mapping[str, Any],
    decision: asyncio.Future,
    resolve: Callable[[str], object],
    *,
    timeout: float = APPROVAL_SECONDS,
) -> str:
    """Wait for the decision, asking in the chat when it can; no answer in time is a denial."""
    notice = ""
    finished = asyncio.Event()
    sending = True
    presentation = None
    owner = None
    if approver is not None and message is not None and not decision.done():
        has_draft = "draft" in request
        can_send = False

        def answer(choice: str) -> object:
            if decision.done() or (has_draft and choice != "deny"
                                   and not (choice == "once" and registration.delivered and can_send)):
                return False
            return resolve(choice)

        registration = owner = ChatApproval(message, answer)

        async def present() -> None:
            nonlocal sending, can_send
            try:
                try:
                    if has_draft:
                        private = not message.is_group and bool(message.sender_id) and message.sender_id == message.conversation_id
                        prompt, can_send = _chat_offer(request, timeout, private=private,
                            limit=getattr(approver, "approval_prompt_limit", _CHAT_PROMPT_BYTES))
                    else:
                        prompt = approval_prompt(request, timeout)
                    registration.delivered = await approver.ask_approval(message, prompt, registration) is not False
                except Exception:
                    logger.exception("could not ask for approval in the %s chat", message.channel)
                sending = False
                await finished.wait()
            finally:
                # If a cancelled send is still unwinding, *it* keeps its slot. A later
                # request must not receive a reply to that late prompt or clear its listener.
                try:
                    async with asyncio.timeout(_CLEANUP_SECONDS):
                        await approver.end_approval(registration, notice)
                except Exception:
                    logger.exception("could not close the approval in the %s chat", message.channel)

        presentation = asyncio.create_task(present())
        _chat_presentations.add(presentation)
        presentation.add_done_callback(_chat_presentations.discard)

    def close_presentation() -> None:
        if presentation is not None and owner is not None and not finished.is_set():
            owner.closed = True
            finished.set()
            if sending:
                presentation.cancel()

    try:
        done, _ = await asyncio.wait({decision}, timeout=timeout)
        if not done:
            # Timeout competes for the same decision as chat and desktop. If a
            # reply already won, its receipt may still be waiting on persistence.
            if resolve("deny"):
                notice = f"{max(1, round(timeout / 60))} 分钟没等到回复，这一步没有执行。"
            close_presentation()
        return await decision
    except asyncio.CancelledError:
        # Match the desktop wait: a cancelled caller cannot publish a late grant.
        decision.cancel()
        raise
    finally:
        close_presentation()
        if presentation is not None:
            await asyncio.wait({presentation}, timeout=min(timeout, _CLEANUP_SECONDS))
