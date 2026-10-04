"""A single-home ticker supervising independently owned, bounded worker processes."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from agent.runtime.instance_lock import InstanceAlreadyRunning, InstanceLock
from agent.runtime.process_env import hidden_process_creationflags

from .store import JobStore


def recover_abandoned(store: JobStore) -> int:
    recovered = 0
    for run in store.history(active=True, limit=100):
        lock = store.run_lock(run["id"])
        try:
            lock.acquire()
        except InstanceAlreadyRunning:
            continue
        try:
            recovered += store.finish(run["id"], "unknown", detail=(
                "Execution owner disappeared before a durable result. Inspect the session; this occurrence was not retried."
            ))
        finally:
            lock.release()
    return recovered


def worker_environment(store: JobStore, run: dict) -> dict[str, str]:
    env = os.environ.copy()
    root = store.run_dir(run["id"])
    env.update(ASTRA_HOME=str(store.home), ASTRA_WORKSPACE=run["spec"]["workdir"],
               SANDBOX_WORKDIR=run["spec"]["workdir"], AGENT_SESSION_DIR=str(root),
               TOOL_RESULT_DIR=str(root / "tool-results"), PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1")
    # Pin imports to this checkout/generation, rather than the scheduled workspace.
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[3])
    return env


def _stop_worker(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def execute(store: JobStore, run: dict) -> None:
    identity = run["id"]
    if run["spec"]["kind"] == "reminder":
        with store.run_lock(identity):
            if store.start(identity):
                store.finish(identity, "completed", output=run["spec"]["prompt"])
        return
    progress = store.run_dir(identity) / "progress"
    try:
        process = subprocess.Popen(
            [sys.executable, "-m", "agent.cli.jobs_worker", "--home", str(store.home), "--run", identity],
            cwd=Path(__file__).resolve().parents[3], env=worker_environment(store, run),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=hidden_process_creationflags(),
        )
    except OSError:
        store.finish(identity, "failed", detail="The scheduled worker could not be started.")
        return
    started = last_activity = time.monotonic()
    last_stamp = None
    budget_exhausted = ""
    try:
        while process.poll() is None:
            now = time.monotonic()
            try:
                stamp = progress.stat().st_mtime_ns
            except FileNotFoundError:
                stamp = None
            if stamp is not None and stamp != last_stamp:
                last_activity, last_stamp = now, stamp
            if now - started >= run["spec"]["timeout_seconds"]:
                budget_exhausted = "hard run limit"
                break
            if now - last_activity >= run["spec"]["idle_seconds"]:
                budget_exhausted = "inactivity limit"
                break
            time.sleep(0.05)
    finally:
        # Terminate and observe child exit before releasing installation ownership.
        _stop_worker(process)
        current = store.run(identity)
        if current["state"] in {"claimed", "running"}:
            store.finish(identity, "unknown" if current["state"] == "running" else "failed", detail=(
                f"Worker stopped without a durable result ({budget_exhausted or 'worker exited'}). "
                "This occurrence was not retried."
            ))


def tick(store: JobStore, *, manual_job: str = "", now: float | None = None, max_runs: int = 10) -> dict:
    try:
        with InstanceLock(store.root / "scheduler.lock"):
            recovered = recover_abandoned(store)
            delivered = store.deliver_pending()
            fired = []
            for _ in range(1 if manual_job else min(100, max(1, max_runs))):
                run = store.claim_due(now=now, manual_job=manual_job)
                if run is None:
                    break
                execute(store, run)
                fired.append({"id": run["id"], "job_id": run["job_id"], "state": store.run(run["id"])["state"]})
                delivered += store.deliver_pending()
            return {"scheduler": "idle", "executed": fired, "recovered_unknown": recovered, "delivered": delivered}
    except InstanceAlreadyRunning:
        return {"scheduler": "busy", "executed": [], "recovered_unknown": 0, "delivered": 0}
