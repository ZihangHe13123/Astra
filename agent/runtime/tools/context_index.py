"""Stable, request-local tools for inspecting and opening Context Index evidence."""

from __future__ import annotations

from ..context_index.broker import ContextIndexBroker, OPEN_MAX_CALLS, OPEN_MAX_HANDLES, OPEN_TOKEN_BUDGET
from ..tool_failure import ToolFailure
from .registry import ToolDef, ToolRegistry

_INSPECT_MAX_CALLS = 2
_ONE_STEP_RESULT = (
    "The result is shown to you for your next step only and is then replaced by a placeholder, "
    "so copy the handles and facts you need when you first see it."
)
# What the broker's bare status tokens mean and what to do next: token -> (message, hint).
_OPEN_FAILURES = {
    "invalid_request": (
        f"context_open needs 1 to {OPEN_MAX_HANDLES} distinct handles and an integer window from 0 to 5.",
        "Pass handles as a list of distinct strings copied from context_inspect, e.g. [\"ctx:s:0a1b\"]. "
        f"A failed call still counts toward the {OPEN_MAX_CALLS} context_open calls per turn.",
    ),
    "invalid_or_expired_handle": (
        "A handle is not a handle of the current Context Index.",
        "Copy exact current handles from context_inspect; do not substitute session IDs or extend handles. "
        "This error does not establish expiry. Old-turn handles cannot be reopened.",
    ),
    "open_limit_reached": (
        f"The {OPEN_MAX_CALLS} evidence openings for this turn are used up.",
        "Work from the evidence already opened. Do not call context_open again in this turn: "
        "a further call is not run and ends the turn.",
    ),
    "open_budget_reached": (
        f"This turn's evidence budget (about {OPEN_TOKEN_BUDGET} tokens) has no room left for this request.",
        "Work from the evidence already opened and the recommendation text. Only if little was opened "
        f"so far and one of the {OPEN_MAX_CALLS} calls is left, one handle with window 0 needs the least room.",
    ),
    "evidence_unavailable": (
        "The evidence behind these handles could not be read: the source record is unavailable or has changed.",
        "Do not retry these handles in this turn. Use the recommendation text you already have; "
        "this is not proof that the recorded event did not happen.",
    ),
}


def _open_failure(code: str) -> ToolFailure:
    message, hint = _OPEN_FAILURES[code]
    return ToolFailure(code=code, message=message, retryable=False, recovery_hint=hint)


def register_context_index_tools(
    registry: ToolRegistry,
    broker: ContextIndexBroker,
) -> None:
    """Register mode-stable inspection and bounded request-local evidence tools."""

    def context_inspect() -> str:
        return broker.inspect()

    registry.register(ToolDef(
        name="context_inspect",
        description=(
            "Inspect the Context Index recommendations already prepared for this turn. "
            "Use to answer whether anything was injected, what it contains, or questions "
            "about counts, limits, omissions, channels, latency and errors; returns exact "
            "current handles. Reads existing state only: does not rerun retrieval, encode "
            "a query or change recommendations. Previous-turn metadata is labeled "
            "separately; core MD memory is not included. "
            f"At most {_INSPECT_MAX_CALLS} calls per turn: a further call is not run and ends the turn. "
            f"{_ONE_STEP_RESULT}"
        ),
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        fn=context_inspect, risk="read", approval="never", sandboxed=True,
        max_calls_per_turn=_INSPECT_MAX_CALLS, cache_results=False, result_persistence="request_local",
        memory_evidence=None, strict_schema=True,
    ))

    def context_open(handles: object = None, window: object = 2) -> str | ToolFailure:
        # Registry schema validation is authoritative for model calls.  Keep
        # this boundary defensive as the callable is also used directly by
        # local Python integrations and focused tests.
        if not isinstance(handles, list) or not 1 <= len(handles) <= OPEN_MAX_HANDLES:
            return _open_failure("invalid_request")
        if any(not isinstance(handle, str) for handle in handles):
            return _open_failure("invalid_request")
        if len(set(handles)) != len(handles):
            return _open_failure("invalid_request")
        if not isinstance(window, int) or isinstance(window, bool):
            return _open_failure("invalid_request")
        bounded_window = max(0, min(window, 5))
        result = broker.open(list(handles), bounded_window)
        # The broker reports a refusal as a bare status token; evidence is text.
        return _open_failure(result) if result in _OPEN_FAILURES else result

    registry.register(
        ToolDef(
            name="context_open",
            description=(
                "Open only useful evidence from handles in the current model-only "
                "Context Index. Copy handles exactly; do not invent or extend them. "
                "Use context_inspect for current handles and diagnostics. Three handles is "
                "the per-call opening limit, not the recommendation limit. "
                f"At most {OPEN_MAX_CALLS} calls per turn (a further call is not run and ends the turn), "
                f"so open up to {OPEN_MAX_HANDLES} handles in one call. {_ONE_STEP_RESULT}"
            ),
            parameters={
                "type": "object",
                "required": ["handles"],
                "properties": {
                    "handles": {
                        "type": "array",
                        "items": {"type": "string", "pattern": "^ctx:[sahm]:[0-9a-f]{4}$", "minLength": 10, "maxLength": 10},
                        "minItems": 1,
                        "maxItems": OPEN_MAX_HANDLES,
                        "description": "Distinct handles copied exactly from this turn's recommendations or context_inspect.",
                    },
                    "window": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 5,
                        "default": 2,
                        "description": (
                            "Neighbouring messages or events to include on each side of the matched one. "
                            "A smaller window leaves more of the turn's evidence budget for other handles."
                        ),
                    },
                },
                "additionalProperties": False,
            },
            fn=context_open,
            risk="read",
            approval="never",
            sandboxed=True,
            max_calls_per_turn=OPEN_MAX_CALLS,
            cache_results=False,
            result_persistence="request_local",
            # Opaque handles must stay literal in replayed calls. Redacting
            # them creates invalid examples that models copy into new calls.
            argument_persistence="durable",
            memory_evidence=None,
            strict_schema=True,
        )
    )


__all__ = ["register_context_index_tools"]
