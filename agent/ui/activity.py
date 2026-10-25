"""Read-only views for the desktop: scheduled jobs with their results, and what Astra did, day by day.

Both read the stores directly and never write: no migration, no marker, no session.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

from agent.runtime.approval_inbox import approval_db_path
from agent.runtime.jobs.calendar import quiet_hours
from agent.runtime.jobs.store import PUSH_TARGETS
from agent.runtime.task_store import task_db_path
from agent.runtime.tools.jobs import schedule_text

PREVIEW_CHARS = 280
# Tool steps shown on the activity page: those that change something, run something or act for the person.
EFFECT_RISKS = frozenset({"write", "execute"})
_SUMMARY_KEYS = ("path", "file_path", "command", "code", "url", "app", "target", "to", "name", "query", "title")


def _connect(path: Path) -> sqlite3.Connection | None:
    """A read-only connection, or None when the store does not exist yet."""
    if not path.is_file():
        return None
    try:
        db = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return None
    db.row_factory = sqlite3.Row
    return db


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}


def _clip(text: Any, limit: int = PREVIEW_CHARS) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[:limit - 1] + "…"


def _instant(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def jobs_overview(home: Path, *, inbox_limit: int = 50) -> dict[str, Any]:
    """Every job that is not removed, the latest results, and the quiet hours."""
    db = _connect(Path(home) / "jobs" / "jobs.sqlite3")
    result: dict[str, Any] = {"jobs": [], "inbox": [], "quiet_hours": quiet_hours(home)}
    if db is None:
        return result
    with closing(db):
        job_columns = _columns(db, "jobs")
        for row in db.execute("SELECT jobs.*, (SELECT state FROM runs WHERE job_id=jobs.id ORDER BY created_at DESC,"
                              "rowid DESC LIMIT 1) AS last_outcome FROM jobs WHERE state!='removed' "
                              "ORDER BY created_at,id"):
            spec = json.loads(row["spec"])
            result["jobs"].append({
                "id": row["id"], "name": spec["name"], "kind": spec["kind"], "state": row["state"],
                "paused_reason": row["paused_reason"] if "paused_reason" in job_columns else "",
                "schedule": schedule_text(spec.get("calendar"), row["interval_seconds"], row["next_at"]),
                "next_at": row["next_at"] if row["state"] == "enabled" else None,
                "notify": spec.get("notify", "always"), "web": bool(spec.get("web")), "prompt": spec["prompt"],
                "targets": spec.get("targets", list(PUSH_TARGETS)), "last_outcome": row["last_outcome"] or "",
            })
        run_columns = _columns(db, "runs")
        pushes = "pushes" in {row["name"] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for row in db.execute("SELECT runs.*, inbox.received_at, inbox.read_at FROM inbox JOIN runs ON runs.id=inbox.run_id "
                              "ORDER BY inbox.received_at DESC, runs.id DESC LIMIT ?", (max(1, min(200, inbox_limit)),)):
            spec = json.loads(row["spec"])
            sent = [push["target"] for push in db.execute(
                "SELECT target FROM pushes WHERE run_id=? AND state='sent' ORDER BY target", (row["id"],))] if pushes else []
            result["inbox"].append({
                "id": row["id"], "job_id": row["job_id"], "name": spec["name"], "kind": spec["kind"],
                "state": row["state"], "quiet": bool(row["quiet"]) if "quiet" in run_columns else False,
                "output": row["output"], "detail": row["detail"], "skipped_count": row["skipped_count"],
                "usage": json.loads(row["usage"]) if "usage" in run_columns and row["usage"] else {},
                "time": row["finished_at"] or row["created_at"], "read": row["read_at"] is not None, "sent": sent,
            })
    return result


def _effect(row: sqlite3.Row) -> dict[str, Any]:
    try:
        arguments = json.loads(row["input_json"] or "{}")
    except ValueError:
        arguments = {}
    summary = ""
    if isinstance(arguments, dict):
        actions = arguments.get("actions")
        if isinstance(actions, list):
            summary = f"{len(actions)} 个操作"
        for key in _SUMMARY_KEYS:
            if not summary and isinstance(arguments.get(key), str) and arguments[key].strip():
                summary = arguments[key]
    return {"name": row["name"], "risk": row["risk"], "status": row["status"], "summary": _clip(summary, 120)}


def activity(home: Path, *, days: int = 7, now: float | None = None, limit: int = 300) -> dict[str, Any]:
    """What Astra did in the last ``days``: conversation turns with their effects and approvals, and job runs."""
    now = time.time() if now is None else now
    since = now - max(1, min(90, days)) * 86400
    entries: list[dict[str, Any]] = []
    approvals: dict[str, list[dict[str, Any]]] = {}
    approval_db = _connect(approval_db_path())
    if approval_db is not None:
        with closing(approval_db):
            for row in approval_db.execute("SELECT task_id, state, decision, request_json, created_at FROM approval_requests "
                                           "WHERE task_id!='' ORDER BY created_at"):
                if _instant(row["created_at"]) < since:
                    continue
                request = json.loads(row["request_json"] or "{}")
                approvals.setdefault(row["task_id"], []).append({
                    "title": _clip(request.get("approval_title") or request.get("tool_name"), 120),
                    "decision": row["decision"] or row["state"]})
    tasks = _connect(task_db_path())
    if tasks is not None:
        with closing(tasks):
            source = "source" if "source" in _columns(tasks, "task_runs") else "''"
            for run in tasks.execute(f"SELECT id, session_id, input_text, status, created_at, {source} AS source "
                                     "FROM task_runs WHERE parent_id IS NULL ORDER BY created_at DESC LIMIT ?", (limit,)):
                started = _instant(run["created_at"])
                if started < since:
                    continue
                effects = [_effect(step) for step in tasks.execute(
                    "SELECT name, risk, status, input_json FROM steps WHERE run_id=? AND kind='tool' AND risk IN "
                    f"({','.join('?' * len(EFFECT_RISKS))}) ORDER BY ordinal", (run["id"], *sorted(EFFECT_RISKS)))]
                entries.append({"kind": "turn", "id": run["id"], "source": run["source"] or "", "time": started,
                                "session": run["session_id"], "title": _clip(run["input_text"], 120),
                                "status": run["status"], "effects": effects, "approvals": approvals.get(run["id"], [])})
    jobs = _connect(Path(home) / "jobs" / "jobs.sqlite3")
    if jobs is not None:
        with closing(jobs):
            quiet = "quiet" in _columns(jobs, "runs")
            for run in jobs.execute("SELECT * FROM runs WHERE created_at>=? ORDER BY created_at DESC LIMIT ?",
                                    (since, limit)):
                if run["state"] in {"claimed", "running"} or (run["state"] == "skipped" and run["skipped_count"]):
                    continue
                spec = json.loads(run["spec"])
                entries.append({"kind": "job", "id": run["id"], "source": "job", "time": run["created_at"],
                                "job_id": run["job_id"], "title": spec["name"], "status": run["state"],
                                "quiet": bool(run["quiet"]) if quiet else False,
                                "summary": _clip(run["output"] or run["detail"])})
    entries.sort(key=lambda entry: entry["time"], reverse=True)
    return {"entries": entries[:limit], "since": since}
