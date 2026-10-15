"""Opt-in live tier: the same worlds and probes answered by the configured model.

Only the model configuration comes from the normal environment (process
variables, the project ``.env`` and the persisted model selection, read once
before any state is isolated). All memory, archive, task and session state is
created in the evaluation's temporary directories, exactly as offline. Answers
are scored with keyword and abstention rules; there is no LLM judge.

Write-path probes start from an empty world: the user states something in one
conversation (or several), then a fresh conversation asks for it. They show
whether anything was retained (core files, records or the archive) and whether
it reached the next conversation.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .fixture import Fixture, LiveSpec, ScenarioSpec, WritePathSpec, normalize
from .harness import (
    AgentBundle,
    RecordingLLM,
    RequestRecorder,
    build_agent,
    evaluate_conversation,
    run_turn,
    start_new_session,
)
from .isolation import StatePaths, isolated_state, path_guard
from .scoring import rate, summarize_mode
from .world import seed_world

PROJECT_ROOT = Path(__file__).resolve().parents[3]
ABSTAIN_MARKERS = (
    "不记得", "没有记录", "没有找到", "没找到", "没有提到", "没提到", "没有提过", "没提过", "没有相关",
    "无法确定", "不确定", "没有说过", "没说过", "查不到", "没有这方面", "没有关于", "未提及", "没有信息",
    "not sure", "don't have", "do not have", "no record", "didn't mention", "did not mention",
    "haven't mentioned", "never mentioned", "couldn't find", "could not find", "no information",
    "don't know", "not aware", "no mention",
)


def resolve_live_config(model_key: str | None = None) -> tuple[Any, str]:
    """Resolve the model configuration the way the backend does at startup."""
    from agent.cli.connections import is_local_url
    from agent.cli.environment import load_project_env
    from agent.cli.mode_preferences import apply_reasoning_effort
    from agent.cli.model_catalog import configured_model_catalog
    from agent.cli.model_preferences import read_selected_model
    from agent.runtime.llm import LLMConfig

    load_project_env(PROJECT_ROOT)
    catalog = configured_model_catalog()
    wanted = model_key or os.getenv("LLM_MODEL", "") or read_selected_model() or ""
    entry = catalog.resolve_persisted(wanted) or catalog.resolve(wanted)
    if entry is None:
        raise SystemExit(f"Live tier: no configured model matches {wanted!r}; pass --model with a catalog key")
    profile = entry.profile
    api_key = profile.api_key()
    if not api_key and (is_local_url(entry.base_url) or not profile.api_key_env):
        api_key = "local"
    if not api_key:
        raise SystemExit(f"Live tier: {profile.api_key_env} is not set for {entry.key}")
    config = apply_reasoning_effort(LLMConfig(
        provider=profile.provider, model=entry.model_id, api_key=api_key, base_url=entry.base_url,
        capabilities=profile.capabilities, **profile.generation_settings(),
    ))
    return config, entry.key


def _date_forms(iso: str) -> tuple[str, ...]:
    moment = datetime.fromisoformat(iso)
    return (
        iso, f"{moment.month}月{moment.day}日", f"{moment.month}月{moment.day}号", f"{moment.month:02d}-{moment.day:02d}",
        f"{moment.month}/{moment.day}", moment.strftime("%b %d").replace(" 0", " "),
        moment.strftime("%B %d").replace(" 0", " "), f"{moment.day} {moment.strftime('%b')}",
        f"{moment.day} {moment.strftime('%B')}",
    )


def score_answer(live: LiveSpec, answer: str, archive_dates: dict[str, str]) -> dict[str, Any]:
    text = normalize(answer)

    def has(keyword: str) -> bool:
        if keyword.startswith("date:"):
            iso = archive_dates.get(keyword[5:], "")
            return bool(iso) and any(normalize(form) in text for form in _date_forms(iso))
        return normalize(keyword) in text

    must_ok = all(any(has(keyword) for keyword in group) for group in live.must)
    forbidden = [keyword for keyword in live.forbid if has(keyword)]
    stale = [keyword for keyword in live.stale if has(keyword)]
    abstained = any(marker in text for marker in ABSTAIN_MARKERS)
    if not live.scored:
        verdict = "unscored"
    elif live.abstain:
        verdict = "pass" if abstained else "fail"
    else:
        verdict = "pass" if must_ok and not forbidden else "fail"
    return {
        "verdict": verdict, "must_satisfied": must_ok, "forbidden_hits": forbidden,
        "stale_hits": stale, "stale_answer": bool(stale) and not must_ok, "abstained": abstained,
    }


def _reset_session_search_singleton() -> None:
    """The session_search tool caches its archive handle; each world has its own."""
    from agent.runtime.tools import session_recall as tool

    cached = getattr(tool, "_SR", None)
    if cached is not None:
        cached.close()
    tool._SR = None


async def _live_scenario(
    scenario: ScenarioSpec, mode: str, paths: StatePaths, now: datetime, client: Any, max_iterations: int,
) -> dict[str, Any]:
    world = seed_world(scenario, now=now, workspace=paths.workspace)
    recorder = RequestRecorder()
    bundle = build_agent(mode, RecordingLLM(client, recorder), recorder, paths,
                         session_name=f"session_live_{scenario.id}", max_iterations=max_iterations)
    turns = []
    try:
        for index, turn in enumerate(scenario.conversation, 1):
            outcome = await run_turn(bundle, turn.text, request_id=f"{scenario.id}-{index:02d}", turn=index)
            turns.append((turn, outcome))
    finally:
        bundle.close()
    result = evaluate_conversation(world, turns, recorder.requests, mode)
    by_id = {turn.id: turn for turn, _outcome in turns if turn.is_probe}
    for probe in result["probes"]:
        probe["answer"] = score_answer(by_id[probe["id"]].live, probe["reply"], world.archive_dates)
    return result


def _retained_state(bundle: AgentBundle, paths: StatePaths) -> dict[str, Any]:
    from agent.runtime.session_recall import SessionRecall

    store = bundle.memory_store
    recall = SessionRecall(paths.session_recall_db)
    try:
        archived = recall.count_sessions()
    finally:
        recall.close()
    return {
        "core_entries": [item["content"] for scope in ("memory", "user") for item in store.list_core(scope)],
        "active_records": [record.content for record in store.record_store.list_records(status="active")],
        "archived_sessions": archived,
    }


async def _live_write_path(
    spec: WritePathSpec, mode: str, paths: StatePaths, client: Any, max_iterations: int,
) -> dict[str, Any]:
    recorder = RequestRecorder()
    name = spec.id.replace(".", "_")
    bundle = build_agent(mode, RecordingLLM(client, recorder), recorder, paths,
                         session_name=f"session_{name}_0", max_iterations=max_iterations)
    teach: list[dict[str, Any]] = []
    try:
        turn = 0
        for index, session in enumerate(spec.sessions):
            if index:
                start_new_session(bundle, paths, f"session_{name}_{index}")
            for text in session:
                turn += 1
                outcome = await run_turn(bundle, text, request_id=f"{name}-teach-{turn}", turn=turn)
                teach.append({"text": text, "reply": outcome["reply"], "tool_calls": outcome["tool_calls"],
                              "errors": outcome["errors"]})
        retained = _retained_state(bundle, paths)
        start_new_session(bundle, paths, f"session_{name}_ask")
        asked = await run_turn(bundle, spec.ask, request_id=f"{name}-ask", turn=turn + 1)
    finally:
        bundle.close()
    return {
        "id": spec.id, "mode": mode, "teach": teach, "retained": retained, "ask": spec.ask,
        "reply": asked["reply"], "tool_calls": asked["tool_calls"], "errors": asked["errors"],
        "answer": score_answer(spec.live, asked["reply"], {}),
    }


def _answer_summary(probes: list[dict[str, Any]]) -> dict[str, Any]:
    scored = [probe for probe in probes if probe["answer"]["verdict"] != "unscored"]
    by_category: dict[str, Any] = {}
    for probe in scored:
        by_category.setdefault(probe["category"], []).append(probe["answer"]["verdict"] == "pass")
    return {
        "scored": len(scored),
        "pass_rate": rate([probe["answer"]["verdict"] == "pass" for probe in scored]),
        "stale_answer_rate": rate([probe["answer"]["stale_answer"] for probe in scored]),
        "by_category": {category: rate(values) for category, values in sorted(by_category.items())},
    }


def run_live(
    fixture: Fixture,
    modes: list[str],
    state_root: Path,
    *,
    model_key: str | None = None,
    max_iterations: int = 8,
    include_write_path: bool = True,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    config, resolved = resolve_live_config(model_key)
    return asyncio.run(_run_live(
        fixture, modes, Path(state_root).resolve(), config, resolved,
        max_iterations=max_iterations, include_write_path=include_write_path, progress=progress,
    ))


async def _run_live(
    fixture: Fixture,
    modes: list[str],
    state_root: Path,
    config: Any,
    model: str,
    *,
    max_iterations: int,
    include_write_path: bool,
    progress: Callable[[str], None] | None,
) -> dict[str, Any]:
    # One event loop and one client for the whole run: provider connections and
    # request throttles belong to the loop that created them.
    from agent.runtime.llm import LLMClient

    client = LLMClient(config)
    now = datetime.now().astimezone()
    report: dict[str, Any] = {"model": model, "modes": {}, "isolation": {"violations": []}}
    for mode in modes:
        scenarios: list[dict[str, Any]] = []
        write_path: list[dict[str, Any]] = []
        failures: list[dict[str, str]] = []
        jobs: list[tuple[str, Any]] = [("scenario", scenario) for scenario in fixture.scenarios]
        if include_write_path:
            jobs.extend(("write_path", spec) for spec in fixture.write_path)
        for kind, job in jobs:
            directory = state_root / "live" / mode / job.id.replace(".", "_")
            with isolated_state(directory) as paths, path_guard(state_root) as guard:
                _reset_session_search_singleton()
                try:
                    if kind == "scenario":
                        scenarios.append(await _live_scenario(job, mode, paths, now, client, max_iterations))
                    else:
                        write_path.append(await _live_write_path(job, mode, paths, client, max_iterations))
                except Exception as exc:  # noqa: BLE001 - one failed world must not discard the others
                    failures.append({"id": job.id, "error": f"{type(exc).__name__}: {exc}"[:500]})
                report["isolation"]["violations"].extend(guard.violations)
            if progress is not None:
                progress(f"[live/{mode}] {job.id}")
        probes = [probe for scenario in scenarios for probe in scenario["probes"]]
        report["modes"][mode] = {
            "summary": summarize_mode(scenarios),
            "answers": _answer_summary(probes),
            "write_path": {
                "pass_rate": rate([item["answer"]["verdict"] == "pass" for item in write_path]),
                "probes": write_path,
            },
            "failures": failures,
            "scenarios": scenarios,
        }
    _reset_session_search_singleton()
    return report


__all__ = ["ABSTAIN_MARKERS", "resolve_live_config", "run_live", "score_answer"]

