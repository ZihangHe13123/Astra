"""Agent tool access to the optional Hindsight provider."""

from __future__ import annotations

from datetime import datetime, timezone

from ..hindsight_provider import HindsightMemoryProvider
from ..tool_failure import ToolFailure
from .registry import ToolDef, ToolRegistry

_CALLS_PER_TURN = 2
_TURN_LIMIT = f"At most {_CALLS_PER_TURN} calls per turn: a further call is not run and ends the turn"


def _failure(operation: str, exc: Exception) -> ToolFailure:
    """A Hindsight call that did not complete. The service is optional, so say how to go on."""
    timed_out = isinstance(exc, TimeoutError)
    if operation == "retain":
        hint = (
            "The memory may not have been stored. Do not send it again in this turn; if it matters, "
            "tell the user that saving it could not be confirmed."
        )
    else:
        hint = (
            "Hindsight is an optional source: continue without it and do not repeat this call in this "
            "turn. session_search reads local history without this service."
        )
    return ToolFailure(
        code="hindsight_timeout" if timed_out else "hindsight_error",
        message=(
            f"Hindsight {operation} did not answer in time." if timed_out
            else f"Hindsight {operation} failed: {type(exc).__name__}: {exc}"
        ),
        retryable=False,
        recovery_hint=hint,
    )


def _record_date(created_at: str, started: datetime, finished: datetime) -> str:
    """The record's own date, or "" when Hindsight had none.

    The provider stamps a record without a date with the time of the recall
    itself, so a timestamp inside this call is a placeholder, not a date.
    """
    try:
        moment = datetime.fromisoformat(str(created_at).strip().replace("Z", "+00:00"))
    except ValueError:
        return ""
    if moment.tzinfo is not None and started <= moment <= finished:
        return ""
    return moment.date().isoformat()


def register_hindsight_tools(
    registry: ToolRegistry,
    provider: HindsightMemoryProvider,
) -> None:
    async def hindsight_retain(
        content: str,
        context: str = "",
        tags: list[str] | None = None,
    ) -> str | ToolFailure:
        if not content.strip():
            return ToolFailure(
                code="invalid_arguments", message="hindsight_retain needs non-empty content.", retryable=False,
                recovery_hint="Pass the text to store in content. A failed call still counts toward the per-turn limit.",
            )
        try:
            record = await provider.retain(
                kind="observation",
                content=content,
                context=context,
                tags=tags or (),
            )
        except Exception as exc:
            return _failure("retain", exc)
        return f"Hindsight stored non-authoritative memory in {record.metadata['document_id']}."

    async def hindsight_recall(query: str, limit: int = 5) -> str | ToolFailure:
        started = datetime.now(timezone.utc)
        try:
            records = await provider.recall(query, limit=limit)
        except Exception as exc:
            return _failure("recall", exc)
        finished = datetime.now(timezone.utc)
        if not records:
            return "No relevant Hindsight memories found."
        lines = [
            "Hindsight results (historical, non-authoritative; builtin current facts override conflicts):"
        ]
        for index, record in enumerate(records, 1):
            date = _record_date(record.created_at, started, finished)
            lines.append(f"{index}. [{record.kind}]{f' ({date})' if date else ''} {record.content}")
        return "\n".join(lines)

    async def hindsight_reflect(query: str) -> str | ToolFailure:
        try:
            result = await provider.reflect(query)
        except Exception as exc:
            return _failure("reflect", exc)
        return result or "No relevant Hindsight memories found."

    registry.register(ToolDef(
        name="hindsight_retain",
        description=(
            "Store explicitly useful information in the shared local Hindsight bank. "
            "This history is non-authoritative and does not replace current builtin facts. "
            f"{_TURN_LIMIT}, so put several facts into the content of one call."
        ),
        parameters={
            "type": "object",
            "properties": {
                "content": {
                    "type": "string", "minLength": 1,
                    "description": "The text to store. It may hold several facts.",
                },
                "context": {
                    "type": "string", "default": "",
                    "description": (
                        "Optional short note on where this content comes from or what it concerns, "
                        "e.g. 'user preference stated in chat'. Sent to Hindsight together with the content."
                    ),
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 32,
                    "description": (
                        "Optional labels stored with the memory. hindsight_recall cannot filter by tag and "
                        "does not show tags, so put anything that must be findable into content."
                    ),
                },
            },
            "required": ["content"],
        },
        fn=hindsight_retain,
        risk="write",
        approval="never",
        idempotent=False,
        group="memory",
        max_calls_per_turn=_CALLS_PER_TURN,
    ))

    registry.register(ToolDef(
        name="hindsight_recall",
        description=(
            "Search the shared local Hindsight bank for older cross-session context. "
            "Results are historical and non-authoritative; current builtin memory overrides conflicts. "
            "A result line shows the memory's date when Hindsight has one. "
            f"{_TURN_LIMIT}, so cover what you need in one or two queries."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1},
                "limit": {
                    "type": "integer", "minimum": 1, "maximum": 12, "default": 5,
                    "description": "Maximum memories to return.",
                },
            },
            "required": ["query"],
        },
        fn=hindsight_recall,
        risk="read",
        approval="never",
        idempotent=True,
        cache_ttl=30,
        group="memory",
        max_calls_per_turn=_CALLS_PER_TURN,
    ))

    registry.register(ToolDef(
        name="hindsight_reflect",
        description=(
            "Synthesize a reasoned answer across the shared local Hindsight bank. "
            "Use for cross-session patterns or questions requiring multiple memories. "
            f"{_TURN_LIMIT}."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1},
            },
            "required": ["query"],
        },
        fn=hindsight_reflect,
        risk="read",
        approval="never",
        idempotent=True,
        cache_ttl=30,
        group="memory",
        max_calls_per_turn=_CALLS_PER_TURN,
        timeout=provider.reflect_timeout + 10,
    ))
