"""Long checks must not block local reminders or hide missed deliveries."""
import json
import subprocess
import time
from types import SimpleNamespace

from agent.runtime.jobs import scheduler
from agent.runtime.jobs.store import JobStore


def test_reminder_is_delivered_while_a_real_check_process_is_waiting(tmp_path, monkeypatch):
    store = JobStore(tmp_path / "home")
    now = time.time()
    check = store.add(name="check", prompt="controlled check", original_request="check",
                      kind="check", model="fixture", workdir=tmp_path, next_at=now - 1,
                      idle_seconds=10, timeout_seconds=15)
    queued_check = store.add(name="queued", prompt="another check", original_request="check",
                             kind="check", model="fixture", workdir=tmp_path, next_at=now - 0.5)
    reminder = store.add(name="notice", prompt="reminder result", original_request="remind",
                         kind="reminder", workdir=tmp_path, next_at=now + 0.4, grace_seconds=1)
    script = """
import json, sys, time
if sys.platform == 'win32' and sys.stdin.buffer.read(1) != b'1': sys.exit(1)
from pathlib import Path
from agent.runtime.jobs.store import JobStore
store = JobStore(Path(sys.argv[1]))
with store.run_lock(sys.argv[2]):
    store.start(sys.argv[2])
    time.sleep(2)
    seen = any(item['job_id'] == sys.argv[3] for item in store.inbox())
    store.finish(sys.argv[2], 'completed', output=json.dumps({'reminder_seen_before_finish': seen}))
"""
    original_popen = subprocess.Popen
    children = []
    def spawn(args, **kwargs):
        identity = args[args.index("--run") + 1]
        child = original_popen([args[0], "-c", script, str(store.home), identity, reminder["id"]], **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(scheduler.subprocess, "Popen", spawn)
    result = scheduler.tick(store, max_runs=1)
    assert len(children) == 1 and children[0].poll() is not None
    check_run = store.history(check["id"])[0]
    reminder_run = store.history(reminder["id"])[0]
    assert json.loads(check_run["output"])["reminder_seen_before_finish"] is True
    assert reminder_run["state"] == "completed"
    assert reminder_run["finished_at"] < check_run["finished_at"]
    assert {item["job_id"] for item in result["executed"]} == {check["id"], reminder["id"]}
    assert result["delivered"] == 2
    assert len(store.inbox()) == 2
    assert store.history(queued_check["id"]) == []
    assert store.job(queued_check["id"])["next_at"] == now - 0.5
    store.set_state(queued_check["id"], "paused")
    assert scheduler.tick(store)["delivered"] == 0


def test_missed_reminder_has_visible_outcome_and_one_inbox_receipt(tmp_path):
    store = JobStore(tmp_path / "home")
    job = store.add(name="missed", prompt="reminder", original_request="remind", kind="reminder",
                    workdir=tmp_path, next_at=1000, now=900, grace_seconds=10)
    result = scheduler.tick(store, now=1100)
    assert result["executed"] == []
    assert result["delivered"] == 1
    assert store.job(job["id"])["last_outcome"] == "skipped"
    assert store.jobs()[0]["last_outcome"] == "skipped"
    inbox = store.inbox()
    assert len(inbox) == 1 and inbox[0]["state"] == "skipped" and inbox[0]["skipped_count"] == 1
    assert scheduler.tick(store, now=1101)["delivered"] == 0


def test_latest_outcome_follows_occurrence_order_when_timestamps_tie(tmp_path, monkeypatch):
    from agent.runtime.jobs import store as store_module
    store = JobStore(tmp_path / "home")
    job = store.add(name="repeat", prompt="reminder", original_request="remind", kind="reminder",
                    workdir=tmp_path, next_at=1000, now=900, interval_seconds=60, grace_seconds=10)
    identities = iter(["f" * 32, "0" * 32])
    with monkeypatch.context() as patch:
        patch.setattr(store_module.uuid, "uuid4", lambda: SimpleNamespace(hex=next(identities)))
        run = store.claim_due(now=1061)
    assert run is not None
    assert store.start(run["id"])
    assert store.finish(run["id"], "completed", output="latest slot")
    assert store.job(job["id"])["last_outcome"] == "completed"
    assert store.jobs()[0]["last_outcome"] == "completed"
