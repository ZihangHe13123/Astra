"""Offline provider-to-session fault replay. No live provider or user profile is loaded."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from openai.types.chat import ChatCompletionChunk
from pydantic import BaseModel, ConfigDict, Field

from agent.core.msg import ContentBlock, Msg
from agent.runtime.context import AgentContext
from agent.runtime.llm import LLMClient, LLMConfig, OpenAICompatibleProvider
from agent.runtime.react import ReActAgent
from agent.runtime.task_store import TaskStore
from agent.runtime.tools.registry import ToolDef, ToolRegistry

DEFAULT_SUITE = Path(__file__).resolve().parents[2] / "evals/turn_faults.json"


class ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CallDelta(ClosedModel):
    index: int = Field(ge=0, le=20)
    id: str | None = None
    name: str | None = None
    arguments: str | None = None


class Frame(ClosedModel):
    kind: Literal["chunk", "stall"] = "chunk"
    content: str | None = None
    reasoning: str | None = None
    finish: Literal["stop", "tool_calls", "length"] | None = None
    calls: list[CallDelta] = Field(default_factory=list, max_length=20)

    def chunk(self) -> ChatCompletionChunk:
        return ChatCompletionChunk.model_validate({
            "id": "replay", "object": "chat.completion.chunk", "created": 0, "model": "replay",
            "choices": [{"index": 0, "finish_reason": self.finish, "delta": {
                "content": self.content, "reasoning_content": self.reasoning,
                "tool_calls": [{"index": c.index, "id": c.id, "type": "function",
                                "function": {"name": c.name, "arguments": c.arguments}} for c in self.calls] or None,
            }}],
        })


class Response(ClosedModel):
    frames: list[Frame] = Field(min_length=1, max_length=200)
    expect_tool_text: str = ""


class Expected(ClosedModel):
    requests: int = Field(ge=1, le=20)
    writes: list[str] = Field(default_factory=list)
    reads: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    recoverable_errors: list[str] = Field(default_factory=list)
    tool_errors: dict[str, str] = Field(default_factory=dict)
    unknown_tools: int = 0
    resume_kind: str = ""
    selected_clicks: int | None = None
    selected: list[str] | None = None
    submits: int | None = None


class TurnCase(ClosedModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,79}$")
    browser: bool = False
    budget_seconds: float = Field(default=0.0, ge=0, le=60)
    cancel_on_tool: bool = False
    responses: list[Response] = Field(min_length=1, max_length=20)
    expected: Expected


class Suite(ClosedModel):
    schema_version: Literal[1]
    cases: list[TurnCase] = Field(min_length=1, max_length=100)


def load_suite(path: Path = DEFAULT_SUITE) -> Suite:
    with path.open("rb") as source:
        data = source.read(512_001)
    if len(data) > 512_000:
        raise ValueError("Replay suite exceeds 512 KB")
    suite = Suite.model_validate_json(data)
    if len({case.id for case in suite.cases}) != len(suite.cases):
        raise ValueError("Replay case ids must be unique")
    return suite


class ReplayStream:
    def __init__(self, frames: list[Frame]):
        self.frames = iter(frames)
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        frame = next(self.frames, None)
        if frame is None:
            raise StopAsyncIteration
        if frame.kind == "stall":
            await asyncio.Event().wait()
        return frame.chunk()

    async def aclose(self):
        self.closed = True


class ReplayProvider(OpenAICompatibleProvider):
    def __init__(self, responses: list[Response]):
        # Deliberately no SDK/HTTP client or env-configured provider initialization.
        self.config = LLMConfig(model="replay", base_url="https://offline.invalid",
                                max_retries=0, idle_timeout=0.12, overall_timeout=5)
        self._estimate_calibration = 1.0
        self.responses = responses
        self.requests: list[dict] = []
        self.streams: list[ReplayStream] = []

    def _tokenize_url(self) -> None:
        return None

    async def _create_completion(self, kwargs):
        index = len(self.requests)
        if index >= len(self.responses):
            raise AssertionError("Unexpected provider request: recovery or tool replay exceeded fixture")
        self.requests.append(kwargs)
        response = self.responses[index]
        tool_text = "\n".join(str(m.get("content", "")) for m in kwargs["messages"] if m.get("role") == "tool")
        if response.expect_tool_text and response.expect_tool_text not in tool_text:
            raise AssertionError(f"Request {index + 1} did not observe expected tool output")
        stream = ReplayStream(response.frames)
        self.streams.append(stream)
        return stream


async def run_case(case: TurnCase, root: Path, *, emit: Callable[[dict], None] | None = None) -> dict:
    allowed = {"fixture_read", "fixture_write", "fixture_wait"}
    if case.browser:
        allowed.update({"browser_open", "browser_snapshot", "browser_check"})
    for response in case.responses:
        for frame in response.frames:
            for call in frame.calls:
                if call.name and call.name not in allowed:
                    raise ValueError(f"Tool is not part of the offline replay fixture: {call.name}")
    root.mkdir(parents=True, exist_ok=True)
    if any(root.iterdir()):
        raise ValueError("Replay requires a fresh artifact directory; existing sessions must not affect its oracle")
    started = time.monotonic()
    registry = ToolRegistry(artifact_dir=root / "artifacts")
    # Recorded responses call the fixture tools by name, as when every tool was sent.
    registry.disclosure = "full"
    writes: list[str] = []
    reads: list[str] = []
    tool_started = asyncio.Event()
    state_file = root / "state.txt"
    state_file.write_text("before")

    async def fixture_write(value: str):
        state_file.write_text(value)
        writes.append(value)
        return value

    async def fixture_read():
        value = state_file.read_text()
        reads.append(value)
        return value

    async def fixture_wait(value: str):
        await fixture_write(value)
        tool_started.set()
        await asyncio.Event().wait()

    for name, fn, risk, params in [
        ("fixture_write", fixture_write, "write", {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]}),
        ("fixture_read", fixture_read, "read", {"type": "object", "properties": {}}),
        ("fixture_wait", fixture_wait, "write", {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]}),
    ]:
        registry.register(ToolDef(name, name, params, fn, risk=risk))

    browser = None
    case_data = case.model_dump()
    if case.browser:
        from agent.evals.turn_replay_browser import ReplayBrowser
        browser = ReplayBrowser(root)
        await browser.start(registry)
        # Only an isolated fixture URL may be navigated by a browser replay.
        for response in case_data["responses"]:
            for frame in response["frames"]:
                for call in frame["calls"]:
                    if call["name"] == "browser_open":
                        args = json.loads(call["arguments"] or "{}")
                        if args != {"url": "${FORM_URL}", "extract": False}:
                            await browser.close()
                            raise ValueError("Browser replay may open only its local fixture")
                        call["arguments"] = json.dumps({"url": browser.url, "extract": False})
    provider = ReplayProvider(TurnCase.model_validate(case_data).responses)
    if case.budget_seconds:
        # Budget scenarios include real task/session writes before their stall.
        # Keep provider timers later so they cannot preempt the intended fault.
        provider.config.idle_timeout = max(0.12, case.budget_seconds * 2)
        provider.config.overall_timeout = max(5, case.budget_seconds * 2)
    store = TaskStore(root / "tasks.db")
    task = store.start_run("replay", case.id, session_id="replay", model="replay")
    agent = ReActAgent("replay", LLMClient(provider.config, provider=provider), registry, task_store=store, max_iterations=10,
                       system_prompt="Run the local fixture, verify state, then reply REPLAY VERIFIED.",
                       timing_log_enabled=False, query_profile_enabled=False, progressive_tools=False,
                       turn_timeout_seconds=case.budget_seconds)
    session_path = root / "session.json"
    agent.context.set_session(str(session_path))
    events: list[dict] = []
    async def consume():
        async for event in agent.reply_stream(Msg(content=[ContentBlock.text("Run the fixture")], metadata={"task_id": task["id"]})):
            events.append(event)
            if emit:
                emit(event)

    turn = asyncio.create_task(consume())
    async def cancel_on_start():
        await tool_started.wait()
        turn.cancel()
    cancel = asyncio.create_task(cancel_on_start()) if case.cancel_on_tool else None
    try:
        await asyncio.wait_for(turn, 30)
        agent.context.save()
        restored = AgentContext()
        restored.set_session(str(session_path))
        restored.load()
        history = restored.messages
        calls = [c["id"] for m in history for c in m.get("tool_calls", [])]
        results = [m["tool_call_id"] for m in history if m["role"] == "tool"]
        stored = store.get_task(task["id"]) or {}
        actual = {
            "requests": len(provider.requests), "writes": writes, "reads": reads,
            "errors": [e.get("code", "") for e in events if e["type"] == "error" and not e.get("recoverable")],
            "recoverable_errors": [e.get("code", "") for e in events if e["type"] == "error" and e.get("recoverable")],
            "tool_errors": {e.get("id", ""): e.get("code", "") for e in events if e["type"] == "tool_result" and e.get("error")},
            "unknown_tools": sum(s["kind"] == "tool" and s["status"] == "unknown" for s in stored.get("steps", [])),
            "resume_kind": stored.get("checkpoint", {}).get("resume_kind", ""),
        }
        if browser:
            actual.update(await browser.oracle())
        expected = case.expected.model_dump(exclude_none=True)
        checks = {key: actual.get(key) == value for key, value in expected.items()}
        checks.update({
            "one_done": sum(e["type"] == "done" for e in events) == 1,
            "streams_closed": all(s.closed for s in provider.streams),
            "session_tool_pairing": calls == results and len(calls) == len(set(calls)),
            "file_state": state_file.read_text() == (writes[-1] if writes else "before"),
        })
        report = {"id": case.id, "passed": all(checks.values()), "checks": checks, "actual": actual,
                  "duration_ms": round((time.monotonic() - started) * 1000)}
        (root / "report.json").write_text(json.dumps(report, indent=2))
        return report
    finally:
        if cancel:
            cancel.cancel()
            await asyncio.gather(cancel, return_exceptions=True)
        if not turn.done():
            turn.cancel()
            await asyncio.gather(turn, return_exceptions=True)
        if browser:
            await browser.close()


async def stdio_case(case: TurnCase, root: Path):
    from agent.cli import backend
    from agent.cli.stream_events import tool_progress_event, tool_result_event
    from agent.runtime.event_stream import RuntimeEventStream
    backend._runtime_event_stream = RuntimeEventStream(root / "events.db")
    backend._write_event({"type": "backend_hello", "protocol_version": 1})
    pending: dict[str, str] = {}
    def emit(event):
        if event["type"] == "tool_calls":
            pending.update({c["id"]: c["name"] for c in event["calls"]})
        elif event["type"] == "tool_result":
            pending.pop(str(event.get("id", "")), None)
            event = tool_result_event(event)
        elif event["type"] == "tool_progress":
            event = tool_progress_event(event)
        elif event["type"] == "done":
            backend._finish_pending_tool_calls(pending, backend._send, "[ToolInterrupted] Tool ended without a result.", "tool_result_missing")
        backend._send(event)
    run = 0
    try:
        while line := await asyncio.to_thread(sys.stdin.readline):
            request = json.loads(line)
            if request.get("type") == "message":
                run += 1
                report = await run_case(case, root / str(run), emit=emit)
                backend._send({"type": "tool_result", "name": "replay_oracle", "output": json.dumps(report), "error": "" if report["passed"] else "Replay failed"})
    finally:
        backend._runtime_event_stream = None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    parser.add_argument("--case", default="")
    parser.add_argument("--browser", action="store_true", help="Include isolated real-browser cases")
    parser.add_argument("--stdio", action="store_true", help="Offline subprocess fixture for the real TUI receiver")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    suite = load_suite(args.suite)
    cases = [c for c in suite.cases if (not args.case or c.id == args.case) and (args.browser or not c.browser)]
    if not cases or (args.stdio and len(cases) != 1):
        parser.error("Select an existing case; stdio requires exactly one non-browser case")
    with tempfile.TemporaryDirectory(prefix="astra-turn-replay-") as directory:
        root = args.output or Path(directory)
        root.mkdir(parents=True, exist_ok=True)
        if args.stdio:
            asyncio.run(stdio_case(cases[0], root))
            return 0
        if args.output:
            root = Path(tempfile.mkdtemp(prefix="run-", dir=root))
        async def run():
            return [await run_case(c, root / c.id) for c in cases]
        reports = asyncio.run(run())
        print(json.dumps({"schema_version": 1, "artifact_dir": str(root.resolve()) if args.output else "",
                          "passed": all(r["passed"] for r in reports), "cases": reports}, indent=2))
        return 0 if all(r["passed"] for r in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
