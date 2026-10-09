"""Tool access to bounded core Markdown memory and session working memory."""

from typing import Callable

from ..memory import CONVERSATION_FIELDS, CORE_SCOPES, WORKING_FIELDS, MemoryStore
from ..tool_failure import ToolFailure
from .registry import ToolDef, ToolRegistry

# Still stored for old clients, but no later turn shows them to the model.
_UNSHOWN_FIELDS = ("goal", "plan", "progress", "artifacts")


def _invalid(message: str, hint: str) -> ToolFailure:
    return ToolFailure(code="invalid_arguments", message=message, retryable=False, recovery_hint=hint)


def register_memory_tools(
    registry: ToolRegistry,
    store: MemoryStore,
    *,
    session_id: Callable[[], str],
) -> None:
    async def _memory(
        action: str,
        scope: str = "memory",
        content: str = "",
        memory_id: str = "",
        field: str = "",
        value: str = "",
        steps: list[str] | None = None,
        step_index: int = 0,
        step_status: str = "pending",
    ) -> str | ToolFailure:
        current_session = session_id() or "default"
        if action == "status":
            return store.status(current_session)
        if action == "core_add":
            if scope not in CORE_SCOPES:
                return _invalid(
                    f"scope must be one of: {', '.join(CORE_SCOPES)}",
                    "Use scope=user for the user's profile and preferences, scope=memory for everything else.",
                )
            if not content.strip():
                return _invalid(
                    "core_add needs the entry text in content"
                    + (" (value is only read by working_update)." if value.strip() else "."),
                    "Call core_add again with the text in content.",
                )
            item = store.add_core(scope, content)
            label = "USER.md" if scope == "user" else "MEMORY.md"
            return f"Stored core {label} memory #{item['id']}: {item['content']}"
        if action == "core_remove":
            if not memory_id.strip():
                return _invalid(
                    "memory_id is required for core_remove",
                    "Call memory with action=status to list the entries with their ids, then pass one id.",
                )
            if store.remove_core(memory_id):
                return f"Removed core memory #{memory_id}."
            return ToolFailure(
                code="memory_not_found",
                message=f"Core memory #{memory_id} was not found or is not a unique match.",
                retryable=False,
                recovery_hint="Call memory with action=status to list the current entries, then pass one full id.",
            )
        if action == "core_import":
            result = store.import_core_markdown()
            return f"Validated Core Markdown hard limits; total={result['total']} entries."
        if action == "working_update":
            if field not in WORKING_FIELDS or not (value or content).strip():
                return _invalid(
                    "working_update needs field and its new text in value.",
                    f"Pass field as one of {', '.join(CONVERSATION_FIELDS)} and the text in value.",
                )
            data = store.update_working(current_session, field, value or content)
            note = (
                f" Note: {field} is a legacy field; it is stored but not shown to you in later turns."
                if field in _UNSHOWN_FIELDS else ""
            )
            return f"Updated working memory for {current_session}: {field}={data[field]}{note}"
        if action == "working_plan_set":
            data = store.set_working_plan(current_session, steps or [], goal=content)
            return f"Set working plan for {current_session}: {len(data['steps'])} steps"
        if action == "working_step_update":
            data = store.update_working_step(current_session, step_index, step_status)
            completed = sum(item["status"] == "completed" for item in data["steps"])
            return f"Updated working plan for {current_session}: {completed}/{len(data['steps'])} completed"
        if action == "working_clear":
            store.clear_working(current_session)
            return f"Cleared working memory for session {current_session}."
        return _invalid(f"Unknown memory action: {action}", "Use one of the actions listed in the tool schema.")

    registry.register(ToolDef(
        name="memory",
        description=(
            "Manage small persistent memory. Execution goals, plans, progress, artifacts, and step status are maintained automatically "
            "by the Case/TaskRun journal; do not mirror them into working memory. Use working_update only for temporary_constraints, "
            "open_questions, assumptions, or turn_notes that are useful during the conversation but are not durable facts; "
            "it replaces the field's whole text, so include what should stay. "
            "working_plan_set and working_step_update exist only for backward compatibility. Proactively consult session_search for "
            "past conversations and saved observations when relevant; Hindsight recall is an optional additional source. "
            "These archives are historical evidence, not pinned facts. Reserve core_add for stable identity, environment, or agent decisions the user "
            "explicitly wants pinned into the small always-visible block. Put user profile and "
            "preferences in USER.md (scope=user); put all other durable facts in MEMORY.md (scope=memory). Never store secrets, credentials, "
            "private reasoning, guesses, or transient search results. Use core_remove only when the user explicitly asks to forget an item, "
            "or when a core file is full and an existing entry is clearly obsolete, duplicated, or superseded. Call status before capacity "
            "cleanup, delete by memory_id, and preserve identity, safety boundaries, and still-current user preferences. MEMORY.md and "
            "USER.md are the direct source of truth with hard-coded limits. Use core_import only to validate external Markdown edits."
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["status", "core_add", "core_remove", "core_import", "working_update", "working_plan_set", "working_step_update", "working_clear"],
                    "description": (
                        "status lists the limits, every core entry with its id, and this session's working fields. "
                        "working_clear removes all of this session's working fields."
                    ),
                },
                "scope": {
                    "type": "string", "enum": list(CORE_SCOPES), "default": "memory",
                    "description": "core_add: the file to add to, memory (MEMORY.md) or user (USER.md).",
                },
                "content": {
                    "type": "string",
                    "description": (
                        "core_add: the entry text (required, at most 600 characters). "
                        "working_plan_set: optional goal. working_update reads it only when value is empty."
                    ),
                },
                "memory_id": {
                    "type": "string",
                    "description": "core_remove: the id of the entry to remove, as shown by status (required).",
                },
                "field": {
                    "type": "string", "enum": list(CONVERSATION_FIELDS),
                    "description": "working_update: the field to set (required).",
                },
                "value": {
                    "type": "string",
                    "description": (
                        "working_update: the field's new text (required, at most 2000 characters). "
                        "It replaces the current text. Not read by core_add."
                    ),
                },
                "steps": {
                    "type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 12,
                    "description": "working_plan_set: the step texts, in order.",
                },
                "step_index": {
                    "type": "integer", "minimum": 1,
                    "description": "working_step_update: the step's number, counted from 1.",
                },
                "step_status": {
                    "type": "string", "enum": ["pending", "in_progress", "completed"],
                    "description": "working_step_update: the step's new status.",
                },
            },
            "required": ["action"],
        },
        fn=_memory,
        risk="write",
        approval="never",
        idempotent=False,
        sandboxed=False,
        group="memory",
    ))
