"""Per-request injection metrics, per-probe scores and their aggregation.

Scoring uses the *current* injected context of a request: the injected text
that is new relative to the previous request or, when nothing changed, the
context carried over from the previous turn (an unchanged snapshot is not
re-sent but still applies). ``*_visible`` variants use every injected text the
request carries, including replayed older snapshots.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from datetime import datetime
from statistics import mean
from typing import Any

from .fixture import CATEGORIES, NO_EVIDENCE_CATEGORIES, TurnSpec, normalize
from .injection import core_occurrences, diff_request, fresh_segments, prefix_stability
from .world import SeededWorld


# The saved-memory index is always visible, like core memory: it counts as evidence
# the model can see, but not as a recall brought in for this turn.
_INDEX_BLOCK = re.compile(r"<memory-index>.*?</memory-index>", re.DOTALL)


def without_index(text: str) -> str:
    return _INDEX_BLOCK.sub("", text)


def estimate_tokens(text: str) -> int:
    from agent.runtime.token_estimator import estimate_value_tokens

    return int(estimate_value_tokens(text)) if text else 0


def analyze_requests(requests: Sequence[dict], core_entries: Sequence[str]) -> list[dict[str, Any]]:
    """Analyze one conversation's captured requests in order.

    Each request is ``{"messages", "tools", "canonical", "latency_ms", "turn"}``.
    """
    results: list[dict[str, Any]] = []
    previous: dict | None = None
    previous_texts: tuple[str, ...] = ()
    current_text = ""
    for request in requests:
        diff = diff_request(request["messages"], request["canonical"])
        texts = diff.texts
        fresh = fresh_segments(texts, previous_texts)
        if fresh:
            current_text = "\n".join(fresh)
        injected = diff.injected_text
        core_found, other = core_occurrences(injected, core_entries)
        core_text = "\n".join(core_found)
        fresh_text = "\n".join(fresh)
        stability = prefix_stability(previous["messages"], request["messages"]) if previous else None
        results.append({
            "turn": request.get("turn"),
            "latency_ms": request.get("latency_ms"),
            "request_messages": len(request["messages"]),
            "canonical_messages": len(request["canonical"]),
            "injected_messages": diff.injected_messages,
            "augmented_messages": diff.augmented_messages,
            "dropped_canonical_messages": diff.dropped,
            "injected_chars": len(injected),
            "core_chars": sum(len(entry) for entry in core_found),
            "core_entry_occurrences": len(core_found),
            "injected_tokens": estimate_tokens(injected),
            "core_tokens": estimate_tokens(core_text),
            "index_tokens": estimate_tokens("\n".join(_INDEX_BLOCK.findall(injected))),
            "other_tokens": estimate_tokens(other.strip()),
            "fresh_chars": len(fresh_text),
            "fresh_tokens": estimate_tokens(fresh_text),
            "prefix_stable": None if stability is None else stability["stable"],
            "prefix": stability,
            "tools_stable": None if previous is None else previous["tools"] == request["tools"],
            "injected_text": injected,
            "current_text": current_text,
        })
        previous = request
        previous_texts = texts
    return results


def _dates(timestamp: float) -> tuple[str, ...]:
    moment = datetime.fromtimestamp(timestamp).astimezone()
    return (moment.date().isoformat(), f"{moment.month}月{moment.day}日", f"{moment.month:02d}-{moment.day:02d}")


def _found(ref: str, normalized_text: str, world: SeededWorld) -> bool:
    if ref.startswith("text:"):
        return normalize(ref[5:]) in normalized_text
    item = world.items.get(ref)
    return item is not None and item.fragment in normalized_text


def _dated(ref: str, text: str, world: SeededWorld) -> bool | None:
    item = world.items.get(ref)
    if item is None:
        return None
    formats = _dates(item.timestamp)
    return any(
        item.fragment in normalize(line) and any(value in line for value in formats)
        for line in text.splitlines()
    )


def score_probe(turn: TurnSpec, world: SeededWorld, request: dict[str, Any]) -> dict[str, Any]:
    current = request["current_text"]
    current_n, visible_n = normalize(current), normalize(request["injected_text"])
    requirements = []
    for group in turn.expect:
        hit = next((ref for ref in group if _found(ref, current_n, world)), None)
        visible = next((ref for ref in group if _found(ref, visible_n, world)), None)
        requirements.append({
            "alternatives": list(group), "hit": hit, "hit_visible": visible,
            "dated": _dated(hit, current, world) if hit else None,
        })
    injected_items = sorted(ref for ref, item in world.items.items() if item.fragment in current_n)
    recalled_n = normalize(without_index(current))
    recalled_items = [ref for ref in injected_items if world.items[ref].fragment in recalled_n]
    expected = {ref for group in turn.expect for ref in group} | set(turn.allowed)
    unexpected = [ref for ref in recalled_items if ref not in expected]
    stale = [snippet for snippet in turn.stale if normalize(snippet) in current_n]
    stale_visible = [snippet for snippet in turn.stale if normalize(snippet) in visible_n]
    hits = [item for item in requirements if item["hit"]]
    return {
        "id": turn.id,
        "category": turn.category,
        "text": turn.text,
        "requirements": requirements,
        "recall": (sum(bool(item["hit"]) for item in requirements) / len(requirements)) if requirements else None,
        "recall_visible": (
            sum(bool(item["hit_visible"]) for item in requirements) / len(requirements)
        ) if requirements else None,
        "all_hit": all(item["hit"] for item in requirements) if requirements else None,
        "dated_hits": [item["dated"] for item in hits if item["dated"] is not None],
        "injected_items": injected_items,
        "unexpected_items": unexpected,
        "superseded_items_injected": [ref for ref in injected_items if world.items[ref].status != "active"],
        "false_injection": bool(unexpected) if turn.category in NO_EVIDENCE_CATEGORIES else None,
        "stale_injected": bool(stale) if turn.stale else None,
        "stale_snippets": stale,
        "stale_visible": bool(stale_visible) if turn.stale else None,
        "current_context_chars": len(current),
        **{key: request[key] for key in (
            "latency_ms", "injected_messages", "augmented_messages", "injected_chars", "core_chars",
            "injected_tokens", "core_tokens", "index_tokens", "other_tokens", "fresh_tokens", "prefix_stable",
        )},
    }


def rate(values: Sequence[bool]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def mean_or_none(values: Sequence[float]) -> float | None:
    return round(mean(values), 2) if values else None


def percentile(values: Sequence[float], fraction: float) -> float | None:
    ordered = sorted(values)
    if not ordered:
        return None
    return round(ordered[max(0, math.ceil(fraction * len(ordered)) - 1)], 2)


def aggregate_probes(probes: Sequence[dict[str, Any]]) -> dict[str, Any]:
    with_evidence = [probe for probe in probes if probe["recall"] is not None]
    controls = [probe for probe in probes if probe["false_injection"] is not None]
    stale = [probe for probe in probes if probe["stale_injected"] is not None]
    dated = [value for probe in probes for value in probe["dated_hits"]]
    latencies = [probe["latency_ms"] for probe in probes if probe["latency_ms"] is not None]
    return {
        "probes": len(probes),
        "evidence_recall": mean_or_none([probe["recall"] for probe in with_evidence]),
        "evidence_recall_visible": mean_or_none([probe["recall_visible"] for probe in with_evidence]),
        "full_hit_rate": rate([probe["all_hit"] for probe in with_evidence]),
        "false_injection_rate": rate([probe["false_injection"] for probe in controls]),
        "stale_injection_rate": rate([probe["stale_injected"] for probe in stale]),
        "stale_visible_rate": rate([probe["stale_visible"] for probe in stale]),
        "dated_evidence_rate": rate(dated),
        "mean_unexpected_items": mean_or_none([len(probe["unexpected_items"]) for probe in probes]),
        "mean_injected_tokens": mean_or_none([probe["injected_tokens"] for probe in probes]),
        "mean_core_tokens": mean_or_none([probe["core_tokens"] for probe in probes]),
        "mean_index_tokens": mean_or_none([probe["index_tokens"] for probe in probes]),
        "mean_other_tokens": mean_or_none([probe["other_tokens"] for probe in probes]),
        "mean_fresh_tokens": mean_or_none([probe["fresh_tokens"] for probe in probes]),
        "mean_injected_messages": mean_or_none([probe["injected_messages"] for probe in probes]),
        "latency_p50_ms": percentile(latencies, 0.5),
        "latency_p95_ms": percentile(latencies, 0.95),
    }


def summarize_mode(scenarios: Sequence[dict[str, Any]]) -> dict[str, Any]:
    probes = [probe for scenario in scenarios for probe in scenario["probes"]]
    requests = [request for scenario in scenarios for request in scenario["requests"]]
    cumulative = [sum(request["injected_tokens"] for request in scenario["requests"]) for scenario in scenarios]
    core_total = sum(request["core_tokens"] for request in requests)
    injected_total = sum(request["injected_tokens"] for request in requests)
    stable = [request["prefix_stable"] for request in requests if request["prefix_stable"] is not None]
    tools = [request["tools_stable"] for request in requests if request["tools_stable"] is not None]
    latencies = [request["latency_ms"] for request in requests if request["latency_ms"] is not None]
    by_category = {
        category: aggregate_probes([probe for probe in probes if probe["category"] == category])
        for category in CATEGORIES if any(probe["category"] == category for probe in probes)
    }
    overall = aggregate_probes(probes)
    overall.update({
        "requests": len(requests),
        "conversations": len(scenarios),
        "mean_cumulative_injected_tokens": mean_or_none(cumulative),
        "max_cumulative_injected_tokens": max(cumulative) if cumulative else None,
        "core_token_share": round(core_total / injected_total, 4) if injected_total else None,
        "prefix_stable_rate": rate(stable),
        "tools_stable_rate": rate(tools),
        "request_latency_p50_ms": percentile(latencies, 0.5),
        "request_latency_p95_ms": percentile(latencies, 0.95),
    })
    return {"overall": overall, "by_category": by_category}


__all__ = [
    "aggregate_probes",
    "analyze_requests",
    "estimate_tokens",
    "mean_or_none",
    "percentile",
    "rate",
    "score_probe",
    "summarize_mode",
]
