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
from typing import Any, Protocol

from agent.channels.base import ChannelMessage

logger = logging.getLogger(__name__)

APPROVAL_SECONDS = 300.0
_APPROVE = frozenset({"同意", "批准", "允许", "执行", "可以", "好", "好的", "行", "yes", "y", "ok", "approve", "/approve"})
_DENY = frozenset({"拒绝", "不同意", "不行", "不要", "不", "别", "取消", "no", "n", "deny", "/deny"})
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


def hide_credentials(text: str) -> str:
    for pattern, replacement in _HIDE:
        text = pattern.sub(replacement, text)
    return text


class ChatApprover(Protocol):
    async def ask_approval(self, message: ChannelMessage, prompt: str, answer: Callable[[str], object]) -> None: ...

    async def end_approval(self, message: ChannelMessage, notice: str = "") -> None: ...


def approval_reply(text: str) -> str | None:
    """The decision a chat message gives, if it is nothing but an approval word."""
    word = text.strip().strip("。.!！~～ ").lower()
    if word in _APPROVE:
        return "once"
    if word in _DENY:
        return "deny"
    return None


def approval_prompt(request: Mapping[str, Any], timeout: float) -> str:
    """What the person reads: the action, its target with credentials hidden, and how to answer."""
    title = str(request.get("approval_title") or request.get("operation") or f"使用工具 {request.get('tool_name') or ''}")
    target = hide_credentials(str(request.get("target") or "")).strip()
    if len(target) > _TARGET_CHARS:
        target = target[:_TARGET_CHARS] + "…"
    reason = str(request.get("agent_reason") or request.get("reason") or "").strip()
    lines = [f"需要你批准：{title.strip()}"]
    if target:
        lines.append(target)
    if reason:
        lines.append(f"原因：{reason}")
    minutes = max(1, round(timeout / 60))
    lines.append(f"回复“同意”执行一次，回复“拒绝”取消；{minutes} 分钟内没回复就不执行。")
    return "\n".join(lines)


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
    # Closed afterwards even when asking failed part way: the chat may already be waiting for an answer.
    asked = approver is not None and message is not None
    if approver is not None and message is not None:
        try:
            await approver.ask_approval(message, approval_prompt(request, timeout), resolve)
        except Exception:
            # The desktop may still answer; the time limit below still applies.
            logger.exception("could not ask for approval in the %s chat", message.channel)
    notice = ""
    try:
        try:
            return await asyncio.wait_for(asyncio.shield(decision), timeout)
        except TimeoutError:
            if resolve("deny"):
                notice = f"{max(1, round(timeout / 60))} 分钟没等到回复，这一步没有执行。"
            return await decision
    finally:
        if asked and approver is not None and message is not None:
            try:
                await approver.end_approval(message, notice)
            except Exception:
                logger.exception("could not close the approval in the %s chat", message.channel)
