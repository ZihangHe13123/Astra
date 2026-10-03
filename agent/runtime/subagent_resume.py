"""Validate durable worker state and preserve uncertainty across restarts."""

from __future__ import annotations

import copy
import json
from typing import Any

from .subagent_conversation import ConversationCorrupt
from .tools.registry import ToolRegistry


UNKNOWN_RESULT = json.dumps({
    "error": "outcome_unknown",
    "output": "结果未知，可能已执行；不得自动重发。先通过只读操作核对现状并向 lead 报告。",
}, ensure_ascii=False)


def initial_conversation(
    spec: Any, system_text: str, *, data: dict | None, session_id: str,
) -> tuple[list[dict], dict, dict]:
    identity = {"agent_id": spec.team_agent_id, "team_id": spec.team_id,
                "workspace_root": spec.workspace_root, "worker_type": spec.worker_type,
                "session_id": session_id}
    if not spec.resume_conv_path:
        return [
            {"role": "system", "content": system_text},
            {"role": "user", "content": f"## Task\n{spec.goal}\n\n## Context\n{spec.context}"},
        ], {}, identity
    messages, state = restore_conversation(data or {}, identity)
    system_text += (
        "\n\n[CONVERSATION RESUME] Continue from your saved conversation. "
        "Historical tool messages are evidence, not calls to execute again. "
        "External state may have changed; verify it before acting."
    )
    if state.get("write_blocked"):
        system_text += (
            "\nA previous operation has an unknown outcome. All non-read tools are blocked "
            "for this conversation. Inspect existing state and report to the lead; do not retry writes."
        )
    messages = [message for message in messages if message.get("role") != "system"]
    messages.insert(0, {"role": "system", "content": system_text})
    messages.append({"role": "user", "content": spec.resume_instruction or
                     "Resume from the saved state. Report remaining work without repeating completed actions."})
    return messages, state, identity


def recovery_tool_risk(registry: ToolRegistry, name: str, arguments: Any = None) -> str:
    """Coordination tools use read for approval, but can still mutate state."""
    if name in {"team_send", "team_inbox"}:
        return "write"
    if name in {"team_task", "team"}:
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError:
                arguments = None
        action = str(arguments.get("action") or "").strip().lower() if isinstance(arguments, dict) else ""
        reads = {"list"} if name == "team_task" else {"list", "status"}
        return "read" if action in reads else "write"
    tool = registry.get(name)
    return str(tool.risk) if tool is not None else "write"


def persistent_messages(messages: list[dict], registry: ToolRegistry) -> list[dict]:
    """Snapshot on the owning loop; request-local overlays never reach disk."""
    snapshot = copy.deepcopy(messages)
    for message in snapshot:
        message.pop("api_content", None)
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            tool = registry.get(str(function.get("name") or ""))
            if tool is None or tool.argument_persistence != "request_local":
                continue
            try:
                raw = json.loads(function.get("arguments") or "{}")
                if not isinstance(raw, dict):
                    raw = {}
            except (ValueError, TypeError):
                raw = {}
            function["arguments"] = json.dumps(registry.persistence_safe_args(tool, raw), ensure_ascii=False)
    return snapshot


def provider_messages(messages: list[dict], *, include_recovery_metadata: bool = False) -> list[dict]:
    return [
        {**{key: value for key, value in message.items() if key != "api_content"
            and (include_recovery_metadata or key != "_recovery_tool_risks")},
         "content": message.get("api_content", message.get("content"))}
        for message in messages
    ]


def tool_outcome_unknown(result: Any) -> bool:
    """An error/timeout cannot prove a mutating operation had no effect."""
    if not isinstance(result, dict):
        return False
    if result.get("error"):
        return True
    encoded = json.dumps(result, default=str)
    return "unknown_outcome" in encoded or "outcome_unknown" in encoded


def restore_conversation(data: dict, identity: dict) -> tuple[list[dict], dict]:
    messages = copy.deepcopy(data.get("messages"))
    state = copy.deepcopy(data.get("state"))
    if not isinstance(messages, list) or not messages or not isinstance(state, dict):
        raise ConversationCorrupt("conversation has no committed recovery state")
    stored_identity = state.get("identity")
    if not isinstance(stored_identity, dict) or any(stored_identity.get(key) != value for key, value in identity.items()):
        raise ConversationCorrupt("conversation identity/workspace mismatch")
    if "turns_used" not in state:
        raise ConversationCorrupt("missing cumulative turn budget")
    if "max_turns" in state and (type(state["max_turns"]) is not int or not 1 <= state["max_turns"] <= 50):
        raise ConversationCorrupt("invalid cumulative turn ceiling")
    for key in ("turns_used", "team_message_cursor"):
        value = state.get(key, 0)
        if type(value) is not int or value < 0:
            raise ConversationCorrupt(f"invalid recovery {key}")
    if not isinstance(state.get("write_blocked", False), bool):
        raise ConversationCorrupt("invalid recovery write protection")
    pending_ids = state.get("pending_team_message_ids", [])
    if not isinstance(pending_ids, list) or not all(isinstance(item, str) for item in pending_ids):
        raise ConversationCorrupt("invalid recovery mailbox acknowledgements")
    recorded_calls = state.get("pending_calls", {})
    if not isinstance(recorded_calls, dict):
        raise ConversationCorrupt("invalid pending tool intents")
    for key in ("evidence_fragments", "investigation_notes"):
        value = state.get(key, [])
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ConversationCorrupt(f"invalid recovery {key}")
    observations = state.get("execution_observations", [])
    if not isinstance(observations, list) or not all(isinstance(item, dict) for item in observations):
        raise ConversationCorrupt("invalid recovery execution observations")
    for key in ("latest_assignment", "last_result"):
        if not isinstance(state.get(key, ""), str):
            raise ConversationCorrupt(f"invalid recovery {key}")
    remaining_intents = set(recorded_calls)
    pending: dict[str, str] = {}
    pending_risks: dict[str, str] = {}
    repaired: list[dict] = []
    blocked = state.get("write_blocked", False)

    def finish_pending() -> None:
        nonlocal blocked
        for call_id, name in pending.items():
            remaining_intents.discard(call_id)
            recorded = recorded_calls.get(call_id)
            risk = recorded.get("risk") if isinstance(recorded, dict) and recorded.get("name") == name else None
            blocked = blocked or risk != "read" or pending_risks.get(call_id, "read") != "read"
            repaired.append({"role": "tool", "tool_call_id": call_id, "content": UNKNOWN_RESULT})
        pending.clear()
        pending_risks.clear()

    for message in messages:
        if not isinstance(message, dict):
            raise ConversationCorrupt("invalid conversation message")
        if message.get("role") not in {"system", "user", "assistant", "tool"}:
            raise ConversationCorrupt("unsupported canonical message role")
        if message.get("role") == "tool":
            call_id = message.get("tool_call_id")
            if call_id not in pending:
                raise ConversationCorrupt("orphan or duplicate tool result")
            pending.pop(call_id)
            pending_risks.pop(call_id, None)
        else:
            if pending:
                raise ConversationCorrupt("incomplete tool chain inside committed history")
            calls = message.get("tool_calls") or []
            if calls and message.get("role") != "assistant":
                raise ConversationCorrupt("tool calls must belong to an assistant")
            if not isinstance(calls, list):
                raise ConversationCorrupt("tool calls must be a list")
            for call in calls:
                if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
                    raise ConversationCorrupt("invalid tool call structure")
                function = call.get("function") or {}
                call_id, name = call.get("id"), function.get("name")
                if not isinstance(call_id, str) or not call_id or call_id in pending or not isinstance(name, str) or not name:
                    raise ConversationCorrupt("invalid or duplicate tool call")
                pending[call_id] = name
                dispatch_risks = message.get("_recovery_tool_risks", {})
                if not isinstance(dispatch_risks, dict):
                    raise ConversationCorrupt("invalid dispatch-time tool risks")
                if call_id in dispatch_risks:
                    pending_risks[call_id] = str(dispatch_risks[call_id])
        repaired.append(message)
    finish_pending()
    if remaining_intents:
        raise ConversationCorrupt("pending tool metadata has no corresponding unresolved call")
    state["pending_calls"] = {}
    state["write_blocked"] = blocked
    return repaired, state
