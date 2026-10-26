"""Which actions have consequences outside Astra: paying, sending, deleting, publishing.

An approval request marked ``consequential`` is decided per call: its card offers only "once" and "deny",
and no earlier task, session or standing grant covers it (tools/registry.py). The categories:

- ``pay``: money leaves (buying, checking out, transferring).
- ``send``: something goes to other people or another place (sending, submitting, replying, uploading).
- ``delete``: data goes away (deleting, discarding changes, cancelling an account or order).
- ``publish``: something becomes public or shared (publishing, posting, pushing, releasing, merging).
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping
from pathlib import PurePath
from typing import Any

LABELS = {"pay": "付款", "send": "发送", "delete": "删除", "publish": "发布"}

_LABEL_PATTERNS = (
    ("pay", re.compile(
        r"\b(pay|payment|checkout|check out|purchase|buy|place order|order now|transfer|donate|subscribe)\b"
        r"|支付|付款|购买|下单|结算|买单|转账|充值|订阅|捐", re.I)),
    ("delete", re.compile(
        r"\b(delete|remove|erase|destroy|discard|unsubscribe|deactivate|close account|cancel (order|subscription|account))\b"
        r"|删除|移除|清空|注销|销毁|退订|取消订阅|取消订单", re.I)),
    ("publish", re.compile(r"\b(publish|post|tweet|go live|release|merge)\b|发布|发表|上线|公开发布|合并", re.I)),
    ("send", re.compile(r"\b(send|submit|reply|forward|share|invite|upload)\b|发送|提交|回复|转发|分享|邀请|上传|寄出",
                        re.I)),
)

# The first word of a tool name is its verb: send_email, postMessage, delete-file.
_VERBS = {
    "pay": {"pay", "purchase", "buy", "order", "checkout", "transfer", "charge", "refund", "donate"},
    "send": {"send", "submit", "reply", "forward", "share", "invite", "email", "mail", "message", "dm", "notify",
             "upload", "comment"},
    "delete": {"delete", "remove", "trash", "destroy", "erase", "drop", "purge", "wipe", "unsubscribe"},
    "publish": {"publish", "post", "tweet", "release", "deploy", "merge", "push"},
}

_DELETE_COMMANDS = {"rm", "rmdir", "unlink", "shred", "srm", "trash", "del", "erase", "rd", "remove-item", "dropdb"}
_PUBLISH_SUBCOMMANDS = {
    "npm": {"publish", "unpublish"}, "yarn": {"publish"}, "pnpm": {"publish"}, "cargo": {"publish"},
    "twine": {"upload"}, "docker": {"push"}, "podman": {"push"}, "poetry": {"publish"}, "gem": {"push"},
}
_WRAPPERS = {"sudo", "env", "time", "nohup", "nice", "command", "exec", "xargs", "doas"}


def label_category(text: str) -> str:
    """The category a button or link's visible name says it has, or ""."""
    for category, pattern in _LABEL_PATTERNS:
        if pattern.search(text or ""):
            return category
    return ""


def tool_category(name: str) -> str:
    """The category a tool name's leading verb says it has, or ""."""
    words = re.findall(r"[A-Z]?[a-z]+|[0-9]+", re.sub(r"[^A-Za-z0-9]+", " ", name or ""))
    verb = words[0].lower() if words else ""
    for category, verbs in _VERBS.items():
        if verb in verbs:
            return category
    return ""


def _words(part: str) -> list[str]:
    try:
        words = shlex.split(part, posix=True)
    except ValueError:
        words = part.split()
    while words and (PurePath(words[0]).name.lower() in _WRAPPERS or "=" in words[0] and not words[0].startswith("-")):
        words = words[1:]
    return words


def command_category(parts: list[str]) -> str:
    """The category of the most consequential simple command in a shell command line, or ""."""
    found = ""
    for part in parts:
        words = _words(part)
        if not words:
            continue
        program = PurePath(words[0]).name.lower()
        rest = [word.lower() for word in words[1:]]
        sub = rest[0] if rest else ""
        if program in _DELETE_COMMANDS or (program == "find" and "-delete" in rest):
            return "delete"
        if program == "git":
            if sub == "push":
                found = "publish"
            elif sub == "clean" or (sub == "reset" and "--hard" in rest) or (sub == "branch" and "-d" in rest):
                return "delete"
        elif program == "gh":
            if sub == "pr" and len(rest) > 1 and rest[1] == "merge" or sub == "release" and "create" in rest:
                found = "publish"
            elif sub == "repo" and "delete" in rest:
                return "delete"
        elif sub in _PUBLISH_SUBCOMMANDS.get(program, set()):
            found = "publish"
        elif program in {"mail", "sendmail", "mutt", "msmtp"}:
            found = found or "send"
    return found


_DRAFT_FIELDS = {
    "to": ("to", "recipient", "recipients", "email", "address", "channel", "chat", "chat_id", "user", "username",
           "phone", "number"),
    "subject": ("subject", "title"),
    "text": ("text", "body", "message", "content", "markdown", "html", "caption", "status"),
}


def draft_from_arguments(args: Mapping[str, Any]) -> dict[str, str]:
    """What a send or publish call would put out, read from its arguments, for the card to show as is."""
    draft: dict[str, str] = {}
    lowered = {str(key).lower(): value for key, value in args.items() if not str(key).startswith("_")}
    for field, names in _DRAFT_FIELDS.items():
        for name in names:
            value = lowered.get(name)
            if isinstance(value, (list, tuple)):
                value = ", ".join(str(item) for item in value)
            if isinstance(value, (str, int, float)) and str(value).strip():
                draft[field] = str(value)
                break
    return draft if draft.get("text") else {}
