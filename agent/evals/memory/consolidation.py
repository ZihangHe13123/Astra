"""Consolidation tier: does one real review pass propose the changes a world needs?

Worlds with a ``consolidation`` block are seeded with some records left out,
so the archive is ahead of memory. A pass reads the whole archive with the
configured model, and its proposals are scored against the expected changes:
hit (text matches every keyword group), revised (the expected memory was
revised rather than duplicated), and extra (proposals matching nothing
expected). Worlds without expectations measure restraint.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from .fixture import ConsolidationExpect, Fixture, normalize
from .isolation import isolated_state, path_guard
from .scoring import rate
from .world import seed_world


def _matches(proposal: dict[str, Any], expect: ConsolidationExpect) -> bool:
    text = normalize(" ".join(str(proposal.get(key, "")) for key in ("summary", "content")))
    return all(any(normalize(word) in text for word in group) for group in expect.must)


def score_world(proposals: list[dict[str, Any]], expects: tuple[ConsolidationExpect, ...],
                record_ids: dict[str, str]) -> dict[str, Any]:
    results, used = [], set()
    for expect in expects:
        found = next((index for index, item in enumerate(proposals) if _matches(item, expect)), None)
        revised = None
        if expect.revises:
            target = record_ids.get(expect.revises, "")
            revised = found is not None and proposals[found].get("memory_id") == target \
                and proposals[found]["action"] == "revise"
        if found is not None:
            used.add(found)
        results.append({"must": [list(group) for group in expect.must], "revises": expect.revises,
                        "hit": found is not None, "revised": revised})
    return {"expected": results, "extra": len(proposals) - len(used)}


async def _world(scenario: Any, paths: Any, now: datetime, client: Any, model: str) -> dict[str, Any]:
    from agent.runtime.memory import MemoryStore
    from agent.runtime.memory_proposals import consolidate

    spec = scenario.consolidation
    world = seed_world(scenario, now=now, workspace=paths.workspace, without=spec.without)
    store = MemoryStore()
    # Review the whole seeded archive, not only the last few days.
    store.record_store.set_meta("consolidation_through", "0")
    proposals: list[dict[str, Any]] = []
    passes, skipped = 0, ""
    while passes < 6:
        result = await consolidate(store, paths.session_recall_db, lambda: (client, model), force=True, now=now)
        passes += 1
        proposals.extend(result.proposed)
        if result.skipped or not result.more:
            skipped = result.skipped
            break
    record_ids = {ref[4:]: item.identity for ref, item in world.items.items() if ref.startswith("rec:")}
    scored = score_world(proposals, spec.expect, record_ids)
    return {"id": scenario.id, "passes": passes, "skipped": skipped, "proposals": [
        {key: item.get(key) for key in ("action", "kind", "scope", "memory_id", "summary", "content", "reason")}
        for item in proposals
    ], **scored}


def run_consolidation(
    fixture: Fixture, state_root: Path, *, model_key: str | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    from .live import resolve_live_config

    config, resolved = resolve_live_config(model_key or "deepseek-flash")
    return asyncio.run(_run(fixture, Path(state_root).resolve(), config, resolved, progress))


async def _run(fixture: Fixture, state_root: Path, config: Any, model: str,
               progress: Callable[[str], None] | None) -> dict[str, Any]:
    from agent.runtime.llm import LLMClient

    client = LLMClient(config)
    now = datetime.now().astimezone()
    worlds, failures, violations = [], [], []
    for scenario in fixture.scenarios:
        if scenario.consolidation is None:
            continue
        with isolated_state(state_root / "consolidation" / scenario.id) as paths, path_guard(state_root) as guard:
            try:
                worlds.append(await _world(scenario, paths, now, client, model))
            except Exception as exc:  # noqa: BLE001 - one failed world must not discard the others
                failures.append({"id": scenario.id, "error": f"{type(exc).__name__}: {exc}"[:500]})
            violations.extend(guard.violations)
        if progress is not None:
            progress(f"[consolidation] {scenario.id}")
    expected = [item for world in worlds for item in world["expected"]]
    revisions = [item["revised"] for item in expected if item["revised"] is not None]
    controls = [world for world in worlds if not world["expected"]]
    return {
        "model": model,
        "consolidation": {
            "summary": {
                "worlds": len(worlds), "expected": len(expected), "hit": rate([item["hit"] for item in expected]),
                "revised": rate(revisions), "proposals": sum(len(world["proposals"]) for world in worlds),
                "extra": sum(world["extra"] for world in worlds),
                "control_proposals": sum(len(world["proposals"]) for world in controls),
            },
            "worlds": worlds,
            "failures": failures,
        },
        "isolation": {"violations": violations},
    }


def render_consolidation(report: dict[str, Any]) -> str:
    part = report["consolidation"]
    summary = part["summary"]
    lines = [
        f"# Astra memory consolidation ({report['model']})", "",
        "| worlds | expected | hit | revised in place | proposals | extra | in control worlds |",
        "|---|---|---|---|---|---|---|",
        f"| {summary['worlds']} | {summary['expected']} | {summary['hit']} | {summary['revised']} | "
        f"{summary['proposals']} | {summary['extra']} | {summary['control_proposals']} |", "",
    ]
    for world in part["worlds"]:
        lines.append(f"## {world['id']} ({world['passes']} pass{'es' if world['passes'] != 1 else ''})")
        for item in world["expected"]:
            mark = "hit" if item["hit"] else "MISSED"
            how = "" if item["revised"] is None else (" · revised in place" if item["revised"] else " · not as a revision")
            lines.append(f"- expected {item['must']}: {mark}{how}")
        for proposal in world["proposals"]:
            target = f" #{str(proposal.get('memory_id') or '')[:8]}" if proposal.get("memory_id") else ""
            lines.append(f"- proposed {proposal['action']}{target}: {proposal.get('summary') or proposal.get('content') or ''}")
        lines.append("")
    for failure in part["failures"]:
        lines.append(f"- FAILED {failure['id']}: {failure['error']}")
    return "\n".join(lines)
