"""Who has already allowed what: the approvals that outlast one call.

A tool's own grant handler only ever applies a one-call grant. Everything broader lives here, keyed by what
the approval request names (its scope, a shell command prefix, a directory), so it can be scoped and taken
back in one place:

- ``task``: until the work started by the person's current message ends (its task run).
- ``session``: until this conversation ends. Each conversation has its own space: the desktop session and
  every WeChat or QQ chat, although they share one backend, never see each other's grants.
- ``always``: a rule the person added from an approval card; kept in settings.json until they remove it.

A consequential request (sending, paying, deleting, publishing) is never covered by any of these.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DECISIONS = ("once", "task", "session", "always", "deny")
# The conversation whose grants apply to the code running now.
_SPACE: contextvars.ContextVar[str] = contextvars.ContextVar("astra_grant_space", default="main")
MAX_RULES = 200


@contextmanager
def grant_space(name: str) -> Iterator[None]:
    """Run a conversation's turn with its own session grants."""
    token = _SPACE.set(name or "main")
    try:
        yield
    finally:
        _SPACE.reset(token)


def enter_grant_space(name: str) -> contextvars.Token[str]:
    """Start a conversation's grants for the code that runs from here (``leave_grant_space`` ends it)."""
    return _SPACE.set(name or "main")


def leave_grant_space(token: contextvars.Token[str]) -> None:
    _SPACE.reset(token)


def current_grant_space() -> str:
    return _SPACE.get()


def _digest(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


def _filesystem_keys(request: Mapping[str, Any]) -> list[str]:
    """A directory grant covers what is under it, and a write grant covers reading."""
    target = str(request.get("target") or "")
    access = str(request.get("access") or "read")
    if not target:
        return []
    accesses = ("write",) if access == "write" else ("write", "read")
    path = Path(os.path.normcase(target))
    return [f"dir:{kind}:{folder}" for folder in (path, *path.parents) for kind in accesses]


def request_keys(tool_name: str, request: Mapping[str, Any] | None, policy_scope: str = "") -> list[str]:
    """Every key an earlier grant could have been recorded under for this request."""
    if request is None:
        return [f"policy:{policy_scope}"] if policy_scope else []
    keys: list[str] = []
    scopes = request.get("scopes")
    if isinstance(scopes, list) and scopes:
        keys.append("scopes:" + "\n".join(sorted(str(scope) for scope in scopes)))
    elif request.get("scope"):
        keys.append(f"scope:{request['scope']}")
    else:
        keys.append("target:" + "|".join([tool_name, *(str(request.get(name) or "")
                                                        for name in ("kind", "target", "access", "operation"))]))
    if request.get("session_scope"):
        keys.append(f"scope:{request['session_scope']}")
    if request.get("kind") == "filesystem":
        keys.extend(_filesystem_keys(request))
    return keys


def granted_keys(tool_name: str, request: Mapping[str, Any] | None, policy_scope: str = "") -> list[str]:
    """What a broader approval of this request records: the request itself, plus the command prefix or
    the directory the card offered to cover."""
    keys = request_keys(tool_name, request, policy_scope)[:1]
    if request is not None:
        if request.get("session_scope"):
            keys.append(f"scope:{request['session_scope']}")
        if request.get("kind") == "filesystem" and request.get("scope_kind") == "directory":
            keys.append(f"dir:{request.get('access') or 'read'}:{os.path.normcase(str(request.get('target') or ''))}")
    return keys


def rule_key(tool_name: str, request: Mapping[str, Any] | None, policy_scope: str = "") -> str:
    """The key repeated approvals are counted under, and an "always" rule is kept as (a digest)."""
    keys = granted_keys(tool_name, request, policy_scope)
    return _digest(keys[-1]) if keys else ""


class AllowRules:
    """The person's "以后都允许" rules, in settings.json; read again whenever the file changes."""

    def __init__(self, path: Path):
        self.path = path
        self._stamp: tuple[int, int] | None = None
        self._rules: list[dict[str, Any]] = []

    def all(self) -> list[dict[str, Any]]:
        try:
            stat = self.path.stat()
            stamp = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            self._stamp, self._rules = None, []
            return []
        if stamp != self._stamp:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            raw = data.get("approval_rules") if isinstance(data, dict) else None
            self._rules = [rule for rule in raw if isinstance(rule, dict) and isinstance(rule.get("key"), str)] \
                if isinstance(raw, list) else []
            self._stamp = stamp
        return list(self._rules)

    def digests(self) -> set[str]:
        return {rule["key"] for rule in self.all()}

    def add(self, key: str, *, label: str, tool: str) -> None:
        from agent.runtime.json_preferences import update_preferences

        def change(data: dict) -> None:
            rules = [rule for rule in data.get("approval_rules") or [] if isinstance(rule, dict) and rule.get("key") != key]
            rules.append({"key": key, "label": label[:300], "tool": tool,
                          "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
            data["approval_rules"] = rules[-MAX_RULES:]

        update_preferences(self.path, change)
        self._stamp = None

    def remove(self, key: str) -> bool:
        from agent.runtime.json_preferences import update_preferences

        found = False

        def change(data: dict) -> None:
            nonlocal found
            rules = [rule for rule in data.get("approval_rules") or [] if isinstance(rule, dict)]
            kept = [rule for rule in rules if rule.get("key") != key]
            found = len(kept) != len(rules)
            data["approval_rules"] = kept

        update_preferences(self.path, change)
        self._stamp = None
        return found


class GrantLedger:
    """Task, session and standing grants, shared by a registry and the delegates that act for it."""

    def __init__(self) -> None:
        self.sessions: dict[str, set[str]] = {}
        self.tasks: dict[str, set[str]] = {}
        self.rules: AllowRules | None = None
        self._task_order: list[str] = []

    def covering(self, keys: list[str], task_id: str = "") -> str:
        """Which standing grant covers one of these keys: "always", "session", "task", or ""."""
        if not keys:
            return ""
        if self.rules is not None and {_digest(key) for key in keys} & self.rules.digests():
            return "always"
        if set(keys) & self.sessions.get(current_grant_space(), set()):
            return "session"
        if task_id and set(keys) & self.tasks.get(task_id, set()):
            return "task"
        return ""

    def remember(self, decision: str, keys: list[str], *, task_id: str = "", rule: str = "", label: str = "",
                 tool: str = "") -> None:
        if decision == "task" and task_id:
            if task_id not in self.tasks:
                self._task_order.append(task_id)
                # Task ids are never reused; keep only the recent ones.
                for stale in self._task_order[:-64]:
                    self.tasks.pop(stale, None)
                self._task_order = self._task_order[-64:]
            self.tasks.setdefault(task_id, set()).update(keys)
        elif decision == "always" and rule and self.rules is not None:
            # Kept only as the rule, so removing it on the desktop takes effect at once, here too.
            self.rules.add(rule, label=label, tool=tool)
        elif decision in {"session", "always"}:
            self.sessions.setdefault(current_grant_space(), set()).update(keys)

    def end_session(self, space: str | None = None) -> None:
        """The conversation ended: its session and task grants go. Standing rules stay."""
        self.sessions.pop(space or current_grant_space(), None)
        if (space or current_grant_space()) == "main":
            self.tasks.clear()
            self._task_order.clear()

    def end_task(self, task_id: str) -> None:
        self.tasks.pop(task_id, None)
