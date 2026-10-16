"""The router assembles core memory, conversation, task and goal blocks, and recalls nothing.

With the Context Index off, no records are added to a turn: saved memories are
listed in the always-visible index, and the model searches the rest on demand.
"""

import asyncio
from pathlib import Path

from agent.cli.memory_commands import execute_memory_command
from agent.runtime.memory import MemoryStore
from agent.runtime.memory_router import MemoryRouter
from agent.runtime.react import ReActAgent
from agent.runtime.task_store import TaskStore
from agent.runtime.tools.registry import ToolRegistry


def make_store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(path=tmp_path / "memory.db", core_dir=tmp_path / "core")


def test_the_router_adds_no_records_to_a_turn(tmp_path):
    store = make_store(tmp_path)
    observation = store.add_record(kind="observation", content="macOS 沙箱使用 astra-sandbox:agent-system-dev 镜像")
    episode = store.add_record(kind="episode", content="用户上次选择了方案 B")
    preference = store.add_record(kind="preference", content="用户偏好简洁回答")
    router = MemoryRouter(store)

    for question in ("你记得我上次选择了哪个方案吗", "mac 沙箱现在使用哪个镜像？", "计算 17 乘以 23"):
        rendered = asyncio.run(router.build_context(session_id="s1")).render()
        assert observation.content not in rendered and episode.content not in rendered, question
        assert "<retrieved-memory>" not in rendered
        # A preference is listed once, as its line in the always-visible index.
        assert rendered.count(preference.content) == 1 and "<memory-index>" in rendered


def test_react_with_the_context_index_off_shows_records_only_through_the_index(tmp_path, monkeypatch):
    monkeypatch.setenv("HINDSIGHT_ENABLED", "0")

    class FakeConfig:
        model = "fake"
        capabilities = frozenset()

    class FakeLLM:
        config = FakeConfig()

    store = make_store(tmp_path)
    observation = store.add_record(kind="observation", content="用户上次选择了方案 B")
    fact = store.add_record(kind="user_fact", content="用户住在里斯本")
    agent = ReActAgent("agent", FakeLLM(), ToolRegistry(), memory_store=store)  # type: ignore[arg-type]
    agent.context.set_session(str(tmp_path / "s1.jsonl"))
    agent.context.add_user("你记得我上次选择了哪个方案吗")
    agent._memory_turn_key = "same-turn"

    prompt, _ = asyncio.run(agent._prepare_prompt_for_llm("你记得我上次选择了哪个方案吗", None))

    assert observation.content not in str(prompt)
    assert fact.content in str(prompt)
    assert "api_content" not in agent.context.messages[0]


def test_continuation_uses_the_task_journal(tmp_path):
    store = make_store(tmp_path)
    task_store = TaskStore(tmp_path / "tasks.db")
    task = task_store.start_run("request-1", "完成留学材料核对", session_id="s1", model="fake")
    task_store.checkpoint(task["id"], {"final_summary": "留学材料核对停在推荐信清单"})
    router = MemoryRouter(store, task_store)

    rendered = asyncio.run(router.build_context(session_id="s1")).render()

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
    router = MemoryRouter(store, task_store)

    rendered = asyncio.run(router.build_context(session_id="s1")).render()

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
    router = MemoryRouter(store, task_store)

    rendered = asyncio.run(router.build_context(session_id="s1")).render()

    assert "active-case" not in rendered
    assert "开发浏览器工具" not in rendered


def test_memory_commands_inspect_why_correct_and_forget(tmp_path):
    store = make_store(tmp_path)
    old = store.add_record(
        kind="preference",
        content="用户偏好很长的回答",
        source_session_id="old-session",
    )

    inspected, error = execute_memory_command(store, ["inspect", old.record_id[:8]], session_id="s1")
    assert error == ""
    assert old.record_id in inspected
    assert "old-session" in inspected

    why, error = execute_memory_command(store, ["why"], session_id="s1")
    assert error == ""
    assert "only through the Context Index" in why and "memory(action=search)" in why

    corrected, error = execute_memory_command(
        store, ["correct", old.record_id[:8], "用户偏好默认简洁、有证据的回答"], session_id="s1",
    )
    assert error == ""
    replacement = store.recall_records("简洁", kinds=["preference"])[0]
    assert replacement.record_id in corrected
    assert replacement.supersedes_id == old.record_id
    assert store.record_store.get(old.record_id).status == "superseded"  # type: ignore[union-attr]

    forgotten, error = execute_memory_command(store, ["forget", replacement.record_id[:8]], session_id="s1")
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
    router = MemoryRouter(store, task_store)

    rendered = asyncio.run(router.build_context(session_id="s1")).render()

    assert '<active-goal status="active" round="1/20">' in rendered
    assert "Objective: make pytest pass" in rendered
    assert "Criteria: exit code 0" in rendered
    assert "Last verdict: not met." in rendered
    assert "fix the import error" in rendered
    assert "independent verifier" in rendered


def test_no_goal_block_without_live_goal(tmp_path):
    store = make_store(tmp_path)
    task_store = TaskStore(tmp_path / "tasks.db")
    router = MemoryRouter(store, task_store)
    assert "<active-goal" not in asyncio.run(router.build_context(session_id="s1")).render()
