"""Which actions have consequences outside Astra: paying, sending, deleting, publishing.

An approval request marked ``consequential`` is decided per call: its card offers only "once" and "deny",
and no earlier task, session or standing grant covers it (tools/registry.py). The categories:

- ``pay``: money leaves (buying, checking out, transferring).
- ``send``: something goes to other people or another place (sending, submitting, replying, uploading).
- ``delete``: data goes away (deleting, discarding changes, cancelling an account or order).
- ``publish``: something becomes public or shared (publishing, posting, pushing, releasing, merging).
"""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
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

# Action names, not words to search for anywhere in a tool name (`list_posts` is not `post`).
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
    return [word.lower() for word in re.findall(r"[A-Z]+(?=[A-Z][a-z]|[^A-Za-z]|$)|[A-Z]?[a-z]+|[A-Z]+|[0-9]+", name)]


_MESSAGE_RESOURCES = frozenset("message messages mail email emails post posts tweet tweets reply replies comment comments notification notifications".split())
_ARTIFACT_RESOURCES = frozenset("file files asset assets artifact artifacts release releases deployment deployments merge merges upload uploads".split())
_READ_ACTIONS = frozenset("get list read find lookup search query fetch retrieve preview inspect describe count check".split())
_CREATE_ACTIONS = {"add", "create", "write", "update", "edit"}
_ARTIFACT_ACTIONS = {"upload", "release", "deploy", "merge", "push"}
_ACTIONS = _READ_ACTIONS | _CREATE_ACTIONS | set().union(*_VERBS.values())
_HTTP = r"GET|HEAD|OPTIONS|POST|PUT|PATCH|DELETE"


@dataclass(frozen=True)
class ToolConsequence:
    category: str = ""
    artifact: bool = False
    # None for RPC-style operations; an HTTP route can have zero or more explicit bindings.
    path_names: frozenset[str] | None = None

    @property
    def sends_content(self) -> bool:
        return self.category in {"send", "publish"}


def _http_operation(method: str, resource: str) -> ToolConsequence:
    method = method.lower()
    path_names = frozenset(braced or colon for braced, colon in re.findall(
        r"\{([\w-]+)\}|(?:^|/):([\w-]+)", resource))
    leaves = [p for p in re.split(r"[/.]", resource) if p and not p.startswith(("{", ":")) and not p.isdecimal()]
    words = _name_words(leaves[-1]) if leaves else []
    if method in {"get", "head", "options"} or not words or words[0] in _READ_ACTIONS:
        return ToolConsequence()
    if method == "delete":
        return ToolConsequence("delete")
    obj = words[-1]  # /email-drafts is a draft resource, unlike /drafts/{id}/send.
    if obj in _ARTIFACT_RESOURCES | _ARTIFACT_ACTIONS:
        return ToolConsequence("send" if obj in {"file", "files", "upload", "uploads"} else "publish", True, path_names)
    if obj in _MESSAGE_RESOURCES or obj in _VERBS["send"] | {"post", "publish"}:
        return ToolConsequence("publish" if obj in {"post", "posts", "tweet", "tweets", "publish"} else "send", path_names=path_names)
    return ToolConsequence()  # HTTP POST alone is not evidence of a message, or of read-only access.


def classify_mcp_operation(name: str, server: str = "") -> ToolConsequence:
    """Classify the original remote operation once, before aliasing. Never infer read *permission*.

    HTTP method + endpoint and qualified operation IDs are two different naming conventions.
    For HTTP, the final resource/action matters, not an arbitrary ancestor in the route.
    For function names, use the qualified action (`chat.postMessage`, `get.user`, `send_email`).
    """
    name = re.sub(r"(?:[./]v\d+)+$", "", name.strip(), flags=re.I)
    prefix = re.fullmatch(rf"({_HTTP})\s+(/?.+)", name, re.I)
    suffix = re.fullmatch(rf"(.+)\.({_HTTP})", name, re.I)
    if prefix:
        return _http_operation(prefix[1], prefix[2])
    if suffix:
        return _http_operation(suffix[2], suffix[1])

    parts = [_name_words(p) for p in re.split(r"[./:]", name) if p]
    if not parts or not parts[-1]:
        return ToolConsequence()
    words = parts[-1] if parts[-1][0] in _ACTIONS else parts[0]
    if not words:
        return ToolConsequence()
    namespace = _name_words(server)
    if (len(parts) == 1 and words[0] not in _ACTIONS and namespace and words[:len(namespace)] == namespace
            and len(words) > len(namespace)):
        words = words[len(namespace):]
    verb = words[0]
    if verb in _READ_ACTIONS:
        return ToolConsequence()
    # The direct object matters. Updating a message's *status*, or creating a reaction
    # under a comments namespace, is not posting a message. In `comments.create` the
    # object comes from the nearest qualifying resource, not every ancestor.
    obj = words[1:] or (parts[-2] if len(parts) > 1 and words is parts[-1] else [])
    obj = obj[:next((i for i, w in enumerate(obj) if w in {"to", "on", "for", "with", "from", "by", "in"}), len(obj))]
    objects = set(obj)
    if verb in _CREATE_ACTIONS:
        if not obj or obj[-1] not in _MESSAGE_RESOURCES:
            return ToolConsequence()
        return ToolConsequence("publish" if objects & {"post", "posts", "tweet", "tweets"} else "send")
    category = next((k for k, verbs in _VERBS.items() if verb in verbs), "")
    artifact = verb in _ARTIFACT_ACTIONS or verb in {"send", "publish"} and bool(objects & _ARTIFACT_RESOURCES)
    return ToolConsequence(category, artifact and not bool(objects & _MESSAGE_RESOURCES))


def tool_category(name: str) -> str:
    return classify_mcp_operation(name).category


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
DRAFT_TEXT_FIELDS = ("to", "cc", "bcc", "destination", "subject", "text", "via")


# Artifact parameters identify a resource or control how to act on it. A number can be a PR
# number here; it is not a phone number or a body. Never coerce a nested envelope into a ref.
_REF_NOUNS = set("file files folder directory bucket container storage object asset artifact release repo repository project pull pr revision branch remote environment target upload deployment".split())
_REF_SUFFIXES = {"id", "uri", "url", "path", "name", "ref", "number", "slug", "sha", "oid"}
_REF_KEYS = _REF_SUFFIXES | _REF_NOUNS | {"pull_request", "owner", "tag", "version", "head", "base", "path", "paths"}
_REF_PARTS = _REF_NOUNS | {"request", "owner", "head", "base", "source", "expected", "destination", "full", "commit"}
_OPTIONS = {"merge_method", "method", "strategy", "content_type", "mime_type", "media_type", "filename"}
_DESTINATIONS = set("owner repo repository project workspace team channel chat thread room conversation comment reply pull pr issue discussion parent message post".split())


def _binding(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) or type(value) is int


def _artifact_inputs(fields: list[tuple[str, Any, str]]) -> bool:
    seen: set[str] = set()
    for key, value, _ in fields:
        words = _name_words(key)
        normalized = "_".join(words)
        if normalized in seen:
            return False
        seen.add(normalized)
        # folder_id, fileUri and expected_head_sha are artifact references; body_ref
        # or to_address_id are not. A suffix alone cannot establish the role.
        ref = normalized in _REF_KEYS or (len(words) > 1 and words[-1] in _REF_SUFFIXES and set(words[:-1]) <= _REF_PARTS)
        if ref and _binding(value) or normalized in _OPTIONS and isinstance(value, str):
            continue
        if normalized in {"paths", "files", "file_paths", "file_uris", "file_ids"} and isinstance(value, list) and all(map(_binding, value)):
            continue
        if type(value) is bool and normalized in {"overwrite", "draft", "prerelease", "dry_run", "delete_branch", "force"}:
            continue
        return False
    return True


def _valid_destination(raw: object) -> bool:
    if not isinstance(raw, str):
        return False
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        return False
    return isinstance(data, dict) and bool(data) and all(k.strip() and _binding(v) for k, v in data.items())


def complete_approval_draft(value: object) -> bool:
    """Source completeness, independent of what fits on a particular approval surface."""
    return (isinstance(value, Mapping) and isinstance(value.get("text"), str) and bool(value["text"])
            and ("destination" not in value or _valid_destination(value["destination"]))
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


def _operation_fields(args: Mapping[str, Any], http: bool) -> list[tuple[str, Any, str]]:
    """One argument view for artifact and message decisions; retain field location and every unknown sibling."""
    fields: list[tuple[str, Any, str]] = []
    wrappers: set[str] = set()
    for key, value in args.items():
        group = ("path" if key in {"path", "path_params", "pathParams"} else
                 "body" if key in {"requestBody", "request_body", "body"} else None)
        if http and group and group not in wrappers and isinstance(value, Mapping) and value:
            fields.extend((k, v, group) for k, v in value.items())
            wrappers.add(group)
        else:
            fields.append((key, value, "flat"))
    return fields


def _project_draft(fields: list[tuple[str, Any, str]], *, required: bool, path_names: frozenset[str]) -> dict[str, str | bool]:
    draft: dict[str, str | bool] = {}
    seen: set[str] = set()
    destination: dict[str, str | int] = {}
    binding_names: set[str] = set()
    omitted = False
    for key, item, location in fields:
        words = _name_words(key)
        field = _DRAFT_ALIASES.get(key.lower())
        # For example owner/repo/pull_number/comment_id. Only identify known scalar targets.
        # An arbitrary field called "credentials", even inside `path`, must not be published.
        is_route_key = bool(words) and (words[0] in _DESTINATIONS and (len(words) == 1 or words[-1] in _REF_SUFFIXES)
                                       or key in {"id", "number"})
        explicit = key in path_names and location != "body"
        if explicit or (location == "path" or field is None) and is_route_key:
            canonical = "_".join(words)
            private = set(words) & {"key", "token", "secret", "auth", "credential", "password", "cookie", "signature"}
            if private or canonical in binding_names or not _binding(item):
                omitted = True
            else:
                destination[key] = item
                binding_names.add(canonical)
            continue
        if location == "path":
            omitted = True
            continue
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
    if destination:
        draft["destination"] = json.dumps(destination, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if not required and not seen:
        return {}
    if omitted or not draft.get("text"):
        draft["truncated"] = True
    return draft


def draft_from_arguments(args: Mapping[str, Any], *, required: bool = False) -> dict[str, str | bool]:
    """Project a conventional flat call without guessing at unknown or duplicated message fields."""
    return _project_draft(_operation_fields(args, False), required=required, path_names=frozenset())


def mcp_draft(operation: ToolConsequence, args: Mapping[str, Any], *, secret: bool = False) -> dict[str, str | bool]:
    if not operation.sends_content:
        return {}
    if secret:
        return {"truncated": True}
    fields = _operation_fields(args, operation.path_names is not None)
    if operation.artifact and _artifact_inputs(fields):
        return {}  # Ordinary consequential approval; actual artifact arguments remain on its card.
    return _project_draft(fields, required=True, path_names=operation.path_names or frozenset())
