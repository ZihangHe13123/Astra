"""Behavioral contracts for durable scheduling, ownership and local delivery."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agent.cli.jobs_commands import execute_jobs_command
from agent.runtime.instance_lock import InstanceLock
from agent.runtime.jobs.scheduler import recover_abandoned, tick, worker_environment
from agent.runtime.jobs.store import JobStore

ROOT = Path(__file__).resolve().parents[1]


def add(store, tmp_path, **kwargs):
    return store.add(name="fixture", prompt="Remember the fixture", original_request="Remind me", kind="reminder",
                     workdir=tmp_path, next_at=1000, now=900, **kwargs)


def test_schedule_and_occurrence_commit_together_before_dispatch(tmp_path):
    store = JobStore(tmp_path / "home")
    job = add(store, tmp_path, interval_seconds=60)
    run = store.claim_due(now=1000)
    reopened = JobStore(store.home)
    assert reopened.job(job["id"])["next_at"] == 1060
    assert reopened.run(run["id"])["state"] == "claimed"
    assert reopened.claim_due(now=1000) is None
    assert recover_abandoned(reopened) == 1
    assert reopened.run(run["id"])["state"] == "unknown"
    assert reopened.claim_due(now=1000) is None
    assert reopened.claim_due(now=1060)["scheduled_at"] == 1060


def test_concurrent_claimants_get_exactly_one_occurrence(tmp_path):
    store = JobStore(tmp_path / "home")
    add(store, tmp_path)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: store.claim_due(now=1000), range(8)))
    assert sum(run is not None for run in results) == 1
    assert len(store.history()) == 1


def test_catch_up_records_range_and_only_runs_latest_eligible_slot(tmp_path):
    store = JobStore(tmp_path / "home")
    job = add(store, tmp_path, interval_seconds=60, grace_seconds=10)
    run = store.claim_due(now=1603)
    history = store.history(job["id"])
    missed = next(row for row in history if row["state"] == "skipped")
    assert missed["scheduled_at"] == 1000 and missed["skipped_count"] == 10
    assert run["scheduled_at"] == 1600
    assert store.job(job["id"])["next_at"] == 1660
    assert store.claim_due(now=1603) is None


def test_missed_slots_and_one_shot_are_accounted_for_without_burst(tmp_path):
    store = JobStore(tmp_path / "home")
    repeating = add(store, tmp_path, interval_seconds=60, grace_seconds=10)
    once = add(store, tmp_path, grace_seconds=10)
    assert store.claim_due(now=1630) is None
    assert store.history(repeating["id"])[0]["skipped_count"] == 11
    assert store.history(once["id"])[0]["skipped_count"] == 1
    assert store.job(once["id"])["state"] == "completed"
    assert store.job(repeating["id"])["next_at"] == 1660
    assert store.claim_due(now=1630) is None


def test_local_outbox_recovery_and_terminal_results_are_idempotent(tmp_path):
    store = JobStore(tmp_path / "home")
    add(store, tmp_path)
    run = store.claim_due(now=1000)
    assert store.start(run["id"])
    assert not store.start(run["id"])
    assert store.finish(run["id"], "completed", output="ONE_RESULT")
    assert not store.finish(run["id"], "unknown", output="OVERWRITE")
    # Simulate death after the result transaction and before inbox delivery.
    reopened = JobStore(store.home)
    assert reopened.deliver_pending() == 1
    assert reopened.deliver_pending() == 0
    inbox = reopened.inbox()
    assert len(inbox) == 1 and inbox[0]["output"] == "ONE_RESULT"
    assert inbox[0]["state"] == "completed" and inbox[0]["delivery_state"] == "delivered"
    reopened.mark_read(run["id"])
    assert reopened.inbox()[0]["read_at"] is not None


def test_home_isolation_a_to_b_to_a_and_closed_database_handles(tmp_path):
    homes = [tmp_path / "A", tmp_path / "B"]
    first = JobStore(homes[0])
    second = JobStore(homes[1])
    job = add(first, tmp_path)
    assert second.jobs() == [] and second.inbox() == []
    tick(first, now=1000)
    assert JobStore(homes[0]).inbox()[0]["job_id"] == job["id"]
    assert JobStore(homes[1]).inbox() == []
    # In particular, Windows must not retain an open SQLite handle after reads.
    for store in (first, second):
        store.path.unlink()


def test_pause_remove_manual_occurrence_and_original_schedule(tmp_path):
    store = JobStore(tmp_path / "home")
    job = add(store, tmp_path, interval_seconds=60)
    store.set_state(job["id"], "paused")
    assert store.claim_due(now=1000) is None
    manual = store.claim_due(now=1000, manual_job=job["id"])
    assert manual["scheduled_at"] is None
    with pytest.raises(ValueError, match="active"):
        store.claim_due(now=1000, manual_job=job["id"])
    store.finish(manual["id"], "completed", output="manual")
    assert store.job(job["id"])["next_at"] == 1000
    store.set_state(job["id"], "enabled")
    assert store.claim_due(now=1000)["scheduled_at"] == 1000
    store.set_state(job["id"], "removed")
    with pytest.raises(ValueError, match="removed"):
        store.set_state(job["id"], "enabled")
    assert len(store.history(job["id"])) == 2


def test_duplicate_ticker_and_live_owner_are_not_recovered(tmp_path):
    store = JobStore(tmp_path / "home")
    add(store, tmp_path)
    run = store.claim_due(now=1000)
    with store.run_lock(run["id"]):
        assert store.start(run["id"])
        assert recover_abandoned(store) == 0
    with InstanceLock(store.root / "scheduler.lock"):
        assert tick(store, now=1000)["scheduler"] == "busy"
    assert recover_abandoned(store) == 1
    assert not store.start(run["id"])  # delayed worker cannot adopt a fenced claim


def test_live_child_keeps_ownership_after_dispatcher_disappears(tmp_path):
    store = JobStore(tmp_path / "home")
    add(store, tmp_path)
    run = store.claim_due(now=1000)
    script = (
        "import sys; from pathlib import Path; from agent.runtime.jobs.store import JobStore; "
        "s=JobStore(Path(sys.argv[1])); lock=s.run_lock(sys.argv[2]); lock.acquire(); "
        "s.start(sys.argv[2]); print('READY',flush=True); sys.stdin.readline(); "
        "s.finish(sys.argv[2],'completed',output='child result'); lock.release()"
    )
    child = subprocess.Popen([sys.executable, "-c", script, str(store.home), run["id"]], cwd=ROOT,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "READY"
        assert recover_abandoned(JobStore(store.home)) == 0
        assert store.run(run["id"])["state"] == "running"
        child.stdin.write("finish\n")
        child.stdin.flush()
        assert child.wait(timeout=10) == 0
        assert recover_abandoned(JobStore(store.home)) == 0
        assert store.deliver_pending() == 1
        assert store.inbox()[0]["output"] == "child result"
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
        child.stdin.close()
        child.stdout.close()


@pytest.mark.parametrize("field,value", [("interval_seconds", 1), ("interval_seconds", float("nan")),
                                         ("grace_seconds", -1), ("idle_seconds", 0),
                                         ("timeout_seconds", 10), ("max_iterations", True)])
def test_invalid_budgets_do_not_create_jobs(tmp_path, field, value):
    store = JobStore(tmp_path / "home")
    with pytest.raises(ValueError):
        add(store, tmp_path, **{field: value})
    assert store.jobs() == []


def test_commands_preserve_process_and_do_not_execute_inside_a_chat(tmp_path):
    home = tmp_path / "home"
    output, error = execute_jobs_command(["add", "--help"], home=home)
    assert not error and "--request" in output
    assert not home.exists()
    output, error = execute_jobs_command(["add", "check", "--after", "60", "--prompt", "check"], home=home)
    assert "original user" in error
    output, error = execute_jobs_command(["serve"], home=home)
    assert "separate terminal" in error
    output, error = execute_jobs_command(["add", "notice", "--after", "60", "--reminder", "--prompt", "fixture"],
                                          home=home, workdir=tmp_path)
    assert not error
    job = json.loads(output)
    assert job["spec"]["workdir"] == str(tmp_path) and job["spec"]["model"] == ""
    output, error = execute_jobs_command(["add", "bad", "--at", "2026-10-05T12:00:00", "--reminder", "--prompt", "fixture"], home=home)
    assert "offset" in error and len(JobStore(home).jobs()) == 1


def test_worker_environment_pins_home_and_never_adopts_live_session(tmp_path, monkeypatch):
    store = JobStore(tmp_path / "home")
    add(store, tmp_path)
    run = store.claim_due(now=1000)
    monkeypatch.setenv("AGENT_SESSION_DIR", str(tmp_path / "live-sessions"))
    monkeypatch.setenv("TOOL_RESULT_DIR", str(tmp_path / "live-tools"))
    env = worker_environment(store, run)
    assert env["ASTRA_HOME"] == str(store.home)
    assert env["SANDBOX_WORKDIR"] == str(tmp_path)
    assert env["AGENT_SESSION_DIR"] == str(store.run_dir(run["id"]))
    assert Path(env["TOOL_RESULT_DIR"]).is_relative_to(store.run_dir(run["id"]))
    assert os.environ["AGENT_SESSION_DIR"] == str(tmp_path / "live-sessions")


def test_real_launcher_command_persists_reminder_across_processes(tmp_path):
    home = tmp_path / "home"
    env = {**os.environ, "ASTRA_HOME": str(home), "ASTRA_ENV_FILE": str(home / ".env")}
    def command(*args):
        process = subprocess.run([sys.executable, str(ROOT / "astra.py"), "jobs", *args], cwd=tmp_path,
                                 env=env, text=True, capture_output=True, timeout=30)
        assert process.returncode == 0, process.stderr
        return json.loads(process.stdout)

    job = command("add", "notice", "--at", "2000-01-01T00:00:00Z", "--reminder", "--prompt", "fixture")
    assert command("list")[0]["id"] == job["id"]
    # Explicit manual invocation is a fresh occurrence even when a one-shot is late.
    result = command("run", job["id"])
    assert result["executed"][0]["state"] == "completed"
    inbox = command("inbox")
    assert len(inbox) == 1 and inbox[0]["output"] == "fixture"
    assert inbox[0]["delivery_state"] == "delivered"
