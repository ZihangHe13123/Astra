import asyncio
import gc
import json

import pytest

from agent.channels.router import active_channel
from agent.core.msg import ContentBlock, Msg
from agent.runtime.memory import MemoryStore
from agent.runtime.prompts import AGENT_CORE_PROMPT
from agent.runtime.react import ReActAgent
from agent.runtime.task_store import TaskStore
from agent.runtime.tools.goals import register_goal_tools
from agent.runtime.tools.plans import register_plan_tools
from agent.runtime.tools.registry import ToolDef, ToolRegistry
from agent.runtime.tools.user_questions import (
    UserQuestionCancelled,
    UserQuestionUnavailable,
    normalize_answers,
    normalize_questions,
    register_user_question_tools,
)
from agent.runtime.user_questions import UserQuestionBroker


class ClarifyThenExecuteLLM:
    class _Config:
        model = "test-model"
        capabilities = frozenset({"reasoning"})

    def __init__(self):
        self.calls = 0
        self.prompts = []
        self.config = self._Config()

    async def chat_stream(self, messages, tools):
        self.calls += 1
        self.prompts.append(messages)
        if self.calls == 1:
            yield {"type": "tool_calls", "calls": [{
                "id": "ask-1", "name": "ask_user_question",
                "arguments": json.dumps({"questions": sample_questions()}),
            }], "content": "", "reasoning_content": "", "usage": None}
        elif self.calls == 2:
            yield {"type": "tool_calls", "calls": [{
                "id": "plan-1", "name": "plan_update",
                "arguments": json.dumps({
                    "goal": "Implement SQLite choice",
                    "steps": [
                        {"text": "Record choice", "status": "in_progress"},
                        {"text": "Verify result", "status": "pending"},
                    ],
                }),
            }, {
                "id": "goal-1", "name": "goal",
                "arguments": json.dumps({"action": "set", "objective": "Implement SQLite choice"}),
            }], "content": "", "reasoning_content": "", "usage": None}
        elif self.calls == 3:
            yield {"type": "tool_calls", "calls": [{
                "id": "work-1", "name": "record_work",
                "arguments": json.dumps({"value": "SQLite (Recommended)"}),
            }], "content": "", "reasoning_content": "", "usage": None}
        else:
            yield {"type": "done", "content": "Implementation started.", "usage": None}


def sample_questions():
    return [{
        "id": "storage",
        "header": "Storage",
        "question": "Choose storage",
        "options": [
            {"label": "SQLite (Recommended)", "description": "Session local"},
            {"label": "Markdown", "description": "Human editable"},
        ],
        "multi_select": False,
    }]


def multi_select_questions():
    return [{
        "id": "features",
        "header": "Features",
        "question": "Choose features",
        "options": [
            {"label": "Search", "description": "Find things"},
            {"label": "Export", "description": "Save things"},
        ],
        "multi_select": True,
    }]


def test_structured_answer_continues_same_turn_into_plan_goal_and_work(tmp_path):
    memory_store = MemoryStore(tmp_path / "memory.db")
    task_store = TaskStore(tmp_path / "tasks.db")
    registry = ToolRegistry()
    recorded = []

    async def ask_questions(_questions):
        return {"answers": [{"id": "storage", "selected": ["SQLite (Recommended)"]}]}

    def record_work(value: str) -> str:
        recorded.append(value)
        return f"recorded {value}"

    register_user_question_tools(registry, ask_questions)
    register_plan_tools(registry, memory_store, session_id=lambda: "session-a")
    register_goal_tools(registry, task_store, session_id=lambda: "session-a")
    registry.register(ToolDef(
        name="record_work",
        description="Record the selected implementation choice.",
        parameters={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        fn=record_work,
        group="core",
    ))
    llm = ClarifyThenExecuteLLM()
    agent = ReActAgent(
        "agent",
        llm,  # type: ignore[arg-type]
        registry,
        system_prompt=AGENT_CORE_PROMPT,
        max_iterations=6,
        progressive_tools=False,
    )

    response = asyncio.run(agent.reply(Msg(
        id="clarify-turn-1",
        content=[ContentBlock.text("Implement the selected storage choice.")],
    )))

    assert response is not None
    assert response.get_text() == "Implementation started."
    assert llm.calls == 4
    assert all(
        sum(message.get("role") == "user" for message in prompt) == 1
        for prompt in llm.prompts
    )
    second_prompt = llm.prompts[1]
    assert any(
        message.get("role") == "tool"
        and "SQLite (Recommended)" in str(message.get("content") or "")
        for message in second_prompt
    )
    assert not any(
        message.get("role") == "user"
        and "SQLite (Recommended)" in str(message.get("content") or "")
        for message in second_prompt
    )
    assert memory_store.get_working("session-a")["steps"] == [
        {"text": "Record choice", "status": "in_progress"},
        {"text": "Verify result", "status": "pending"},
    ]
    goal = task_store.active_goal_for_session("session-a")
    assert goal is not None
    assert goal["objective"] == "Implement SQLite choice"
    assert recorded == ["SQLite (Recommended)"]
    assert any(
        message.get("role") == "system"
        and "若原始请求已要求实施且工作并非简单单步" in str(message.get("content") or "")
        for message in llm.prompts[0]
    )


def test_question_tool_returns_canonical_answer_json():
    seen = []

    async def ask(questions):
        seen.append(questions)
        return {"answers": [{"id": "storage", "selected": ["SQLite (Recommended)"]}]}

    registry = ToolRegistry()
    register_user_question_tools(registry, ask)
    result = asyncio.run(registry.execute("ask_user_question", {"questions": sample_questions()}))

    assert result["error"] == ""
    assert json.loads(result["output"])["answers"][0]["selected"] == ["SQLite (Recommended)"]
    assert seen[0][0]["multi_select"] is False


@pytest.mark.parametrize("raw, message", [
    ([], "1 to 3 questions"),
    ([{"id": "x", "question": "?"}] * 4, "1 to 3 questions"),
    ([{"id": "", "question": "?"}], "non-empty id"),
    ([{"id": "x", "question": ""}], "non-empty question"),
    ([{"id": "x", "question": "?", "options": [{"label": "one"}]}], "2 to 4 options"),
])
def test_normalize_questions_rejects_invalid_batches(raw, message):
    with pytest.raises(ValueError, match=message):
        normalize_questions(raw)


def test_normalize_questions_rejects_duplicate_ids():
    with pytest.raises(ValueError, match="question ids must be unique"):
        normalize_questions([
            {"id": "x", "question": "one"},
            {"id": "x", "question": "two"},
        ])


def test_normalize_questions_rejects_duplicate_option_labels():
    with pytest.raises(ValueError, match="option labels must be unique"):
        normalize_questions([{
            "id": "x", "question": "choose",
            "options": [{"label": "one"}, {"label": "one"}],
        }])


def test_normalize_questions_rejects_unknown_keys():
    with pytest.raises(ValueError, match="unknown question key"):
        normalize_questions([{"id": "x", "question": "?", "surprise": True}])
    with pytest.raises(ValueError, match="unknown option key"):
        normalize_questions([{
            "id": "x", "question": "?",
            "options": [{"label": "one", "description": "d", "surprise": True}, {"label": "two"}],
        }])


def test_normalize_questions_cleans_control_characters_and_detaches_data():
    raw = [{
        "id": " x\n\t",
        "header": "Header\x00\n",
        "question": "Choose\r\n one",
        "options": [{"label": " one\x01 ", "description": " desc\n"}, {"label": "two"}],
    }]
    result = normalize_questions(raw)
    assert result == [{
        "id": "x",
        "header": "Header",
        "question": "Choose one",
        "options": [{"label": "one", "description": "desc"}, {"label": "two"}],
        "multi_select": False,
    }]
    raw[0]["options"][0]["label"] = "changed"
    assert result[0]["options"][0]["label"] == "one"


def test_normalize_answers_rejects_unoffered_label():
    with pytest.raises(ValueError, match="was not offered"):
        normalize_answers(sample_questions(), {
            "answers": [{"id": "storage", "selected": ["Remote"]}],
        })


def test_normalize_answers_rejects_answer_id_mismatch():
    with pytest.raises(ValueError, match="answer ids must match"):
        normalize_answers(sample_questions(), {
            "answers": [{"id": "other", "selected": ["SQLite (Recommended)"]}],
        })


def test_normalize_answers_rejects_duplicate_answer_ids():
    questions = sample_questions() + [{
        "id": "format",
        "question": "Choose format",
        "options": [{"label": "JSON"}, {"label": "YAML"}],
    }]
    with pytest.raises(ValueError, match="answer ids must be unique"):
        normalize_answers(questions, {
            "answers": [
                {"id": "storage", "selected": []},
                {"id": "storage", "selected": []},
            ],
        })


def test_normalize_answers_rejects_duplicate_selected_labels():
    with pytest.raises(ValueError, match="selected labels must be unique"):
        normalize_answers(sample_questions(), {
            "answers": [{"id": "storage", "selected": ["Markdown", "Markdown"]}],
        })


def test_normalize_answers_rejects_multiple_single_select_labels():
    with pytest.raises(ValueError, match="single-select"):
        normalize_answers(sample_questions(), {
            "answers": [{"id": "storage", "selected": ["SQLite (Recommended)", "Markdown"]}],
        })


def test_normalize_answers_rejects_single_select_selected_and_custom():
    with pytest.raises(ValueError, match="selected and custom"):
        normalize_answers(sample_questions(), {
            "answers": [{"id": "storage", "selected": ["Markdown"], "custom": "Use JSON"}],
        })


def test_normalize_answers_permits_multiselect_custom():
    result = normalize_answers(multi_select_questions(), {
        "answers": [{"id": "features", "selected": ["Search"], "custom": "Also import"}],
    })
    assert result == {"answers": [{"id": "features", "selected": ["Search"], "custom": "Also import"}]}


def test_normalize_answers_accepts_skipped_answer():
    assert normalize_answers(sample_questions(), {"answers": [{"id": "storage", "selected": []}]}) == {
        "answers": [{"id": "storage", "selected": []}],
    }


def test_normalize_answers_rejects_unknown_keys_and_missing_answers():
    with pytest.raises(ValueError, match="unknown answer key"):
        normalize_answers(sample_questions(), {
            "answers": [{"id": "storage", "selected": []}],
            "surprise": True,
        })
    with pytest.raises(ValueError, match="unknown answer key"):
        normalize_answers(sample_questions(), {"answers": [{"id": "storage", "selected": [], "extra": 1}]})
    with pytest.raises(ValueError, match="one answer for every question"):
        normalize_answers(sample_questions(), {"answers": []})


def test_normalize_answers_rejects_custom_over_limit():
    with pytest.raises(ValueError, match="2,000"):
        normalize_answers(sample_questions(), {
            "answers": [{"id": "storage", "selected": [], "custom": "x" * 2_001}],
        })


def test_normalize_answers_cleans_custom_and_accepts_exact_limit():
    custom = "x" * 1_998 + "\x00y"
    result = normalize_answers(sample_questions(), {
        "answers": [{"id": "storage", "selected": [], "custom": custom}],
    })
    assert result == {
        "answers": [{"id": "storage", "selected": [], "custom": "x" * 1_998 + " y"}],
    }


def test_normalize_answers_returns_request_order():
    questions = sample_questions() + [{
        "id": "format",
        "question": "Choose format",
        "options": [{"label": "JSON"}, {"label": "YAML"}],
        "multi_select": False,
    }]
    result = normalize_answers(questions, {
        "answers": [
            {"id": "format", "selected": ["YAML"]},
            {"id": "storage", "selected": ["Markdown"]},
        ],
    })
    assert [answer["id"] for answer in result["answers"]] == ["storage", "format"]


def test_single_select_custom_overrides_selected():
    result = normalize_answers(sample_questions(), {
        "answers": [{"id": "storage", "selected": [], "custom": "Use JSON"}],
    })
    assert result == {"answers": [{"id": "storage", "selected": [], "custom": "Use JSON"}]}


@pytest.mark.parametrize("exc, code", [
    (UserQuestionUnavailable("human interaction is unavailable on this channel"), "user_question_unavailable"),
    (UserQuestionCancelled("The user dismissed the question request."), "user_question_cancelled"),
])
def test_question_control_flow_is_a_recoverable_tool_failure(exc, code):
    async def ask(_questions):
        raise exc

    registry = ToolRegistry()
    register_user_question_tools(registry, ask)
    result = asyncio.run(registry.execute("ask_user_question", {"questions": sample_questions()}))

    assert result["code"] == code
    assert result["recoverable"] is True
    assert result["retryable"] is True
    assert result["output"] == ""


def test_question_tool_definition_has_interactive_contract():
    async def ask(_questions):
        return {"answers": [{"id": "storage", "selected": []}]}

    registry = ToolRegistry()
    register_user_question_tools(registry, ask)
    definition = registry.get("ask_user_question")
    assert definition is not None
    assert definition.risk == "read"
    assert definition.approval == "never"
    assert definition.group == "core"
    assert definition.max_calls_per_turn == 6
    assert definition.timeout is None
    description = definition.description.lower()
    assert "top-level user" in description
    assert "only tool call" in description
    assert "may be repeated" in description
    assert "does not grant" in description


def test_broker_waits_for_matching_response():
    async def scenario():
        events = []
        broker = UserQuestionBroker(events.append, available=lambda: True)
        task = asyncio.create_task(broker.ask(sample_questions()))
        await asyncio.sleep(0)
        request = next(event for event in events if event["type"] == "user_question_request")

        accepted, reason, retryable = broker.resolve(request["request_id"], {
            "answers": [{"id": "storage", "selected": ["SQLite (Recommended)"]}],
        })

        assert accepted is True and reason == "" and retryable is False
        assert (await task)["answers"][0]["id"] == "storage"
        assert broker.pending_count == 0
        assert events[-1] == {
            "type": "user_question_resolved",
            "request_id": request["request_id"],
            "state": "answered",
        }

    asyncio.run(scenario())


def test_broker_rejects_stale_and_duplicate_responses():
    async def scenario():
        events = []
        broker = UserQuestionBroker(events.append, available=lambda: True)
        assert broker.resolve("missing", {"answers": []}) == (
            False,
            "Question request is no longer pending.",
            False,
        )
        task = asyncio.create_task(broker.ask(sample_questions()))
        await asyncio.sleep(0)
        request_id = events[0]["request_id"]
        answers = {"answers": [{"id": "storage", "selected": []}]}
        assert broker.resolve(request_id, answers) == (True, "", False)
        assert broker.resolve(request_id, answers) == (
            False,
            "Question request is no longer pending.",
            False,
        )
        await task

    asyncio.run(scenario())


def test_broker_fails_fast_without_answerer():
    events = []
    broker = UserQuestionBroker(events.append, available=lambda: False)

    with pytest.raises(UserQuestionUnavailable, match="unavailable"):
        asyncio.run(broker.ask(sample_questions()))

    assert broker.pending_count == 0
    assert events == []


def test_question_tool_fails_fast_in_messaging_channel_without_hidden_request():
    async def scenario():
        events = []
        broker = UserQuestionBroker(events.append, available=lambda: not active_channel.get())
        registry = ToolRegistry()
        register_user_question_tools(registry, broker.ask)
        token = active_channel.set("qq")
        try:
            result = await registry.execute(
                "ask_user_question",
                {"questions": sample_questions()},
            )
        finally:
            active_channel.reset(token)
        return result, events, broker.pending_count

    result, events, pending_count = asyncio.run(scenario())

    assert "human interaction is unavailable on this channel" in result["error"].lower()
    assert events == []
    assert pending_count == 0


def test_clarified_execution_policy_requires_successful_supported_local_question_result():
    assert "先检查代码和配置" in AGENT_CORE_PROMPT
    assert "若原始请求已要求实施且工作并非简单单步" in AGENT_CORE_PROMPT
    assert "受支持的顶层本地交互回合" in AGENT_CORE_PROMPT
    assert "成功获得 ask_user_question 工具结果" in AGENT_CORE_PROMPT
    assert "消息渠道" in AGENT_CORE_PROMPT
    assert "不能触发此流程" in AGENT_CORE_PROMPT
    assert "设置 Goal 前先用 show 检查现有目标" in AGENT_CORE_PROMPT
    assert "复用或恢复匹配目标" in AGENT_CORE_PROMPT
    assert "替换无关目标前必须询问用户" in AGENT_CORE_PROMPT
    assert "问题答案不授予高风险工具权限" in AGENT_CORE_PROMPT


def test_react_cleanup_drains_concurrently_failed_tool_task_under_repeated_cancellation():
    async def scenario():
        registry = ToolRegistry()

        async def ask_questions(_questions):
            return {"answers": []}

        register_user_question_tools(registry, ask_questions)
        agent = ReActAgent(
            "agent",
            ClarifyThenExecuteLLM(),  # type: ignore[arg-type]
            registry,
            system_prompt=AGENT_CORE_PROMPT,
            max_iterations=2,
            progressive_tools=False,
        )
        child_started = asyncio.Event()
        release_child = asyncio.Event()
        consumer_holder = {}
        leaked = []
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()

        def capture_exception(_loop, context):
            leaked.append(context)

        async def fail_as_parent_is_repeatedly_cancelled(*_args, **_kwargs):
            child_started.set()
            await release_child.wait()
            consumer = consumer_holder["task"]
            loop.call_soon(consumer.cancel)
            loop.call_soon(consumer.cancel)
            raise RuntimeError("concurrent tool failure")

        agent._execute_tool_calls = fail_as_parent_is_repeatedly_cancelled  # type: ignore[method-assign]

        async def consume():
            async for _event in agent._run_react_loop(
                Msg(content=[ContentBlock.text("choose storage")]),
                emit_events=True,
            ):
                pass

        loop.set_exception_handler(capture_exception)
        try:
            consumer = asyncio.create_task(consume())
            consumer_holder["task"] = consumer
            await child_started.wait()
            release_child.set()
            with pytest.raises(asyncio.CancelledError):
                await consumer
            del consumer
            gc.collect()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        finally:
            loop.set_exception_handler(previous_handler)

        assert not [
            context for context in leaked
            if "Task exception was never retrieved" in str(context.get("message") or "")
        ]

    asyncio.run(scenario())


def test_broker_rejects_second_simultaneous_question():
    async def scenario():
        events = []
        broker = UserQuestionBroker(events.append, available=lambda: True)
        first = asyncio.create_task(broker.ask(sample_questions()))
        await asyncio.sleep(0)

        with pytest.raises(UserQuestionUnavailable, match="already pending"):
            await broker.ask(sample_questions())

        request_id = events[0]["request_id"]
        assert broker.cancel(request_id, "superseded") == (True, "")
        with pytest.raises(UserQuestionCancelled, match="superseded"):
            await first
        assert broker.pending_count == 0

    asyncio.run(scenario())


def test_broker_explicit_cancel_emits_one_bounded_terminal_event():
    async def scenario():
        events = []
        broker = UserQuestionBroker(events.append, available=lambda: True)
        task = asyncio.create_task(broker.ask(sample_questions()))
        await asyncio.sleep(0)
        request_id = events[0]["request_id"]

        assert broker.cancel(request_id, " dismissed\x00 " + "x" * 600) == (True, "")
        with pytest.raises(UserQuestionCancelled) as caught:
            await task

        assert "\x00" not in str(caught.value)
        assert len(str(caught.value)) <= 500
        assert broker.pending_count == 0
        assert [event for event in events if event["type"] == "user_question_resolved"] == [{
            "type": "user_question_resolved",
            "request_id": request_id,
            "state": "cancelled",
        }]

    asyncio.run(scenario())


def test_broker_preserves_caller_task_cancellation():
    async def scenario():
        events = []
        broker = UserQuestionBroker(events.append, available=lambda: True)
        task = asyncio.create_task(broker.ask(sample_questions()))
        await asyncio.sleep(0)
        request_id = events[0]["request_id"]

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert broker.pending_count == 0
        assert events[-1] == {
            "type": "user_question_resolved",
            "request_id": request_id,
            "state": "cancelled",
        }

    asyncio.run(scenario())


def test_broker_close_cancels_current_request():
    async def scenario():
        events = []
        broker = UserQuestionBroker(events.append, available=lambda: True)
        task = asyncio.create_task(broker.ask(sample_questions()))
        await asyncio.sleep(0)

        assert broker.close("Backend is shutting down.") is None
        with pytest.raises(UserQuestionCancelled, match="Backend is shutting down"):
            await task
        assert broker.pending_count == 0
        assert events[-1]["state"] == "cancelled"

    asyncio.run(scenario())


def test_broker_rejects_invalid_answers_but_keeps_request_pending():
    async def scenario():
        events = []
        broker = UserQuestionBroker(events.append, available=lambda: True)
        task = asyncio.create_task(broker.ask(sample_questions()))
        await asyncio.sleep(0)
        request_id = events[0]["request_id"]

        accepted, reason, retryable = broker.resolve(request_id, {
            "answers": [{"id": "storage", "selected": ["Remote"]}],
        })
        assert accepted is False
        assert "was not offered" in reason
        assert retryable is True
        assert broker.pending_count == 1
        assert not task.done()

        assert broker.resolve(request_id, {
            "answers": [{"id": "storage", "selected": ["Markdown"]}],
        }) == (True, "", False)
        assert (await task)["answers"][0]["selected"] == ["Markdown"]

    asyncio.run(scenario())


def test_late_response_cannot_resolve_newer_request():
    async def scenario():
        events = []
        broker = UserQuestionBroker(events.append, available=lambda: True)
        first = asyncio.create_task(broker.ask(sample_questions()))
        await asyncio.sleep(0)
        first_id = events[0]["request_id"]
        broker.cancel(first_id, "First request dismissed.")
        with pytest.raises(UserQuestionCancelled):
            await first

        second = asyncio.create_task(broker.ask(sample_questions()))
        await asyncio.sleep(0)
        second_id = next(
            event["request_id"]
            for event in reversed(events)
            if event["type"] == "user_question_request"
        )
        assert second_id != first_id
        answers = {"answers": [{"id": "storage", "selected": ["Markdown"]}]}
        assert broker.resolve(first_id, answers) == (
            False,
            "Question request is no longer pending.",
            False,
        )
        assert broker.pending_count == 1
        assert not second.done()
        assert broker.resolve(second_id, answers) == (True, "", False)
        assert await second == answers

    asyncio.run(scenario())


def test_timed_question_returns_pending_and_late_answer_is_delivered_once_to_origin():
    async def scenario():
        events = []
        identity = {"session_id": "session-a", "task_id": "task-a"}
        broker = UserQuestionBroker(events.append, available=lambda: True, identity=lambda: identity)
        result = await broker.ask(sample_questions(), mode="timed", optional=True, timeout_seconds=0.01, call_id="call-a")
        assert result["state"] == "pending" and "answers" not in result
        assert "not approval" in result["message"].lower()
        assert broker.pending_count == 1
        assert events[-1]["type"] == "user_question_pending"
        assert events[0]["session_id"] == "session-a"
        assert events[0]["task_id"] == "task-a" and events[0]["call_id"] == "call-a"
        answers = {"answers": [{"id": "storage", "selected": ["Markdown"]}]}
        assert broker.resolve(result["request_id"], answers) == (True, "", False)
        assert broker.resolve(result["request_id"], answers)[0] is False
        assert broker.next_answer(session_id="session-b", task_id="task-a", busy=False) is None
        assert broker.next_answer(session_id="session-a", task_id="unrelated-task", busy=True) is None
        late = broker.next_answer(session_id="session-a", task_id="task-a", busy=True)
        assert late["questions"] == sample_questions()
        assert late["answers"] == answers["answers"]
        assert late["request_id"] == result["request_id"] and late["call_id"] == "call-a"
        broker.acknowledge_answer(late["request_id"])
        assert broker.next_answer(session_id="session-a", task_id="task-a", busy=False) is None
        assert broker.pending_count == 0
    asyncio.run(scenario())


def test_timed_question_refuses_required_input_and_tool_retains_pending_contract():
    async def scenario():
        broker = UserQuestionBroker(lambda _: None, available=lambda: True)
        with pytest.raises(ValueError, match="optional"):
            await broker.ask(sample_questions(), mode="timed", timeout_seconds=0.01)
        registry = ToolRegistry()
        register_user_question_tools(registry, broker.ask)
        result = await registry.execute("ask_user_question", {
            "questions": sample_questions(), "mode": "timed", "optional": True, "timeout_seconds": 0.01,
        }, call_id="call-tool")
        assert result["error"] == ""
        assert json.loads(result["output"])["state"] == "pending"
        broker.close("shutdown")
    asyncio.run(scenario())


def test_editing_holds_timed_question_and_answer_before_deadline_stays_synchronous():
    async def scenario():
        events = []
        broker = UserQuestionBroker(events.append, available=lambda: True)
        task = asyncio.create_task(broker.ask(sample_questions(), mode="timed", optional=True, timeout_seconds=0.02))
        await asyncio.sleep(0)
        request_id = events[0]["request_id"]
        assert broker.set_editing(request_id, True)
        await asyncio.sleep(0.04)
        assert not task.done() and not any(e["type"] == "user_question_pending" for e in events)
        answer = {"answers": [{"id": "storage", "selected": ["Markdown"]}]}
        assert broker.resolve(request_id, answer)[0]
        assert await task == answer
        assert broker.next_answer(session_id="", task_id="", busy=False) is None
    asyncio.run(scenario())


def test_pending_question_expires_on_session_change_and_cancel_never_produces_answer():
    async def scenario():
        events = []
        broker = UserQuestionBroker(events.append, available=lambda: True,
                                    identity=lambda: {"session_id": "session-a", "task_id": "task-a"})
        result = await broker.ask(sample_questions(), mode="timed", optional=True, timeout_seconds=0.01)
        broker.expire_other_sessions("session-b")
        assert events[-1]["state"] == "expired"
        assert broker.resolve(result["request_id"], {"answers": []})[0] is False
        assert broker.pending_count == 0
        assert broker.next_answer(session_id="session-b", task_id="", busy=False) is None
        result = await broker.ask(sample_questions(), mode="timed", optional=True, timeout_seconds=0.01)
        assert broker.cancel(result["request_id"], "dismissed")[0]
        assert events[-1]["state"] == "cancelled"
        assert broker.next_answer(session_id="session-a", task_id="", busy=False) is None
    asyncio.run(scenario())


def test_timed_question_shutdown_expires_durably_without_persisting_contents(tmp_path):
    async def scenario():
        events = []
        broker = UserQuestionBroker(events.append, available=lambda: True,
                                    identity=lambda: {"session_id": "s", "task_id": "t"}, journal_dir=tmp_path)
        result = await broker.ask(sample_questions(), mode="timed", optional=True, timeout_seconds=0.01)
        broker.close("Backend is shutting down.")
        record = json.loads((tmp_path / f"{result['request_id']}.json").read_text())
        assert record["state"] == "expired"
        assert "questions" not in record and "answers" not in record
        recovered = []
        replacement = UserQuestionBroker(recovered.append, available=lambda: True, journal_dir=tmp_path)
        replacement.recover_expired("s")
        assert recovered[-1]["state"] == "expired"
        assert replacement.resolve(result["request_id"], {"answers": []})[0] is False
        replacement.recover_expired("s")
        assert len(recovered) == 1
    asyncio.run(scenario())


def test_edit_hold_release_returns_pending_promptly():
    async def scenario():
        events = []
        broker = UserQuestionBroker(events.append, available=lambda: True)
        task = asyncio.create_task(broker.ask(sample_questions(), mode="timed", optional=True, timeout_seconds=0.01))
        await asyncio.sleep(0)
        request_id = events[0]["request_id"]
        broker.set_editing(request_id, True)
        await asyncio.sleep(0.02)
        broker.set_editing(request_id, False)
        result = await asyncio.wait_for(task, 0.5)
        assert result["state"] == "pending"
        broker.close("shutdown")
    asyncio.run(scenario())


def test_user_answer_inbox_persists_consumed_input_and_keeps_unconsumed_reply():
    async def scenario():
        registry = ToolRegistry()
        class ReplyLLM:
            config = ClarifyThenExecuteLLM._Config()
            async def chat_stream(self, messages, tools):
                self.messages = messages
                yield {"type": "done", "content": "Answer seen", "usage": None}
        llm = ReplyLLM()
        agent = ReActAgent("agent", llm, registry, max_iterations=2, progressive_tools=False)
        agent.queue_user_question_answer("q", "Original question q answer: Markdown")
        agent.queue_user_question_answer("q", "Original question q answer: Markdown")
        await agent.reply(Msg(content=[ContentBlock.text("Continue independent work")]))
        assert agent.user_question_answer_consumed("q")
        saved = [m for m in agent.context.messages if m.get("provenance") == "user_question_answer"]
        assert len(saved) == 1 and "Markdown" in saved[0]["content"]
        agent.queue_user_question_answer("later", "Answer arriving after the final request")
        assert not agent.user_question_answer_consumed("later")
        agent.remove_user_question_answer("later")
        assert not agent.user_question_answer_consumed("later")
    asyncio.run(scenario())


def test_new_task_explicitly_expires_pending_and_queued_answers():
    async def scenario():
        events = []
        broker = UserQuestionBroker(events.append, available=lambda: True)
        result = await broker.ask(sample_questions(), mode="timed", optional=True, timeout_seconds=0.01)
        broker.resolve(result["request_id"], {"answers": [{"id": "storage", "selected": ["Markdown"]}]})
        broker.supersede("A new task replaced the original request.")
        assert broker.next_answer(session_id="", task_id="", busy=False) is None
        assert events[-1]["state"] == "expired"
        result = await broker.ask(sample_questions(), mode="timed", optional=True, timeout_seconds=0.01)
        broker.supersede("A new task replaced the original request.")
        assert broker.resolve(result["request_id"], {"answers": []})[0] is False
        assert events[-1]["state"] == "expired"
    asyncio.run(scenario())


def test_question_inbox_cannot_cross_session_or_task_when_context_is_borrowed():
    async def scenario():
        class ReplyLLM:
            config = ClarifyThenExecuteLLM._Config()
            async def chat_stream(self, messages, tools):
                yield {"type": "done", "content": "Independent reply", "usage": None}
        agent = ReActAgent("agent", ReplyLLM(), ToolRegistry(), max_iterations=2, progressive_tools=False)
        agent.queue_user_question_answer("other", "PRIVATE ANSWER", session_scope="different", task_id="task-original")
        await agent.reply(Msg(content=[ContentBlock.text("different task")], metadata={"task_id":"task-other"}))
        assert not agent.user_question_answer_consumed("other")
        assert not any("PRIVATE ANSWER" in str(m.get("content", "")) for m in agent.context.messages)
    asyncio.run(scenario())


def test_followup_question_answer_is_acknowledged_only_after_session_save():
    async def scenario():
        class ReplyLLM:
            config = ClarifyThenExecuteLLM._Config()
            async def chat_stream(self, messages, tools):
                assert agent.user_question_answer_consumed("q-followup")
                assert saved
                yield {"type": "done", "content": "Preference applied", "usage": None}
        agent = ReActAgent("agent", ReplyLLM(), ToolRegistry(), max_iterations=2, progressive_tools=False)
        saved = []
        async def save():
            saved.append(list(agent.context.messages))
        agent.context.save_async = save
        await agent.reply(Msg(content=[ContentBlock.text("Earlier answer: Markdown")],
                              metadata={"source":"user_question_answer", "question_request_id":"q-followup"}))
    asyncio.run(scenario())


def test_crashed_question_recovery_is_scoped_and_redacted(tmp_path, monkeypatch):
    record = {"request_id":"d"*32,"pid":123456,"session_id":"same","session_scope":"work", "state":"pending"}
    (tmp_path / ('d'*32 + '.json')).write_text(json.dumps(record))
    def dead_process(*_args):
        raise ProcessLookupError()
    monkeypatch.setattr('agent.runtime.user_questions.os.kill', dead_process)
    events = []
    broker = UserQuestionBroker(events.append, available=lambda:True, journal_dir=tmp_path)
    broker.recover_expired("same", "writing")
    assert events == []
    broker.recover_expired("same", "work")
    assert events[-1]["state"] == "expired"
    assert "restart" in events[-1]["reason"]
    assert json.loads((tmp_path / ('d'*32 + '.json')).read_text())["state"] == "expired"


def test_late_answer_journal_write_failure_is_retryable_and_never_drops_question(tmp_path, monkeypatch):
    async def scenario():
        broker=UserQuestionBroker(lambda _:None,available=lambda:True,journal_dir=tmp_path)
        result=await broker.ask(sample_questions(),mode="timed",optional=True,timeout_seconds=0.01)
        original=broker._record
        def fail(*_args):
            raise OSError("Disk unavailable")
        monkeypatch.setattr(broker,"_record",fail)
        accepted,reason,retryable=broker.resolve(result["request_id"],{"answers":[{"id":"storage","selected":["Markdown"]}]})
        assert not accepted and retryable and "save" in reason.lower()
        assert broker.pending_count == 1
        monkeypatch.setattr(broker,"_record",original)
        broker.close("shutdown")
    asyncio.run(scenario())


def test_question_expiry_still_clears_control_when_journal_is_unavailable(tmp_path, monkeypatch):
    async def scenario():
        events=[]
        broker=UserQuestionBroker(events.append,available=lambda:True,journal_dir=tmp_path)
        await broker.ask(sample_questions(),mode="timed",optional=True,timeout_seconds=0.01)
        def fail(*_args):
            raise OSError("Disk unavailable")
        monkeypatch.setattr(broker,"_record",fail)
        broker.close("Backend is shutting down.")
        assert broker.pending_count == 0
        assert events[-1]["state"] == "expired"
    asyncio.run(scenario())
