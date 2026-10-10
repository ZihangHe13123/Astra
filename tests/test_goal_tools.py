import asyncio
from pathlib import Path

from agent.runtime.prompts import AGENT_CORE_PROMPT
from agent.runtime.task_store import TaskStore
from agent.runtime.tools.goals import register_goal_tools
from agent.runtime.tools.registry import ToolRegistry


def _setup(tmp_path: Path):
    store = TaskStore(tmp_path / "tasks.db")
    registry = ToolRegistry()
    events: list[dict] = []
    register_goal_tools(
        registry,
        store,
        session_id=lambda: "s1",
        on_goal_event=events.append,
    )
    return store, registry, events


def test_goal_tool_set_show_pause_resume_clear(tmp_path: Path):
    _store, registry, events = _setup(tmp_path)
    tool = registry.get("goal")
    assert tool is not None

    set_result = tool.fn(action="set", objective="make pytest pass", criteria="exit code 0")
    assert "Goal set" in set_result
    assert events and events[-1]["status"] == "active"

    show_result = tool.fn(action="show")
    assert "make pytest pass" in show_result

    pause_result = tool.fn(action="pause")
    assert "paused" in pause_result
    assert events[-1]["status"] == "paused"

    resume_result = tool.fn(action="resume")
    assert "resumed" in resume_result

    clear_result = tool.fn(action="clear")
    assert "cleared" in clear_result
    assert "No live goal to clear." in tool.fn(action="clear").message


def test_goal_tool_set_validates_objective(tmp_path: Path):
    _, registry, _ = _setup(tmp_path)
    tool = registry.get("goal")
    result = tool.fn(action="set", objective="   ")
    assert "Could not set goal" in result.message


def test_goal_tool_complete_is_a_verified_claim_not_a_state_bypass(tmp_path: Path):
    store, registry, events = _setup(tmp_path)
    tool = registry.get("goal")
    goal = store.set_goal("s1", "mua")

    result = tool.fn(action="complete", evidence="Delivered a matching affectionate reply")

    assert "Completion claimed" in result
    assert "independent verifier" in result
    persisted = store.get_goal(goal["id"])
    assert persisted is not None
    assert persisted["status"] == "active"
    assert persisted["round"] == 0
    assert events == []


def test_goal_tool_complete_requires_active_goal(tmp_path: Path):
    _, registry, _ = _setup(tmp_path)
    tool = registry.get("goal")
    assert "No active goal" in tool.fn(action="complete", evidence="done").message


def test_goal_tool_handles_missing_store():
    registry = ToolRegistry()
    register_goal_tools(registry, None, session_id=lambda: "s1")
    tool = registry.get("goal")
    assert "unavailable" in tool.fn(action="show").message


def test_goal_tool_description_and_core_policy_require_safe_replacement_guidance(tmp_path: Path):
    _, registry, _ = _setup(tmp_path)
    tool = registry.get("goal")
    assert tool is not None
    description = tool.description.lower()
    assert all(action in description for action in ("show", "set", "resume"))
    assert "question answer can replace" not in description
    assert "设置 Goal 前先用 show 检查现有目标" in AGENT_CORE_PROMPT
    assert "替换无关目标前必须询问用户" in AGENT_CORE_PROMPT
    assert "若原始请求已要求实施且工作并非简单单步" in AGENT_CORE_PROMPT
    assert "问题答案不授予高风险工具权限" in AGENT_CORE_PROMPT


def test_goal_tool_unknown_action(tmp_path: Path):
    _, registry, _ = _setup(tmp_path)
    tool = registry.get("goal")
    assert "Unknown goal action" in tool.fn(action="explode").message


def test_goal_tool_history_reports_no_rounds_yet(tmp_path: Path):
    store, registry, _ = _setup(tmp_path)
    tool = registry.get("goal")
    store.set_goal("s1", "make pytest pass")
    result = tool.fn(action="history")
    assert "no verification rounds yet" in result


def test_goal_tool_history_lists_verification_rounds(tmp_path: Path):
    store, registry, _ = _setup(tmp_path)
    tool = registry.get("goal")
    goal = store.set_goal("s1", "make pytest pass")
    store.record_goal_round(goal["id"], {"met": False, "evidence": "tests failed", "next_step": "fix the bug"})
    store.record_goal_round(goal["id"], {"met": True, "evidence": "all green", "next_step": ""})

    result = tool.fn(action="history")

    # The second round completed the goal; history must still be visible.
    assert "Goal [completed] history" in result
    assert "2 verification round(s)" in result
    assert "make pytest pass" in result
    assert "round 1: not met" in result
    assert "tests failed" in result
    assert "next: fix the bug" in result
    assert "round 2: MET" in result
    assert "all green" in result


def test_goal_tool_history_without_goal(tmp_path: Path):
    _, registry, _ = _setup(tmp_path)
    tool = registry.get("goal")
    assert "No live goal" in tool.fn(action="history")


def test_goal_refusals_reach_the_model_as_failures_not_results(tmp_path: Path):
    store, registry, events = _setup(tmp_path)

    def call(**arguments):
        return asyncio.run(registry.execute("goal", arguments))

    too_long = call(action="set", objective="x" * 2001)
    assert too_long["output"] == "" and too_long["code"] == "invalid_arguments"
    assert "objective is too long (max 2000 chars)" in too_long["error"]
    assert store.active_goal_for_session("s1") is None and events == []

    for action, code in (("complete", "goal_not_active"), ("pause", "goal_not_active"),
                         ("resume", "goal_not_paused"), ("clear", "goal_not_found"),
                         ("explode", "invalid_arguments")):
        refused = call(action=action)
        assert refused["output"] == "" and refused["error"] and refused["code"] == code, action

    assert call(action="set", objective="make pytest pass")["error"] == ""
    assert call(action="pause")["error"] == ""
    paused = call(action="complete", evidence="done")
    assert "paused" in paused["error"] and "resume" in paused["recovery_hint"]


def test_goal_set_says_what_it_replaced_and_what_it_cut(tmp_path: Path):
    store, registry, _ = _setup(tmp_path)
    tool = registry.get("goal")
    first = tool.fn(action="set", objective="ship the parser", max_rounds=99)
    assert "replaced" not in first
    # A budget above the ceiling is lowered, and the result shows the value in force.
    assert "round 0/50" in first

    second = tool.fn(action="set", objective="ship the lexer", criteria="c" * 2500)

    assert "This replaced the previous goal (active, round 0/50): ship the parser" in second
    assert "criteria were cut to their first 2000 characters" in second
    assert len(store.active_goal_for_session("s1")["criteria"]) == 2000


def test_goal_show_without_a_live_goal_names_the_tool_action_and_the_last_outcome(tmp_path: Path):
    store, registry, _ = _setup(tmp_path)
    tool = registry.get("goal")
    empty = tool.fn(action="show")
    # /goal is the user's command; the model sets a goal with action=set.
    assert "/goal" not in empty and "action=set" in empty

    goal = store.set_goal("s1", "make pytest pass", max_rounds=1)
    store.record_goal_round(goal["id"], {"met": False, "evidence": "2 failed", "next_step": "fix them"})

    ended = tool.fn(action="show")
    assert "exhausted" in ended and "make pytest pass" in ended and "action=history" in ended


def test_goal_history_says_when_older_rounds_are_no_longer_kept(tmp_path: Path):
    store, registry, _ = _setup(tmp_path)
    tool = registry.get("goal")
    goal = store.set_goal("s1", "make pytest pass", max_rounds=50)
    for index in range(23):
        store.record_goal_round(goal["id"], {"met": False, "evidence": f"run {index}", "next_step": "again"})

    result = tool.fn(action="history")

    listed = [line for line in result.splitlines() if line.startswith("round ")]
    assert f"the last {len(listed)} of 23 verification rounds" in result
    assert len(listed) < 23 and listed[0].startswith("round 4:") and listed[-1].startswith("round 23:")


def test_goal_complete_says_when_the_claim_was_shortened(tmp_path: Path):
    store, registry, _ = _setup(tmp_path)
    tool = registry.get("goal")
    store.set_goal("s1", "make pytest pass")

    assert "characters of the evidence" not in tool.fn(action="complete", evidence="all 40 tests pass")
    assert "(first 500 characters of the evidence)" in tool.fn(action="complete", evidence="e" * 900)
