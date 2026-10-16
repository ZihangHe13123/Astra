"""Per-turn context assembly: core memory, conversation state, task state and goal.

Records are not recalled here. With the Context Index on, it chooses what history
and memory a turn sees; with it off, nothing is added automatically and the model
looks things up itself (memory search, session_search).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .memory import MemoryStore
    from .task_store import TaskStore


_TASK_STATE_CHAR_LIMIT = 1000
_TASK_ATTENTION_STATUSES = frozenset({
    "cancelling", "interrupted", "blocked_on_user", "waiting_external", "scheduled",
})
_UNCERTAIN_TOOL_STATUSES = frozenset({"running", "unknown"})


def _safe(text: str) -> str:
    return str(text).replace("<", "&lt;").replace(">", "&gt;")


def _task_text(value: Any, limit: int = 160) -> str:
    text = _safe(" ".join(str(value or "").split()))
    return text if len(text) <= limit else text[:limit - 1] + "…"


@dataclass(frozen=True)
class ContextPack:
    session_id: str
    core: str
    working: str
    task: str

    def render(self, *, include_core: bool = True) -> str:
        # Work mode sends core memory as its own standing-memory stream instead.
        blocks = [block for block in (self.core if include_core else "", self.working, self.task) if block]
        if not blocks:
            return ""
        return "<agent-memory>\n" + "\n\n".join(blocks) + "\n</agent-memory>"


class MemoryRouter:
    """Assemble the turn's memory, conversation and task blocks; recall nothing."""

    def __init__(self, store: "MemoryStore", task_store: "TaskStore | None" = None):
        self.store = store
        self.task_store = task_store

    def _task_prompt(self, session_id: str) -> str:
        if self.task_store is None:
            return ""
        try:
            task = self.task_store.latest_task_for_session(session_id)
        except Exception:
            return ""
        if not isinstance(task, dict) or not task:
            return ""
        return self._format_task_state(task)

    @staticmethod
    def _format_task_state(task: dict[str, Any]) -> str:
        """Show actionable journal state without echoing the current request."""
        checkpoint = task.get("checkpoint") or {}
        status = str(task.get("status", "running"))
        resumed = bool(task.get("resume_count"))
        tools = [step for step in task.get("steps", []) if step.get("kind") == "tool"]
        stop_reason = checkpoint.get("stop_reason") or task.get("error")
        # A fresh run and LLM-only bookkeeping add nothing to the user message
        # and tool history. This gate uses journal state, never query keywords.
        if not (resumed or status in _TASK_ATTENTION_STATUSES or tools
                or stop_reason or checkpoint.get("final_summary")
                or checkpoint.get("phase") == "after_tools"):
            return ""

        lines = [f'<task-state status="{_task_text(status, 32)}">']
        if resumed:
            lines.append("Resumed from an earlier attempt.")
        if task.get("block_reason"):
            lines.append(f"Blocked: {_task_text(task['block_reason'])}")
        if stop_reason and stop_reason != task.get("block_reason"):
            lines.append(f"Stop reason: {_task_text(stop_reason)}")
        if status == "scheduled" and task.get("scheduled_at"):
            lines.append(f"Scheduled for: {_task_text(task['scheduled_at'], 64)}")
        uncertain = [step for step in tools if step.get("status") in _UNCERTAIN_TOOL_STATUSES]
        if uncertain:
            lines.append(f"Unresolved tool outcomes: {len(uncertain)}; verify effects before retrying.")
        if checkpoint.get("phase"):
            lines.append(f"Checkpoint: {_task_text(checkpoint['phase'], 48)}")
        # Keep uncertain side effects visible even if many newer reads succeeded.
        selected = uncertain[-4:]
        remaining = 4 - len(selected)
        if remaining:
            selected += [step for step in tools if step.get("status") not in _UNCERTAIN_TOOL_STATUSES][-remaining:]
        if selected:
            lines.append(f"Tool steps ({len(selected)} of {len(tools)}):")
            for step in selected:
                lines.append(f"- [{_task_text(step.get('status', 'unknown'), 24)}] {_task_text(step.get('name'), 72)}")
        if checkpoint.get("final_summary"):
            lines.append(f"Result: {_task_text(checkpoint['final_summary'], 240)}")

        closing = "\n</task-state>"
        body = "\n".join(lines)
        budget = _TASK_STATE_CHAR_LIMIT - len(closing)
        if len(body) > budget:
            body = body[:budget - 1].rstrip() + "…"
        return body + closing

    def _goal_prompt(self, session_id: str) -> str:
        if self.task_store is None:
            return ""
        try:
            goal = self.task_store.active_goal_for_session(session_id)
        except Exception:
            return ""
        if not isinstance(goal, dict) or not goal:
            return ""
        lines = [
            f'<active-goal status="{_safe(goal["status"])}" round="{_safe(goal["round"])}/{_safe(goal["max_rounds"])}">',
            f"Objective: {_safe(goal['objective'])}",
        ]
        if goal.get("criteria"):
            lines.append(f"Criteria: {_safe(goal['criteria'])}")
        verdict = goal.get("last_verdict") or {}
        if verdict:
            if verdict.get("met"):
                lines.append("Last verdict: MET.")
            else:
                lines.append("Last verdict: not met.")
                if verdict.get("evidence"):
                    lines.append(f"Evidence so far: {_safe(verdict['evidence'])}")
                if verdict.get("next_step"):
                    lines.append(f"Verifier next step: {_safe(verdict['next_step'])}")
        if goal["status"] == "paused":
            lines.append("Goal is paused; do not auto-continue it unless the user asks.")
        else:
            lines.append(
                "After this turn completes, an independent verifier checks the goal against real "
                "evidence. Work toward concrete, checkable progress; claims without output will "
                "fail verification."
            )
        lines.append("</active-goal>")
        return "\n".join(lines)

    async def build_context(self, *, session_id: str, workspace_key: str = "") -> ContextPack:
        task_block = self._task_prompt(session_id)
        goal_block = self._goal_prompt(session_id)
        if goal_block:
            task_block = f"{task_block}\n\n{goal_block}" if task_block else goal_block
        return ContextPack(
            session_id=session_id,
            core=self.store.format_core_prompt(workspace_key),
            working=self.store.format_working_prompt(session_id),
            task=task_block,
        )
