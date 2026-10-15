"""Astra-native reciprocal-rank fusion and maximal-marginal-relevance selection.

RRF fuses channel ranks, never BM25 and cosine values. MMR penalizes redundant
evidence. These are local algorithms, with no external memory framework.
"""

from __future__ import annotations

from dataclasses import replace
from math import isfinite

from .models import RecommendationCandidate, SourceResult
from .lexical import LexicalQuery
from .query import QueryPlan, text_features
from .ranking import _normalized

_RRF_K = 60
_POOL_LIMIT = 40


def _key(candidate: RecommendationCandidate) -> tuple[str, str, int]:
    return candidate.locator.kind, candidate.locator.primary, candidate.locator.secondary


def _order(candidate: RecommendationCandidate) -> tuple:
    rank = candidate.native_query_rank
    return (rank if isfinite(rank) else float("inf"), -candidate.timestamp, _key(candidate))


def fuse_candidates(candidates: tuple[RecommendationCandidate, ...]) -> tuple[RecommendationCandidate, ...]:
    merged: dict[tuple, RecommendationCandidate] = {}
    for item in candidates:
        key = _key(item)
        previous = merged.get(key)
        if previous is None:
            merged[key] = item
            continue
        if previous.revision and item.revision and previous.revision != item.revision:
            continue  # A concurrent edit must not give two versions a shared vote.
        ranks: dict[str, int] = {}
        for name, rank in (*previous.channel_ranks, *item.channel_ranks):
            if rank > 0:
                ranks[name] = min(ranks.get(name, rank), rank)
        merged[key] = replace(previous, channel_ranks=tuple(sorted(ranks.items())))
    return tuple(sorted(merged.values(), key=lambda c: (-rrf_score(c), _key(c))))


def rrf_score(candidate: RecommendationCandidate) -> float:
    ranks: dict[str, int] = {}
    for name, rank in candidate.channel_ranks[:12]:
        if 0 < rank <= 1000:
            ranks[name] = min(ranks.get(name, rank), rank)
    return sum(1 / (_RRF_K + rank) for rank in ranks.values())


def select_rows(
    session_result: SourceResult,
    activity_result: SourceResult,
    plan: QueryPlan | None = None,
    memory_result: SourceResult | None = None,
) -> tuple[RecommendationCandidate, ...]:
    """Select zero to four complementary records across all eligible sources."""
    if plan is not None and not plan.should_recall:
        return ()
    lexical = LexicalQuery.from_text(plan.query) if plan else None
    recent = plan is None or plan.include_recent
    # A named topic that matched nothing in a multi-day window is a question to
    # abstain on, not one to fill with whatever else happened then.
    if (plan is not None and recent and plan.intent == "temporal" and lexical is not None and lexical.terms
            and (not plan.time_end or plan.time_end - plan.time_start > 86400)
            and not any(plan.contains(item.timestamp)
                        for result in (session_result, activity_result, memory_result) if result is not None
                        for item in result.relevance)):
        recent = False
    pool: list[RecommendationCandidate] = []
    for source_name, result in (("session", session_result), ("activity", activity_result), ("memory", memory_result)):
        if result is None:
            continue
        channels = [("lexical", result.relevance)]
        if recent:
            channels.append(("recent", result.recency))
        for channel, values in channels:
            if lexical is not None and lexical.anchors:
                values = tuple(item for item in values if lexical.matches_anchors(item.private_text or item.description))
            for rank, item in enumerate(sorted(values, key=_order)[:40], 1):
                if item.source != source_name:
                    continue
                if not item.description.strip() or not isfinite(item.timestamp):
                    continue
                if plan is not None and (not plan.contains(item.timestamp) or item.available_at > plan.cutoff):
                    continue
                if channel == "recent" and item.workspace_tier >= 2 and (plan is None or plan.intent == "resume"):
                    continue
                ranks = (
                    item.channel_ranks
                    if channel == "lexical" and item.channel_ranks
                    else ((f"{source_name}-{channel}", rank),)
                )
                pool.append(replace(item, channel_ranks=ranks, reason="recent" if channel == "recent" else "related"))
        habit = result.habit
        if habit is not None and habit.source == "habit" and habit.support_days >= 3:
            if plan is not None and plan.intent == "preference" and plan.contains(habit.timestamp):
                if (text_features(plan.query) & text_features(habit.description)
                        and (lexical is None or lexical.matches_anchors(habit.description))):
                    pool.append(replace(habit, channel_ranks=(("habit", 1),), reason="inferred"))
    candidates = list(fuse_candidates(tuple(pool)))
    features = {_key(item): text_features(item.private_text or item.description) for item in candidates}
    coverage = {_key(item): lexical.coverage(item.private_text or item.description) if lexical else 0.0
                for item in candidates}

    def relevance(item: RecommendationCandidate) -> float:
        tier = max(0, min(2, item.workspace_tier))
        utility = max(-0.1, min(0.1, item.utility)) if isfinite(item.utility) else 0.0
        return rrf_score(item) * 60 + 0.30 * coverage[_key(item)] + 0.12 * (2 - tier) + utility

    candidates.sort(key=lambda item: (-relevance(item), _key(item)))
    candidates = candidates[:_POOL_LIMIT]
    chosen: list[RecommendationCandidate] = []
    seen: set[str] = set()
    limit = min(6, max(0, plan.max_items)) if plan else 4
    while candidates and len(chosen) < limit:

        def score(item: RecommendationCandidate) -> tuple:
            words = features[_key(item)]
            redundancy = max(
                (len(words & features[_key(other)]) / max(1, len(words | features[_key(other)])) for other in chosen),
                default=0.0,
            )
            return (-(relevance(item) - 0.65 * redundancy), _key(item))

        item = min(candidates, key=score)
        candidates.remove(item)
        normalized = _normalized(item.description)
        words = features[_key(item)]
        if normalized in seen or any(
            len(words & features[_key(other)]) / max(1, len(words | features[_key(other)])) >= 0.88 for other in chosen
        ):
            continue
        seen.add(normalized)
        chosen.append(replace(item, slot=f"R{len(chosen) + 1}"))
    return tuple(chosen)
