from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import sys
import threading

from agent.cli.jobs_commands import execute_jobs_command
from agent.runtime.jobs.scheduler import tick
from agent.runtime.jobs.store import JobStore
from agent.runtime.jobs.triggers import TriggerWatcher


def test_pause_then_resume_cannot_rerun_completed_one_shot(tmp_path):
    import time
    home = tmp_path / "home"
    store = JobStore(home)
    now = time.time()
    job = store.add(name="one shot", prompt="show once", original_request="remind once",
                    workdir=tmp_path, kind="reminder", next_at=now)
    assert len(tick(store, now=now)["executed"]) == 1
    assert store.job(job["id"])["state"] == "completed"
    transitions = [execute_jobs_command([verb, job["id"]], home=home)[1] for verb in ("pause", "resume")]
    later = store.add(name="unrelated", prompt="also due", original_request="a second reminder",
                      workdir=tmp_path, kind="reminder", next_at=now + 1)
    errors = []
    for moment in (now + 1, now + 2, now + 3601):
        try:
            tick(store, now=moment)
        except Exception as error:
            errors.append(f"{type(error).__name__}: {error}")
    assert store.history(later["id"]), (transitions, store.job(job["id"])["state"], errors)


def test_event_job_with_three_consecutive_failed_runs_auto_pauses(tmp_path):
    directory = tmp_path / "files"
    directory.mkdir()
    store = JobStore(tmp_path / "home")
    job = store.add(name="incoming PDFs", prompt="inspect", original_request="inspect incoming PDFs",
                    workdir=directory, kind="check", trigger={"type": "file", "pattern": "*.pdf"})
    watcher = TriggerWatcher(store)
    assert watcher.look() == 0
    for index in range(3):
        (directory / f"{index}.pdf").touch()
        assert watcher.look() == 1
        run = store.claim_due()
        assert run["job_id"] == job["id"]
        assert store.finish(run["id"], "failed", detail="model unavailable")
    assert store.job(job["id"])["state"] == "paused"


def test_scheduler_repairs_an_existing_reactivated_row(tmp_path):
    store = JobStore(tmp_path)
    args = dict(prompt="once", original_request="once", workdir=tmp_path, kind="reminder")
    first = store.add(name="first", next_at=1000, **args)
    assert len(tick(store, now=1000)["executed"]) == 1
    with store.transaction() as db:
        db.execute("UPDATE jobs SET state='enabled' WHERE id=?", (first["id"],))
    second = store.add(name="second", next_at=1001, **args)
    tick(store, now=1001)
    assert store.job(first["id"])["state"] == "completed"
    assert len(store.history(first["id"])) == len(store.history(second["id"])) == 1


def test_standalone_tick_detects_new_file_after_baseline(tmp_path):
    directory = tmp_path / "watched"
    directory.mkdir()
    store = JobStore(tmp_path / "home")
    job = store.add(name="new", prompt="arrival", original_request="file reminder", workdir=directory,
                    kind="reminder", trigger={"type": "file", "pattern": "*.pdf"})
    for n in range(2):
        if n:
            (directory / "new.pdf").touch()
        _, error = execute_jobs_command(["tick"], home=store.home, allow_execution=True)
        assert not error
    assert len(store.history(job["id"])) == 1


def test_cli_tick_and_background_watcher_deliver_each_file_once(tmp_path, monkeypatch):
    folder = tmp_path / "watched"
    folder.mkdir()
    store = JobStore(tmp_path / "home")
    job = store.add(name="new file", prompt="arrival", original_request="file reminder",
                    workdir=folder, kind="reminder", trigger={"type": "file", "pattern": "*.pdf"})
    watcher = TriggerWatcher(store)
    assert watcher.look() == 0
    (folder / "first.pdf").touch()
    read, resume = threading.Event(), threading.Event()
    original = store.trigger_cursor

    def held_cursor(job_id):
        cursor = original(job_id)
        read.set()
        assert resume.wait(30)
        return cursor

    monkeypatch.setattr(store, "trigger_cursor", held_cursor)
    with ThreadPoolExecutor(max_workers=1) as worker:
        scan = worker.submit(watcher.look)
        try:
            assert read.wait(30)
            subprocess.run([sys.executable, "-c",
                "import sys; from pathlib import Path; from agent.cli.jobs_commands import execute_jobs_command; "
                "_, error = execute_jobs_command(['tick'], home=Path(sys.argv[1]), allow_execution=True); "
                "assert not error, error", str(store.home)], cwd=Path(__file__).resolve().parents[1],
                check=True, timeout=20)
        finally:
            resume.set()
        assert scan.result() == 1
    (folder / "second.pdf").touch()
    _, error = execute_jobs_command(["tick"], home=store.home, allow_execution=True)
    assert not error
    assert sorted(run["spec"]["event"]["name"] for run in store.history(job["id"])) == ["first.pdf", "second.pdf"]
