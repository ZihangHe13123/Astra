"""Atomic schedules, immutable occurrence outcomes and a local delivery outbox."""

from __future__ import annotations

import json
import math
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from agent.runtime.instance_lock import InstanceLock
from agent.runtime.jobs.calendar import calendar_schedule, next_slot

MAX_OUTPUT = 64_000
SCHEMA_VERSION = 2
NOTIFY_POLICIES = frozenset({"always", "on_change"})
# A check under ``on_change`` answers exactly this when it has nothing new to report.
NO_UPDATE = "NO_UPDATE"
FAILURES_BEFORE_PAUSE = 3
INACTIVE_SECONDS = 7 * 86400
INTERACTION_FILE = "last-interaction"
# Columns added after version 1, with their definitions.
_V2_COLUMNS = {
    "jobs": (("paused_reason", "TEXT NOT NULL DEFAULT ''"), ("active_since", "REAL NOT NULL DEFAULT 0"),
             ("run_requested_at", "REAL")),
    "runs": (("quiet", "INTEGER NOT NULL DEFAULT 0"), ("usage", "TEXT NOT NULL DEFAULT ''")),
}


def note_interaction(home: Path, *, now: float | None = None) -> None:
    """Record that the person just talked to Astra; recurring jobs pause after long silence."""
    root = Path(home).expanduser() / "jobs"
    try:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = root / f".{INTERACTION_FILE}.{uuid.uuid4().hex}"
        temporary.write_text(f"{time.time() if now is None else now:.3f}\n", encoding="utf-8")
        os.replace(temporary, root / INTERACTION_FILE)
    except OSError:
        pass


def last_interaction(home: Path) -> float | None:
    """When the person last talked to Astra, or None when it was never recorded."""
    try:
        value = float((Path(home).expanduser() / "jobs" / INTERACTION_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if math.isfinite(value) else None


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
            if version not in {0, 1, SCHEMA_VERSION}:
                raise ValueError("Unsupported jobs database version; preserve it for inspection.")
            if version == SCHEMA_VERSION:
                return
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
            """)
            # Version 2 only adds columns, in one transaction, so version 1
            # rows keep their meaning and a concurrent opener sees all or none.
            db.execute("BEGIN IMMEDIATE")
            try:
                if db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                    for table, columns in _V2_COLUMNS.items():
                        present = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
                        for column, definition in columns:
                            if column not in present:
                                db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
                    db.execute("UPDATE jobs SET active_since=created_at WHERE active_since=0")
                    db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            except BaseException:
                db.rollback()
                raise
            db.commit()

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
        if "usage" in result:
            result["usage"] = json.loads(result["usage"]) if result["usage"] else {}
        return result

    def add(self, *, name: str, prompt: str, original_request: str,
            workdir: Path, next_at: float | None = None, interval_seconds: float = 0,
            calendar: dict | None = None, notify: str = "always",
            grace_seconds: float = 300, kind: str = "check", model: str = "",
            web: bool = False, idle_seconds: float = 120, timeout_seconds: float = 900,
            max_iterations: int = 10, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        calendar, next_at = self._schedule(calendar, interval_seconds, next_at, now)
        if notify not in NOTIFY_POLICIES:
            raise ValueError("Notify must be 'always' or 'on_change'.")
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
                "max_iterations": max_iterations, "notify": notify}
        if calendar:
            spec["calendar"] = calendar
        identity = uuid.uuid4().hex
        with self.transaction() as db:
            db.execute("INSERT INTO jobs (id,name,spec,state,next_at,interval_seconds,grace_seconds,created_at,"
                       "active_since) VALUES (?,?,?,'enabled',?,?,?,?,?)",
                       (identity, spec["name"], json.dumps(spec), next_at, interval_seconds, grace_seconds, now, now))
        return self.job(identity)

    @staticmethod
    def _schedule(calendar: dict | None, interval_seconds: float, next_at: float | None,
                  now: float) -> tuple[dict | None, float]:
        """Validate one schedule: an interval, a wall-clock calendar, or a single instant."""
        finite(interval_seconds, 0, 365 * 86400, "Interval")
        if 0 < interval_seconds < 60:
            raise ValueError("Interval must be at least 60 seconds.")
        if calendar:
            if interval_seconds:
                raise ValueError("Choose a fixed interval or a time of day, not both.")
            calendar = calendar_schedule(calendar.get("time", ""), calendar.get("days"), calendar.get("tz", ""))
            if next_at is None:
                next_at = next_slot(calendar, now)
        if next_at is None:
            raise ValueError("A job needs a first run time.")
        finite(next_at, 0, 253402300799, "First run timestamp")
        return calendar or None, next_at

    def jobs(self) -> list[dict]:
        with self.connect() as db:
            return [self.record(row) for row in db.execute(
                "SELECT jobs.*, (SELECT state FROM runs WHERE job_id=jobs.id "
                "ORDER BY created_at DESC,rowid DESC LIMIT 1) AS last_outcome FROM jobs ORDER BY created_at,id")]

    def job(self, identity: str) -> dict:
        with self.connect() as db:
            row = db.execute(
                "SELECT jobs.*, (SELECT state FROM runs WHERE job_id=jobs.id "
                "ORDER BY created_at DESC,rowid DESC LIMIT 1) AS last_outcome FROM jobs WHERE id=?",
                (identity,)).fetchone()
        if row is None:
            raise ValueError("Unknown job ID.")
        return self.record(row)

    def set_state(self, identity: str, state: str, *, now: float | None = None) -> dict:
        if state not in {"enabled", "paused", "removed"}:
            raise ValueError("Invalid job state.")
        now = time.time() if now is None else now
        with self.transaction() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (identity,)).fetchone()
            if row is None:
                raise ValueError("Unknown job ID.")
            if row["state"] == "removed" or (state == "enabled" and row["state"] == "completed"):
                raise ValueError("A removed or completed one-shot cannot be resumed; create a new job.")
            next_at = row["next_at"]
            calendar = json.loads(row["spec"]).get("calendar")
            if state == "enabled" and next_at < now and (row["interval_seconds"] or calendar):
                # Slots that passed while paused were declined, not missed.
                next_at = self._slots(next_at, row["interval_seconds"], calendar, now)[2]
            db.execute("UPDATE jobs SET state=?,paused_reason='',next_at=?,active_since=? WHERE id=?",
                       (state, next_at, now if state == "enabled" else row["active_since"], identity))
        return self.job(identity)

    def update(self, identity: str, *, request: str, name: str | None = None, prompt: str | None = None,
               interval_seconds: float | None = None, calendar: dict | None = None, at: float | None = None,
               notify: str | None = None, web: bool | None = None, now: float | None = None) -> dict:
        """Change a job's future runs; ``request`` is the person's words asking for the change."""
        now = time.time() if now is None else now
        if not isinstance(request, str) or not request.strip():
            raise ValueError("A change needs the person's request.")
        with self.transaction() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (identity,)).fetchone()
            if row is None:
                raise ValueError("Unknown job ID.")
            if row["state"] in {"removed", "completed"}:
                raise ValueError("A removed or completed job cannot be changed; create a new job.")
            spec = json.loads(row["spec"])
            for key, value, limit in (("name", name, 120), ("prompt", prompt, 4000)):
                if value is not None:
                    if not isinstance(value, str) or not value.strip() or len(value) > limit:
                        raise ValueError(f"The {key} must be non-empty and at most {limit} characters.")
                    spec[key] = value.strip()
            if notify is not None:
                if notify not in NOTIFY_POLICIES:
                    raise ValueError("Notify must be 'always' or 'on_change'.")
                spec["notify"] = notify
            if web is not None:
                if type(web) is not bool or (web and spec["kind"] == "reminder"):
                    raise ValueError("A reminder does not use network tools.")
                spec["web"] = web
            interval, next_at = row["interval_seconds"], row["next_at"]
            if interval_seconds is not None or calendar is not None or at is not None:
                interval = interval_seconds or 0
                first = at if at is not None else None if calendar else now + interval if interval else None
                found, next_at = self._schedule(calendar, interval, first, now)
                spec.pop("calendar", None)
                if found:
                    spec["calendar"] = found
            changed = time.strftime("%Y-%m-%d %H:%M", time.localtime(now))
            spec["original_request"] = (f"{spec['original_request']}\n\nChanged {changed}: {request.strip()}")[-8000:]
            db.execute("UPDATE jobs SET name=?,spec=?,next_at=?,interval_seconds=? WHERE id=?",
                       (spec["name"], json.dumps(spec), next_at, interval, identity))
        return self.job(identity)

    def request_run(self, identity: str, *, now: float | None = None) -> dict:
        """Ask the scheduler for one extra occurrence now, outside the schedule."""
        with self.transaction() as db:
            if not db.execute("UPDATE jobs SET run_requested_at=? WHERE id=? AND state!='removed'",
                              (time.time() if now is None else now, identity)).rowcount:
                raise ValueError("Unknown or removed job ID.")
        return self.job(identity)

    def last_reported_output(self, job_id: str, *, before: str) -> str:
        """The latest result the person was actually told, for an ``on_change`` comparison."""
        with self.connect() as db:
            row = db.execute("SELECT output FROM runs WHERE job_id=? AND id!=? AND state='completed' AND quiet=0 "
                             "ORDER BY finished_at DESC,rowid DESC LIMIT 1", (job_id, before)).fetchone()
        return row["output"] if row is not None else ""

    @staticmethod
    def _slots(first: float, period: float, calendar: dict | None, now: float) -> tuple[int, float, float | None]:
        """Slots from ``first`` up to ``now``: how many passed before the latest, the latest, and the next."""
        if calendar:
            older, latest, following = 0, first, next_slot(calendar, first)
            while following <= now:
                older, latest, following = older + 1, following, next_slot(calendar, following)
            return older, latest, following
        if period:
            older = max(0, int((now - first) // period))
            return older, first + older * period, first + (older + 1) * period
        return 0, first, None

    @staticmethod
    def _insert_run(db: sqlite3.Connection, job: sqlite3.Row, slot: float | None,
                    now: float, *, skipped: int = 0, notice: str = "") -> str:
        identity = uuid.uuid4().hex
        state = "skipped" if skipped or notice else "claimed"
        detail = notice or (f"Missed {skipped} slot(s) outside the catch-up policy." if skipped else "")
        db.execute("INSERT INTO runs (id,job_id,scheduled_at,created_at,finished_at,state,spec,detail,skipped_count) "
                   "VALUES (?,?,?,?,?,?,?,?,?)", (identity, job["id"], slot, now, now if state == "skipped" else None,
                                                state, job["spec"], detail, skipped))
        if state == "skipped":
            db.execute("INSERT INTO deliveries (run_id,state) VALUES (?,'pending')", (identity,))
        return identity

    def claim_due(self, *, now: float | None = None, manual_job: str = "",
                  reminders_only: bool = False, last_interaction: float | None = None) -> dict | None:
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
                identity = (self._claim_requested(db, now, reminders_only)
                            or self._claim_scheduled(db, now, reminders_only, last_interaction))
        return self.run(identity) if identity else None

    def _claim_requested(self, db: sqlite3.Connection, now: float, reminders_only: bool) -> str:
        for job in db.execute("SELECT * FROM jobs WHERE run_requested_at IS NOT NULL AND state!='removed' "
                              "AND NOT EXISTS (SELECT 1 FROM runs WHERE runs.job_id=jobs.id "
                              "AND runs.state IN ('claimed','running')) ORDER BY run_requested_at,id").fetchall():
            if reminders_only and json.loads(job["spec"])["kind"] != "reminder":
                continue
            db.execute("UPDATE jobs SET run_requested_at=NULL WHERE id=?", (job["id"],))
            return self._insert_run(db, job, None, now)
        return ""

    def _claim_scheduled(self, db: sqlite3.Connection, now: float, reminders_only: bool,
                         last_interaction: float | None) -> str:
        candidates = db.execute("SELECT * FROM jobs WHERE state='enabled' AND next_at<=? "
                                "AND NOT EXISTS (SELECT 1 FROM runs WHERE runs.job_id=jobs.id "
                                "AND runs.state IN ('claimed','running')) ORDER BY next_at,id", (now,)).fetchall()
        for job in candidates:
            spec = json.loads(job["spec"])
            # The supervisor can service lightweight reminders while a
            # check owns the worker. Do not advance other checks here.
            if reminders_only and spec["kind"] != "reminder":
                continue
            period = job["interval_seconds"]
            calendar = spec.get("calendar")
            if (period or calendar) and last_interaction is not None and (
                    now - max(last_interaction, job["active_since"]) > INACTIVE_SECONDS):
                # Nobody has talked to Astra for a week: stop spending on a
                # schedule that may no longer matter, and say so once.
                db.execute("UPDATE jobs SET state='paused',paused_reason='inactive' WHERE id=?", (job["id"],))
                self._insert_run(db, job, None, now, notice=(
                    "Paused because nobody talked to Astra for 7 days. Resume the job to continue."))
                continue
            older, latest, following = self._slots(job["next_at"], period, calendar, now)
            eligible = now - latest <= job["grace_seconds"]
            missed = older if eligible else older + 1
            if missed:
                self._insert_run(db, job, job["next_at"], now, skipped=missed)
            if following is not None:
                db.execute("UPDATE jobs SET next_at=? WHERE id=?", (following, job["id"]))
            else:
                db.execute("UPDATE jobs SET state='completed' WHERE id=?", (job["id"],))
            if eligible:
                return self._insert_run(db, job, latest, now)
        return ""

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

    def finish(self, identity: str, state: str, *, output: str = "", detail: str = "",
               quiet: bool = False, usage: dict | None = None) -> bool:
        if state not in {"completed", "failed", "unknown"}:
            raise ValueError("Invalid occurrence outcome.")
        with self.transaction() as db:
            changed = db.execute("UPDATE runs SET state=?,finished_at=?,output=?,detail=?,quiet=?,usage=? "
                                 "WHERE id=? AND state IN ('claimed','running')",
                                 (state, time.time(), output[:MAX_OUTPUT], detail[:1000],
                                  int(quiet and state == "completed"), json.dumps(usage) if usage else "",
                                  identity)).rowcount
            if changed:
                if state != "completed":
                    self._pause_after_failures(db, identity)
                db.execute("INSERT INTO deliveries (run_id,state) VALUES (?,'pending')", (identity,))
        return bool(changed)

    @staticmethod
    def _pause_after_failures(db: sqlite3.Connection, run_id: str) -> None:
        """Stop a recurring job after several failed runs in a row, and say so on the last one."""
        job_id = db.execute("SELECT job_id FROM runs WHERE id=?", (run_id,)).fetchone()["job_id"]
        recent = [row["state"] for row in db.execute(
            "SELECT state FROM runs WHERE job_id=? AND state!='skipped' ORDER BY created_at DESC,rowid DESC LIMIT ?",
            (job_id, FAILURES_BEFORE_PAUSE))]
        if len(recent) < FAILURES_BEFORE_PAUSE or any(state not in {"failed", "unknown"} for state in recent):
            return
        if db.execute("UPDATE jobs SET state='paused',paused_reason='failures' WHERE id=? AND state='enabled' "
                      "AND (interval_seconds>0 OR json_extract(spec,'$.calendar') IS NOT NULL)", (job_id,)).rowcount:
            db.execute("UPDATE runs SET detail=substr(? || detail,1,1000) WHERE id=?", (
                f"Paused after {FAILURES_BEFORE_PAUSE} failed runs in a row; resume it once the cause is fixed. ",
                run_id))

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
