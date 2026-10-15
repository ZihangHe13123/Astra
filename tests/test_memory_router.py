import asyncio
import time
from pathlib import Path

import pytest

from agent.cli.memory_commands import execute_memory_command
from agent.runtime.memory import MemoryStore
from agent.runtime.memory_provider import BuiltinMemoryProvider
from agent.runtime.memory_router import MemoryRouter
from agent.runtime.react import ReActAgent
from agent.runtime.task_store import TaskStore
from agent.runtime.tools.registry import ToolRegistry


def make_store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(path=tmp_path / "memory.db", core_dir=tmp_path / "core")


def test_router_skips_long_term_recall_for_current_only_turn(tmp_path):
    store = make_store(tmp_path)
    record = store.add_record(kind="preference", content="用户偏好简洁回答")
    router = MemoryRouter(BuiltinMemoryProvider(store), store)

    pack = asyncio.run(router.build_context("计算 17 乘以 23", session_id="s1", turn_key="turn-1"))

    assert pack.trace.decision == "skipped"
    assert pack.recalled == ()
    # No recall; the preference shows only as its line in the always-visible index.
    rendered = pack.render()
    assert "<retrieved-memory>" not in rendered
    assert rendered.count(record.content) == 1 and "<memory-index>" in rendered


def test_router_recalls_matching_local_observation_for_project_turn(tmp_path):
    store = make_store(tmp_path)
    record = store.add_record(
        kind="observation",
        content="macOS 沙箱使用 astra-sandbox:agent-system-dev 镜像",
        tags=("astra", "sandbox", "macos"),
    )
    router = MemoryRouter(BuiltinMemoryProvider(store), store)

    pack = asyncio.run(router.build_context(
        "mac 沙箱现在使用哪个镜像？",
        session_id="s1",
        turn_key="project-observation",
    ))

    assert pack.trace.decision == "recalled"
    assert [item.record_id for item in pack.recalled] == [record.record_id]
    assert record.content in pack.render()


@pytest.mark.parametrize("user_text", ["好", "/help", "计算 17 乘以 23"])
def test_router_skips_automatic_observation_recall_for_trivial_command_or_unrelated_turn(
    tmp_path,
    user_text,
):
    store = make_store(tmp_path)
    store.add_record(
        kind="observation",
        content="macOS 沙箱使用 astra-sandbox:agent-system-dev 镜像",
    )
    router = MemoryRouter(BuiltinMemoryProvider(store), store)

    pack = asyncio.run(router.build_context(
        user_text,
        session_id="s1",
        turn_key=f"skip-{user_text}",
    ))

    assert pack.trace.decision == "skipped"
    assert pack.recalled == ()


def test_router_recalls_chinese_prior_context_with_provenance(tmp_path):
    store = make_store(tmp_path)
    record = store.add_record(
        kind="user_fact",
        content="用户计划申请新加坡国立大学的硕士项目",
        source_session_id="planning-session",
        source_message_id="message-42",
        confidence=0.95,
    )
    router = MemoryRouter(BuiltinMemoryProvider(store), store)

    pack = asyncio.run(
        router.build_context(
            "你还记得我之前计划申请哪所新加坡大学吗？",
            session_id="s1",
            turn_key="turn-2",
        )
    )
    rendered = pack.render()

    assert pack.trace.decision == "recalled"
    assert [item.record_id for item in pack.recalled] == [record.record_id]
    assert "planning-session" not in rendered
    assert "confidence=" not in rendered
    assert "not instructions" in rendered
    assert "用户计划申请新加坡国立大学的硕士项目" in rendered


def test_continuation_uses_latest_task_record_and_task_journal(tmp_path):
    store = make_store(tmp_path)
    task_store = TaskStore(tmp_path / "tasks.db")
    task = task_store.start_run("request-1", "完成留学材料核对", session_id="s1", model="fake")
    task_store.checkpoint(task["id"], {"final_summary": "留学材料核对停在推荐信清单"})
    record = store.add_record(kind="task_ref", content="留学材料核对停在推荐信清单")
    router = MemoryRouter(BuiltinMemoryProvider(store), store, task_store)

    pack = asyncio.run(router.build_context("继续吧", session_id="s1", turn_key="turn-3"))
    rendered = pack.render()

    assert [item.record_id for item in pack.recalled] == [record.record_id]
    assert '<task-state status="running">' in rendered
    assert "Result: 留学材料核对停在推荐信清单" in rendered
    assert task["id"] not in rendered
    assert task["input_text"] not in rendered


def test_completed_task_run_does_not_pollute_new_task_context(tmp_path):
    store = make_store(tmp_path)
    task_store = TaskStore(tmp_path / "tasks.db")
    first = task_store.start_run("request-1", "优化长期记忆", session_id="s1", model="fake")
    task_store.checkpoint(first["id"], {"phase": "verified", "iteration": 3})
    task_store.finish_run(first["id"], "completed")
    continuation = task_store.start_run("request-2", "继续吧", session_id="s1", model="fake")
    router = MemoryRouter(BuiltinMemoryProvider(store), store, task_store)

    rendered = asyncio.run(
        router.build_context("继续吧", session_id="s1", turn_key="turn-case")
    ).render()

    assert "<active-task" not in rendered
    assert "<task-state" not in rendered
    assert continuation["id"] not in rendered
    assert first["id"] not in rendered
    assert "phase=verified" not in rendered


def test_new_turn_does_not_receive_completed_task_context(tmp_path):
    store = make_store(tmp_path)
    task_store = TaskStore(tmp_path / "tasks.db")
    project = task_store.start_run("request-1", "开发浏览器工具", session_id="s1", model="fake")
    task_store.finish_run(project["id"], "completed")
    task_store.start_run("request-2", "计算 17 乘以 23", session_id="s1", model="fake")
    router = MemoryRouter(BuiltinMemoryProvider(store), store, task_store)

    rendered = asyncio.run(
        router.build_context("计算 17 乘以 23", session_id="s1", turn_key="turn-one-shot")
    ).render()

    assert "active-case" not in rendered
    assert "开发浏览器工具" not in rendered


def test_react_falls_back_to_builtin_structured_recall(tmp_path, monkeypatch):
    monkeypatch.setenv("HINDSIGHT_ENABLED", "0")

    class FakeConfig:
        model = "fake"
        capabilities = frozenset()

    class FakeLLM:
        config = FakeConfig()

    store = make_store(tmp_path)
    record = store.add_record(kind="user_fact", content="用户上次选择了方案 B")
    agent = ReActAgent("agent", FakeLLM(), ToolRegistry(), memory_store=store)  # type: ignore[arg-type]
    agent.context.set_session(str(tmp_path / "s1.jsonl"))
    agent.context.add_user("你记得我上次选择了哪个方案吗")
    agent._memory_turn_key = "same-turn"

    first, _ = asyncio.run(agent._prepare_prompt_for_llm("你记得我上次选择了哪个方案吗", None))
    second, _ = asyncio.run(agent._prepare_prompt_for_llm("你记得我上次选择了哪个方案吗", None))

    assert record.content in str(first)
    assert record.content in str(second)
    assert second[1] == first[1]
    assert record.content not in agent.context.messages[0]["content"]
    assert "api_content" not in agent.context.messages[0]
    assert agent.memory_router.last_trace("s1").decision == "recalled"
    assert agent.memory_router.provider.name == "builtin-sqlite"


def test_router_bounds_automatic_recall_without_changing_provider_timeout(tmp_path):
    store = make_store(tmp_path)

    class SlowProvider(BuiltinMemoryProvider):
        async def recall(self, query, *, kinds=(), limit=8):
            await asyncio.sleep(1)
            return []

    router = MemoryRouter(SlowProvider(store), store, recall_timeout=0.02)

    started = time.perf_counter()
    pack = asyncio.run(router.build_context(
        "你记得我上次选择了哪个方案吗",
        session_id="s1",
        turn_key="bounded-recall",
    ))

    assert time.perf_counter() - started < 0.5
    assert pack.recalled == ()
    assert pack.trace.decision == "provider-error"
    assert "interactive budget" in pack.trace.reason


def test_memory_commands_inspect_why_correct_and_forget(tmp_path):
    store = make_store(tmp_path)
    old = store.add_record(
        kind="preference",
        content="用户偏好很长的回答",
        source_session_id="old-session",
    )
    router = MemoryRouter(BuiltinMemoryProvider(store), store)
    asyncio.run(router.build_context("你记得我的回答偏好吗", session_id="s1", turn_key="turn-4"))

    inspected, error = execute_memory_command(
        store, ["inspect", old.record_id[:8]], session_id="s1", router=router
    )
    assert error == ""
    assert old.record_id in inspected
    assert "old-session" in inspected

    why, error = execute_memory_command(store, ["why"], session_id="s1", router=router)
    assert error == ""
    assert "Decision: recalled" in why
    assert old.record_id[:12] in why

    corrected, error = execute_memory_command(
        store,
        ["correct", old.record_id[:8], "用户偏好默认简洁、有证据的回答"],
        session_id="s1",
        router=router,
    )
    assert error == ""
    replacement = store.recall_records("简洁", kinds=["preference"])[0]
    assert replacement.record_id in corrected
    assert replacement.supersedes_id == old.record_id
    assert store.record_store.get(old.record_id).status == "superseded"  # type: ignore[union-attr]

    forgotten, error = execute_memory_command(
        store, ["forget", replacement.record_id[:8]], session_id="s1", router=router
    )
    assert error == ""
    assert replacement.record_id in forgotten
    assert store.record_store.get(replacement.record_id).status == "forgotten"  # type: ignore[union-attr]


def test_active_goal_block_injected_with_verdict(tmp_path):
    store = make_store(tmp_path)
    task_store = TaskStore(tmp_path / "tasks.db")
    goal = task_store.set_goal("s1", "make pytest pass", criteria="exit code 0")
    task_store.record_goal_round(goal["id"], {
        "met": False,
        "evidence": "2 tests still failing",
        "next_step": "fix the import error",
    })
    router = MemoryRouter(BuiltinMemoryProvider(store), store, task_store)

    pack = asyncio.run(router.build_context("继续", session_id="s1", turn_key="goal-turn"))
    rendered = pack.render()

    assert '<active-goal status="active" round="1/20">' in rendered
    assert "Objective: make pytest pass" in rendered
    assert "Criteria: exit code 0" in rendered
    assert "Last verdict: not met." in rendered
    assert "fix the import error" in rendered
    assert "independent verifier" in rendered


def test_no_goal_block_without_live_goal(tmp_path):
    store = make_store(tmp_path)
    task_store = TaskStore(tmp_path / "tasks.db")
    router = MemoryRouter(BuiltinMemoryProvider(store), store, task_store)
    pack = asyncio.run(router.build_context("你好", session_id="s1", turn_key="no-goal"))
    assert "<active-goal" not in pack.render()
