"""A single-home ticker supervising independently owned, bounded worker processes."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from agent.runtime.instance_lock import InstanceAlreadyRunning, InstanceLock
from agent.runtime.process_env import hidden_process_creationflags
from agent.sandbox.windows_job import WindowsJob

from .store import JobStore, last_interaction


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


_WINDOWS_WORKER_GATE = (
    "import runpy,sys; "
    "token=sys.stdin.buffer.read(1); "
    "sys.stdin.close(); "
    "sys.exit(1) if token != b'1' else None; "
    "runpy.run_module('agent.cli.jobs_worker', run_name='__main__')"
)


class WorkerCleanupUnconfirmed(RuntimeError):
    """Startup failed, but the spawned execution has not been proved stopped."""


def _stop_worker(process: subprocess.Popen, job: WindowsJob | None = None, *,
                 store: JobStore | None = None, identity: str = "") -> None:
    """Stop only this spawned worker's group/job, including a live wrapper's children."""
    if sys.platform == "win32":
        # Job ownership survives the root process exiting; taskkill /T does not.
        # Without a job this is a failed startup still waiting at the input gate.
        try:
            if job is not None:
                job.close()
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
        return
    # Every POSIX worker is launched in its own session. Signal the saved group
    # even if its wrapper already exited, since the actual owner can remain.
    if process.pid == os.getpgrp():
        raise OSError("Scheduled worker does not own a separate process group.")
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 5
    try:
        process.wait(timeout=max(0, deadline - time.monotonic()))
        if store is not None:
            # The wrapper can die first. Give the actual run owner its remaining
            # grace period to close detached provider processes in finally.
            _wait_for_owner_exit(store, identity, deadline=deadline)
    except (OSError, subprocess.TimeoutExpired):
        pass
    finally:
        # A wrapper can exit on TERM before its children do. This also stops
        # same-group children that ignored the graceful cancellation request.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait(timeout=5)


def _spawn_worker(store: JobStore, run: dict) -> tuple[subprocess.Popen, WindowsJob | None]:
    windows = sys.platform == "win32"
    env = worker_environment(store, run)
    executable = sys.executable
    if windows:
        # Match CPython multiprocessing: bypass the Windows venv redirector so
        # the gated interpreter itself is assigned to the job before it can fork.
        base = getattr(sys, "_base_executable", executable) or executable
        if os.path.normcase(base) != os.path.normcase(executable):
            env["__PYVENV_LAUNCHER__"] = executable
            executable = base
    arguments = [executable, "-c", _WINDOWS_WORKER_GATE] if windows else [executable, "-m", "agent.cli.jobs_worker"]
    arguments += ["--home", str(store.home), "--run", run["id"]]
    process = subprocess.Popen(
        arguments, cwd=Path(__file__).resolve().parents[3], env=env,
        stdin=subprocess.PIPE if windows else subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=hidden_process_creationflags(new_process_group=windows),
        start_new_session=not windows,
    )
    job = None
    if windows:
        try:
            handle = getattr(process, "_handle", None)
            if handle is None or process.stdin is None:
                raise OSError("Scheduled worker has no controllable process handle.")
            job = WindowsJob.create(process_handle=int(handle), memory_mb=None,
                                    cpu_seconds=None, max_processes=64)
            process.stdin.write(b"1")
            process.stdin.close()
        except BaseException:
            try:
                if process.stdin is not None:
                    process.stdin.close()
            except BaseException:
                pass  # Preserve the startup error; process cleanup is decisive.
            try:
                _stop_worker(process, job)
                _wait_for_owner_exit(store, run["id"])
            except BaseException as cleanup_error:
                # Do not let a cleanup OSError look like a safely failed launch.
                # The admitted interpreter may not have acquired its lease yet.
                raise WorkerCleanupUnconfirmed(
                    "Scheduled worker startup failed and cleanup could not be confirmed."
                ) from cleanup_error
            raise
    return process, job


def _wait_for_owner_exit(store: JobStore, identity: str, *, deadline: float | None = None) -> None:
    """Observe termination through the kernel lease before declaring cleanup done."""
    deadline = time.monotonic() + 5 if deadline is None else deadline
    while True:
        lock = store.run_lock(identity)
        try:
            lock.acquire()
        except InstanceAlreadyRunning:
            if time.monotonic() >= deadline:
                raise OSError("Scheduled execution owner is still running after cleanup.")
            time.sleep(0.05)
        else:
            lock.release()
            return


def event_line(event: dict | None) -> str:
    """What started an event-started run, as the person reads it."""
    if not event:
        return ""
    if event.get("type") == "mail":
        return f"新邮件：{event.get('from', '')} — {event.get('subject', '')}"
    return f"新文件：{event.get('name') or event.get('path', '')}"


def execute(store: JobStore, run: dict, *, service_reminders: Callable[[], None] | None = None) -> None:
    identity = run["id"]
    if run["spec"]["kind"] == "reminder":
        with store.run_lock(identity):
            if store.start(identity):
                store.finish(identity, "completed", output="\n".join(filter(None, (
                    run["spec"]["prompt"], event_line(run["spec"].get("event"))))))
        return
    progress = store.run_dir(identity) / "progress"
    try:
        process, job = _spawn_worker(store, run)
    except OSError:
        # A safely cleaned startup can still have admitted a worker before a
        # pipe error. Serialize with its start and preserve unknown outcomes.
        with store.run_lock(identity):
            current = store.run(identity)
            store.finish(identity, "unknown" if current["state"] == "running" else "failed",
                         detail="The scheduled worker could not be started or confirmed. This occurrence was not retried.")
        return
    started = last_activity = time.monotonic()
    next_reminders = started
    last_stamp = None
    budget_exhausted = ""
    try:
        while process.poll() is None:
            now = time.monotonic()
            if service_reminders is not None and now >= next_reminders:
                service_reminders()
                next_reminders = time.monotonic() + 0.25
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
        _stop_worker(process, job, store=store, identity=identity)
        # A failed cleanup must keep the active occurrence fence. Recovery can
        # mark it unknown only after its kernel owner has actually disappeared.
        _wait_for_owner_exit(store, identity)
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
            interaction = last_interaction(store.home)
            fired = []

            def service_reminders() -> None:
                nonlocal delivered
                # Retain the single scheduler owner and per-job occurrence
                # locks. Only model-free reminders may run beside a check.
                for _ in range(100):
                    notice = store.claim_due(now=now, reminders_only=True, last_interaction=interaction)
                    if notice is None:
                        break
                    execute(store, notice)
                    fired.append({"id": notice["id"], "job_id": notice["job_id"],
                                  "state": store.run(notice["id"])["state"]})
                delivered += store.deliver_pending()

            for _ in range(1 if manual_job else min(100, max(1, max_runs))):
                run = store.claim_due(now=now, manual_job=manual_job, last_interaction=interaction)
                if run is None:
                    break
                execute(store, run, service_reminders=service_reminders)
                fired.append({"id": run["id"], "job_id": run["job_id"], "state": store.run(run["id"])["state"]})
                delivered += store.deliver_pending()
            # claim_due can record only missed slots and return no runnable
            # occurrence; those outcomes still need delivery in this tick.
            delivered += store.deliver_pending()
            return {"scheduler": "idle", "executed": fired, "recovered_unknown": recovered, "delivered": delivered}
    except InstanceAlreadyRunning:
        return {"scheduler": "busy", "executed": [], "recovered_unknown": 0, "delivered": 0}
