"""Tool access to bounded core Markdown memory and session working memory."""

from typing import Any, Callable

from ..hooks import ToolDecision
from ..memory import (
    CONVERSATION_FIELDS, CORE_SCOPES, MEMORY_KINDS, MEMORY_SCOPES, WORKING_FIELDS, MemoryStore,
    current_workspace_key,
)
from ..tool_failure import ToolFailure
from .registry import ToolDef, ToolRegistry

# Still stored for old clients, but no later turn shows them to the model.
_UNSHOWN_FIELDS = ("goal", "plan", "progress", "artifacts")


# Writes that persist across conversations, and what can bring outside text into a turn.
_DURABLE_WRITES = frozenset({"remember", "revise", "core_add"})
_DELIVERED = frozenset({"delegate", "peer_message", "background_run", "background_completion"})
_INGESTING_GROUPS = frozenset({"web", "browser"})


def _invalid(message: str, hint: str) -> ToolFailure:
    return ToolFailure(code="invalid_arguments", message=message, retryable=False, recovery_hint=hint)


def _current_turn(messages: list[dict]) -> tuple[int, list[dict]]:
    """The index of the user's latest own message, and everything since it."""
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.get("role") == "user" and not message.get("provenance") and not message.get("_meta"):
            return index, messages[index + 1:]
    return -1, messages


def outside_content(messages: list[dict], registry: ToolRegistry) -> str:
    """What brought text from outside the conversation into this turn, or "".

    Saved memories persist across conversations, so a write after reading a web
    page, an MCP result or delivered content needs the user's approval: such text
    can carry instructions meant to plant themselves in memory.
    """
    for message in _current_turn(messages)[1]:
        if message.get("role") == "user" and message.get("provenance") in _DELIVERED:
            return "content delivered by a subagent, another session or a background run"
        for call in message.get("tool_calls") or []:
            name = str((call.get("function") or {}).get("name") or call.get("name") or "")
            tool = registry.get(name)
            if name.startswith("mcp__") or (tool is not None and tool.group in _INGESTING_GROUPS):
                return f"the {name} tool"
    return ""


def register_memory_tools(
    registry: ToolRegistry,
    store: MemoryStore,
    *,
    session_id: Callable[[], str],
    messages: Callable[[], list[dict]] | None = None,
    workspace_key: Callable[[], str] | None = None,
) -> None:
    def _source() -> str:
        index = _current_turn(list(messages()))[0] if messages is not None else -1
        return str(index) if index >= 0 else ""

    async def _saved_memory(action: str, args: dict[str, Any], current_session: str) -> str | ToolFailure:
        memory_id = str(args.get("memory_id") or "").strip().lstrip("#")
        content = str(args.get("content") or "")
        summary = str(args.get("summary") or "")
        try:
            if action == "remember":
                scope = str(args.get("scope") or "")
                scope = scope if scope in MEMORY_SCOPES else "global"
                record, created = store.remember(
                    kind=str(args.get("kind") or ""), summary=summary, content=content, scope=scope,
                    workspace_key=(workspace_key or current_workspace_key)(),
                    source_session_id=current_session, source_message_id=_source(),
                    valid_until=str(args.get("valid_until") or "") or None,
                )
                if not created:
                    return (f"Already saved as #{record.record_id[:8]}: {store.memory_summary(record)}. "
                            "Use revise to change it.")
                return f"Saved memory #{record.record_id[:8]} ({record.kind}, {scope}): {store.memory_summary(record)}"
            if not memory_id:
                return _invalid(f"{action} needs memory_id", "Pass the id shown in the memory index.")
            if action == "read":
                return store.read_memory(memory_id)
            if action == "revise":
                if not content.strip():
                    return _invalid("revise needs the new full text in content",
                                    "Read the memory, then pass its corrected text in content.")
                previous = store.resolve_record(memory_id, active_only=True)
                record = store.revise(memory_id, content=content, summary=summary,
                                      source_session_id=current_session, source_message_id=_source())
                return (f"Revised memory #{previous.record_id[:8] if previous else memory_id} "
                        f"as #{record.record_id[:8]}: {store.memory_summary(record)}")
            record = store.resolve_record(memory_id, active_only=True)
            if record is None or not store.forget_record(record.record_id):
                raise ValueError(f"Active memory not found: {memory_id}")
            return f"Forgot memory #{record.record_id[:8]}."
        except ValueError as exc:
            return ToolFailure(code="memory_rejected", message=str(exc), retryable=False,
                               recovery_hint="Check the arguments; the memory index lists current ids.")

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
        kind: str = "",
        summary: str = "",
        valid_until: str = "",
    ) -> str | ToolFailure:
        current_session = session_id() or "default"
        if action == "status":
            return store.status(current_session)
        if action in {"remember", "revise", "read", "forget"}:
            return await _saved_memory(action, {
                "scope": scope, "content": content, "memory_id": memory_id, "kind": kind,
                "summary": summary, "valid_until": valid_until,
            }, current_session)
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
            "Manage persistent memory. Saved memories (remember, revise, read, forget) are the growing layer: each is "
            "listed by its one-line summary, with its id and date, in the always-visible memory index, and its full "
            "text is read on demand. Remember what is worth knowing in later conversations and is not evident from "
            "files, git history or this conversation: the user's preferences, corrections and facts about them; "
            "decisions and facts about a project or environment; where something lives. Write summary as one "
            "specific line, the details and the reason in content; scope=project for the current project, "
            "otherwise global. When something changes, revise the existing memory instead of adding another (the "
            "old one is kept as superseded). Read a memory before relying on more than its summary. forget only "
            "when the user asks. Never store secrets, credentials, private reasoning, guesses or transient results. "
            "After you read web pages, browser pages, MCP results or delivered content in a turn, saving needs the "
            "user's approval. "
            "Pinned core entries (core_add, core_remove) are the small block of MEMORY.md and USER.md: reserve "
            "core_add for stable identity, environment or agent decisions the user explicitly wants pinned (user "
            "profile in USER.md with scope=user, everything else in MEMORY.md with scope=memory); core_remove only "
            "when the user asks or a full file holds an obsolete, duplicated or superseded entry, after status. "
            "Use core_import only to validate external Markdown edits. "
            "Working memory: use working_update only for temporary_constraints, open_questions, assumptions or "
            "turn_notes useful during this conversation; it replaces the field's whole text. Goals, plans and "
            "progress are kept by the TaskRun journal; working_plan_set and working_step_update exist only for "
            "backward compatibility. Past conversations are found with session_search."
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["remember", "revise", "read", "forget", "status", "core_add", "core_remove", "core_import",
                             "working_update", "working_plan_set", "working_step_update", "working_clear"],
                    "description": (
                        "remember saves a memory; revise supersedes one; read shows one in full with its dates and "
                        "history; forget removes one (the user approves). status lists the limits, every core entry "
                        "with its id, and this session's working fields. working_clear removes all of this "
                        "session's working fields."
                    ),
                },
                "scope": {
                    "type": "string", "enum": [*MEMORY_SCOPES, *CORE_SCOPES],
                    "description": (
                        "remember: global (default) or project (the current project only). "
                        "core_add: the file to add to, memory (MEMORY.md, default) or user (USER.md)."
                    ),
                },
                "kind": {
                    "type": "string", "enum": list(MEMORY_KINDS),
                    "description": (
                        "remember: preference (how the user wants things), user_fact (about the user), "
                        "observation (about a project or environment), episode (something that happened or was decided)."
                    ),
                },
                "summary": {
                    "type": "string", "maxLength": 160,
                    "description": "remember (required), revise (optional): the one line the index shows.",
                },
                "valid_until": {
                    "type": "string",
                    "description": "remember: optional ISO date after which the memory no longer holds.",
                },
                "content": {
                    "type": "string",
                    "description": (
                        "remember: the details and why (defaults to summary). revise: the new full text (required). "
                        "core_add: the entry text (required, at most 600 characters). "
                        "working_plan_set: optional goal. working_update reads it only when value is empty."
                    ),
                },
                "memory_id": {
                    "type": "string",
                    "description": (
                        "read, revise, forget: the saved memory's id from the index. "
                        "core_remove: the id of the entry to remove, as shown by status."
                    ),
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

    def _decide(name: str, args: dict, tool_def: Any) -> ToolDecision | None:
        if name != "memory":
            return None
        action = str(args.get("action") or "")
        if action == "forget":
            return ToolDecision.ask("Forget a saved memory")
        if action in _DURABLE_WRITES and messages is not None:
            source = outside_content(list(messages()), registry)
            if source:
                return ToolDecision.ask(f"Save memory in a turn that read outside content ({source})")
        return None

    registry.hooks.on_tool_decision(_decide)
