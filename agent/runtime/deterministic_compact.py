"""Cheapest-first deterministic compaction: drop completed tool steps.

Runs before the expensive LLM summary when a conversation is rebuilt to fit
a budget (a subagent's replay); a conversation in use is only summarized. A
completed step is an assistant tool-call message followed by its tool
results; when every result succeeded and every tool is a read/network
tool (no side effects), the whole step collapses into a one-line marker
and the assistant's prose is kept. Side-effecting steps and the current
turn are never touched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable


_ERROR_RE = re.compile(
    r"(?:\bstatus\s*[:=]\s*(?:error|failed)|\berror\s*[:=]|traceback|exception|"
    r"permission denied|approval required|exit code\s*[1-9]|\[ToolInputError\]|"
    r"\[ToolDisabled\]|\[SandboxDenied\]|\[ApprovalDenied\])",
    re.IGNORECASE,
)

# Tool risks that are safe to collapse into a marker: no durable side effect.
_DROPPABLE_RISKS = frozenset({"read", "network"})


@dataclass(frozen=True)
class DropStats:
    dropped_steps: int = 0
    saved_chars: int = 0


def _latest_real_user(messages: list[dict]) -> int:
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.get("role") != "user":
            continue
        if message.get("provenance") in {"notification", "delegate", "synthetic"}:
            continue
        if str(message.get("content") or "").startswith("[SYSTEM-SUPPLIED"):
            continue
        return index
    return -1


def _call_names(assistant: dict) -> list[str]:
    names: list[str] = []
    for call in assistant.get("tool_calls") or []:
        function = call.get("function") if isinstance(call, dict) else None
        name = (
            function.get("name") if isinstance(function, dict)
            else call.get("name") if isinstance(call, dict)
            else ""
        )
        if name:
            names.append(str(name))
    return names


def _result_succeeded(tool_message: dict) -> bool:
    return not _ERROR_RE.search(str(tool_message.get("content") or ""))


def drop_completed_steps(
    messages: list[dict],
    *,
    tool_risk: Callable[[str], str] | None = None,
    tool_request_local: Callable[[str], bool] | None = None,
    keep_recent: int = 4,
) -> tuple[list[dict], DropStats]:
    """Collapse completed read/network tool steps before the latest user turn.

    ``keep_recent`` preserves the trailing tool chain (assistant + tool
    messages) so an active multi-step sequence — e.g. computer snapshot →
    act, where the snapshot result is the next action's input — is never
    compacted mid-flight. Request-local tools (``tool_request_local``)
    are never collapsed: their results are not persisted anywhere, so
    dropping them loses the information permanently.
    """
    latest_user = _latest_real_user(messages)
    if latest_user <= 0:
        return messages, DropStats()

    risk = tool_risk or (lambda name: "read")
    is_request_local = tool_request_local or (lambda name: False)

    # Count droppable completed steps first so the trailing ``keep_recent``
    # steps can be preserved: an active multi-step tool chain (e.g. computer
    # snapshot → act, where the snapshot result feeds the next action) must
    # never be compacted mid-flight.
    def _step_is_completed(index: int) -> bool:
        message = messages[index]
        if (
            index >= latest_user
            or message.get("role") != "assistant"
            or not message.get("tool_calls")
        ):
            return False
        call_ids = {tc.get("id") for tc in message.get("tool_calls") or [] if tc.get("id")}
        j = index + 1
        tool_messages: list[dict] = []
        while j < len(messages) and messages[j].get("role") == "tool":
            tool_messages.append(messages[j])
            j += 1
        found = {str(t.get("tool_call_id") or "") for t in tool_messages}
        return bool(call_ids) and found == call_ids and len(tool_messages) == len(call_ids)

    completed_indexes = [
        index for index in range(len(messages)) if _step_is_completed(index)
    ]
    protected_from = max(0, len(completed_indexes) - keep_recent)
    protected_step_indexes = set(completed_indexes[protected_from:])

    prepared: list[dict] = []
    dropped = 0
    saved = 0
    i = 0
    while i < len(messages):
        message = messages[i]
        is_completed_step = (
            i < latest_user
            and message.get("role") == "assistant"
            and bool(message.get("tool_calls"))
            and i not in protected_step_indexes
        )
        if not is_completed_step:
            prepared.append(message)
            i += 1
            continue

        call_ids = {tc.get("id") for tc in message.get("tool_calls") or [] if tc.get("id")}
        j = i + 1
        tool_messages: list[dict] = []
        while j < len(messages) and messages[j].get("role") == "tool":
            tool_messages.append(messages[j])
            j += 1
        found = {str(t.get("tool_call_id") or "") for t in tool_messages}
        complete = bool(call_ids) and found == call_ids and len(tool_messages) == len(call_ids)
        names = _call_names(message)
        droppable = (
            complete
            and all(_result_succeeded(t) for t in tool_messages)
            and all(risk(name) in _DROPPABLE_RISKS for name in names)
            and not any(is_request_local(name) for name in names)
        )
        if not droppable:
            prepared.append(message)
            i += 1
            continue

        prose = str(message.get("content") or "").strip()
        marker = f"[Completed tool step: {', '.join(names)} ({len(names)} call(s))]"
        content = f"{prose}\n{marker}" if prose else marker
        prepared.append({"role": "assistant", "content": content})
        saved += sum(len(str(t.get("content") or "")) for t in tool_messages)
        dropped += 1
        i = j
    return prepared, DropStats(dropped_steps=dropped, saved_chars=saved)
