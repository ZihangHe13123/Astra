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
    reminder(store, tmp_path, {"type": "mail", "from": "tan"})
    watcher = TriggerWatcher(store, mail_service=box.service)
    watcher.look()
    box.deliver(2, "tan@example.edu", "late night")
    watcher.look()
    tick(store)
    sent: list[str] = []

    async def push(text: str) -> None:
        sent.append(text)

    from datetime import datetime
    from zoneinfo import ZoneInfo

    night = datetime(2026, 10, 26, 23, 30, tzinfo=ZoneInfo("Asia/Singapore")).timestamp()
    run = store.history()[0]
    with store.transaction() as db:
        db.execute("UPDATE runs SET created_at=? WHERE id=?", (night, run["id"]))
    asyncio.run(JobDelivery(store, desktop=None, weixin=push).deliver_due(now=night))
    assert sent == []
    morning = datetime(2026, 10, 27, 8, 0, tzinfo=ZoneInfo("Asia/Singapore")).timestamp()
    asyncio.run(JobDelivery(store, desktop=None, weixin=push).deliver_due(now=morning))
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
    assert execute_jobs_command(["add", "x", "--reminder", "--prompt", "x", "--on-new-file", "*", "--time", "08:00"],
                                home=tmp_path / "home", workdir=tmp_path)[1]
