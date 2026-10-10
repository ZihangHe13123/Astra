import asyncio
import json
from pathlib import Path

import pytest

from agent.runtime.memory import MemoryStore
from agent.runtime.tools.plans import register_plan_tools
from agent.runtime.tools.registry import ToolRegistry


def setup_plan(tmp_path: Path):
    store = MemoryStore(tmp_path / "memory.db")
    registry = ToolRegistry()
    register_plan_tools(registry, store, session_id=lambda: "session-a")
    return store, registry


def test_plan_update_replaces_complete_session_plan(tmp_path: Path):
    store, registry = setup_plan(tmp_path)
    result = asyncio.run(registry.execute("plan_update", {
        "goal": "Ship structured clarification",
        "steps": [
            {"text": "Add broker", "status": "in_progress"},
            {"text": "Add TUI", "status": "pending"},
        ],
    }))

    assert result["error"] == ""
    assert json.loads(result["output"])["counts"] == {
        "pending": 1, "in_progress": 1, "completed": 0,
    }
    assert store.get_working("session-a")["steps"] == [
        {"text": "Add broker", "status": "in_progress"},
        {"text": "Add TUI", "status": "pending"},
    ]


def test_plan_section_symbols_round_trip_without_relaxing_core_memory(tmp_path):
    from agent.runtime.memory import _safe_memory_text

    store, registry = setup_plan(tmp_path)
    result = asyncio.run(registry.execute("plan_update", {
        "goal": "Review §2",
        "steps": [
            {"text": "Read §2.1", "status": "in_progress"},
            {"text": "Verify §2.2", "status": "pending"},
        ],
    }))
    assert not result["error"]
    reloaded = MemoryStore(tmp_path / "memory.db")
    assert reloaded.get_working("session-a")["goal"] == "Review §2"
    assert store.get_working("session-a")["steps"][0]["text"] == "Read §2.1"
    with pytest.raises(ValueError, match="entry delimiter"):
        _safe_memory_text("Core § entry", max_chars=600)


def test_plan_step_that_is_too_long_is_reported_as_plan_text(tmp_path: Path):
    _, registry = setup_plan(tmp_path)
    result = asyncio.run(registry.execute("plan_update", {
        "goal": "Ship structured clarification",
        "steps": [
            {"text": "Add broker " + "x" * 300, "status": "in_progress"},
            {"text": "Add TUI", "status": "pending"},
        ],
    }))

    assert "Plan text is too long (max 240 characters)" in result["error"]
    assert "Memory content" not in result["error"]


def test_plan_update_rejects_ambiguous_active_state(tmp_path: Path):
    _, registry = setup_plan(tmp_path)
    result = asyncio.run(registry.execute("plan_update", {
        "goal": "Ship it",
        "steps": [
            {"text": "One", "status": "pending"},
            {"text": "Two", "status": "pending"},
        ],
    }))
    assert "exactly one step must be in_progress" in result["error"]


@pytest.mark.parametrize("goal", ["", " \t\n "])
def test_plan_update_still_rejects_empty_or_whitespace_goal(tmp_path: Path, goal: str):
    _, registry = setup_plan(tmp_path)

    result = asyncio.run(registry.execute("plan_update", {
        "goal": goal,
        "steps": [
            {"text": "One", "status": "in_progress"},
            {"text": "Two", "status": "pending"},
        ],
    }))

    assert "Plan goal must not be empty" in result["error"]


def test_plan_update_accepts_all_completed_and_isolates_sessions(tmp_path: Path):
    store = MemoryStore(tmp_path / "memory.db")
    registry = ToolRegistry()
    current = "session-a"
    register_plan_tools(registry, store, session_id=lambda: current)
    result = asyncio.run(registry.execute("plan_update", {
        "goal": "Done",
        "steps": [
            {"text": "One", "status": "completed"},
            {"text": "Two", "status": "completed"},
        ],
    }))
    assert result["error"] == ""
    assert store.get_working("session-b").get("steps") is None


@pytest.mark.parametrize(
    "steps",
    [
        [{"text": "Only", "status": "in_progress"}],
        [{"text": f"Step {index}", "status": "in_progress" if index == 1 else "pending"}
         for index in range(1, 14)],
        (
            [
                {"text": "Duplicate", "status": "in_progress"},
                {"text": "Duplicate", "status": "pending"},
            ],
        ),
        (
            [
                {"text": " ", "status": "in_progress"},
                {"text": "Second", "status": "pending"},
            ],
        ),
        (
            [
                {"text": "First", "status": "started"},
                {"text": "Second", "status": "pending"},
            ],
        ),
        (
            [
                {"text": "First", "status": "in_progress"},
                {"text": "Second", "status": "in_progress"},
            ],
        ),
    ],
)
def test_plan_update_rejects_invalid_complete_snapshots(tmp_path: Path, steps):
    _, registry = setup_plan(tmp_path)
    result = asyncio.run(registry.execute("plan_update", {
        "goal": "Ship it",
        "steps": steps,
    }))

    assert result["error"]
