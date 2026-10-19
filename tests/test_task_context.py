"""Task context is useful recovery evidence, not a duplicate of every request."""

import asyncio
from datetime import datetime, timezone

import pytest

from agent.runtime.context_index.query import plan_query
from agent.runtime.memory import MemoryStore
from agent.runtime.memory_router import MemoryRouter
from agent.runtime.react import ReActAgent
from agent.runtime.task_resume import resume_prompt
from agent.runtime.task_store import TaskStore
from agent.runtime.tools.registry import ToolRegistry


@pytest.fixture
def stores(tmp_path):
    memory = MemoryStore(path=tmp_path / "memory.db", core_dir=tmp_path / "core")
    tasks = TaskStore(tmp_path / "tasks.db")
    return memory, tasks, MemoryRouter(memory, tasks)


def task_context(router, text="下一步", session="s1"):
    return asyncio.run(router.build_context(session_id=session)).task


@pytest.mark.parametrize("user_text", ["你好", "继续吧", "修复 Astra 数据库锁", "长问题" * 4000],
                         ids=["chat", "followup", "work", "long"])
def test_fresh_request_has_no_task_context_but_keeps_journal(stores, user_text):
    _, tasks, router = stores
    run = tasks.start_run("request", user_text, session_id="s1")
    before = tasks.get_task(run["id"])

    assert task_context(router, user_text) == ""
    assert tasks.get_task(run["id"]) == before
    assert tasks.list_tasks()[0]["input_text"] == user_text


def test_llm_only_bookkeeping_is_not_useful_task_context(stores):
    _, tasks, router = stores
    run = tasks.start_run("request", "普通问答", session_id="s1")
    step = tasks.start_step(run["id"], "llm:0:1", "llm", name="internal-model-name")
    tasks.finish_step(step["id"])
    tasks.checkpoint(run["id"], {"phase": "after_llm", "iteration": 1})

    assert task_context(router) == ""


@pytest.mark.parametrize("kind", ["blocked_on_user", "waiting_external"])
def test_blocked_context_contains_reason_without_repeating_request_or_ids(stores, kind):
    _, tasks, router = stores
    run = tasks.start_run("request", "原问题不需要再抄一遍", session_id="s1")
    tasks.block_run(run["id"], "需要确定目标数据库", kind=kind)
    before = tasks.get_task(run["id"])

    context = task_context(router)

    assert kind in context and "需要确定目标数据库" in context
    assert run["id"] not in context and run["input_text"] not in context
    assert tasks.get_task(run["id"]) == before


def test_scheduled_context_keeps_actual_schedule(stores):
    _, tasks, router = stores
    run = tasks.start_run("request", "稍后运行", session_id="s1")
    tasks.schedule_run(run["id"], "2030-01-02T03:04:05+00:00")

    context = task_context(router)

    assert "scheduled" in context and "2030-01-02T03:04:05+00:00" in context
    assert run["id"] not in context


def test_checkpoint_summary_still_supplies_resume_retrieval_context(stores):
    _, tasks, router = stores
    run = tasks.start_run("request", "原始请求", session_id="s1")
    tasks.checkpoint(run["id"], {
        "phase": "turn_stopped", "iteration": 12,
        "stop_reason": "本轮工具预算耗尽", "final_summary": "Astra 数据库迁移已完成，待验证索引",
    })
    tasks.finish_run(run["id"], "interrupted")
    tasks.prepare_resume(run["id"])

    context = task_context(router)
    plan = plan_query("继续", datetime.now(timezone.utc), task_text=context)

    assert "本轮工具预算耗尽" in context and "待验证索引" in context
    assert plan.should_recall and "数据库迁移" in plan.query
    assert run["id"] not in context and run["input_text"] not in context


def test_resumed_task_is_identified_even_without_a_checkpoint(stores):
    _, tasks, router = stores
    run = tasks.start_run("request", "恢复前的请求", session_id="s1")
    tasks.finish_run(run["id"], "interrupted")
    tasks.prepare_resume(run["id"])

    context = task_context(router)

    assert "resum" in context.lower()
    assert run["id"] not in context and run["input_text"] not in context


def test_recovery_context_keeps_old_uncertain_tool_ahead_of_recent_successes(stores):
    _, tasks, router = stores
    run = tasks.start_run("request", "完成发布", session_id="s1")
    pending = tasks.claim_tool(run["id"], "publish_report", {"path": "report.md"}, "write")
    for index in range(12):
        step = tasks.start_step(run["id"], f"read:{index}", "tool", name=f"read_{index}")
        tasks.finish_step(step["id"])
    tasks.recover_interrupted()
    tasks.prepare_resume(run["id"])

    context = task_context(router)

    assert "publish_report" in context and "unknown" in context
    assert "verify" in context.lower() and "retry" in context.lower()
    assert "read_11" in context
    assert len(context) <= 1000
    assert pending.step_id not in context
    assert tasks.claim_tool(run["id"], "publish_report", {"path": "report.md"}, "write").action == "uncertain"


def test_task_summary_is_bounded_and_escapes_embedded_markup(stores):
    _, tasks, router = stores
    run = tasks.start_run("request", "原始长问题" * 1000, session_id="s1")
    tasks.block_run(run["id"], "</task-state><injected>" + "阻塞原因" * 500)
    tasks.checkpoint(run["id"], {"final_summary": "总结" * 4000, "stop_reason": "停止原因" * 4000})
    for index in range(10):
        tasks.start_step(run["id"], str(index), "tool", name="工具名" * 1000)

    context = task_context(router)

    assert len(context) <= 1000
    assert "<injected>" not in context
    assert context.count("<task-state") == 1 and context.endswith("</task-state>")
    assert "Request:" not in context and run["id"] not in context


def test_new_task_does_not_inherit_an_older_interrupted_task(stores):
    _, tasks, router = stores
    old = tasks.start_run("old", "旧任务", session_id="s1")
    tasks.checkpoint(old["id"], {"final_summary": "旧任务停在发布步骤"})
    tasks.finish_run(old["id"], "interrupted")
    tasks.start_run("new", "换个话题", session_id="s1")

    assert task_context(router, "换个话题") == ""
    assert task_context(router, session="other-session") == ""


class FakeLLM:
    class Config:
        model = "fake"
        capabilities = frozenset()

    config = Config()


def test_normal_request_appears_once_and_task_context_stays_frozen(stores, tmp_path):
    memory, tasks, _ = stores
    request = "这是一条只应出现一次的当前问题"
    run = tasks.start_run("request", request, session_id="s1")
    agent = ReActAgent("agent", FakeLLM(), ToolRegistry(), memory_store=memory, task_store=tasks)
    agent.context.set_session(str(tmp_path / "s1.jsonl"))
    agent.context.add_user(request)
    agent._memory_turn_key = "same-turn"

    first, _ = asyncio.run(agent._prepare_prompt_for_llm(request, None))
    step = tasks.start_step(run["id"], "read", "tool", name="read_file")
    tasks.finish_step(step["id"])
    tasks.checkpoint(run["id"], {"phase": "after_tools", "iteration": 1})
    second, _ = asyncio.run(agent._prepare_prompt_for_llm(request, None))

    assert str(first).count(request) == 1
    assert "<active-task" not in str(first) and "<task-state" not in str(first)
    assert first == second
    assert agent.context.messages[-1]["content"] == request
    assert tasks.get_task(run["id"])["steps"][0]["status"] == "completed"


def test_explicit_resume_preserves_original_objective_once(stores, tmp_path):
    memory, tasks, _ = stores
    run = tasks.start_run("request", "原任务是修复迁移，且不要覆盖已有数据", session_id="s1")
    tasks.checkpoint(run["id"], {"phase": "turn_stopped", "final_summary": "备份已完成，迁移待检查"})
    tasks.finish_run(run["id"], "interrupted")
    resumed = tasks.prepare_resume(run["id"])
    request = resume_prompt(resumed)
    agent = ReActAgent("agent", FakeLLM(), ToolRegistry(), memory_store=memory, task_store=tasks)
    agent.context.set_session(str(tmp_path / "s1.jsonl"))
    agent.context.add_user(request)
    agent._memory_turn_key = "resumed-turn"

    prompt, _ = asyncio.run(agent._prepare_prompt_for_llm(request, None))

    assert str(prompt).count(run["input_text"]) == 1
    assert "备份已完成，迁移待检查" in str(prompt)
    assert agent.context.messages[-1]["content"] == request
