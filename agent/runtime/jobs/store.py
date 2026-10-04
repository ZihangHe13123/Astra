"""Atomic schedules, immutable occurrence outcomes and a local delivery outbox."""

from __future__ import annotations

import json
import math
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from agent.runtime.instance_lock import InstanceLock

MAX_OUTPUT = 64_000


def finite(value: float, minimum: float, maximum: float, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{label} must be a finite number.")
    if not minimum <= value <= maximum:
        raise ValueError(f"{label} must be between {minimum:g} and {maximum:g}.")
    return float(value)


class JobStore:
    def __init__(self, home: Path):
        self.home = Path(home).expanduser().resolve()
        self.root = self.home / "jobs"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / "jobs.sqlite3"
        with self.connect() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in {0, 1}:
                raise ValueError("Unsupported jobs database version; preserve it for inspection.")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, spec TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('enabled','paused','completed','removed')),
                    next_at REAL NOT NULL, interval_seconds REAL NOT NULL,
                    grace_seconds REAL NOT NULL, created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id),
                    scheduled_at REAL, created_at REAL NOT NULL, finished_at REAL,
                    state TEXT NOT NULL CHECK(state IN ('claimed','running','completed','failed','unknown','skipped')),
                    spec TEXT NOT NULL, output TEXT NOT NULL DEFAULT '',
                    detail TEXT NOT NULL DEFAULT '', skipped_count INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(job_id, scheduled_at)
                );
                CREATE TABLE IF NOT EXISTS deliveries (
                    run_id TEXT PRIMARY KEY REFERENCES runs(id),
                    state TEXT NOT NULL CHECK(state IN ('pending','delivered')),
                    delivered_at REAL
                );
                CREATE TABLE IF NOT EXISTS inbox (
                    run_id TEXT PRIMARY KEY REFERENCES deliveries(run_id),
                    received_at REAL NOT NULL, read_at REAL
                );
                CREATE INDEX IF NOT EXISTS jobs_due ON jobs(state, next_at);
                CREATE INDEX IF NOT EXISTS runs_owner ON runs(job_id, state);
                PRAGMA user_version=1;
            """)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except BaseException:
                db.rollback()
                raise
            else:
                db.commit()

    @staticmethod
    def record(row: sqlite3.Row) -> dict:
        result = dict(row)
        if "spec" in result:
            result["spec"] = json.loads(result["spec"])
        return result

    def add(self, *, name: str, prompt: str, original_request: str,
            workdir: Path, next_at: float, interval_seconds: float = 0,
            grace_seconds: float = 300, kind: str = "check", model: str = "",
            web: bool = False, idle_seconds: float = 120, timeout_seconds: float = 900,
            max_iterations: int = 10, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        finite(next_at, 0, 253402300799, "First run timestamp")
        finite(interval_seconds, 0, 365 * 86400, "Interval")
        if 0 < interval_seconds < 60:
            raise ValueError("Interval must be at least 60 seconds.")
        finite(grace_seconds, 1, 86400, "Catch-up grace")
        finite(idle_seconds, 1, 3600, "Idle budget")
        finite(timeout_seconds, idle_seconds, 86400, "Hard budget")
        if type(max_iterations) is not int or not 1 <= max_iterations <= 50:
            raise ValueError("Iterations must be between 1 and 50.")
        if kind not in {"check", "reminder"} or type(web) is not bool:
            raise ValueError("Choose a check or reminder and a boolean network option.")
        fields = ((name, 120), (prompt, 4000), (original_request, 8000))
        if any(not isinstance(value, str) or not value.strip() or len(value) > limit for value, limit in fields):
            raise ValueError("A job needs a bounded name, prompt and original user request.")
        directory = Path(workdir).expanduser().resolve(strict=True)
        if not directory.is_dir():
            raise ValueError("The working directory must be a directory.")
        if kind == "check" and (not model.strip() or len(model) > 200):
            raise ValueError("An agent check needs a configured model key.")
        if kind == "reminder" and (model or web):
            raise ValueError("A reminder does not use a model or network tools.")
        spec = {"name": name.strip(), "prompt": prompt.strip(), "original_request": original_request.strip(),
                "workdir": str(directory), "kind": kind, "model": model, "web": web,
                "idle_seconds": idle_seconds, "timeout_seconds": timeout_seconds,
                "max_iterations": max_iterations}
        identity = uuid.uuid4().hex
        with self.transaction() as db:
            db.execute("INSERT INTO jobs VALUES (?, ?, ?, 'enabled', ?, ?, ?, ?)",
                       (identity, spec["name"], json.dumps(spec), next_at, interval_seconds, grace_seconds, now))
        return self.job(identity)

    def jobs(self) -> list[dict]:
        with self.connect() as db:
            return [self.record(row) for row in db.execute("SELECT * FROM jobs ORDER BY created_at, id")]

    def job(self, identity: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise ValueError("Unknown job ID.")
        return self.record(row)

    def set_state(self, identity: str, state: str) -> dict:
        if state not in {"enabled", "paused", "removed"}:
            raise ValueError("Invalid job state.")
        with self.transaction() as db:
            row = db.execute("SELECT state, interval_seconds FROM jobs WHERE id=?", (identity,)).fetchone()
            if row is None:
                raise ValueError("Unknown job ID.")
            if row["state"] == "removed" or (state == "enabled" and row["state"] == "completed"):
                raise ValueError("A removed or completed one-shot cannot be resumed; create a new job.")
            db.execute("UPDATE jobs SET state=? WHERE id=?", (state, identity))
        return self.job(identity)

    @staticmethod
    def _insert_run(db: sqlite3.Connection, job: sqlite3.Row, slot: float | None,
                    now: float, *, skipped: int = 0) -> str:
        identity = uuid.uuid4().hex
        state = "skipped" if skipped else "claimed"
        detail = f"Missed {skipped} slot(s) outside the catch-up policy." if skipped else ""
        db.execute("INSERT INTO runs (id,job_id,scheduled_at,created_at,finished_at,state,spec,detail,skipped_count) "
                   "VALUES (?,?,?,?,?,?,?,?,?)", (identity, job["id"], slot, now, now if skipped else None,
                                                state, job["spec"], detail, skipped))
        return identity

    def claim_due(self, *, now: float | None = None, manual_job: str = "") -> dict | None:
        now = time.time() if now is None else now
        identity = ""
        with self.transaction() as db:
            if manual_job:
                job = db.execute("SELECT * FROM jobs WHERE id=? AND state!='removed'", (manual_job,)).fetchone()
                if job is None:
                    raise ValueError("Unknown or removed job ID.")
                if db.execute("SELECT 1 FROM runs WHERE job_id=? AND state IN ('claimed','running')",
                              (manual_job,)).fetchone():
                    raise ValueError("This job already has an active occurrence.")
                identity = self._insert_run(db, job, None, now)
            else:
                candidates = db.execute("SELECT * FROM jobs WHERE state='enabled' AND next_at<=? "
                                        "AND NOT EXISTS (SELECT 1 FROM runs WHERE runs.job_id=jobs.id "
                                        "AND runs.state IN ('claimed','running')) ORDER BY next_at,id", (now,)).fetchall()
                for job in candidates:
                    first = job["next_at"]
                    period = job["interval_seconds"]
                    older = int((now - first) // period) if period else 0
                    latest = first + older * period
                    eligible = now - latest <= job["grace_seconds"]
                    missed = older if eligible else older + 1
                    if missed:
                        self._insert_run(db, job, first, now, skipped=missed)
                    if period:
                        db.execute("UPDATE jobs SET next_at=? WHERE id=?", (latest + period, job["id"]))
                    else:
                        db.execute("UPDATE jobs SET state='completed' WHERE id=?", (job["id"],))
                    if eligible:
                        identity = self._insert_run(db, job, latest, now)
                        break
        return self.run(identity) if identity else None

    def run(self, identity: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise ValueError("Unknown occurrence ID.")
        return self.record(row)

    def history(self, job_id: str = "", *, active: bool = False, limit: int = 50) -> list[dict]:
        limit = min(100, max(1, limit))
        sql = "SELECT * FROM runs WHERE (?='' OR job_id=?)"
        if active:
            sql += " AND state IN ('claimed','running')"
        with self.connect() as db:
            rows = db.execute(sql + " ORDER BY created_at DESC,id DESC" if active else
                              sql + " ORDER BY created_at DESC,id DESC LIMIT ?",
                              (job_id, job_id) if active else (job_id, job_id, limit))
            return [self.record(row) for row in rows]

    def run_lock(self, identity: str) -> InstanceLock:
        # IDs originate in this database, never arbitrary paths from a worker.
        self.run(identity)
        if len(identity) != 32 or any(char not in "0123456789abcdef" for char in identity):
            raise ValueError("Invalid occurrence ID.")
        return InstanceLock(self.root / "owners" / f"{identity}.lock")

    def run_dir(self, identity: str) -> Path:
        self.run_lock(identity)
        path = self.root / "runs" / identity
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        return path

    def start(self, identity: str) -> bool:
        with self.transaction() as db:
            return db.execute("UPDATE runs SET state='running' WHERE id=? AND state='claimed'", (identity,)).rowcount == 1

    def finish(self, identity: str, state: str, *, output: str = "", detail: str = "") -> bool:
        if state not in {"completed", "failed", "unknown"}:
            raise ValueError("Invalid occurrence outcome.")
        with self.transaction() as db:
            changed = db.execute("UPDATE runs SET state=?,finished_at=?,output=?,detail=? "
                                 "WHERE id=? AND state IN ('claimed','running')",
                                 (state, time.time(), output[:MAX_OUTPUT], detail[:1000], identity)).rowcount
            if changed:
                db.execute("INSERT INTO deliveries (run_id,state) VALUES (?,'pending')", (identity,))
        return bool(changed)

    def deliver_pending(self) -> int:
        """The only first-version transport: idempotent, transaction-owned local inbox."""
        with self.transaction() as db:
            pending = db.execute("SELECT run_id FROM deliveries WHERE state='pending'").fetchall()
            now = time.time()
            for row in pending:
                db.execute("INSERT OR IGNORE INTO inbox (run_id,received_at) VALUES (?,?)", (row["run_id"], now))
                db.execute("UPDATE deliveries SET state='delivered',delivered_at=? WHERE run_id=?", (now, row["run_id"]))
        return len(pending)

    def inbox(self, *, limit: int = 50) -> list[dict]:
        with self.connect() as db:
            return [self.record(row) for row in db.execute(
                "SELECT runs.*,inbox.received_at,inbox.read_at,deliveries.state AS delivery_state,deliveries.delivered_at "
                "FROM inbox JOIN runs ON runs.id=inbox.run_id JOIN deliveries ON deliveries.run_id=runs.id "
                "ORDER BY inbox.received_at DESC,runs.id DESC LIMIT ?", (min(100, max(1, limit)),))]

    def mark_read(self, identity: str) -> None:
        with self.transaction() as db:
            if db.execute("UPDATE inbox SET read_at=? WHERE run_id=?", (time.time(), identity)).rowcount != 1:
                raise ValueError("Unknown inbox occurrence ID.")
