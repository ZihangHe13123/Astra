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


def _name_words(name: str) -> list[str]:
    return [word.lower() for word in re.findall(r"[A-Z]?[a-z]+|[0-9]+", name or "")]


def tool_category(name: str) -> str:
    """The category a tool name's leading verb says it has, or ""."""
    words = _name_words(name)
    verb = words[0] if words else ""
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
           "phone", "number", "to_recipients", "to_addresses"),
    "cc": ("cc", "cc_recipients", "cc_addresses", "carbon_copy"),
    "bcc": ("bcc", "bcc_recipients", "bcc_addresses", "blind_carbon_copy"),
    "subject": ("subject", "title"),
    "text": ("text", "body", "message", "content", "markdown", "html", "caption", "status"),
}


_DRAFT_ALIASES = {alias: field for field, names in _DRAFT_FIELDS.items() for name in names
                  for alias in (name, name.replace("_", ""))}
DRAFT_TEXT_FIELDS = ("to", "cc", "bcc", "subject", "text", "via")


def message_draft_required(name: str, args: Mapping[str, Any]) -> bool:
    """Do not confuse an opaque message with the absence of a message.

    A named artifact operation on simple references still has its ordinary consequential card.
    A post, or an unknown/nested payload, cannot claim that exception merely by omitting `text`.
    """
    words = set(_name_words(name))
    artifact = words & {"upload", "release", "deploy", "merge", "push"}
    references = {"file", "files", "path", "paths", "file_path", "tag", "version", "ref", "branch", "base", "head",
                  "repo", "repository", "owner", "remote", "environment", "target"}
    return not (artifact and not words & {"message", "mail", "email", "post", "tweet"} and all(
        key in references and (isinstance(value, str) or key in {"files", "paths"} and isinstance(value, list)
                               and all(isinstance(item, str) for item in value)) for key, value in args.items()))


def complete_approval_draft(value: object) -> bool:
    """Source completeness, independent of what fits on a particular approval surface."""
    return (isinstance(value, Mapping) and isinstance(value.get("text"), str) and bool(value["text"])
            and value.get("truncated", False) is False and all(
                key == "truncated" or key in DRAFT_TEXT_FIELDS and isinstance(item, str)
                for key, item in value.items()))


def draft_approval_choices(request: Mapping[str, object]) -> list[str]:
    """One exact human answer, or only refusal when this copy can't disclose the send."""
    offered = request.get("choices")
    if (request.get("risk") != "secret" and complete_approval_draft(request.get("draft"))
            and (offered is None or isinstance(offered, (list, tuple)) and "once" in offered)):
        return ["once", "deny"]
    return ["deny"]


def draft_from_arguments(args: Mapping[str, Any], *, required: bool = False) -> dict[str, str | bool]:
    """Project actual arguments, never an apparently complete subset of a message.

    Do not expose arbitrary fields/structures in the card or guess which of two aliases a server uses.
    Non-message operations with no message fields have no draft. A required but opaque message is deny-only.
    """
    draft: dict[str, str | bool] = {}
    seen: set[str] = set()
    omitted = False
    for key, item in args.items():
        field = _DRAFT_ALIASES.get(key.lower())
        if field is None or field in seen:
            omitted = True
            continue
        seen.add(field)
        if field in {"to", "cc", "bcc"} and isinstance(item, (list, tuple)):
            # Simple address lists keep their familiar display. Never stringify a nested mailbox or
            # invent list boundaries for an entry which itself contains separators.
            if all(isinstance(address, str) and address.strip() and not any(c in address for c in ",;\r\n")
                   for address in item):
                item = ", ".join(item)
        if isinstance(item, str):
            draft[field] = item
        else:
            omitted = True
    if not required and not seen:
        return {}
    if omitted or not draft.get("text"):
        draft["truncated"] = True
    return draft
