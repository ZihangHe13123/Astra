"""Compact a worker's history with the shared pipeline, and bound each request.

A worker's history is compacted the way the main agent's is: at the same threshold, by the
same compressor, and the result replaces the history it came from, so its summary is kept
and updated rather than rebuilt for every request (compact_history). Each request is still
checked against the budget on its own (bound_messages): unavailable summaries and oversized
active evidence stop it, never silently turn it into a fresh conversation.
"""

from __future__ import annotations

import copy
import os

from .context import AgentContext
from .context_compressor import ContextCompressor, _is_synthetic_user_turn
from .prompt_budget import prompt_token_budget
from .subagent_resume import recovery_tool_risk
from .token_estimator import estimate_messages_tokens, estimate_value_tokens


class ReplayBudgetExceeded(ValueError):
    """The recovered prompt cannot safely fit its effective token budget."""


def _provider_messages(messages: list[dict]) -> list[dict]:
    """Dispatch-time risk is private recovery state, never provider input."""
    return [
        {key: value for key, value in message.items() if key != "_recovery_tool_risks"}
        for message in messages
    ]


def _positive_int(value, default: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return default
    try:
        result = int(value)
    except (TypeError, ValueError):
        return default
    return result if result > 0 else default


# The main agent's own budget when its model's window is unknown (AgentContext's default).
_UNKNOWN_WINDOW_BUDGET = 100_000


def replay_budget(llm, *, generation_reserve: int | None = None) -> int:
    """The prompt size at which a worker's history is compacted: the main agent's rule.

    The model's window less the output the request may use, its own compaction setting,
    and for most models half the window, exactly as prompt_token_budget decides for the main
    agent. Workers can request more output than config.max_tokens (investigation has a
    runtime floor); the dispatch owner supplies that actual allowance here.
    ASTRA_SUBAGENT_MAX_PROMPT_TOKENS lowers the budget further when it is set.
    """
    config = getattr(llm, "config", None)
    reserve = _positive_int(getattr(config, "max_tokens", None), 4096)
    if generation_reserve is not None:
        requested = _positive_int(generation_reserve)
        if not requested:
            raise ReplayBudgetExceeded("Subagent replay generation reserve must be positive.")
        reserve = max(reserve, requested)
    compact_limit = _positive_int(getattr(config, "auto_compact_token_limit", None))
    context_limit = _positive_int(getattr(config, "context_limit", None))
    if context_limit and reserve >= context_limit:
        raise ReplayBudgetExceeded("Subagent replay budget is exhausted by the generation reserve.")
    if context_limit:
        budget = prompt_token_budget(
            context_limit, reserve,
            model=str(getattr(config, "model", "") or ""), provider=str(getattr(config, "provider", "") or ""),
            auto_compact_token_limit=compact_limit or None,
        )
    else:
        budget = min(_UNKNOWN_WINDOW_BUDGET, compact_limit) if compact_limit else _UNKNOWN_WINDOW_BUDGET
    cap = _positive_int(os.getenv("ASTRA_SUBAGENT_MAX_PROMPT_TOKENS"))
    if cap:
        budget = min(budget, cap)
    if budget <= 0:
        raise ReplayBudgetExceeded("Subagent replay budget is exhausted by the generation reserve.")
    return budget


def _request_tokens(messages: list[dict], *, llm, tools_tokens: int) -> int:
    # Provider estimators can include vision/reasoning overhead that the local
    # estimator cannot see. Never let a provider underestimate erase the local
    # lower bound, and always charge schemas separately.
    messages = _provider_messages(messages)
    tokens = estimate_messages_tokens(messages)
    estimate = getattr(llm, "estimate_tokens", None)
    if callable(estimate):
        tokens = max(tokens, _positive_int(estimate(messages)))
    return tokens + tools_tokens


def _protected_start(messages: list[dict]) -> int:
    """Protect the active request and the last four complete protocol steps."""
    latest_user = next((
        index for index in range(len(messages) - 1, -1, -1)
        if messages[index].get("role") == "user"
        and not _is_synthetic_user_turn(messages[index])
    ), len(messages))
    steps = []
    for index, message in enumerate(messages):
        if message.get("role") != "assistant" or not message.get("tool_calls"):
            continue
        ids = {call.get("id") for call in message["tool_calls"] if isinstance(call, dict)}
        results = []
        end = index + 1
        while end < len(messages) and messages[end].get("role") == "tool":
            results.append(messages[end].get("tool_call_id"))
            end += 1
        if ids and None not in ids and set(results) == ids and len(results) == len(ids):
            steps.append(index)
    if steps:
        latest_user = min(latest_user, steps[max(0, len(steps) - 4)])
    return latest_user


def _prepare_context(
    messages: list[dict], *, registry, llm, budget: int,
    tool_schemas: list[dict] | None = None,
):
    source = copy.deepcopy(messages)
    head = source[:1] if source and source[0].get("role") == "system" else []
    history = source[len(head):]
    if any(message.get("role") == "system" for message in history):
        raise ReplayBudgetExceeded("Subagent replay requires one leading system prompt.")
    system = head[0].get("content", "") if head else ""
    if not isinstance(system, str):
        raise ReplayBudgetExceeded("Subagent replay requires a text system prompt.")
    schemas = registry.to_openai_tools() if tool_schemas is None else tool_schemas
    tools_tokens = estimate_value_tokens(schemas) if schemas else 0
    context = AgentContext(system_prompt=system, messages=history, max_prompt_tokens=budget)
    context.set_tools_token_cost(tools_tokens)
    recorded_risky_names = set()
    for message in history:
        if "_recovery_tool_risks" not in message:
            continue
        recorded = message["_recovery_tool_risks"]
        for call in message.get("tool_calls") or []:
            call_id = call.get("id")
            if isinstance(recorded, dict) and call_id not in recorded:
                continue
            recorded_risk = recorded.get(call_id) if isinstance(recorded, dict) else None
            if recorded_risk != "read":
                recorded_risky_names.add(str((call.get("function") or {}).get("name") or ""))

    def request_local(name: str) -> bool:
        tool = registry.get(name)
        return bool(tool is not None and getattr(tool, "result_persistence", "durable") == "request_local")

    def risk(name: str) -> str:
        tool = registry.get(name)
        # micro_compact accepts only a risk callback, so request-local tools
        # also need a non-droppable risk to retain their transient evidence.
        if tool is None or request_local(name) or name in recorded_risky_names:
            return "write"
        # Compaction callbacks receive a name, not per-call arguments. With
        # no action proof, coordination families must preserve write effects
        # even though their approval policy labels the registry tool as read.
        # Recovery only proves read operations side-effect-free. The shared
        # compactor also drops network, so map every other risk to write here.
        return "read" if recovery_tool_risk(registry, name) == "read" else "write"

    context.tool_risk_provider = risk
    context.tool_request_local_provider = request_local

    def current_messages() -> list[dict]:
        # Return canonical dictionaries, not get_prompt(): the latter injects
        # provider timestamp/media projections that must not be persisted.
        return [*head, *context.messages]

    def measure() -> int:
        return _request_tokens(current_messages(), llm=llm, tools_tokens=tools_tokens)

    return context, current_messages, measure, tools_tokens


def _protected_tool_chains(messages: list[dict], risk) -> list[list[dict]]:
    """Protect entire mixed-call groups, including every contiguous result."""
    chains = []
    for index, message in enumerate(messages):
        calls = message.get("tool_calls") if message.get("role") == "assistant" else None
        if not calls or not any(
            risk(str((call.get("function") or {}).get("name") or "")) != "read"
            for call in calls
        ):
            continue
        end = index + 1
        while end < len(messages) and messages[end].get("role") == "tool":
            end += 1
        chains.append(copy.deepcopy(messages[index:end]))
    return chains


def _tool_chains_survived(messages: list[dict], chains: list[list[dict]]) -> bool:
    # Ordered matching also preserves duplicate groups; finding one matching
    # group twice must not pass after one occurrence disappeared.
    cursor = 0
    for chain in chains:
        for start in range(cursor, len(messages) - len(chain) + 1):
            if messages[start:start + len(chain)] == chain:
                cursor = start + len(chain)
                break
        else:
            return False
    return True


def _suffix_survived(messages: list[dict], protected: list[dict]) -> bool:
    if not protected:
        return True
    # ContextCompressor may append its synthetic attention recap after the
    # preserved suffix; it is not part of the worker's active instruction.
    candidates = [message for message in messages if not ContextCompressor._is_compression_recap(message)]
    expected = [message for message in protected if not ContextCompressor._is_compression_recap(message)]
    return not expected or candidates[-len(expected):] == expected


async def bound_messages(
    messages: list[dict], *, registry, llm, max_prompt_tokens: int | None = None,
    generation_reserve: int | None = None,
    tool_schemas: list[dict] | None = None,
) -> list[dict]:
    """Return an admitted request copy, or refuse recovery without data loss."""
    budget = replay_budget(llm, generation_reserve=generation_reserve)
    if max_prompt_tokens is not None:
        requested = _positive_int(max_prompt_tokens)
        if not requested:
            raise ReplayBudgetExceeded("Subagent replay budget must be positive.")
        budget = min(budget, requested)
    context, current_messages, measure, tools_tokens = _prepare_context(
        messages, registry=registry, llm=llm, budget=budget, tool_schemas=tool_schemas,
    )
    if measure() <= budget:
        return _provider_messages(current_messages())
    protected = copy.deepcopy(context.messages[_protected_start(context.messages):])
    protected_chains = _protected_tool_chains(context.messages, context.tool_risk_provider)
    fixed = ([messages[0]] if messages and messages[0].get("role") == "system" else []) + protected
    if _request_tokens(fixed, llm=llm, tools_tokens=tools_tokens) > budget:
        raise ReplayBudgetExceeded(
            f"Subagent replay budget ({budget} tokens) cannot fit schemas, system, and active/recent evidence."
        )
    context.compressor = ContextCompressor(llm)
    await context.compress_if_needed(preserve_on_failure=True, measure_tokens=measure)
    result = current_messages()
    if not _tool_chains_survived(result, protected_chains):
        raise ReplayBudgetExceeded(
            "Subagent replay budget cannot be met without modifying protected historical tool evidence."
        )
    if not _suffix_survived(result, protected):
        raise ReplayBudgetExceeded(
            "Subagent replay budget cannot be met without modifying active/recent evidence."
        )
    actual = measure()
    if actual > budget:
        raise ReplayBudgetExceeded(
            f"Subagent replay remains above budget after safe compaction ({actual} > {budget} tokens)."
        )
    return _provider_messages(result)


def _latest_request(messages: list[dict]) -> dict | None:
    return next((
        message for message in reversed(messages)
        if message.get("role") == "user" and not _is_synthetic_user_turn(message)
    ), None)


def _recent_steps_start(messages: list[dict]) -> int:
    """Where the last four complete protocol steps begin; the latest request when there are fewer."""
    latest = next((
        index for index in range(len(messages) - 1, -1, -1)
        if messages[index].get("role") == "user" and not _is_synthetic_user_turn(messages[index])
    ), len(messages))
    steps = []
    for index, message in enumerate(messages):
        if index < latest or message.get("role") != "assistant" or not message.get("tool_calls"):
            continue
        ids = {call.get("id") for call in message["tool_calls"] if isinstance(call, dict)}
        results = []
        end = index + 1
        while end < len(messages) and messages[end].get("role") == "tool":
            results.append(messages[end].get("tool_call_id"))
            end += 1
        if ids and None not in ids and set(results) == ids and len(results) == len(ids):
            steps.append(index)
    return steps[-4] if len(steps) >= 4 else latest


async def compact_history(
    messages: list[dict], *, registry, llm, compressor: ContextCompressor,
    generation_reserve: int | None = None, tool_schemas: list[dict] | None = None,
) -> list[dict]:
    """Return the history compacted to replace the worker's own, or the same list unchanged.

    The threshold, the compressor and what is kept as it was are the main agent's: the
    leading system prompt, the latest request and the last four complete steps, which also
    hold any call still waiting for its result. Earlier completed steps are summarised,
    writes among them, as the main agent summarises the start of a long turn; the summary
    records what was done and which files were changed. What recovery needs is untouched:
    a call whose outcome is unknown is in the recent steps, and the read-only lock it sets
    is part of the saved state, not of the history. A compaction that could not keep all
    of that is not applied; neither is one that fails: the history stays whole and the
    request bound decides what is sent.
    """
    try:
        budget = replay_budget(llm, generation_reserve=generation_reserve)
        context, current_messages, measure, _ = _prepare_context(
            messages, registry=registry, llm=llm, budget=budget, tool_schemas=tool_schemas,
        )
    except ReplayBudgetExceeded:
        return messages
    if measure() < budget:
        return messages
    recent = copy.deepcopy(context.messages[_recent_steps_start(context.messages):])
    request = copy.deepcopy(_latest_request(context.messages))
    context.compressor = compressor
    await context.compress_if_needed(preserve_on_failure=True, measure_tokens=measure)
    result = current_messages()
    if len(result) >= len(messages):
        return messages
    if request is not None and request not in result:
        return messages
    if not _suffix_survived(result, recent):
        return messages
    return result
