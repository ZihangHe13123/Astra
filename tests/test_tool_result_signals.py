"""What the model is told about a tool result: fresh or replayed, whole or partial."""

import asyncio
from pathlib import Path

import pytest

from agent.core.msg import ContentBlock, Msg
from agent.runtime.react import ReActAgent
from agent.runtime.task_store import TaskStore
from agent.runtime.tool_execution import PartialResult
from agent.runtime.tools.registry import ToolDef, ToolRegistry


class _ScriptedLLM:
    """Issue one scripted tool call per request, then finish."""

    class _Config:
        model = "test-model"
        capabilities = frozenset()

    config = _Config()

    def __init__(self, calls: list[tuple[str, str]]):
        self._calls = list(calls)
        self.requests = 0

    async def chat_stream(self, messages, tools):
        index = self.requests
        self.requests += 1
        if index < len(self._calls):
            name, arguments = self._calls[index]
            yield {
                "type": "tool_calls",
                "calls": [{"id": f"call-{index + 1}", "name": name, "arguments": arguments}],
                "content": "",
                "reasoning_content": "",
                "usage": None,
            }
            yield {"type": "done", "content": "", "usage": None}
        else:
            yield {"type": "done", "content": "done", "usage": None}


class _World:
    """A value one tool reports and another tool changes."""

    def __init__(self):
        self.value = "before"
        self.reads = 0

    def register(self, registry: ToolRegistry) -> None:
        async def status() -> str:
            self.reads += 1
            return f"value is {self.value}"

        async def change(to: str) -> str:
            self.value = to
            return "changed"

        registry.register(ToolDef("status", "report the value", {"type": "object"}, status, idempotent=True))
        registry.register(ToolDef(
            "change", "change the value",
            {"type": "object", "properties": {"to": {"type": "string"}}, "required": ["to"]},
            change, risk="write",
        ))


def _tool_messages(agent: ReActAgent) -> list[str]:
    return [str(message.get("content") or "") for message in agent.context.messages if message.get("role") == "tool"]


def _turn(agent: ReActAgent, store: TaskStore | None) -> list[dict]:
    metadata = {}
    if store is not None:
        metadata["task_id"] = store.start_run("req-1", "check the value", session_id="default")["id"]

    async def scenario() -> list[dict]:
        message = Msg(content=[ContentBlock.text("check the value")], metadata=metadata)
        return [event async for event in agent.reply_stream(message)]

    return asyncio.run(scenario())


@pytest.fixture(params=["memory", "durable"])
def store(request, tmp_path: Path) -> TaskStore | None:
    return TaskStore(tmp_path / "tasks.db") if request.param == "durable" else None


def test_observation_runs_again_after_something_changed(store):
    world = _World()
    registry = ToolRegistry()
    world.register(registry)
    llm = _ScriptedLLM([("status", "{}"), ("change", '{"to": "after"}'), ("status", "{}")])
    agent = ReActAgent("agent", llm, registry, max_iterations=6, task_store=store)

    _turn(agent, store)

    first, _, second = _tool_messages(agent)
    assert world.reads == 2
    assert "value is before" in first
    assert "value is after" in second
    assert "not run again" not in second


def test_repeated_observation_says_it_is_a_replay_and_that_one_more_ends_the_turn(store):
    world = _World()
    registry = ToolRegistry()
    world.register(registry)
    llm = _ScriptedLLM([("status", "{}"), ("status", "{}")])
    agent = ReActAgent("agent", llm, registry, max_iterations=6, task_store=store)

    _turn(agent, store)

    first, second = _tool_messages(agent)
    assert world.reads == 1
    assert "not run again" not in first
    assert "identical call" not in first
    assert "value is before" in second
    assert "not run again" in second
    assert "one more identical call" in second


def test_failed_observation_is_tried_again_on_retry(tmp_path: Path):
    attempts = 0

    async def fetch(url: str) -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TimeoutError("upstream timed out")
        return f"body of {url}"

    registry = ToolRegistry()
    registry.register(ToolDef(
        "fetch", "fetch a page",
        {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
        fetch, risk="network", idempotent=True,
    ))
    store = TaskStore(tmp_path / "tasks.db")
    run = store.start_run("req-1", "fetch the page", session_id="default")
    agent = ReActAgent("agent", _ScriptedLLM([]), registry, task_store=store)
    call = {"id": "call-1", "name": "fetch", "arguments": '{"url": "https://example.invalid/a"}'}

    async def scenario() -> tuple[dict, dict]:
        failed = await agent._execute_tool_call(call, task_id=run["id"])
        retried = await agent._execute_tool_call({**call, "id": "call-2"}, task_id=run["id"])
        return failed, retried

    failed, retried = asyncio.run(scenario())

    assert failed["error"]
    assert attempts == 2
    assert retried["output"] == "body of https://example.invalid/a"
    assert not retried.get("error")
    assert not retried.get("cached")


class _BatchLLM:
    """Issue scripted batches of tool calls, one batch per request, then finish."""

    config = _ScriptedLLM.config

    def __init__(self, batches: list[list[tuple[str, str]]]):
        self._batches = list(batches)
        self.requests = 0

    async def chat_stream(self, messages, tools):
        index = self.requests
        self.requests += 1
        if index < len(self._batches):
            yield {
                "type": "tool_calls",
                "calls": [
                    {"id": f"call-{index + 1}-{position}", "name": name, "arguments": arguments}
                    for position, (name, arguments) in enumerate(self._batches[index])
                ],
                "content": "", "reasoning_content": "", "usage": None,
            }
            yield {"type": "done", "content": "", "usage": None}
        else:
            yield {"type": "done", "content": "done", "usage": None}


def _limited_registry(saved: list[str], looked_up: list[str]) -> ToolRegistry:
    async def save(fact: str) -> str:
        saved.append(fact)
        return f"saved {fact}"

    async def look(topic: str) -> str:
        looked_up.append(topic)
        return f"about {topic}"

    registry = ToolRegistry()
    registry.register(ToolDef(
        "save", "save a fact",
        {"type": "object", "properties": {"fact": {"type": "string"}}, "required": ["fact"]},
        save, risk="write", max_calls_per_turn=2,
    ))
    registry.register(ToolDef(
        "look", "look something up",
        {"type": "object", "properties": {"topic": {"type": "string"}}, "required": ["topic"]},
        look,
    ))
    return registry


def test_a_message_over_a_per_turn_limit_is_refused_once_and_the_turn_goes_on():
    saved: list[str] = []
    looked_up: list[str] = []
    llm = _BatchLLM([
        [("look", '{"topic": "a"}'), ("save", '{"fact": "1"}'), ("save", '{"fact": "2"}'), ("save", '{"fact": "3"}')],
        [("look", '{"topic": "a"}'), ("save", '{"fact": "1 and 2"}'), ("save", '{"fact": "3"}')],
    ])
    agent = ReActAgent("agent", llm, _limited_registry(saved, looked_up), max_iterations=6)

    events = _turn(agent, None)

    refused = _tool_messages(agent)[:4]
    # Nothing in the over-limit message ran or counted, so the resend fits.
    assert all("status: error" in message and "was not run" in message for message in refused)
    assert "Send this call again" in refused[0]
    assert all("asked for 3 calls and 2 of its 2 per turn are left" in message for message in refused[1:])
    assert saved == ["1 and 2", "3"]
    assert looked_up == ["a"]
    assert not any(str(event.get("message", "")).startswith("Stopping") for event in events)
    assert agent.context.messages[-1]["content"] == "done"


def test_going_over_the_same_limit_again_ends_the_turn():
    saved: list[str] = []
    over = [("save", '{"fact": "1"}'), ("save", '{"fact": "2"}'), ("save", '{"fact": "3"}')]
    llm = _BatchLLM([over, [(name, arguments.replace('"}', ' again"}')) for name, arguments in over]])
    agent = ReActAgent("agent", llm, _limited_registry(saved, []), max_iterations=6)

    events = _turn(agent, None)

    assert saved == []
    assert any("per-turn tool budget exhausted (save, limit=2)" in str(event.get("message", "")) for event in events)


def test_a_used_up_limit_refuses_one_more_call_and_says_the_next_ends_the_turn():
    saved: list[str] = []
    llm = _BatchLLM([
        [("save", '{"fact": "1"}')], [("save", '{"fact": "2"}')], [("save", '{"fact": "3"}')],
    ])
    registry = _limited_registry(saved, [])
    agent = ReActAgent("agent", llm, registry, max_iterations=6)
    # Keep the tool offered so the test reaches the execution limit itself.
    agent._available_tool_schemas = lambda *_args: registry.to_openai_tools()

    events = _turn(agent, None)

    assert saved == ["1", "2"]
    third = _tool_messages(agent)[2]
    assert "its limit of 2 calls per turn is used up" in third
    assert "ends the turn" in third
    assert not any(str(event.get("message", "")).startswith("Stopping") for event in events)
    assert agent.context.messages[-1]["content"] == "done"


def test_a_call_refused_for_its_arguments_does_not_use_up_a_per_turn_limit():
    served: list[str] = []

    async def serve(name: str) -> str:
        served.append(name)
        return f"served {name}"

    registry = ToolRegistry()
    registry.register(ToolDef(
        "serve", "serve one drink",
        {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
        serve, risk="write", max_calls_per_turn=1,
    ))
    llm = _BatchLLM([[("serve", "{}")], [("serve", '{"name": "tea"}')]])
    agent = ReActAgent("agent", llm, registry, max_iterations=6)

    events = _turn(agent, None)

    first, second = _tool_messages(agent)
    assert "status: error" in first and "name" in first
    assert "served tea" in second
    assert served == ["tea"]
    assert not any(str(event.get("message", "")).startswith("Stopping") for event in events)


def test_a_long_failure_text_is_bounded_and_kept_whole_on_disk(tmp_path: Path):
    from agent.runtime.tool_failure import ToolFailure

    lines = [f"test_case_{number} FAILED: expected {number}" for number in range(4000)]
    full = "\n".join(["collected 4000 items", *lines, "== 4000 failed in 12.3s =="])

    async def run_tests(how: str) -> ToolFailure:
        if how == "raise":
            raise RuntimeError(full)
        return ToolFailure("tests_failed", full, False)

    registry = ToolRegistry(artifact_dir=str(tmp_path / "tool-results"))
    registry.register(ToolDef(
        "run_tests", "run the tests",
        {"type": "object", "properties": {"how": {"type": "string"}}, "required": ["how"]},
        run_tests, risk="execute",
    ))

    for how in ("raise", "report"):
        result = asyncio.run(registry.execute("run_tests", {"how": how}))

        assert len(result["error"]) < 14_000 < len(full)
        # The beginning and, above all, the summary at the end survive.
        assert "collected 4000 items" in result["error"]
        assert "== 4000 failed in 12.3s ==" in result["error"]
        assert result["output_truncated"] is True
        assert full in Path(result["artifact_path"]).read_text(encoding="utf-8")

    agent = ReActAgent("agent", _ScriptedLLM([("run_tests", '{"how": "report"}')]), registry, max_iterations=4)
    _turn(agent, None)
    (message,) = _tool_messages(agent)
    assert "status: error" in message
    assert "inspect the complete result at" in message
    assert len(message) < 15_000


def test_partial_result_is_not_described_as_complete():
    async def page(full: bool) -> str:
        if full:
            return "lines 1-3 of 3"
        return PartialResult("lines 1-3 of 900; continue with offset=3")

    registry = ToolRegistry()
    registry.register(ToolDef(
        "page", "read a page",
        {"type": "object", "properties": {"full": {"type": "boolean"}}, "required": ["full"]},
        page,
    ))
    llm = _ScriptedLLM([("page", '{"full": false}'), ("page", '{"full": true}')])
    agent = ReActAgent("agent", llm, registry, max_iterations=6)

    _turn(agent, None)

    partial, whole = _tool_messages(agent)
    assert "continue with offset=3" in partial
    assert "Result completeness: partial" in partial
    assert "Result completeness: complete" not in partial
    assert "Result completeness: complete" in whole


def test_failure_that_left_partial_output_is_not_described_as_complete():
    from agent.runtime.tool_failure import ToolFailure

    async def scan() -> ToolFailure:
        return ToolFailure(
            code="scan_timeout",
            message="scan stopped after 2 of 9 folders:\nfolder-1\nfolder-2",
            retryable=True,
            partial=True,
        )

    registry = ToolRegistry()
    registry.register(ToolDef("scan", "scan folders", {"type": "object"}, scan))
    agent = ReActAgent("agent", _ScriptedLLM([("scan", "{}")]), registry, max_iterations=4)

    _turn(agent, None)

    (message,) = _tool_messages(agent)
    assert "status: error" in message
    assert "folder-2" in message
    assert "Result completeness: partial" in message
    assert "Result completeness: complete" not in message


def test_failed_postcondition_shows_its_reason_next_to_the_kept_output():
    async def save(path: str) -> str:
        return f"saved {path}"

    registry = ToolRegistry()
    registry.register(ToolDef(
        "save", "save a file",
        {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        save, risk="write",
        postcondition=lambda args, result: (False, "the file on disk differs from what was sent"),
    ))
    agent = ReActAgent("agent", _ScriptedLLM([("save", '{"path": "a.txt"}')]), registry, max_iterations=4)

    _turn(agent, None)

    (message,) = _tool_messages(agent)
    assert "status: error" in message
    assert "saved a.txt" in message
    assert "the file on disk differs from what was sent" in message


_SHAPES = {
    "type": "object",
    "properties": {
        "steps": {
            "type": "array",
            "items": {
                "oneOf": [
                    {
                        "type": "object",
                        "properties": {"kind": {"type": "string", "const": "press"}, "name": {"type": "string"}},
                        "required": ["kind", "name"],
                        "additionalProperties": False,
                    },
                    {
                        "type": "object",
                        "properties": {
                            "kind": {"type": "string", "const": "press"},
                            "x": {"type": "number"},
                            "y": {"type": "number"},
                        },
                        "required": ["kind", "x", "y"],
                        "additionalProperties": False,
                    },
                    {
                        "type": "object",
                        "properties": {
                            "kind": {"type": "string", "const": "pause"},
                            "ms": {"type": "integer", "maximum": 1000},
                        },
                        "required": ["kind", "ms"],
                        "additionalProperties": False,
                    },
                ]
            },
        }
    },
    "required": ["steps"],
}


@pytest.mark.parametrize(
    ("step", "expected", "unexpected"),
    [
        # Two forms of the same kind mixed together: name the stray field and the forms.
        ({"kind": "press", "name": "ok", "x": 1, "y": 2}, ["is not allowed", "name | x + y"], ["pause"]),
        # A kind that does not exist: list the kinds that do.
        ({"kind": "tap", "name": "ok"}, ["$.steps[0].kind must be one of pause, press"], ["is required"]),
        # The right form with one bad value: report that value, not the other forms.
        ({"kind": "pause", "ms": 5000}, ["$.steps[0].ms must be at most 1000"], ["name", "is not allowed"]),
    ],
)
def test_one_of_error_names_the_problem_in_the_form_the_caller_meant(step, expected, unexpected):
    ran = False

    async def act(steps: list) -> str:
        nonlocal ran
        ran = True
        return "ok"

    registry = ToolRegistry()
    registry.register(ToolDef("act", "run steps", _SHAPES, act, strict_schema=True))

    result = asyncio.run(registry.execute("act", {"steps": [step]}))

    assert not ran
    assert result["code"] == "invalid_arguments"
    assert "$.steps[0] does not match oneOf" in result["error"]
    for text in expected:
        assert text in result["error"]
    for text in unexpected:
        assert text not in result["error"].split("does not match oneOf", 1)[1]


def test_valid_one_of_value_still_runs():
    async def act(steps: list) -> str:
        return f"ran {len(steps)}"

    registry = ToolRegistry()
    registry.register(ToolDef("act", "run steps", _SHAPES, act, strict_schema=True))

    result = asyncio.run(registry.execute("act", {"steps": [{"kind": "press", "x": 1, "y": 2}, {"kind": "pause", "ms": 10}]}))

    assert result["output"] == "ran 2"
    assert not result["error"]
