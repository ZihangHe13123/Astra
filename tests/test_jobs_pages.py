"""The desktop's jobs and activity views: read-only, across stores, and what each row says."""

from __future__ import annotations

import json
import time

import pytest

from agent.cli.jobs_commands import execute_jobs_command
from agent.runtime.approval_inbox import ApprovalInbox
from agent.runtime.jobs.store import NO_UPDATE, JobStore
from agent.runtime.task_store import TaskStore
from agent.ui.activity import activity, jobs_overview
from agent.ui.queries import query


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("ASTRA_HOME", str(home))
    monkeypatch.setenv("AGENT_TASK_DB", str(tmp_path / "tasks.db"))
    monkeypatch.setenv("ASTRA_APPROVAL_DB", str(tmp_path / "approvals.db"))
    monkeypatch.setenv("AGENT_TIMEZONE", "Asia/Singapore")
    return home


def test_views_of_an_empty_home_create_nothing(home, tmp_path):
    assert jobs_overview(home) == {"jobs": [], "inbox": [], "quiet_hours": {"start": "23:00", "end": "08:00"}}
    assert activity(home)["entries"] == []
    assert not home.exists() and not (tmp_path / "tasks.db").exists()


def test_jobs_overview_lists_jobs_results_and_where_they_went(home, tmp_path):
    store = JobStore(home)
    now = time.time()
    job = store.add(name="Rust 早报", prompt="看看 Rust 新闻", original_request="每天告诉我", workdir=tmp_path,
                    calendar={"time": "08:00", "tz": "Asia/Singapore"}, notify="on_change", web=True, model="fixture",
                    targets=["weixin"], now=now)
    store.request_run(job["id"])
    first = store.claim_due(now=now)
    store.finish(first["id"], "completed", output="两条新讨论", usage={"prompt_tokens": 90})
    store.deliver_pending()
    store.mark_push(first["id"], "weixin", "sent")
    store.request_run(job["id"])
    second = store.claim_due(now=now)
    store.finish(second["id"], "completed", output=NO_UPDATE, quiet=True)
    store.deliver_pending()
    removed = store.add(name="旧的", prompt="x", original_request="x", kind="reminder", workdir=tmp_path,
                        next_at=now + 60, now=now)
    store.set_state(removed["id"], "removed")
    view = query({"method": "jobs"})
    assert [item["name"] for item in view["jobs"]] == ["Rust 早报"]
    [row] = view["jobs"]
    assert row["schedule"] == "每天 08:00（Asia/Singapore）" and row["targets"] == ["weixin"] and row["web"]
    assert row["next_at"] and row["notify"] == "on_change"
    by_id = {item["id"]: item for item in view["inbox"]}
    assert by_id[first["id"]]["sent"] == ["weixin"] and by_id[first["id"]]["usage"] == {"prompt_tokens": 90}
    assert by_id[second["id"]]["quiet"] and by_id[second["id"]]["sent"] == []
    assert not by_id[first["id"]]["read"]


def test_activity_lists_effects_approvals_and_job_runs_across_surfaces(home, tmp_path):
    tasks = TaskStore(tmp_path / "tasks.db")
    desk = tasks.start_run("r1", "整理下载文件夹", session_id="session_20000101_000000_1", source="desktop")
    tasks.start_step(desk["id"], "s1", "tool", name="read_file", risk="read", input_value={"path": "a.txt"})
    write = tasks.start_step(desk["id"], "s2", "tool", name="write_file", risk="write", input_value={"path": "notes.md"})
    tasks.finish_step(write["id"], status="completed", output={})
    tasks.finish_run(desk["id"], "completed")
    chat = tasks.start_run("r2", "你好", session_id="channel_weixin_private_abc", source="weixin")
    tasks.finish_run(chat["id"], "completed")
    inbox = ApprovalInbox(tmp_path / "approvals.db")
    inbox.create({"approval_title": "写入 notes.md", "tool_name": "write_file"}, request_id="approval-1",
                 session_id="session_20000101_000000_1", task_id=desk["id"], surface="gui")
    inbox.resolve("approval-1", "once")
    store = JobStore(home)
    job = store.add(name="喝水", prompt="该喝水了", original_request="提醒我", kind="reminder", workdir=tmp_path,
                    next_at=time.time(), now=time.time())
    run = store.claim_due()
    store.finish(run["id"], "completed", output="该喝水了")
    entries = query({"method": "activity", "params": {"days": 7}})["entries"]
    assert [entry["kind"] for entry in entries].count("job") == 1
    desktop = next(entry for entry in entries if entry["id"] == desk["id"])
    assert desktop["source"] == "desktop" and desktop["title"] == "整理下载文件夹"
    assert desktop["effects"] == [{"name": "write_file", "risk": "write", "status": "completed", "summary": "notes.md"}]
    assert desktop["approvals"] == [{"title": "写入 notes.md", "decision": "once"}]
    weixin = next(entry for entry in entries if entry["id"] == chat["id"])
    assert weixin["source"] == "weixin" and weixin["effects"] == []
    reminder = next(entry for entry in entries if entry["kind"] == "job")
    assert reminder["title"] == "喝水" and reminder["summary"] == "该喝水了" and reminder["job_id"] == job["id"]
    assert entries == sorted(entries, key=lambda entry: entry["time"], reverse=True)
    assert activity(home, now=time.time() + 30 * 86400)["entries"] == []


def test_queue_run_leaves_the_occurrence_to_the_scheduler(home, tmp_path):
    output, error = execute_jobs_command(["add", "x", "--after", "3600", "--reminder", "--prompt", "x"],
                                         home=home, workdir=tmp_path)
    job = json.loads(output)
    queued, error = execute_jobs_command(["run", job["id"], "--queue"], home=home, workdir=tmp_path)
    assert error == "" and json.loads(queued)["run_requested_at"]
    store = JobStore(home)
    assert store.history(job["id"]) == []
    assert store.claim_due()["job_id"] == job["id"]
    # Without --queue, running here stays a terminal's job.
    assert "separate terminal" in execute_jobs_command(["run", job["id"]], home=home, workdir=tmp_path)[1]
