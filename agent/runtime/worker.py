"""Stable worker contracts layered over the generic process lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from .tools.processes import ManagedProcess, ProcessManager
from .execution_limits import WORKER_TIMEOUT_MAX_SECONDS


REASONING_EFFORTS = ("low", "medium", "high", "max")
MAX_WORKER_TURNS = 50


def normalize_reasoning_effort(value: Any) -> str:
    """Normalize one explicit worker reasoning override or reject a typo."""

    normalized = str(value or "").strip().lower()
    if normalized and normalized not in REASONING_EFFORTS:
        allowed = ", ".join(REASONING_EFFORTS)
        raise ValueError(f"reasoning_effort must be one of: {allowed}")
    return normalized


class WorkerStatus(str, Enum):
    """Worker-visible lifecycle states.

    ProcessManager keeps its wider compatibility vocabulary (including
    ``interrupted``). Worker consumers get one stable running state and four
    explicit terminal outcomes.
    """

    RUNNING = "running"
    IDLE = "idle"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


TERMINAL_WORKER_STATUSES = frozenset(
    {
        WorkerStatus.COMPLETED,
        WorkerStatus.PARTIAL,
        WorkerStatus.FAILED,
        WorkerStatus.CANCELLED,
        WorkerStatus.TIMED_OUT,
    }
)


@dataclass(frozen=True)
class WorkerSpec:
    """Immutable declaration of one bounded worker run."""

    worker_type: str
    goal: str
    context: str
    requested_tools: tuple[str, ...] | None = None
    max_turns: int = 12
    timeout_seconds: int = 120
    model: str = ""
    reasoning_effort: str = ""
    task_id: str = ""
    team_id: str = ""
    team_agent_id: str = ""
    parent_agent_id: str = ""
    agent_name: str = ""
    keep_alive: bool = False
    workspace_root: str = ""
    resume_conv_path: str = ""
    resume_instruction: str = ""

    def __post_init__(self) -> None:
        worker_type = self.worker_type.strip()
        goal = self.goal.strip()
        if not worker_type:
            raise ValueError("worker_type is required")
        if not goal:
            raise ValueError("goal is required")
        if self.max_turns < 1 or self.max_turns > MAX_WORKER_TURNS:
            raise ValueError(f"max_turns must be 1-{MAX_WORKER_TURNS}")
        if self.timeout_seconds < 1 or self.timeout_seconds > WORKER_TIMEOUT_MAX_SECONDS:
            raise ValueError(f"timeout must be 1-{WORKER_TIMEOUT_MAX_SECONDS}")
        object.__setattr__(self, "worker_type", worker_type)
        object.__setattr__(self, "goal", goal)
        object.__setattr__(self, "model", str(self.model or "").strip())
        object.__setattr__(
            self, "workspace_root", str(self.workspace_root or "").strip()
        )
        object.__setattr__(self, "resume_conv_path", str(self.resume_conv_path or "").strip())
        object.__setattr__(self, "resume_instruction", str(self.resume_instruction or "").strip())
        object.__setattr__(
            self,
            "reasoning_effort",
            normalize_reasoning_effort(self.reasoning_effort),
        )
        if self.requested_tools is not None:
            object.__setattr__(
                self,
                "requested_tools",
                tuple(dict.fromkeys(str(name) for name in self.requested_tools)),
            )

    def public(self) -> dict[str, Any]:
        """Return a manifest-safe summary; full context is intentionally omitted."""

        return {
            "worker_type": self.worker_type,
            "goal": self.goal,
            "context_chars": len(self.context),
            "requested_tools": (
                list(self.requested_tools)
                if self.requested_tools is not None
                else None
            ),
            "max_turns": self.max_turns,
            "timeout_seconds": self.timeout_seconds,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "task_id": self.task_id,
            "team_id": self.team_id,
            "team_agent_id": self.team_agent_id,
            "parent_agent_id": self.parent_agent_id,
            "agent_name": self.agent_name,
            "keep_alive": self.keep_alive,
            "workspace_root": self.workspace_root,
            **({"resume_conv_path": self.resume_conv_path} if self.resume_conv_path else {}),
        }

    def process_metadata(self) -> dict[str, Any]:
        metadata = {"worker_spec": self.public()}
        if self.team_id:
            metadata["agent_team"] = {
                "team_id": self.team_id,
                "agent_id": self.team_agent_id,
                "parent_agent_id": self.parent_agent_id,
                "name": self.agent_name,
                "role": self.worker_type,
            }
        return metadata


@dataclass(frozen=True)
class WorkerRun:
    """Normalized observation of a worker backed by a ManagedProcess."""

    run_id: str
    worker_type: str
    status: WorkerStatus
    goal: str
    task_id: str
    started_at: float
    completed_at: float | None
    duration_ms: int
    turns_used: int
    max_turns: int
    turns_remaining: int
    error_code: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "worker_type": self.worker_type,
            "status": self.status.value,
            "goal": self.goal,
            "task_id": self.task_id,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_ms": self.duration_ms,
            "turns_used": self.turns_used,
            "max_turns": self.max_turns,
            "turns_remaining": self.turns_remaining,
            "error_code": self.error_code,
        }

    @classmethod
    def from_process(
        cls,
        manager: ProcessManager,
        process: ManagedProcess,
    ) -> "WorkerRun":
        process_info = manager.describe(process)
        result = process.result or {}
        spec = process.metadata.get("worker_spec", {})
        if not isinstance(spec, dict):
            spec = {}
        raw_turns_used = result.get("turns_used")
        if raw_turns_used is None:
            raw_turns_used = process.metadata.get("turns_used")
        turns_used = int(raw_turns_used or 0)
        max_turns = int(
            result.get("max_turns")
            or spec.get("max_turns")
            or 0
        )
        return cls(
            run_id=process.process_id,
            worker_type=str(spec.get("worker_type") or process.kind),
            status=_normalize_worker_status(
                str(process_info.get("status") or ""),
                str(result.get("worker_status") or process.metadata.get("worker_status") or ""),
            ),
            goal=str(spec.get("goal") or process.label),
            task_id=str(spec.get("task_id") or process.task_id),
            started_at=float(process_info.get("started_at") or process.started_at),
            completed_at=process_info.get("completed_at"),
            duration_ms=int(process_info.get("duration_ms") or 0),
            turns_used=turns_used,
            max_turns=max_turns,
            turns_remaining=max(0, max_turns - turns_used),
            error_code=str(result.get("error_code") or ""),
        )


def attach_worker_run(
    manager: ProcessManager,
    process: ManagedProcess,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Attach the compatibility-preserving worker envelope to a payload."""

    enriched = dict(payload or manager.describe(process))
    enriched["worker"] = WorkerRun.from_process(manager, process).to_dict()
    if "execution_binding" in process.metadata:
        enriched["execution_binding"] = dict(process.metadata["execution_binding"])
    if "runtime" in process.metadata:
        enriched["runtime"] = dict(process.metadata["runtime"])
    transcript_path = str(process.metadata.get("session_transcript_path") or "")
    if transcript_path:
        enriched["session_transcript_path"] = transcript_path
    return enriched


def _normalize_worker_status(
    process_status: str,
    explicit_status: str,
) -> WorkerStatus:
    try:
        explicit = WorkerStatus(explicit_status)
    except ValueError:
        explicit = None
    if explicit is not None and explicit in TERMINAL_WORKER_STATUSES:
        return explicit
    if process_status == "running":
        if explicit == WorkerStatus.IDLE:
            return WorkerStatus.IDLE
        return WorkerStatus.RUNNING
    if process_status == "cancelled":
        return WorkerStatus.CANCELLED
    if process_status == "completed":
        return WorkerStatus.COMPLETED
    return WorkerStatus.FAILED
