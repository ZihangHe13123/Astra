"""Event-started jobs: new mail that matches, or a new file in a folder, starts a run once."""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import replace
from pathlib import Path

import pytest
from test_mail163_service import ClientFactory, ScriptedClient, message

from agent.cli.jobs_commands import execute_jobs_command
from agent.mail163.models import FolderSnapshot, MailConfig
from agent.mail163.service import MailService
from agent.runtime.jobs.delivery import JobDelivery
from agent.runtime.jobs.scheduler import tick
from agent.runtime.jobs.store import INACTIVE_SECONDS, JobStore, note_interaction
from agent.runtime.jobs.triggers import MAX_EVENTS, TriggerWatcher
from agent.runtime.tools.jobs import register_jobs_tool
from agent.runtime.tools.registry import ToolRegistry


def mail_config(tmp_path: Path) -> MailConfig:
    mail_dir = tmp_path / "mail"
    return MailConfig(project_root=tmp_path, account="me@gmail.com", auth_code="x", host="imap.gmail.com", port=993,
                      database_path=mail_dir / "cache.sqlite3", attachments_dir=mail_dir / "attachments",
                      max_body_bytes=4096, default_folders=("INBOX",), sent_folder="[Gmail]/Sent Mail", send_id=False)


class Mailbox:
    def __init__(self, tmp_path: Path):
        self.messages: dict[int, object] = {}
        self.validity = 7
        self.client = ScriptedClient(snapshots={}, messages={})
        self.service = lambda: MailService(mail_config(tmp_path), client_factory=ClientFactory(self.client))

    def deliver(self, uid: int, sender: str, subject: str) -> None:
        self.messages[uid] = replace(message("INBOX", self.validity, uid, subject=subject), sender=sender)
        self._publish()

    def renumber(self, validity: int) -> None:
        self.validity = validity
        self.messages = {uid: replace(item, uidvalidity=validity) for uid, item in self.messages.items()}
        self._publish()

    def _publish(self) -> None:
        uids = frozenset(self.messages)
        self.client.snapshots["INBOX"] = FolderSnapshot("INBOX", self.validity, uids, {uid: () for uid in uids})
        self.client.messages.update({("INBOX", uid): item for uid, item in self.messages.items()})


def reminder(store: JobStore, tmp_path: Path, trigger: dict, **kwargs) -> dict:
    return store.add(name="导师来信", prompt="导师来信了", original_request="导师来信就告诉我", kind="reminder",
                     workdir=tmp_path, trigger=trigger, **kwargs)


def outputs(store: JobStore) -> list[str]:
    return [row["output"] for row in store.history() if row["state"] == "completed"]


def test_new_mail_that_matches_starts_one_run_each_and_old_mail_none(tmp_path):
    store, box = JobStore(tmp_path / "home"), Mailbox(tmp_path)
    box.deliver(1, "Prof Tan <tan@example.edu>", "Old thread")
    job = reminder(store, tmp_path, {"type": "mail", "from": "tan@", "subject": ""})
    watcher = TriggerWatcher(store, mail_service=box.service)
    assert job["spec"]["mail"] is False and job["next_at"] > 1e11
    assert watcher.look() == 0 and store.trigger_cursor(job["id"]) == "7:1"
    box.deliver(2, "Newsletter <news@example.com>", "Weekly digest")
    box.deliver(3, "Prof Tan <tan@example.edu>", "Meeting moved to 3pm")
    assert watcher.look() == 1
    tick(store)
    assert outputs(store) == ["导师来信了\n新邮件：Prof Tan <tan@example.edu> — Meeting moved to 3pm"]
    assert watcher.look() == 0 and len(outputs(store)) == 1
    for uid in range(4, 4 + MAX_EVENTS + 3):
        box.deliver(uid, "tan@example.edu", f"note {uid}")
    assert watcher.look() == MAX_EVENTS
    # A mailbox that renumbered its messages starts over from where it stands now.
    box.renumber(8)
    box.deliver(50, "tan@example.edu", "after renumbering")
    assert watcher.look() == 0 and store.trigger_cursor(job["id"]) == "8:50"


def test_a_new_file_that_matches_starts_a_run_and_others_do_not(tmp_path):
    folder = tmp_path / "Downloads"
    folder.mkdir()
    (folder / "old.pdf").write_text("x")
    store = JobStore(tmp_path / "home")
    job = reminder(store, folder, {"type": "file", "pattern": "*.pdf"})
    watcher = TriggerWatcher(store)
    assert watcher.look() == 0
    later = time.time() + 5
    for name in ("syllabus.pdf", "notes.txt", ".hidden.pdf"):
        (folder / name).write_text("x")
        os.utime(folder / name, (later, later))
    assert watcher.look() == 1
    tick(store)
    assert outputs(store) == ["导师来信了\n新文件：syllabus.pdf"]
    assert watcher.look() == 0
    assert job["spec"]["workdir"] == str(folder.resolve())


def test_an_event_started_job_pauses_after_a_week_without_the_person(tmp_path):
    store, box = JobStore(tmp_path / "home"), Mailbox(tmp_path)
    box.deliver(1, "tan@example.edu", "x")
    now = time.time()
    job = reminder(store, tmp_path, {"type": "mail", "from": "tan"}, now=now)
    note_interaction(store.home, now=now)
    TriggerWatcher(store, mail_service=box.service).look(now=now + INACTIVE_SECONDS + 60)
    assert store.job(job["id"])["state"] == "paused" and store.job(job["id"])["paused_reason"] == "inactive"


def test_a_mail_started_reminder_waits_out_the_quiet_hours(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_TIMEZONE", "Asia/Singapore")
    store, box = JobStore(tmp_path / "home"), Mailbox(tmp_path)
    box.deliver(1, "tan@example.edu", "x")
    reminder(store, tmp_path, {"type": "mail", "from": "tan"}, targets=["weixin"])
    watcher = TriggerWatcher(store, mail_service=box.service)
    watcher.look()
    box.deliver(2, "tan@example.edu", "late night")
    watcher.look()
    tick(store)
    sent: list[str] = []

    async def push(text: str) -> None:
        sent.append(text)

    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    # The push was queued now, so the night has to come after it: the next 23:30 there, and the morning after.
    night = datetime.now(ZoneInfo("Asia/Singapore")).replace(hour=23, minute=30, second=0, microsecond=0)
    if night.timestamp() <= time.time():
        night += timedelta(days=1)
    morning = (night + timedelta(days=1)).replace(hour=8, minute=0)
    run = store.history()[0]
    with store.transaction() as db:
        db.execute("UPDATE runs SET created_at=? WHERE id=?", (night.timestamp(), run["id"]))
    asyncio.run(JobDelivery(store, desktop=None, weixin=push).deliver_due(now=night.timestamp()))
    assert sent == []
    asyncio.run(JobDelivery(store, desktop=None, weixin=push).deliver_due(now=morning.timestamp()))
    assert sent == ["【定时任务：导师来信】\n导师来信了\n新邮件：tan@example.edu — late night"]


def test_the_tool_and_command_line_start_jobs_on_events(tmp_path, monkeypatch):
    registry = ToolRegistry()
    asked: list[dict] = []

    async def approve(request):
        asked.append(request)
        return "once"

    registry.set_approval_handler(approve)
    register_jobs_tool(registry, home=tmp_path / "home", workdir=lambda: tmp_path,
                       request=lambda: "导师来邮件就提醒我", model_key=lambda: "fixture")

    def call(**args):
        return asyncio.run(registry.execute("jobs", args, call_id="c"))

    made = call(action="add", name="导师来信", prompt="总结导师的新邮件", on_new_mail={"from": "tan@", "folder": "inbox"})
    job = json.loads(made["output"])["job"]
    assert job["mail"] is True and job["next_run"] == "" and job["schedule"] == "on new mail from 'tan@'"
    assert asked[-1]["target"] == "有新邮件时：发件人含「tan@」；一次只读检查：工作区文件、你的邮件"
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    call(action="add", name="新课件", kind="reminder", prompt="有新课件", on_new_file={"folder": str(downloads), "pattern": "*.pdf"})
    assert asked[-1]["target"].startswith(f"「{downloads.resolve()}」里出现新的 *.pdf 文件时")
    before = len(asked)
    for args in ({"action": "add", "name": "x", "prompt": "x", "on_new_mail": {"from": "a"}, "time": "08:00"},
                 {"action": "add", "name": "x", "prompt": "x", "on_new_file": {"folder": str(tmp_path / "missing")}},
                 {"action": "update", "job_id": job["job_id"], "on_new_mail": {"from": "b"}}):
        assert call(**args)["error_type"] == "invalid_input"
    assert len(asked) == before
    with pytest.raises(ValueError, match="event"):
        JobStore(tmp_path / "home").update(job["job_id"], request="改成每天", calendar={"time": "08:00", "tz": "UTC"})
    output, error = execute_jobs_command(["add", "news", "--reminder", "--prompt", "x", "--on-mail-subject", "deadline"],
                                         home=tmp_path / "home", workdir=tmp_path)
    assert error == "" and json.loads(output)["spec"]["trigger"] == {"type": "mail", "from": "", "subject": "deadline",
                                                                      "folder": "INBOX"}
    # No time of its own: nothing formats the far-future sentinel, which Windows cannot represent.
    assert json.loads(output)["next_at_utc"] is None
    listed = json.loads(execute_jobs_command(["list"], home=tmp_path / "home", workdir=tmp_path)[0])
    assert {job["next_at_utc"] for job in listed if job["spec"].get("trigger")} == {None}
    assert execute_jobs_command(["add", "x", "--reminder", "--prompt", "x", "--on-new-file", "*", "--time", "08:00"],
                                home=tmp_path / "home", workdir=tmp_path)[1]


def queued_events(store: JobStore, job_id: str) -> list[dict]:
    with store.transaction() as db:
        rows = db.execute("SELECT payload FROM trigger_events WHERE job_id=? ORDER BY id", (job_id,)).fetchall()
    return [json.loads(row["payload"]) for row in rows]


def test_more_new_files_than_one_look_takes_all_start_a_run_each_once(tmp_path):
    folder = tmp_path / "Downloads"
    folder.mkdir()
    store = JobStore(tmp_path / "home")
    job = reminder(store, folder, {"type": "file", "pattern": "*.pdf"})
    watcher = TriggerWatcher(store)
    watcher.look()
    later = time.time() + 5
    # Eight with times of their own, then six sharing one time: a look stops among them and goes on later.
    for index in range(8):
        (folder / f"{index}.pdf").write_text("x")
        os.utime(folder / f"{index}.pdf", (later + index, later + index))
    for name in "abcdef":
        (folder / f"same-{name}.pdf").write_text("x")
        os.utime(folder / f"same-{name}.pdf", (later + 20, later + 20))
    counts = [watcher.look() for _ in range(5)]
    assert counts == [MAX_EVENTS, MAX_EVENTS, 4, 0, 0]
    names = [event["name"] for event in queued_events(store, job["id"])]
    assert names == [f"{index}.pdf" for index in range(8)] + [f"same-{name}.pdf" for name in "abcdef"]


def test_more_new_mail_than_one_look_or_page_takes_all_starts_a_run_each_once(tmp_path):
    store, box = JobStore(tmp_path / "home"), Mailbox(tmp_path)
    box.deliver(1, "tan@example.edu", "before")
    job = reminder(store, tmp_path, {"type": "mail", "from": "tan@", "subject": ""})
    watcher = TriggerWatcher(store, mail_service=box.service)
    watcher.look()
    # Seventy new messages, more than the newest-fifty page; every tenth is from the sender watched for.
    for uid in range(2, 72):
        box.deliver(uid, "tan@example.edu" if uid % 10 == 0 else "news@example.com", f"mail {uid}")
    counts = [watcher.look() for _ in range(3)]
    assert counts == [MAX_EVENTS, 2, 0]
    assert [event["uid"] for event in queued_events(store, job["id"])] == [10, 20, 30, 40, 50, 60, 70]


def test_an_empty_folder_watched_from_the_start_triggers_on_its_first_mail(tmp_path):
    store, box = JobStore(tmp_path / "home"), Mailbox(tmp_path)
    box._publish()  # The inbox exists and is empty.
    job = reminder(store, tmp_path, {"type": "mail", "from": "tan@", "subject": ""})
    watcher = TriggerWatcher(store, mail_service=box.service)
    assert watcher.look() == 0 and store.trigger_cursor(job["id"]) == "7:0"
    box.deliver(1, "tan@example.edu", "first")
    box.deliver(2, "tan@example.edu", "second")
    assert watcher.look() == 2
    assert [event["subject"] for event in queued_events(store, job["id"])] == ["first", "second"]


def test_a_look_that_cannot_reach_the_mailbox_sets_no_baseline_and_loses_nothing(tmp_path):
    from agent.mail163.imap_client import MailNetworkError

    store, box = JobStore(tmp_path / "home"), Mailbox(tmp_path)
    job = reminder(store, tmp_path, {"type": "mail", "from": "tan@", "subject": ""})
    watcher = TriggerWatcher(store, mail_service=box.service)
    box.client.enter_error = MailNetworkError("IMAP connection failed")
    assert watcher.look() == 0 and store.trigger_cursor(job["id"]) is None
    box.client.enter_error = None
    box.deliver(1, "tan@example.edu", "there when the first look succeeds")
    # The first look that succeeds notes where things stand: what was there is not new to the job.
    assert watcher.look() == 0 and store.trigger_cursor(job["id"]) == "7:1"
    box.client.enter_error = MailNetworkError("IMAP connection failed")
    box.deliver(2, "tan@example.edu", "while unreachable")
    assert watcher.look() == 0
    box.client.enter_error = None
    assert watcher.look() == 1
    assert [event["subject"] for event in queued_events(store, job["id"])] == ["while unreachable"]
