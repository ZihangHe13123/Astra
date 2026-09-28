"""Which conversation messages count as original evidence for a saved skill.

Retrieved history and learning or skill tools never count as new proof; only real
user turns and tool observations do. This module never executes a command or calls
a model.
"""

from __future__ import annotations

from .context_compressor import _is_synthetic_user_turn

_NON_EVIDENCE = frozenset({
    "context_inspect", "context_open", "session_search", "session_read",
    "session_recall", "memory", "memory_search", "memory_fetch", "activity_search",
    "activity_read", "skills_list", "skill_view", "skill_manage", "learning",
    "learning_search", "project_verifier_init", "conclave", "delegate", "skill_review_snapshot", "skill_review_apply",
    "run_code", "process_read",  # Inner tools / process_poll provide authoritative outcomes.
})


def is_evidence_tool(name: str) -> bool:
    return bool(name) and name not in _NON_EVIDENCE and not name.startswith(
        ("learning_", "session_", "activity_", "context_", "memory_")
    )


def evidence_messages(messages: list[dict]) -> list[dict]:
    """Keep original user/tool sources; never count retrieved text as new proof."""
    names: dict[str, str] = {}
    result = []
    for message in messages:
        for call in message.get("tool_calls") or []:
            if isinstance(call, dict):
                names[str(call.get("id", ""))] = str((call.get("function") or {}).get("name", ""))
        role = message.get("role")
        if role == "user" and not _is_synthetic_user_turn(message):
            result.append(message)
        elif role == "tool":
            name = str(message.get("name") or names.get(str(message.get("tool_call_id", "")), ""))
            # Legacy transcripts have no tool name; keep them as provenance only.
            if not name or is_evidence_tool(name):
                result.append(message)
    return result
