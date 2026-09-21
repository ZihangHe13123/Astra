"""Durable task, step, checkpoint, and event storage.

The task database is an execution journal beside the existing conversation
store. It never replaces session history; it makes long-running turns
observable and prevents known tool results from being replayed after restart.
"""

from __future__ import annotations

from agent.runtime.paths import state_path

import hashlib
import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


TERMINAL_TASK_STATUSES = {"completed", "failed", "cancelled", "done_verified"}
RESUMABLE_TASK_STATUSES = {"interrupted", "failed", "cancelled"}
BLOCKED_TASK_STATUSES = {"blocked_on_user", "waiting_external"}
SCHEDULED_TASK_STATUSES = {"scheduled"}
NON_RUNNING_ACTIVE_STATUSES = BLOCKED_TASK_STATUSES | SCHEDULED_TASK_STATUSES

# Session goal mode: one live goal per session, verified round by round.
GOAL_ACTIVE_STATUSES = {"active", "paused"}
GOAL_TERMINAL_STATUSES = {"completed", "cleared", "exhausted"}
GOAL_STATUSES = GOAL_ACTIVE_STATUSES | GOAL_TERMINAL_STATUSES

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _loads(value: str | None, fallback: Any = None) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def task_db_path() -> Path:
    override = os.getenv("AGENT_TASK_DB", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return state_path("tasks.db")


@dataclass(frozen=True)
class ToolClaim:
    action: str  # execute | cached | uncertain
    step_id: str
    output: dict[str, Any] | None = None


class TaskStore:
    def __init__(
        self,
        path: str | Path | None = None,
        *,
        busy_timeout_ms: int = 10_000,
    ):
        self.path = Path(path) if path is not None else task_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._busy_timeout_ms = max(1, int(busy_timeout_ms))
        self._lock = threading.RLock()
        self._initialize()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=self._busy_timeout_ms / 1000)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._lock, self._connection() as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS task_runs (
                    id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL UNIQUE,
                    parent_id TEXT REFERENCES task_runs(id),
                    session_id TEXT NOT NULL DEFAULT '',
                    input_text TEXT NOT NULL,
                    model TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    error TEXT NOT NULL DEFAULT '',
                    checkpoint_json TEXT NOT NULL DEFAULT '{}',
                    resume_count INTEGER NOT NULL DEFAULT 0,
                    last_event_seq INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    finished_at TEXT
                );
                CREATE TABLE IF NOT EXISTS steps (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES task_runs(id) ON DELETE CASCADE,
                    step_key TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    name TEXT NOT NULL DEFAULT '',
                    risk TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 1,
                    input_hash TEXT NOT NULL DEFAULT '',
                    input_json TEXT NOT NULL DEFAULT '{}',
                    output_json TEXT,
                    error TEXT NOT NULL DEFAULT '',
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    UNIQUE(run_id, step_key)
                );
                CREATE TABLE IF NOT EXISTS task_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES task_runs(id) ON DELETE CASCADE,
                    step_id TEXT REFERENCES steps(id) ON DELETE SET NULL,
                    seq INTEGER NOT NULL,
                    event_key TEXT NOT NULL,
                    type TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    UNIQUE(run_id, seq),
                    UNIQUE(run_id, event_key)
                );
                CREATE INDEX IF NOT EXISTS idx_task_runs_status_updated
                    ON task_runs(status, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_steps_run_ordinal
                    ON steps(run_id, ordinal);
                CREATE TABLE IF NOT EXISTS session_goals (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    objective TEXT NOT NULL,
                    criteria TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active',
                    round INTEGER NOT NULL DEFAULT 0,
                    max_rounds INTEGER NOT NULL DEFAULT 20,
                    last_verdict_json TEXT NOT NULL DEFAULT '{}',
                    history_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    finished_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_session_goals_session
                    ON session_goals(session_id, updated_at DESC);
                """
            )
            columns = {str(row["name"]) for row in db.execute("PRAGMA table_info(task_runs)")}
            # Phase 4: task lifecycle columns
            if "block_reason" not in columns:
                db.execute("ALTER TABLE task_runs ADD COLUMN block_reason TEXT NOT NULL DEFAULT ''")
            if "blocked_at" not in columns:
                db.execute("ALTER TABLE task_runs ADD COLUMN blocked_at TEXT")
            if "scheduled_at" not in columns:
                db.execute("ALTER TABLE task_runs ADD COLUMN scheduled_at TEXT")
            if "verification_json" not in columns:
                db.execute("ALTER TABLE task_runs ADD COLUMN verification_json TEXT NOT NULL DEFAULT '{}'")
            if "verified_at" not in columns:
                db.execute("ALTER TABLE task_runs ADD COLUMN verified_at TEXT")
            db.execute("DROP INDEX IF EXISTS idx_task_runs_case_updated")
            db.execute("CREATE INDEX IF NOT EXISTS idx_task_runs_status_scheduled ON task_runs(status, scheduled_at)")

    def recover_interrupted(self, *, session_id: str | None = None) -> int:
        """Mark work left in-flight by a previous process as interrupted/unknown."""
        now = _now()
        with self._lock, self._connection() as db:
            runs = db.execute(
                "SELECT id FROM task_runs WHERE status IN ('pending','running','cancelling')"
                + (" AND session_id=?" if session_id is not None else ""),
                (session_id,) if session_id is not None else (),
            ).fetchall()
            run_ids = [row["id"] for row in runs]
            if not run_ids:
                return 0
            placeholders = ",".join("?" for _ in run_ids)
            db.execute(
                f"UPDATE steps SET status='unknown', error=?, finished_at=? "
                f"WHERE status='running' AND run_id IN ({placeholders})",
                ["Process ended before step completion could be confirmed", now, *run_ids],
            )
            db.execute(
                f"UPDATE task_runs SET status='interrupted', error=?, updated_at=? "
                f"WHERE id IN ({placeholders})",
                ["Previous process ended before task completion", now, *run_ids],
            )
        for run_id in run_ids:
            self.append_event(run_id, f"recovered:{now}", "task_interrupted", {"reason": "process_restart"})
        return len(run_ids)

    def start_run(
        self,
        request_id: str,
        input_text: str,
        *,
        session_id: str = "",
        model: str = "",
        parent_id: str | None = None,
    ) -> dict[str, Any]:
        with self._lock, self._connection() as db:
            existing = db.execute("SELECT * FROM task_runs WHERE request_id=?", (request_id,)).fetchone()
        if existing is not None:
            return self._task_dict(existing)
        now = _now()
        run_id = uuid.uuid4().hex[:12]
        with self._lock, self._connection() as db:
            db.execute(
                """INSERT OR IGNORE INTO task_runs
                   (id, request_id, parent_id, session_id, input_text, model, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?)""",
                (run_id, request_id, parent_id, session_id, input_text, model, now, now),
            )
            row = db.execute("SELECT * FROM task_runs WHERE request_id=?", (request_id,)).fetchone()
        assert row is not None
        result = self._task_dict(row)
        self.append_event(result["id"], "task:started", "task_started", {
            "request_id": request_id,
        })
        return result

    def start_step(
        self,
        run_id: str,
        step_key: str,
        kind: str,
        *,
        name: str = "",
        risk: str = "",
        input_value: Any = None,
    ) -> dict[str, Any]:
        now = _now()
        payload = _json(input_value if input_value is not None else {})
        input_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        step_id = uuid.uuid4().hex[:12]
        with self._lock, self._connection() as db:
            # BEGIN IMMEDIATE: ordinal 的“读后写”(MAX+1) 与插入必须在同一
            # 写事务内，否则跨进程并发时两个 step 可能拿到相同 ordinal。
            db.execute("BEGIN IMMEDIATE")
            ordinal = int(db.execute(
                "SELECT COALESCE(MAX(ordinal), 0) + 1 FROM steps WHERE run_id=?", (run_id,)
            ).fetchone()[0])
            db.execute(
                """INSERT OR IGNORE INTO steps
                   (id, run_id, step_key, ordinal, kind, name, risk, status, input_hash, input_json, started_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?)""",
                (step_id, run_id, step_key, ordinal, kind, name, risk, input_hash, payload, now),
            )
            row = db.execute(
                "SELECT * FROM steps WHERE run_id=? AND step_key=?", (run_id, step_key)
            ).fetchone()
        assert row is not None
        result = self._step_dict(row)
        self.append_event(run_id, f"step:{result['id']}:started", "step_started", {
            "step_id": result["id"], "kind": kind, "name": name, "risk": risk,
        }, step_id=result["id"])
        return result

    def claim_tool(
        self,
        run_id: str,
        name: str,
        args: dict[str, Any],
        risk: str,
        invocation_id: str | None = None,
        replay: str = "never",
    ) -> ToolClaim:
        if replay not in {"never", "safe"}:
            raise ValueError(f"invalid tool replay policy: {replay}")
        payload = _json(args)
        digest = hashlib.sha256(f"{name}\0{payload}".encode("utf-8")).hexdigest()
        step_key = f"tool:{name}:{digest}"
        if invocation_id:
            invocation_digest = hashlib.sha256(invocation_id.encode("utf-8")).hexdigest()[:16]
            step_key = f"{step_key}:{invocation_digest}"
        with self._lock, self._connection() as db:
            # 单事务原子 claim：查重 → 创建 running step → 判断执行权。
            # 旧实现把“查重”和“创建”拆在两个事务里，跨进程并发时两个执行者
            # 都可能看到“尚不存在”并各自拿到 execute，重复触发有副作用的工具。
            # BEGIN IMMEDIATE 让后到的执行者等待先到的写事务提交后再查重，
            # 因此只会有一个执行者得到 execute。
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT * FROM steps WHERE run_id=? AND step_key=?", (run_id, step_key)
            ).fetchone()
            if existing is not None:
                step = self._step_dict(existing)
                if step["status"] == "completed":
                    return ToolClaim("cached", step["id"], step.get("output"))
                if step["status"] == "unknown" and replay == "safe":
                    now = _now()
                    db.execute(
                        """UPDATE steps SET status='running', attempt=attempt+1,
                           error='', finished_at=NULL, started_at=? WHERE id=?""",
                        (now, step["id"]),
                    )
                    replay_step_id = str(step["id"])
                    replay_attempt = int(step.get("attempt") or 1) + 1
                elif step["status"] in {"running", "unknown"}:
                    return ToolClaim("uncertain", step["id"])
                else:
                    return ToolClaim("cached", step["id"], step.get("output") or {
                        "output": "", "error": step.get("error") or "Previous tool attempt failed"
                    })
                # Reuse the original durable step identity, but record a new
                # attempt so recovery remains auditable and race-safe.
                step_id = replay_step_id
                event_kind = "step_replayed"
                event_key = f"step:{step_id}:replayed:{replay_attempt}"
            else:
                # 不存在：在同一写事务内创建 running step（含 ordinal 计算）。
                now = _now()
                step_id = uuid.uuid4().hex[:12]
                payload = _json(args)
                input_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
                ordinal = int(db.execute(
                    "SELECT COALESCE(MAX(ordinal), 0) + 1 FROM steps WHERE run_id=?", (run_id,)
                ).fetchone()[0])
                db.execute(
                    """INSERT INTO steps
                       (id, run_id, step_key, ordinal, kind, name, risk, status, input_hash, input_json, started_at)
                       VALUES (?, ?, ?, ?, 'tool', ?, ?, 'running', ?, ?, ?)""",
                    (step_id, run_id, step_key, ordinal, name, risk, input_hash, payload, now),
                )
                event_kind = "step_started"
                event_key = f"step:{step_id}:started"
        self.append_event(run_id, event_key, event_kind, {
            "step_id": step_id, "kind": "tool", "name": name, "risk": risk,
            "replay": replay,
        }, step_id=step_id)
        return ToolClaim("execute", step_id)

    def finish_step(
        self,
        step_id: str,
        *,
        status: str = "completed",
        output: Any = None,
        error: str = "",
    ) -> None:
        now = _now()
        with self._lock, self._connection() as db:
            row = db.execute(
                "SELECT run_id FROM steps WHERE id=?",
                (step_id,),
            ).fetchone()
            if row is None:
                return
            db.execute(
                "UPDATE steps SET status=?, output_json=?, error=?, finished_at=? WHERE id=?",
                (status, _json(output) if output is not None else None, error, now, step_id),
            )
            run_id = row["run_id"]
        self.append_event(run_id, f"step:{step_id}:finished", "step_finished", {
            "step_id": step_id, "status": status, "error": error,
        }, step_id=step_id)

    def checkpoint(self, run_id: str, data: dict[str, Any]) -> None:
        now = _now()
        with self._lock, self._connection() as db:
            db.execute(
                "UPDATE task_runs SET checkpoint_json=?, updated_at=? WHERE id=?",
                (_json(data), now, run_id),
            )
        self.append_event(run_id, f"checkpoint:{data.get('iteration', 0)}:{data.get('phase', '')}",
                          "checkpoint", data)

    def finish_run(self, run_id: str, status: str, error: str = "") -> None:
        now = _now()
        finished_at = now if status in TERMINAL_TASK_STATUSES else None
        with self._lock, self._connection() as db:
            if status in {"cancelled", "failed", "interrupted"}:
                # A cancellation may arrive during the durable claim, before
                # its caller receives the step id. Never leave that claim
                # looking active after its owning task has ended.
                db.execute(
                    "UPDATE steps SET status='unknown', error=?, finished_at=? "
                    "WHERE run_id=? AND status='running'",
                    ("Task ended before the step outcome was confirmed", now, run_id),
                )
            db.execute(
                "UPDATE task_runs SET status=?, error=?, updated_at=?, finished_at=? WHERE id=?",
                (status, error, now, finished_at, run_id),
            )
        self.append_event(run_id, f"task:{status}:{now}", f"task_{status}", {"error": error})

    def request_cancel(self, run_id: str) -> bool:
        with self._lock, self._connection() as db:
            row = db.execute("SELECT status FROM task_runs WHERE id=?", (run_id,)).fetchone()
            if row is None or row["status"] in TERMINAL_TASK_STATUSES:
                return False
            db.execute(
                "UPDATE task_runs SET status='cancelling', updated_at=? WHERE id=?", (_now(), run_id)
            )
        self.append_event(run_id, "task:cancel_requested", "cancel_requested", {})
        return True

    def prepare_resume(self, run_id: str) -> dict[str, Any]:
        now = _now()
        with self._lock, self._connection() as db:
            row = db.execute("SELECT * FROM task_runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(f"Unknown task: {run_id}")
            if row["status"] not in RESUMABLE_TASK_STATUSES:
                raise ValueError(f"Task {run_id} is {row['status']}; only interrupted/failed/cancelled tasks can resume")
            db.execute(
                """UPDATE task_runs SET status='running', error='', updated_at=?, finished_at=NULL,
                   resume_count=resume_count+1 WHERE id=?""",
                (now, run_id),
            )
        self.append_event(run_id, f"task:resumed:{now}", "task_resumed", {})
        result = self.get_task(run_id)
        assert result is not None
        return result

    # ------------------------------------------------------------------
    # Phase 4: Task lifecycle — block / unblock / schedule / verify
    # ------------------------------------------------------------------

    def block_run(self, run_id: str, reason: str, *, kind: str = "blocked_on_user") -> dict[str, Any]:
        """Transition a running task to a blocked state.

        kind must be 'blocked_on_user' (waiting for user input/decision)
        or 'waiting_external' (waiting for email reply, API callback, etc.).
        """
        if kind not in BLOCKED_TASK_STATUSES:
            raise ValueError(f"Invalid block kind: {kind}; must be one of {BLOCKED_TASK_STATUSES}")
        now = _now()
        with self._lock, self._connection() as db:
            row = db.execute("SELECT status FROM task_runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(f"Unknown task: {run_id}")
            if row["status"] != "running":
                raise ValueError(f"Task {run_id} is {row['status']}; only running tasks can be blocked")
            db.execute(
                "UPDATE task_runs SET status=?, block_reason=?, blocked_at=?, updated_at=? WHERE id=?",
                (kind, " ".join(str(reason).split())[:500], now, now, run_id),
            )
        self.append_event(run_id, f"task:{kind}:{now}", f"task_{kind}", {"reason": reason})
        result = self.get_task(run_id)
        assert result is not None
        return result

    def unblock_run(self, run_id: str) -> dict[str, Any]:
        """Resume a blocked/scheduled task back to running."""
        now = _now()
        resumable = BLOCKED_TASK_STATUSES | SCHEDULED_TASK_STATUSES
        with self._lock, self._connection() as db:
            row = db.execute("SELECT status FROM task_runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(f"Unknown task: {run_id}")
            if row["status"] not in resumable:
                raise ValueError(
                    f"Task {run_id} is {row['status']}; only blocked/scheduled tasks can be unblocked"
                )
            db.execute(
                "UPDATE task_runs SET status='running', block_reason='', blocked_at=NULL, "
                "scheduled_at=NULL, updated_at=? WHERE id=?",
                (now, run_id),
            )
        self.append_event(run_id, f"task:unblocked:{now}", "task_unblocked", {})
        result = self.get_task(run_id)
        assert result is not None
        return result

    def schedule_run(self, run_id: str, scheduled_at: str) -> dict[str, Any]:
        """Defer a task to run at a specific time (ISO 8601).

        The task must be running or blocked. It transitions to 'scheduled'
        and can be picked up by a scheduler via due_scheduled_runs().
        """
        now = _now()
        with self._lock, self._connection() as db:
            row = db.execute("SELECT status FROM task_runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(f"Unknown task: {run_id}")
            if row["status"] not in {"running"} | BLOCKED_TASK_STATUSES:
                raise ValueError(
                    f"Task {run_id} is {row['status']}; only running or blocked tasks can be scheduled"
                )
            db.execute(
                "UPDATE task_runs SET status='scheduled', scheduled_at=?, block_reason='', "
                "blocked_at=NULL, updated_at=? WHERE id=?",
                (scheduled_at, now, run_id),
            )
        self.append_event(run_id, f"task:scheduled:{scheduled_at}", "task_scheduled", {
            "scheduled_at": scheduled_at,
        })
        result = self.get_task(run_id)
        assert result is not None
        return result

    def verify_completion(
        self,
        run_id: str,
        *,
        verifier: str = "",
        evidence: dict[str, Any] | None = None,
        passed: bool = True,
    ) -> dict[str, Any]:
        """Mark a completed task as verified (or reject verification).

        The task must be 'completed'. If passed=True, transitions to
        'done_verified'. If passed=False, reverts to 'running' with the
        verification failure recorded.
        """
        now = _now()
        verification = {
            "verifier": verifier,
            "passed": passed,
            "evidence": evidence or {},
            "verified_at": now,
        }
        with self._lock, self._connection() as db:
            row = db.execute("SELECT status FROM task_runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(f"Unknown task: {run_id}")
            if row["status"] != "completed":
                raise ValueError(
                    f"Task {run_id} is {row['status']}; only completed tasks can be verified"
                )
            if passed:
                db.execute(
                    "UPDATE task_runs SET status='done_verified', verification_json=?, "
                    "verified_at=?, updated_at=? WHERE id=?",
                    (_json(verification), now, now, run_id),
                )
            else:
                db.execute(
                    "UPDATE task_runs SET status='running', verification_json=?, "
                    "verified_at=NULL, finished_at=NULL, updated_at=? WHERE id=?",
                    (_json(verification), now, run_id),
                )
        event_type = "task_verified" if passed else "task_verification_failed"
        self.append_event(run_id, f"task:verify:{now}", event_type, verification)
        result = self.get_task(run_id)
        assert result is not None
        return result

    def record_verification_contract(self, run_id: str, contract: dict[str, Any]) -> dict[str, Any]:
        """Persist coding evidence without changing the task completion state.

        ``verify_completion`` remains the only API that may transition a task
        to ``done_verified``. This prevents a passing command from becoming a
        task-completion claim by itself.
        """
        payload = dict(contract)
        payload["recorded_at"] = _now()
        now = _now()
        with self._lock, self._connection() as db:
            row = db.execute("SELECT id FROM task_runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(f"Unknown task: {run_id}")
            db.execute(
                "UPDATE task_runs SET verification_json=?, updated_at=? WHERE id=?",
                (_json(payload), now, run_id),
            )
        self.append_event(run_id, f"verification-contract:{payload['recorded_at']}", "verification_contract", payload)
        result = self.get_task(run_id)
        assert result is not None
        return result

    def blocked_runs(self, *, kind: str = "", limit: int = 20) -> list[dict[str, Any]]:
        """List tasks currently blocked on user or external events."""
        statuses = BLOCKED_TASK_STATUSES if not kind else {kind}
        if not statuses <= BLOCKED_TASK_STATUSES:
            raise ValueError(f"Invalid block kind filter: {kind}")
        placeholders = ",".join("?" for _ in statuses)
        with self._lock, self._connection() as db:
            rows = db.execute(
                f"SELECT * FROM task_runs WHERE status IN ({placeholders}) "
                "ORDER BY blocked_at DESC LIMIT ?",
                (*statuses, max(1, min(limit, 100))),
            ).fetchall()
        return [self._task_dict(row) for row in rows]

    def due_scheduled_runs(self, *, as_of: str = "", limit: int = 20) -> list[dict[str, Any]]:
        """List scheduled tasks whose scheduled_at <= as_of (default: now)."""
        cutoff = as_of or _now()
        with self._lock, self._connection() as db:
            rows = db.execute(
                "SELECT * FROM task_runs WHERE status='scheduled' AND scheduled_at <= ? "
                "ORDER BY scheduled_at ASC LIMIT ?",
                (cutoff, max(1, min(limit, 100))),
            ).fetchall()
        return [self._task_dict(row) for row in rows]

    # ── Session goal mode ──

    @staticmethod
    def _goal_dict(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["last_verdict"] = _loads(result.pop("last_verdict_json", None), {})
        result["history"] = _loads(result.pop("history_json", None), [])
        return result

    def set_goal(
        self,
        session_id: str,
        objective: str,
        *,
        criteria: str = "",
        max_rounds: int = 0,
    ) -> dict[str, Any]:
        """Set the session goal, replacing any previous unfinished goal.

        A session has one live goal at a time. Setting a new goal clears
        the previous active/paused one and starts round counting from zero.
        """
        text = " ".join(str(objective or "").split())
        if not text:
            raise ValueError("Goal objective must not be empty")
        if len(text) > 2000:
            raise ValueError("Goal objective is too long (max 2000 chars)")
        budget = int(max_rounds) if int(max_rounds) > 0 else int(os.getenv("GOAL_MAX_ROUNDS", "20") or 20)
        budget = max(1, min(budget, 50))
        now = _now()
        goal_id = uuid.uuid4().hex[:16]
        with self._lock, self._connection() as db:
            db.execute(
                "UPDATE session_goals SET status='cleared', updated_at=?, finished_at=? "
                "WHERE session_id=? AND status IN ('active', 'paused')",
                (now, now, session_id),
            )
            db.execute(
                "INSERT INTO session_goals (id, session_id, objective, criteria, status, "
                "round, max_rounds, last_verdict_json, history_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'active', 0, ?, '{}', '[]', ?, ?)",
                (goal_id, session_id, text, str(criteria or "").strip()[:2000], budget, now, now),
            )
        row = self.get_goal(goal_id)
        assert row is not None
        return row

    def get_goal(self, goal_id: str) -> dict[str, Any] | None:
        with self._lock, self._connection() as db:
            row = db.execute("SELECT * FROM session_goals WHERE id=?", (goal_id,)).fetchone()
        return self._goal_dict(row) if row is not None else None

    def active_goal_for_session(self, session_id: str) -> dict[str, Any] | None:
        """Return the session's live goal (active or paused), if any."""
        with self._lock, self._connection() as db:
            row = db.execute(
                "SELECT * FROM session_goals WHERE session_id=? AND status IN ('active', 'paused') "
                "ORDER BY updated_at DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        return self._goal_dict(row) if row is not None else None

    def _transition_session_goal(self, session_id: str, *, from_status: str, to_status: str) -> dict[str, Any] | None:
        now = _now()
        finished = now if to_status in GOAL_TERMINAL_STATUSES else None
        with self._lock, self._connection() as db:
            row = db.execute(
                "SELECT * FROM session_goals WHERE session_id=? AND status=? "
                "ORDER BY updated_at DESC LIMIT 1",
                (session_id, from_status),
            ).fetchone()
            if row is None:
                return None
            db.execute(
                "UPDATE session_goals SET status=?, updated_at=?, finished_at=? WHERE id=?",
                (to_status, now, finished, row["id"]),
            )
        return self.get_goal(str(row["id"]))

    def pause_goal(self, session_id: str) -> dict[str, Any] | None:
        """Pause the active session goal; completed rounds and files stay."""
        return self._transition_session_goal(session_id, from_status="active", to_status="paused")

    def resume_goal(self, session_id: str) -> dict[str, Any] | None:
        """Resume a paused session goal."""
        return self._transition_session_goal(session_id, from_status="paused", to_status="active")

    def clear_goal(self, session_id: str) -> dict[str, Any] | None:
        """Clear the live session goal (active or paused)."""
        goal = self._transition_session_goal(session_id, from_status="active", to_status="cleared")
        if goal is None:
            goal = self._transition_session_goal(session_id, from_status="paused", to_status="cleared")
        return goal

    def record_goal_round(self, goal_id: str, verdict: dict[str, Any]) -> dict[str, Any]:
        """Record one verification round and advance the goal state machine.

        met=True completes the goal. Otherwise the round counter advances and
        the goal becomes 'exhausted' once it reaches its max_rounds budget.
        """
        from .goal_verifier import verdict_is_usable
        if not verdict_is_usable(verdict):
            # Defense in depth for callers other than the current backend.
            # Unusable evidence neither completes the goal nor spends a round.
            current = self.get_goal(goal_id)
            if current is None:
                raise KeyError(f"Unknown goal: {goal_id}")
            return current
        now = _now()
        met = bool(verdict.get("met"))
        entry = {
            "at": now,
            "met": met,
            "evidence": str(verdict.get("evidence") or "")[:1000],
            "next_step": str(verdict.get("next_step") or "")[:1000],
        }
        with self._lock, self._connection() as db:
            row = db.execute("SELECT * FROM session_goals WHERE id=?", (goal_id,)).fetchone()
            if row is None:
                raise KeyError(f"Unknown goal: {goal_id}")
            if row["status"] != "active":
                raise ValueError(f"Goal {goal_id} is {row['status']}; only active goals log rounds")
            round_no = int(row["round"]) + 1
            entry["round"] = round_no
            history = _loads(row["history_json"], [])
            if not isinstance(history, list):
                history = []
            history.append(entry)
            history = history[-20:]
            if met:
                status, finished = "completed", now
            elif round_no >= int(row["max_rounds"]):
                status, finished = "exhausted", now
            else:
                status, finished = "active", None
            db.execute(
                "UPDATE session_goals SET round=?, status=?, last_verdict_json=?, history_json=?, "
                "updated_at=?, finished_at=? WHERE id=?",
                (round_no, status, _json({**entry, "verifier": str(verdict.get("verifier") or "")}),
                 _json(history), now, finished, goal_id),
            )
        result = self.get_goal(goal_id)
        assert result is not None
        return result

    def format_goal_status(self, session_id: str) -> str:
        """Human-readable goal status for commands and tool output."""
        goal = self.active_goal_for_session(session_id)
        if goal is None:
            return "No active session goal. Set one with /goal <objective> or the goal tool."
        lines = [
            f"Goal [{goal['status']}] round {goal['round']}/{goal['max_rounds']}",
            f"Objective: {goal['objective']}",
        ]
        if goal.get("criteria"):
            lines.append(f"Criteria: {goal['criteria']}")
        verdict = goal.get("last_verdict") or {}
        if verdict:
            lines.append(
                f"Last verdict: {'MET' if verdict.get('met') else 'not met'}"
                + (f" — next: {verdict['next_step']}" if verdict.get("next_step") else "")
            )
        return "\n".join(lines)

    def get_goal_history(self, session_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
        """Return the session's goal plus its verification rounds, oldest first.

        Prefers the live goal (active or paused); falls back to the most
        recently updated goal so history stays visible after completion or
        exhaustion. Returns None when the session has never set a goal.
        """
        goal = self.active_goal_for_session(session_id)
        if goal is None:
            with self._lock, self._connection() as db:
                row = db.execute(
                    "SELECT * FROM session_goals WHERE session_id=? "
                    "ORDER BY updated_at DESC LIMIT 1",
                    (session_id,),
                ).fetchone()
            goal = self._goal_dict(row) if row is not None else None
        if goal is None:
            return None
        return goal, list(goal.get("history") or [])

    def append_event(
        self,
        run_id: str,
        event_key: str,
        event_type: str,
        payload: Any,
        *,
        step_id: str | None = None,
    ) -> int:
        now = _now()
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT seq FROM task_events WHERE run_id=? AND event_key=?", (run_id, event_key)
            ).fetchone()
            if existing is not None:
                return int(existing["seq"])
            row = db.execute("SELECT last_event_seq FROM task_runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(f"Unknown task: {run_id}")
            seq = int(row["last_event_seq"]) + 1
            db.execute(
                """INSERT INTO task_events
                   (run_id, step_id, seq, event_key, type, payload_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (run_id, step_id, seq, event_key, event_type, _json(payload), now),
            )
            db.execute(
                "UPDATE task_runs SET last_event_seq=?, updated_at=? WHERE id=?", (seq, now, run_id)
            )
            return seq

    def get_task(self, run_id: str) -> dict[str, Any] | None:
        with self._lock, self._connection() as db:
            row = db.execute("SELECT * FROM task_runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                return None
            steps = db.execute(
                "SELECT * FROM steps WHERE run_id=? ORDER BY ordinal", (run_id,)
            ).fetchall()
        result = self._task_dict(row)
        result["steps"] = [self._step_dict(step) for step in steps]
        return result

    def list_tasks(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock, self._connection() as db:
            rows = db.execute(
                """SELECT task_runs.*,
                   (SELECT COUNT(*) FROM steps WHERE steps.run_id=task_runs.id) AS step_count
                   FROM task_runs ORDER BY updated_at DESC LIMIT ?""",
                (max(1, min(limit, 100)),),
            ).fetchall()
        return [self._task_dict(row) | {"step_count": int(row["step_count"])} for row in rows]

    def recent_tool_results(self, session_id: str, limit: int = 100) -> list[dict[str, Any]]:
        """Read completed tool events for one session, in completion order."""
        with self._lock, self._connection() as db:
            rows = db.execute(
                """SELECT s.output_json FROM steps s
                   JOIN task_runs t ON t.id=s.run_id
                   WHERE t.session_id=? AND s.kind='tool'
                     AND s.status IN ('completed', 'failed') AND s.output_json IS NOT NULL
                   ORDER BY s.finished_at DESC, s.rowid DESC LIMIT ?""",
                (str(session_id), max(1, min(limit, 100))),
            ).fetchall()
        results = [_loads(row["output_json"], {}) for row in reversed(rows)]
        return [result for result in results if isinstance(result, dict) and result.get("type") == "tool_result"]

    def latest_task_for_session(
        self,
        session_id: str,
        *,
        statuses: tuple[str, ...] = (
            "running", "cancelling", "interrupted",
            "blocked_on_user", "waiting_external", "scheduled",
        ),
    ) -> dict[str, Any] | None:
        """Return the newest relevant task for a session, including its steps."""
        if not statuses:
            return None
        placeholders = ",".join("?" for _ in statuses)
        with self._lock, self._connection() as db:
            row = db.execute(
                f"SELECT id FROM task_runs WHERE session_id=? AND status IN ({placeholders}) "
                "ORDER BY updated_at DESC LIMIT 1",
                (str(session_id), *statuses),
            ).fetchone()
        return self.get_task(str(row["id"])) if row else None

    @staticmethod
    def _task_dict(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        # Existing databases may retain the removed Case compatibility column.
        result.pop("case_id", None)
        result["checkpoint"] = _loads(result.pop("checkpoint_json", None), {})
        result["verification"] = _loads(result.pop("verification_json", None), {})
        return result

    @staticmethod
    def _step_dict(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["input"] = _loads(result.pop("input_json", None), {})
        result["output"] = _loads(result.pop("output_json", None), None)
        return result


def format_task_list(tasks: list[dict[str, Any]]) -> str:
    if not tasks:
        return "No recorded tasks."
    lines = [f"Tasks ({len(tasks)} shown):"]
    for task in tasks:
        text = " ".join(task["input_text"].split())
        if len(text) > 60:
            text = text[:57] + "..."
        lines.append(f"  {task['id']}  {task['status']:<11}  steps={task.get('step_count', 0):<3}  {text}")
    return "\n".join(lines)


def format_task_detail(task: dict[str, Any]) -> str:
    lines = [
        f"Task {task['id']}",
        f"Status: {task['status']}",
        f"Model: {task['model'] or '(unknown)'}",
        f"Session: {task['session_id'] or '(unknown)'}",
        f"Resumes: {task['resume_count']}",
        f"Input: {task['input_text']}",
    ]
    if task.get("error"):
        lines.append(f"Error: {task['error']}")
    if task.get("block_reason"):
        lines.append(f"Blocked: {task['block_reason']}")
    if task.get("blocked_at"):
        lines.append(f"Blocked at: {task['blocked_at']}")
    if task.get("scheduled_at"):
        lines.append(f"Scheduled at: {task['scheduled_at']}")
    verification = task.get("verification") or {}
    if verification:
        if verification.get("required"):
            lines.append("Verification: UNVERIFIED — execution receipts require assessment")
            for check in (verification.get("checks") or [])[-3:]:
                lines.append(f"  {check.get('tool', '?')}: exit={check.get('exit_code', 'unknown')} "
                             f"state={check.get('execution_status', 'unknown')}")
        else:
            v_status = "PASSED" if verification.get("passed") else "FAILED"
            lines.append(f"Verification: {v_status} by {verification.get('verifier') or '(unknown)'}")
        if verification.get("evidence"):
            lines.append(f"  Evidence: {verification['evidence']}")
    checkpoint = task.get("checkpoint") or {}
    if checkpoint:
        lines.append(f"Checkpoint: {checkpoint.get('phase', '?')} iteration={checkpoint.get('iteration', '?')}")
    lines.append("Steps:")
    for step in task.get("steps", []):
        suffix = f" — {step['error']}" if step.get("error") else ""
        lines.append(f"  {step['ordinal']:>2}. {step['kind']} {step['name']} [{step['status']}]{suffix}")
    if not task.get("steps"):
        lines.append("  (none)")
    return "\n".join(lines)


def format_today(store: TaskStore) -> str:
    """Build a /today dashboard from durable TaskRun state."""
    sections: list[str] = []

    # 1. Blocked on user
    user_blocked = store.blocked_runs(kind="blocked_on_user", limit=10)
    if user_blocked:
        lines = ["⏳ Waiting for YOU:"]
        for task in user_blocked:
            text = " ".join(task["input_text"].split())[:60]
            reason = task.get("block_reason") or ""
            lines.append(f"  • {task['id']}  {text}")
            if reason:
                lines.append(f"    → {reason}")
        sections.append("\n".join(lines))

    # 2. Waiting on external
    ext_blocked = store.blocked_runs(kind="waiting_external", limit=10)
    if ext_blocked:
        lines = ["📬 Waiting on external:"]
        for task in ext_blocked:
            text = " ".join(task["input_text"].split())[:60]
            reason = task.get("block_reason") or ""
            lines.append(f"  • {task['id']}  {text}")
            if reason:
                lines.append(f"    → {reason}")
        sections.append("\n".join(lines))

    # 3. Scheduled / due
    due = store.due_scheduled_runs(limit=10)
    if due:
        lines = ["📅 Scheduled (due):"]
        for task in due:
            text = " ".join(task["input_text"].split())[:60]
            lines.append(f"  • {task['id']}  {task.get('scheduled_at', '?')}  {text}")
        sections.append("\n".join(lines))

    # 4. Recently completed / verified
    recent = store.list_tasks(limit=5)
    done_recent = [t for t in recent if t["status"] in ("completed", "done_verified")]
    if done_recent:
        lines = ["✅ Recently done:"]
        for task in done_recent:
            text = " ".join(task["input_text"].split())[:60]
            verified = " ✓" if task["status"] == "done_verified" else ""
            lines.append(f"  • {task['id']}  {text}{verified}")
        sections.append("\n".join(lines))

    if not sections:
        return "🎉 All clear — no pending items, no blocked tasks, nothing scheduled."

    return "\n\n".join(sections)
