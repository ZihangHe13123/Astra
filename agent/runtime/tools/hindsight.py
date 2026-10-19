"""Agent tool access to the optional Hindsight provider; memory(action="search") reads it."""

from __future__ import annotations

from ..hindsight_provider import HindsightMemoryProvider
from ..tool_failure import ToolFailure
from .registry import ToolDef, ToolRegistry

_CALLS_PER_TURN = 2
_TURN_LIMIT = f"At most {_CALLS_PER_TURN} calls per turn: a further call is not run"


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
                        "Optional labels stored with the memory. memory search cannot filter by tag and "
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
