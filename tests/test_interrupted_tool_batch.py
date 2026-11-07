"""A tool batch stopped midway keeps a complete, truthful call chain.

Calls that returned keep their real result, a call that started without
returning has an unknown outcome (and is never replayed), and a call that never
started says so. The saved session, a resumed session and the next request all
show the model what actually happened.
"""

import asyncio
import json
import time

import pytest

from agent.core.msg import ContentBlock, Msg
from agent.runtime.react import ReActAgent
from agent.runtime.session_store import SessionStore
from agent.runtime.task_store import TaskStore
from agent.runtime.tools.registry import ToolDef, ToolRegistry
from agent.runtime.turn_budget import current_turn_budget

CALLS = [
    {"id": "call-write", "name": "write_note", "arguments": json.dumps({"text": "hello"})},
    {"id": "call-read", "name": "slow_read", "arguments": "{}"},
    {"id": "call-second-write", "name": "write_note", "arguments": json.dumps({"text": "second"})},
]


class BatchLLM:
    class config:
        model = "fake"
        capabilities = frozenset()

    def __init__(self, *, batch: bool = True, calls: list[dict] = CALLS):
        self.batch = batch
        self.calls = calls
        self.requests: list[list[dict]] = []

    @staticmethod
    def estimate_tokens(value):
        return len(str(value)) // 4

    async def chat_stream(self, messages, tools):
        del tools
        self.requests.append(messages)
        if self.batch and len(self.requests) == 1:
            yield {
                "type": "tool_calls", "calls": [dict(call) for call in self.calls], "content": "Writing then reading.",
                "reasoning_content": "", "finish_reason": "tool_calls", "usage": None,
            }
            return
        yield {"type": "chunk", "content": "ok"}
        yield {"type": "done", "content": "ok", "usage": None}


def _registry(note, read_started: asyncio.Event) -> ToolRegistry:
    async def write_note(text: str):
        with note.open("a", encoding="utf-8") as handle:
            handle.write(text + "\n")
        return f"wrote {len(text)} bytes to note.txt"

    async def slow_read():
        read_started.set()
        await asyncio.Event().wait()

    registry = ToolRegistry()
    registry.register(ToolDef(
        name="write_note", description="write", fn=write_note, risk="write",
        parameters={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
    ))
    registry.register(ToolDef(
        name="slow_read", description="read", fn=slow_read, risk="read",
        parameters={"type": "object", "properties": {}},
    ))
    return registry


def _chain(messages: list[dict], expected: list[dict] = CALLS) -> list[tuple[str, str]]:
    """The batch's tool-call chain, as (call id, result) pairs in history order."""
    with_calls = [message for message in messages if message.get("tool_calls")]
    assert len(with_calls) == 1, "the batch's tool calls must stay in history"
    calls = with_calls[0]
    index = messages.index(calls)
    results = messages[index + 1:index + 1 + len(calls["tool_calls"])]
    assert [call["id"] for call in calls["tool_calls"]] == [call["id"] for call in expected]
    assert all(message["role"] == "tool" for message in results)
    return [(message["tool_call_id"], message["content"]) for message in results]


def _assert_truthful_chain(messages: list[dict], reason: str) -> None:
    chain = dict(_chain(messages))
    assert list(chain) == [call["id"] for call in CALLS]
    assert chain["call-write"].startswith("[Tool result: write_note | status: success]\nwrote 5 bytes to note.txt")
    unknown = chain["call-read"]
    assert unknown.startswith("[Tool result: slow_read | status: error]\n[ToolOutcomeUnknown] slow_read started")
    assert reason in unknown and "may have taken effect" in unknown and "Error code: outcome_unknown" in unknown
    assert "Result completeness: none" in unknown and "Result completeness: complete" not in unknown
    not_started = chain["call-second-write"]
    assert not_started.startswith("[Tool result: write_note | status: error]\n[ToolNotStarted] write_note did not start")
    assert reason in not_started and "Error code: not_started" in not_started
    assert not any("Session recovery" in str(message.get("content")) for message in messages)


async def _run_interrupted(tmp_path, *, cancel: bool):
    note = tmp_path / "note.txt"
    session = tmp_path / "sessions" / "batch.json"
    read_started = asyncio.Event()
    llm = BatchLLM()
    store = TaskStore(tmp_path / "tasks.db")
    task = store.start_run("req-1", "write then read", session_id="batch", model="fake")
    agent = ReActAgent(
        "agent", llm, _registry(note, read_started), system_prompt="base", task_store=store,
        turn_timeout_seconds=0 if cancel else 1.0,
    )
    agent.context.set_session(str(session))
    events: list[dict] = []

    async def consume():
        message = Msg(content=[ContentBlock.text("write then read")], metadata={"task_id": task["id"]})
        async for event in agent.reply_stream(message):
            events.append(event)

    turn = asyncio.create_task(consume())
    await asyncio.wait_for(read_started.wait(), timeout=5)
    if cancel:
        await asyncio.sleep(0.05)
        turn.cancel()
    await asyncio.wait_for(asyncio.gather(turn, return_exceptions=True), timeout=10)
    return agent, llm, store, task, session, note, events


@pytest.mark.parametrize(("cancel", "reason", "code"), [
    (True, "the turn was cancelled", "cancelled"),
    (False, "the turn ran out of time", "turn_budget_exhausted"),
])
def test_interrupted_batch_keeps_completed_results_and_marks_the_rest(tmp_path, cancel, reason, code):
    async def scenario():
        agent, llm, store, task, session, note, events = await _run_interrupted(tmp_path, cancel=cancel)

        # The write happened once; the queued second write never ran.
        assert note.read_text(encoding="utf-8") == "hello\n"
        assert [event["id"] for event in events if event.get("type") == "tool_result"] == ["call-write"]
        assert [event.get("code") for event in events if event.get("type") == "error"] == [code]
        steps = {step["name"]: step["status"] for step in store.get_task(task["id"])["steps"] if step["kind"] == "tool"}
        assert steps == {"write_note": "completed", "slow_read": "unknown"}

        _assert_truthful_chain(agent.context.messages, reason)
        saved = SessionStore(session).load(readonly=True)["messages"]
        assert saved == json.loads(json.dumps(agent.context.messages))

        # The next request shows the model the same chain, unchanged.
        async for _ in agent.reply_stream(Msg(content=[ContentBlock.text("continue")])):
            pass
        follow_up = [message for message in llm.requests[-1] if message["role"] in {"assistant", "tool"}]
        _assert_truthful_chain(follow_up, reason)
        assert note.read_text(encoding="utf-8") == "hello\n"
        return session

    session = asyncio.run(scenario())

    # A resumed session reads the same chain and runs nothing again.
    resumed_llm = BatchLLM(batch=False)
    resumed = ReActAgent(
        "agent", resumed_llm, _registry(tmp_path / "note.txt", asyncio.Event()), system_prompt="base",
    )
    resumed.context.set_session(str(session))
    assert resumed.context.load()
    _assert_truthful_chain(resumed.context.messages, reason)

    async def resume():
        async for _ in resumed.reply_stream(Msg(content=[ContentBlock.text("what happened?")])):
            pass

    asyncio.run(resume())
    _assert_truthful_chain(
        [message for message in resumed_llm.requests[-1] if message["role"] in {"assistant", "tool"}], reason,
    )
    assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "hello\n"


def test_a_call_cancelled_while_awaiting_approval_is_recorded_as_not_started(tmp_path):
    calls = [CALLS[0], {"id": "call-guarded", "name": "guarded_write", "arguments": json.dumps({"text": "x"})}]
    note = tmp_path / "note.txt"
    session = tmp_path / "sessions" / "approval.json"
    asked = asyncio.Event()
    entered: list[str] = []

    async def wait_for_person(request: dict) -> str:
        del request
        asked.set()
        await asyncio.Event().wait()
        return "once"

    def guarded_write(text: str):
        entered.append(text)
        return "written"

    registry = _registry(note, asyncio.Event())
    registry.register(ToolDef(
        name="guarded_write", description="write after approval", fn=guarded_write, risk="write",
        parameters={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        permission_check=lambda args: {"kind": "fixture_write", "scope": "fixture:x", "target": "x"},
        permission_grant=lambda args, request, decision: None,
    ))
    registry.set_approval_handler(wait_for_person)
    agent = ReActAgent("agent", BatchLLM(calls=calls), registry, system_prompt="base")
    agent.context.set_session(str(session))

    async def scenario():
        async def consume():
            async for _ in agent.reply_stream(Msg(content=[ContentBlock.text("write twice")])):
                pass

        turn = asyncio.create_task(consume())
        await asyncio.wait_for(asked.wait(), timeout=5)
        turn.cancel()
        await asyncio.wait_for(asyncio.gather(turn, return_exceptions=True), timeout=10)

    asyncio.run(scenario())

    # The guarded tool was never entered: the person had not answered yet.
    assert entered == []
    assert note.read_text(encoding="utf-8") == "hello\n"
    for messages in (agent.context.messages, SessionStore(session).load(readonly=True)["messages"]):
        chain = dict(_chain(messages, calls))
        assert chain["call-write"].startswith("[Tool result: write_note | status: success]")
        assert chain["call-guarded"].startswith(
            "[Tool result: guarded_write | status: error]\n[ToolNotStarted] guarded_write did not start: "
            "the turn was cancelled first"
        )
        assert "ToolOutcomeUnknown" not in chain["call-guarded"]


def test_refusals_and_the_budget_check_before_a_call_keep_their_own_labels(tmp_path):
    calls = [
        {"id": "call-invalid", "name": "write_note", "arguments": "{}"},
        {"id": "call-wait", "name": "wait_past_work_deadline", "arguments": "{}"},
        {"id": "call-late", "name": "write_note", "arguments": json.dumps({"text": "late"})},
    ]
    note = tmp_path / "note.txt"
    session = tmp_path / "sessions" / "budget.json"

    async def wait_past_work_deadline():
        budget = current_turn_budget()
        assert budget is not None
        await asyncio.sleep(max(0.0, budget.work_deadline - time.monotonic()) + 0.02)
        return "waited"

    registry = _registry(note, asyncio.Event())
    registry.register(ToolDef(
        name="wait_past_work_deadline", description="wait", fn=wait_past_work_deadline, risk="write",
        parameters={"type": "object", "properties": {}},
    ))
    agent = ReActAgent(
        "agent", BatchLLM(calls=calls), registry, system_prompt="base", turn_timeout_seconds=1.0,
    )
    agent.context.set_session(str(session))

    async def scenario():
        return [event async for event in agent.reply_stream(Msg(content=[ContentBlock.text("go")]))]

    events = asyncio.run(scenario())

    assert [event.get("code") for event in events if event.get("type") == "error"] == ["turn_budget_exhausted"]
    assert not note.exists()
    for messages in (agent.context.messages, SessionStore(session).load(readonly=True)["messages"]):
        chain = dict(_chain(messages, calls))
        # Refused by argument validation: its own result, not a relabel.
        assert chain["call-invalid"].startswith("[Tool result: write_note | status: error]\n[ToolInputError]")
        assert chain["call-wait"].startswith("[Tool result: wait_past_work_deadline | status: success]\nwaited")
        # Stopped by the budget check before its tool was entered.
        assert chain["call-late"].startswith(
            "[Tool result: write_note | status: error]\n[ToolNotStarted] write_note did not start: "
            "the turn ran out of time first"
        )
