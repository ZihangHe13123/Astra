"""Session goal management tool (Goal Mode).

A session goal is verified by an independent evidence-based checker after
every successfully completed turn; when unmet, the next round starts
automatically. Modeled after ZCode's Goal Mode.
"""

from __future__ import annotations

from typing import Any, Callable

from ..tool_failure import ToolFailure
from .registry import ToolDef, ToolRegistry

# What the task store keeps of each text; the tool says so instead of cutting silently.
_MAX_OBJECTIVE_CHARS = 2000
_MAX_CRITERIA_CHARS = 2000
_MAX_EVIDENCE_CHARS = 500
_KEPT_HISTORY_ROUNDS = 20


def _refused(code: str, message: str, hint: str) -> ToolFailure:
    return ToolFailure(code=code, message=message, retryable=False, recovery_hint=hint)


def register_goal_tools(
    registry: ToolRegistry,
    task_store: Any,
    session_id: Callable[[], str],
    on_goal_event: Callable[[dict[str, Any]], None] | None = None,
) -> None:
    def _notify(goal: dict[str, Any] | None) -> None:
        if goal is not None and on_goal_event is not None:
            try:
                on_goal_event(goal)
            except Exception:
                pass

    def _goal(
        action: str,
        objective: str = "",
        criteria: str = "",
        max_rounds: int = 0,
        evidence: str = "",
    ) -> str | ToolFailure:
        if task_store is None:
            return _refused(
                "goal_unavailable",
                "Goal mode is unavailable: task persistence is disabled.",
                "Continue the work without a session goal.",
            )
        sid = str(session_id() or "default")
        act = str(action or "show").strip().lower()

        if act == "set":
            previous = task_store.active_goal_for_session(sid)
            try:
                goal = task_store.set_goal(sid, objective, criteria=criteria, max_rounds=max_rounds)
            except ValueError as exc:
                return _refused(
                    "invalid_arguments",
                    f"Could not set goal: {exc}",
                    f"Pass a non-empty objective of at most {_MAX_OBJECTIVE_CHARS} characters and "
                    "max_rounds as a whole number. No goal was changed.",
                )
            _notify(goal)
            return (
                f"Goal set for this session (round 0/{goal['max_rounds']}): {goal['objective']}\n"
                + (f"Criteria: {goal['criteria']}\n" if goal.get("criteria") else "")
                + (
                    f"Note: criteria were cut to their first {_MAX_CRITERIA_CHARS} characters.\n"
                    if len(str(criteria or "").strip()) > _MAX_CRITERIA_CHARS else ""
                )
                + (
                    f"This replaced the previous goal ({previous['status']}, round "
                    f"{previous['round']}/{previous['max_rounds']}): {previous['objective']}\n"
                    if previous is not None else ""
                )
                + "Each completed turn is now checked by an independent verifier; unmet rounds "
                "continue automatically. Use pause/clear to stop the loop."
            )

        if act == "show":
            if task_store.active_goal_for_session(sid) is None:
                # The store's own text points at /goal, which is the user's command.
                last = task_store.get_goal_history(sid)
                return "No live goal in this session" + (
                    f" (the last one is {last[0]['status']}: {last[0]['objective']}; action=history lists its rounds)"
                    if last is not None else ""
                ) + ". Set one with action=set."
            return task_store.format_goal_status(sid)

        if act == "history":
            pair = task_store.get_goal_history(sid)
            if pair is None:
                return "No live goal. Set one with the goal tool (action=set)."
            goal, history = pair
            if not history:
                return (
                    f"Goal [{goal['status']}] round {goal['round']}/{goal['max_rounds']} has "
                    f"no verification rounds yet: {goal['objective']}"
                )
            total = int(goal.get("round") or 0)
            lines = [
                f"Goal [{goal['status']}] history — "
                + (
                    # Only the newest rounds are stored; say so instead of presenting them as all.
                    f"the last {len(history)} of {total} verification rounds (earlier ones are not kept): "
                    if total > len(history) else f"{len(history)} verification round(s): "
                )
                + f"{goal['objective']}"
            ]
            for entry in history:
                round_no = entry.get("round", "?")
                mark = "MET" if entry.get("met") else "not met"
                evidence = str(entry.get("evidence") or "(none)").strip()
                line = f"round {round_no}: {mark} — evidence: {evidence}"
                next_step = str(entry.get("next_step") or "").strip()
                if next_step:
                    line += f" | next: {next_step}"
                lines.append(line)
            return "\n".join(lines)

        if act == "complete":
            goal = task_store.active_goal_for_session(sid)
            if goal is None or goal.get("status") != "active":
                paused = goal is not None and goal.get("status") == "paused"
                return _refused(
                    "goal_not_active",
                    "No active goal to mark complete." + (" The goal is paused." if paused else ""),
                    "Use action=resume first." if paused else "Use action=show to see the session's goal state.",
                )
            full_claim = " ".join(str(evidence or "").split())
            claim = full_claim[:_MAX_EVIDENCE_CHARS]
            return (
                "Completion claimed for the independent verifier"
                + (f": {claim}" if claim else ".")
                + (
                    f" (first {_MAX_EVIDENCE_CHARS} characters of the evidence)"
                    if len(full_claim) > len(claim) else ""
                )
                + " The goal remains active until verification succeeds."
            )

        if act == "pause":
            goal = task_store.pause_goal(sid)
            if goal is None:
                return _refused("goal_not_active", "No active goal to pause.",
                                "Use action=show to see the session's goal state.")
            _notify(goal)
            return f"Goal paused at round {goal['round']}/{goal['max_rounds']}. Completed rounds and files stay; resume to continue."

        if act == "resume":
            goal = task_store.resume_goal(sid)
            if goal is None:
                return _refused("goal_not_paused", "No paused goal to resume.",
                                "Use action=show to see the session's goal state.")
            _notify(goal)
            return f"Goal resumed at round {goal['round']}/{goal['max_rounds']}: {goal['objective']}"

        if act == "clear":
            goal = task_store.clear_goal(sid)
            if goal is None:
                return _refused("goal_not_found", "No live goal to clear.",
                                "Nothing needs clearing; continue.")
            _notify(goal)
            return f"Goal cleared: {goal['objective']}"

        return _refused(
            "invalid_arguments",
            f"Unknown goal action: {act!r}.",
            "Use set, show, history, complete, pause, resume, or clear.",
        )

    registry.register(ToolDef(
        name="goal",
        description=(
            "Manage the session goal for long-horizon work (Goal Mode). While a goal is active, "
            "an independent verifier checks every completed turn against real evidence (command "
            "output, test results, confirmed file changes); unmet rounds auto-continue until the "
            "goal is met, paused, cleared, or the round budget runs out. Actions: set (objective "
            "required; optional criteria describing how to verify, optional max_rounds; it replaces "
            "the session's current goal, active or paused, and starts again at round 0), show, "
            "history (list this goal's recorded verification rounds: round/met/evidence/next_step; "
            f"the newest {_KEPT_HISTORY_ROUNDS} are kept), "
            "complete (claim completion with evidence for independent verification), pause, resume, "
            "clear. For self-contained conversational or creative goals, the delivered answer is "
            "valid evidence; for external work, provide real outputs or confirmed changes."
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["set", "show", "history", "complete", "pause", "resume", "clear"],
                    "description": "Goal operation to perform.",
                },
                "objective": {
                    "type": "string",
                    "description": (
                        "For action=set: one-sentence verifiable objective (required, at most "
                        f"{_MAX_OBJECTIVE_CHARS} characters)."
                    ),
                    "default": "",
                },
                "criteria": {
                    "type": "string",
                    "description": (
                        "For action=set: optional evidence criteria, e.g. 'pytest exits 0'. "
                        f"The first {_MAX_CRITERIA_CHARS} characters are kept."
                    ),
                    "default": "",
                },
                "max_rounds": {
                    "type": "integer",
                    "description": (
                        "For action=set: verification round budget, 1 to 50. 0 or omitted uses the "
                        "configured default (20 unless the user changed it); a larger value is lowered to 50."
                    ),
                    "default": 0,
                },
                "evidence": {
                    "type": "string",
                    "description": (
                        "For action=complete: concise evidence that the active goal is satisfied; "
                        f"the first {_MAX_EVIDENCE_CHARS} characters are kept in the claim."
                    ),
                    "default": "",
                },
            },
            "required": ["action"],
        },
        fn=_goal,
        risk="write",
    ))
