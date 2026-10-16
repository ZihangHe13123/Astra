"""Drive Astra's real agent and prompt assembly over seeded scenario worlds.

The agent is assembled like the backend does (memory store, task store, skill
store, persona prompt, Context Index broker from the real factory, memory,
session-search and Context Index tools), and each turn follows the backend's
order: log the user text to Session Recall, start a task run, stream the
reply, finish the run and log the assistant text. Only the model differs: the
offline tier answers instantly through a capturing fake provider.
"""

from __future__ import annotations

import asyncio
import copy
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .fixture import Fixture, ScenarioSpec
from .isolation import StatePaths, isolated_state, path_guard, resolved_state_locations
from .scoring import analyze_requests, score_probe, summarize_mode
from .world import SeededWorld, seed_world

CONTEXT_INDEX_MODES = ("off", "session", "all")
OFFLINE_ANSWER = "OK."


@dataclass
class RequestRecorder:
    """Capture every provider request with the canonical history at that moment."""

    snapshot: Callable[[], list[dict]] | None = None
    requests: list[dict[str, Any]] = field(default_factory=list)
    _turn: int | None = None
    _turn_started: float | None = None

    def begin_turn(self, turn: int) -> None:
        self._turn = turn
        self._turn_started = time.perf_counter()

    def record(self, messages: list[dict], tools: list[dict]) -> None:
        called = time.perf_counter()
        latency = None if self._turn_started is None else (called - self._turn_started) * 1000
        self._turn_started = None  # only the first request of a turn measures preparation
        self.requests.append({
            "turn": self._turn,
            "latency_ms": None if latency is None else round(latency, 3),
            "messages": copy.deepcopy(messages),
            "tools": copy.deepcopy(tools),
            "canonical": copy.deepcopy(self.snapshot()) if self.snapshot is not None else [],
        })


class OfflineLLM:
    """A provider that records the request and answers at once."""

    def __init__(self, recorder: RequestRecorder, answer: str = OFFLINE_ANSWER):
        from agent.runtime.llm import LLMConfig

        self.recorder = recorder
        self.answer = answer
        self.config = LLMConfig(
            model="memory-eval-offline", api_key="offline", base_url="http://127.0.0.1:9/v1",
            provider="openai-compatible",
        )

    async def chat_stream(self, messages, tools, **kwargs):
        del kwargs
        self.recorder.record(messages, tools)
        yield {"type": "chunk", "content": self.answer}
        yield {"type": "done", "content": self.answer, "usage": None}


class RecordingLLM:
    """Wrap a real client: record each request, then delegate unchanged."""

    def __init__(self, client: Any, recorder: RequestRecorder):
        self._client = client
        self.recorder = recorder

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    def chat_stream(self, messages, tools, **kwargs):
        self.recorder.record(messages, tools)
        return self._client.chat_stream(messages=messages, tools=tools, **kwargs)


@dataclass
class AgentBundle:
    agent: Any
    recorder: RequestRecorder
    memory_store: Any
    task_store: Any
    broker: Any
    recall: Any
    workspace: Any
    model: str

    def close(self) -> None:
        try:
            self.agent.end_session("memory-eval")
        finally:
            self.recall.close()


def build_agent(
    mode: str,
    llm: Any,
    recorder: RequestRecorder,
    paths: StatePaths,
    *,
    session_name: str,
    max_iterations: int = 6,
) -> AgentBundle:
    """Assemble the agent the way ``agent.cli.backend`` does, under isolated state."""
    from agent.cli.context_index_preferences import ContextIndexPreferences
    from agent.cli.persona_preferences import startup_persona
    from agent.runtime.context_index import create_context_index_broker
    from agent.runtime.context_index.workspace import resolve_workspace
    from agent.runtime.memory import MemoryStore
    from agent.runtime.react import ReActAgent
    from agent.runtime.session_recall import SessionRecall
    from agent.runtime.skills import SkillStore
    from agent.runtime.task_store import TaskStore
    from agent.runtime.tools.context_index import register_context_index_tools
    from agent.runtime.tools.memory import register_memory_tools
    from agent.runtime.tools.registry import ToolRegistry
    from agent.runtime.tools.session_recall import register_session_recall_tools
    from agent.runtime.tools.time import register_time_tools

    if mode not in CONTEXT_INDEX_MODES:
        raise ValueError(f"mode must be one of {', '.join(CONTEXT_INDEX_MODES)}")
    memory_store = MemoryStore()
    task_store = TaskStore()
    skill_store = SkillStore()
    tools = ToolRegistry()
    holder: dict[str, Any] = {}

    def session_scope() -> str:
        return holder["agent"].context.session_scope if holder else "default"

    register_memory_tools(tools, memory_store, session_id=session_scope,
                          messages=lambda: holder["agent"].context.messages if holder else [])
    register_session_recall_tools(tools)
    register_time_tools(tools)
    broker = create_context_index_broker(ContextIndexPreferences(mode, 900), str(paths.workspace))
    broker.start_background(memory_store.path)
    register_context_index_tools(tools, broker)
    _name, system_prompt, persona_state = startup_persona(None)
    agent = ReActAgent(
        name="agent",
        llm_client=llm,
        tool_registry=tools,
        system_prompt=system_prompt,
        max_iterations=max_iterations,
        task_store=task_store,
        memory_store=memory_store,
        skill_store=skill_store,
        context_index_broker=broker,
    )
    holder["agent"] = agent
    agent.set_persona(persona_state, system_prompt)
    agent.context.set_session(str(paths.sessions / f"{session_name}.json"))
    agent.begin_session()
    recorder.snapshot = lambda: agent.context.get_prompt(include_runtime_context=False)
    recall = SessionRecall()
    recall.init_db()
    model = str(getattr(getattr(llm, "config", None), "model", "") or "unknown")
    return AgentBundle(agent, recorder, memory_store, task_store, broker, recall,
                       resolve_workspace(paths.workspace), model)


def start_new_session(bundle: AgentBundle, paths: StatePaths, session_name: str) -> None:
    """Follow the backend's /reset order to begin a fresh conversation."""
    agent = bundle.agent
    agent.end_session("reset")
    agent.reset_conversation()
    agent.context.set_session(str(paths.sessions / f"{session_name}.json"))
    agent.begin_session()


def _index_trace(bundle: AgentBundle) -> dict[str, Any] | None:
    trace = getattr(bundle.broker, "last_trace", None)
    if trace is None:
        return None
    return {"decision": getattr(trace, "decision", ""), "intent": getattr(trace, "intent", ""),
            "selected": getattr(trace, "selected_count", 0), "displayed": len(getattr(trace, "displayed", []) or []),
            "latency_ms": round(float(getattr(trace, "latency_ms", 0.0) or 0.0), 2),
            "source_status": dict(getattr(trace, "source_status", {}) or {})}


async def run_turn(bundle: AgentBundle, text: str, *, request_id: str, turn: int) -> dict[str, Any]:
    """One user turn in the backend's order, with Session Recall auto-logging."""
    from agent.core.msg import ContentBlock, Msg

    agent = bundle.agent
    stem = Path(agent.context.session_path).stem
    recall_session = bundle.recall.get_or_create_session(
        stem, title=stem, personality=agent.context.persona_id,
        workspace_key=bundle.workspace.key, workspace_root=bundle.workspace.root,
    )
    bundle.recall.log_message(recall_session, "user", text)
    run = bundle.task_store.start_run(request_id, text, session_id=agent.context.session_scope, model=bundle.model)
    message = Msg(content=[ContentBlock.text(text)], id=request_id,
                  metadata={"request_id": request_id, "task_id": run["id"], "runtime_task_id": run["id"]})
    before = len(bundle.recorder.requests)
    bundle.recorder.begin_turn(turn)
    chunks: list[str] = []
    errors: list[str] = []
    tool_calls: list[str] = []
    async for event in agent.reply_stream(message):
        kind = event.get("type")
        if kind == "chunk":
            chunks.append(str(event.get("content", "")))
        elif kind == "error":
            errors.append(str(event.get("code") or event.get("message") or "error"))
        elif kind == "tool_calls":
            tool_calls.extend(str(call.get("name") or "") for call in event.get("calls") or [])
    bundle.task_store.finish_run(run["id"], "failed" if errors else "completed", errors[0] if errors else "")
    reply = "".join(chunks)
    if reply:
        bundle.recall.log_message(recall_session, "assistant", reply)
    return {
        "reply": reply, "errors": errors, "tool_calls": tool_calls,
        "requests": len(bundle.recorder.requests) - before,
        "index": _index_trace(bundle),
    }


async def _run_scenario(scenario: ScenarioSpec, mode: str, paths: StatePaths, now: datetime) -> dict[str, Any]:
    world = seed_world(scenario, now=now, workspace=paths.workspace)
    recorder = RequestRecorder()
    bundle = build_agent(mode, OfflineLLM(recorder), recorder, paths, session_name=f"session_eval_{scenario.id}")
    turns = []
    try:
        for index, turn in enumerate(scenario.conversation, 1):
            outcome = await run_turn(bundle, turn.text, request_id=f"{scenario.id}-{index:02d}", turn=index)
            turns.append((turn, outcome))
    finally:
        bundle.close()
    return evaluate_conversation(world, turns, recorder.requests, mode)


def evaluate_conversation(
    world: SeededWorld,
    turns: list[tuple[Any, dict[str, Any]]],
    requests: list[dict[str, Any]],
    mode: str,
) -> dict[str, Any]:
    analyses = analyze_requests(requests, world.core_entries)
    first_by_turn: dict[int, dict[str, Any]] = {}
    for item in analyses:
        first_by_turn.setdefault(int(item["turn"]), item)
    # A turn that failed before reaching the provider has no request to score.
    missing: dict[str, Any] = dict.fromkeys((
        "injected_messages", "augmented_messages", "injected_chars", "core_chars", "injected_tokens",
        "core_tokens", "other_tokens", "fresh_tokens"), 0)
    missing.update({"latency_ms": None, "prefix_stable": None, "injected_text": "", "current_text": ""})
    probes = []
    for index, (turn, outcome) in enumerate(turns, 1):
        if not turn.is_probe:
            continue
        analysis = first_by_turn.get(index, missing)
        probe = score_probe(turn, world, analysis)
        probe.update({
            "scenario": world.scenario.id, "turn": index, "errors": outcome["errors"],
            "index": outcome["index"], "current_context": analysis["current_text"],
            "reply": outcome["reply"], "tool_calls": outcome["tool_calls"], "requests_in_turn": outcome["requests"],
        })
        probes.append(probe)
    return {
        "id": world.scenario.id,
        "title": world.scenario.title,
        "mode": mode,
        "core_chars": {"memory": len("\n§\n".join(world.scenario.core_memory)),
                       "user": len("\n§\n".join(world.scenario.core_user))},
        "turns": len(turns),
        "requests": [
            {key: value for key, value in analysis.items() if key not in {"injected_text", "current_text"}}
            for analysis in analyses
        ],
        "probes": probes,
    }


def run_offline(
    fixture: Fixture,
    modes: list[str],
    state_root: Path,
    *,
    guard: bool = True,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Replay every scenario in each mode, each in its own isolated state directory."""
    now = datetime.now().astimezone()
    state_root = Path(state_root).resolve()
    report: dict[str, Any] = {"modes": {}, "isolation": {"violations": [], "resolved_paths": {}}}
    for mode in modes:
        scenarios = []
        for scenario in fixture.scenarios:
            root = state_root / mode / scenario.id
            with isolated_state(root) as paths:
                if not report["isolation"]["resolved_paths"]:
                    report["isolation"]["resolved_paths"] = {
                        name: str(path) for name, path in resolved_state_locations().items()
                    }
                if guard:
                    with path_guard(state_root) as active:
                        result = asyncio.run(_run_scenario(scenario, mode, paths, now))
                    report["isolation"]["violations"].extend(active.violations)
                else:
                    result = asyncio.run(_run_scenario(scenario, mode, paths, now))
            scenarios.append(result)
            if progress is not None:
                progress(f"[{mode}] {scenario.id}: {len(result['probes'])} probes, {len(result['requests'])} requests")
        report["modes"][mode] = {"summary": summarize_mode(scenarios), "scenarios": scenarios}
    return report


__all__ = [
    "AgentBundle",
    "CONTEXT_INDEX_MODES",
    "OfflineLLM",
    "RecordingLLM",
    "RequestRecorder",
    "build_agent",
    "evaluate_conversation",
    "isolated_state",
    "run_offline",
    "run_turn",
    "start_new_session",
]
