"""Scheduled-job results pushed to the desktop and WeChat: once each, in quiet hours or not, and given up in time."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from agent.cli.jobs_commands import execute_jobs_command
from agent.runtime.jobs.calendar import quiet_hours, quiet_until, set_quiet_hours
from agent.runtime.jobs.delivery import INBOX_NOTE, MAX_AGE, RETRY_SECONDS, TEXT_LIMIT, JobDelivery, push_message
from agent.runtime.jobs.store import ACTIVE_MARKER, INACTIVE_SECONDS, NO_UPDATE, JobStore, note_interaction

SINGAPORE = ZoneInfo("Asia/Singapore")


def at(text: str) -> float:
    return datetime.fromisoformat(text).replace(tzinfo=SINGAPORE).timestamp()


def reminder(store: JobStore, tmp_path: Path, *, next_at: float, **kwargs) -> dict:
    return store.add(name="喝水", prompt="该喝水了", original_request="每天提醒我喝水", kind="reminder",
                     workdir=tmp_path, next_at=next_at, now=next_at - 60, **kwargs)


def check(store: JobStore, tmp_path: Path, *, next_at: float, **kwargs) -> dict:
    return store.add(name="Rust 早报", prompt="看看 Rust 新闻", original_request="每天告诉我 Rust 新闻",
                     workdir=tmp_path, next_at=next_at, model="fixture", now=next_at - 60, **kwargs)


def finish(store: JobStore, now: float, state: str = "completed", output: str = "", **kwargs) -> dict:
    run = store.claim_due(now=now)
    assert run is not None
    store.finish(run["id"], state, output=output, **kwargs)
    store.deliver_pending(now=now)
    return run


class Recorder:
    def __init__(self, failures: int = 0):
        self.desktop: list[dict] = []
        self.weixin: list[str] = []
        self.failures = failures

    def notify(self, event: dict) -> None:
        self.desktop.append(event)

    async def send(self, text: str) -> None:
        if self.failures:
            self.failures -= 1
            raise ConnectionError("WeChat is not online")
        self.weixin.append(text)


def deliver(store: JobStore, recorder: Recorder, now: float, *, weixin: bool = True) -> int:
    delivery = JobDelivery(store, desktop=recorder.notify, weixin=recorder.send if weixin else None)
    return asyncio.run(delivery.deliver_due(now=now))


@pytest.fixture(autouse=True)
def singapore(monkeypatch):
    monkeypatch.setenv("AGENT_TIMEZONE", "Asia/Singapore")


def test_results_are_pushed_once_to_each_target_and_kept_in_the_inbox(tmp_path):
    store = JobStore(tmp_path / "home")
    now = at("2026-10-26T15:00")
    job = check(store, tmp_path, next_at=now)
    run = finish(store, now, output="Rust 1.92 发布了")
    recorder = Recorder()
    assert deliver(store, recorder, now) == 2
    assert recorder.weixin == ["【定时任务：Rust 早报】\nRust 1.92 发布了"]
    assert recorder.desktop == [{"type": "job_notification", "title": "定时任务：Rust 早报", "body": "Rust 1.92 发布了",
                                 "job_id": job["id"], "run_id": run["id"]}]
    assert {push["target"]: push["state"] for push in store.pushes(run["id"])} == {"desktop": "sent", "weixin": "sent"}
    assert deliver(store, recorder, now + 60) == 0
    assert len(recorder.weixin) == 1 and store.inbox()[0]["id"] == run["id"]


def test_wechat_gets_a_long_result_whole_and_the_desktop_its_start(tmp_path):
    from agent.channels.weixin import chat_bubbles

    store = JobStore(tmp_path / "home")
    now = at("2026-10-26T20:00")
    check(store, tmp_path, next_at=now)
    report = "\n\n".join(f"{index}. 第 {index} 条新闻：" + "内容" * 120 for index in range(1, 9))
    finish(store, now, output=report)
    recorder = Recorder()
    assert deliver(store, recorder, now) == 2
    assert recorder.weixin == [f"【定时任务：Rust 早报】\n{report}"] and INBOX_NOTE not in recorder.weixin[0]
    assert len(recorder.desktop[0]["body"]) == 300
    # The channel sends it as several messages, none over its limit, nothing left out.
    pieces = chat_bubbles(recorder.weixin[0], 1800)
    assert len(pieces) > 1 and all(len(piece) <= 1800 for piece in pieces)
    assert "".join("".join(pieces).split()) == "".join(recorder.weixin[0].split())


def test_quiet_results_missed_slots_and_inbox_only_jobs_are_not_pushed(tmp_path):
    store = JobStore(tmp_path / "home")
    now = at("2026-10-26T15:00")
    check(store, tmp_path, next_at=now, notify="on_change")
    quiet = finish(store, now, output=NO_UPDATE, quiet=True)
    inbox_only = reminder(store, tmp_path, next_at=now + 600, targets=[])
    finish(store, now + 600, output="该喝水了")
    repeating = reminder(store, tmp_path, next_at=now, interval_seconds=60, grace_seconds=10)
    store.claim_due(now=now + 3600)
    store.deliver_pending(now=now + 3600)
    missed = next(row for row in store.history(repeating["id"]) if row["state"] == "skipped")
    assert missed["skipped_count"] > 0
    for run_id in (quiet["id"], missed["id"], *[row["id"] for row in store.history(inbox_only["id"])]):
        assert store.pushes(run_id) == []
    assert len(store.inbox()) >= 3


def test_check_results_wait_out_quiet_hours_but_a_timed_reminder_does_not(tmp_path):
    store = JobStore(tmp_path / "home")
    night = at("2026-10-26T23:30")
    check(store, tmp_path, next_at=night)
    result = finish(store, night, output="夜里有新讨论")
    reminder(store, tmp_path, next_at=night + 60)
    finish(store, night + 60, output="该睡觉了")
    recorder = Recorder()
    deliver(store, recorder, night + 60)
    assert recorder.weixin == ["【提醒：喝水】\n该睡觉了"]
    held = {push["target"]: push for push in store.pushes(result["id"])}
    assert held["weixin"]["state"] == "pending" and held["weixin"]["next_try"] == at("2026-10-27T08:00")
    assert held["weixin"]["attempts"] == 0
    deliver(store, recorder, at("2026-10-27T07:59"))
    assert len(recorder.weixin) == 1
    deliver(store, recorder, at("2026-10-27T08:00"))
    assert recorder.weixin[-1] == "【定时任务：Rust 早报】\n夜里有新讨论"
    set_quiet_hours(store.home, None)
    assert quiet_hours(store.home) is None and quiet_until(None, night) is None


def test_quiet_hours_are_read_from_the_home_and_validated(tmp_path):
    home = tmp_path / "home"
    assert quiet_hours(home) == {"start": "23:00", "end": "08:00"}
    assert set_quiet_hours(home, {"start": "0:30", "end": "7:00"}) == {"start": "00:30", "end": "07:00"}
    assert quiet_until(quiet_hours(home), at("2026-10-26T06:00")) == at("2026-10-26T07:00")
    assert quiet_until(quiet_hours(home), at("2026-10-26T07:00")) is None
    with pytest.raises(ValueError):
        set_quiet_hours(home, {"start": "25:00", "end": "07:00"})
    output, error = execute_jobs_command(["quiet", "off"], home=home, workdir=tmp_path)
    assert error == "" and json.loads(output) == {"quiet_hours": None}
    output, error = execute_jobs_command(["quiet", "22:00", "07:30"], home=home, workdir=tmp_path)
    assert json.loads(output) == {"quiet_hours": {"start": "22:00", "end": "07:30"}}
    assert execute_jobs_command(["quiet", "22:00"], home=home, workdir=tmp_path)[1]


def test_failed_pushes_are_retried_with_growing_pauses_then_given_up(tmp_path):
    store = JobStore(tmp_path / "home")
    now = at("2026-10-26T15:00")
    reminder(store, tmp_path, next_at=now, targets=["weixin"])
    run = finish(store, now, output="该喝水了")
    recorder = Recorder(failures=len(RETRY_SECONDS) + 1)
    clock = now
    for pause in RETRY_SECONDS:
        deliver(store, recorder, clock)
        [push] = store.pushes(run["id"])
        assert push["state"] == "pending" and push["next_try"] == clock + pause
        clock += pause
    deliver(store, recorder, clock)
    [push] = store.pushes(run["id"])
    assert push["state"] == "failed" and "not online" in push["detail"] and recorder.weixin == []


def test_a_missing_channel_fails_at_once_and_an_old_result_is_not_sent(tmp_path):
    store = JobStore(tmp_path / "home")
    now = at("2026-10-26T15:00")
    reminder(store, tmp_path, next_at=now)
    run = finish(store, now, output="该喝水了")
    recorder = Recorder()
    deliver(store, recorder, now, weixin=False)
    pushes = {push["target"]: push for push in store.pushes(run["id"])}
    assert pushes["weixin"]["state"] == "failed" and pushes["weixin"]["attempts"] == 1
    assert pushes["desktop"]["state"] == "sent"
    later = reminder(store, tmp_path, next_at=now + 60)
    stale = finish(store, now + 60, output="早就过时了")
    deliver(store, recorder, now + 60 + MAX_AGE + 1)
    assert {push["state"] for push in store.pushes(stale["id"])} == {"failed"}
    assert all("过时" not in text for text in recorder.weixin) and later["id"]


def test_messages_name_the_job_and_explain_pauses_and_failures(tmp_path):
    store = JobStore(tmp_path / "home")
    now = at("2026-10-26T15:00")
    job = check(store, tmp_path, next_at=now, interval_seconds=3600)
    for index in range(3):
        finish(store, now + index * 3600, "failed", detail="provider_error")
    pushes = store.due_pushes(now=now + 3 * 3600)
    titles = [push_message(push) for push in pushes if push["target"] == "weixin"]
    assert titles[0] == ("定时任务没能完成：Rust 早报", "这次没能完成：provider_error")
    assert titles[-1] == ("定时任务已暂停：Rust 早报", "连续 3 次没能完成，我先暂停了。最近一次的情况：provider_error")
    store.set_state(job["id"], "enabled", now=now + 4 * 3600)
    note_interaction(store.home, now=now)
    store.claim_due(now=now + INACTIVE_SECONDS + 5 * 3600, last_interaction=now)
    store.deliver_pending(now=now + INACTIVE_SECONDS + 5 * 3600)
    notice = [push for push in store.due_pushes(now=now + INACTIVE_SECONDS + 5 * 3600)
              if push["run_state"] == "skipped" and push["target"] == "weixin"]
    assert push_message(notice[0]) == ("定时任务已暂停：Rust 早报", "你已经 7 天没找我了，我先把它暂停了。要继续就跟我说一声。")
    long = {"spec": {"name": "x", "kind": "check"}, "run_state": "completed", "output": "字" * (TEXT_LIMIT + 50),
            "detail": "", "job_state": "enabled", "paused_reason": ""}
    assert push_message(long)[1].endswith(INBOX_NOTE)
    # A full news report fits whole.
    report = {**long, "output": "\n\n".join(f"{index}. " + "新闻" * 150 for index in range(10))}
    assert push_message(report)[1] == report["output"] and len(report["output"]) > 2000
    # Out of rounds, the check still answered from what it found: that answer is sent, marked as maybe incomplete.
    used_up = {**long, "output": "三条已核实的新闻", "detail": "iteration_budget"}
    assert push_message(used_up) == ("定时任务：x（轮数用完，可能不全）", "三条已核实的新闻")
    repeated = {**used_up, "detail": "repeated_tool_call"}
    assert push_message(repeated) == ("定时任务：x（提前收尾，可能不全）", "三条已核实的新闻")


def test_the_active_marker_follows_jobs_and_pending_pushes(tmp_path):
    store = JobStore(tmp_path / "home")
    marker = store.root / ACTIVE_MARKER
    assert not marker.exists()
    now = at("2026-10-26T15:00")
    job = reminder(store, tmp_path, next_at=now)
    assert marker.exists()
    finish(store, now, output="该喝水了")
    # The one-shot is done, but its pushes still need a host.
    assert store.job(job["id"])["state"] == "completed" and marker.exists()
    deliver(store, Recorder(), now)
    assert not marker.exists()
    paused = reminder(store, tmp_path, next_at=now + 3600, interval_seconds=3600)
    store.set_state(paused["id"], "paused")
    assert not marker.exists()
    store.request_run(paused["id"])
    assert marker.exists()


def test_cli_and_tool_choose_where_results_go(tmp_path):
    output, error = execute_jobs_command(["add", "x", "--after", "60", "--reminder", "--prompt", "x", "--targets", "weixin"],
                                         home=tmp_path / "home", workdir=tmp_path)
    assert error == "" and json.loads(output)["spec"]["targets"] == ["weixin"]
    assert execute_jobs_command(["add", "y", "--after", "60", "--reminder", "--prompt", "y", "--targets", "email"],
                                home=tmp_path / "home", workdir=tmp_path)[1]
