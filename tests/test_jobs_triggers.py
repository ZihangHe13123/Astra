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
from agent.runtime.tools.mail import register_mail_tool
from agent.runtime.tools.registry import ToolRegistry


def mail_config(tmp_path: Path) -> MailConfig:
    mail_dir = tmp_path / "mail"
    return MailConfig(project_root=tmp_path, account="me@gmail.com", auth_code="x", host="imap.gmail.com", port=993,
                      database_path=mail_dir / "cache.sqlite3", attachments_dir=mail_dir / "attachments",
                      max_body_bytes=4096, default_folders=("INBOX",), sent_folder="[Gmail]/Sent Mail", send_id=False)


class Mailbox:
    def __init__(self, tmp_path: Path, name: str = "main"):
        self.messages: dict[int, object] = {}
        self.validity = 7
        self.client = ScriptedClient(snapshots={}, messages={})
        config = mail_config(tmp_path)
        if name != "main":
            config = replace(config, name=name, account=f"me@{name}.example", database_path=tmp_path / "mail" / name)
        self.service = lambda: MailService(config, client_factory=ClientFactory(self.client))

    def deliver(self, uid: int, sender: str, subject: str, received_at: float | None = None) -> None:
        self.messages[uid] = replace(message("INBOX", self.validity, uid, subject=subject, received_at=received_at),
                                     sender=sender)
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

    async def push(text: str, _store, _id) -> None:
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


def test_a_file_is_new_by_its_name_appearing_not_by_its_time(tmp_path):
    folder = tmp_path / "Downloads"
    folder.mkdir()
    (folder / "existing.pdf").write_text("x")
    store = JobStore(tmp_path / "home")
    job = reminder(store, folder, {"type": "file", "pattern": "*.pdf"})
    watcher = TriggerWatcher(store)
    assert watcher.look() == 0
    # Copied, moved or unpacked: the file keeps a time from a month ago and is still new.
    month_ago = time.time() - 30 * 86400
    (folder / "copied.pdf").write_text("x")
    os.utime(folder / "copied.pdf", (month_ago, month_ago))
    assert watcher.look() == 1
    # Changing a file that was already there is not a new file, however often.
    for edit in ("y", "z"):
        later = time.time() + 60
        (folder / "existing.pdf").write_text(edit)
        os.utime(folder / "existing.pdf", (later, later))
        assert watcher.look() == 0
    # Removed and back again is new again.
    (folder / "copied.pdf").unlink()
    assert watcher.look() == 0
    (folder / "copied.pdf").write_text("x")
    os.utime(folder / "copied.pdf", (month_ago, month_ago))
    assert watcher.look() == 1
    assert [event["name"] for event in queued_events(store, job["id"])] == ["copied.pdf", "copied.pdf"]


def test_a_time_cursor_from_before_names_were_kept_still_starts_the_files_after_it(tmp_path):
    folder = tmp_path / "Downloads"
    folder.mkdir()
    store = JobStore(tmp_path / "home")
    job = reminder(store, folder, {"type": "file", "pattern": "*.pdf"})
    base = time.time() - 100
    for index, name in enumerate(("seen.pdf", "same-a.pdf", "same-b.pdf", "after.pdf")):
        (folder / name).write_text("x")
        when = base + (10 if name.startswith("same") else index * 20)
        os.utime(folder / name, (when, when))
    # Written by the earlier watcher: everything up to same-a.pdf at its time was taken.
    store.fire(job["id"], [], f"{base + 10!r}|same-a.pdf")
    assert TriggerWatcher(store).look() == 2
    assert [event["name"] for event in queued_events(store, job["id"])] == ["same-b.pdf", "after.pdf"]
    assert TriggerWatcher(store).look() == 0


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


def test_a_mail_trigger_watches_every_mailbox_unless_it_names_one(tmp_path):
    store, main, work = JobStore(tmp_path / "home"), Mailbox(tmp_path), Mailbox(tmp_path, "work")
    main.deliver(1, "tan@example.edu", "old in main")
    work.deliver(1, "tan@example.edu", "old at work")
    every = reminder(store, tmp_path, {"type": "mail", "from": "tan@", "subject": ""})
    only_work = reminder(store, tmp_path, {"type": "mail", "from": "tan@", "subject": "", "account": "work"})
    watcher = TriggerWatcher(store, mail_service=lambda: [main.service(), work.service()])
    assert watcher.look() == 0  # Each mailbox's first look notes where it stands.
    main.deliver(2, "tan@example.edu", "new in main")
    work.deliver(2, "tan@example.edu", "new at work")
    assert watcher.look() == 3
    assert [(event["account"], event["subject"]) for event in queued_events(store, every["id"])] == [
        ("main", "new in main"), ("work", "new at work")]
    assert [(event["account"], event["subject"]) for event in queued_events(store, only_work["id"])] == [
        ("work", "new at work")]
    assert json.loads(store.trigger_cursor(every["id"]).partition(":")[2]) == {"main": "7:2", "work": "7:2"}
    tick(store)
    assert "导师来信了\n新邮件（work）：tan@example.edu — new at work" in outputs(store)
    assert watcher.look() == 0


def test_a_mail_trigger_from_one_mailbox_keeps_its_place_when_more_are_added(tmp_path):
    store, main, work = JobStore(tmp_path / "home"), Mailbox(tmp_path), Mailbox(tmp_path, "work")
    main.deliver(1, "tan@example.edu", "before")
    job = reminder(store, tmp_path, {"type": "mail", "from": "tan@", "subject": ""})
    assert TriggerWatcher(store, mail_service=main.service).look() == 0
    assert store.trigger_cursor(job["id"]) == "7:1"  # One mailbox: the cursor it always had.
    # A second mailbox is set up, with mail from before; main gets one new message meanwhile.
    for uid in (1, 2, 3):
        work.deliver(uid, "tan@example.edu", f"already at work {uid}")
    main.deliver(2, "tan@example.edu", "while adding a mailbox")
    both = TriggerWatcher(store, mail_service=lambda: [main.service(), work.service()])
    assert both.look() == 1
    assert [event["subject"] for event in queued_events(store, job["id"])] == ["while adding a mailbox"]
    work.deliver(4, "tan@example.edu", "new at work")
    assert both.look() == 1 and queued_events(store, job["id"])[-1]["subject"] == "new at work"
    # And back to main alone: its place is kept, and so is work's for when it returns.
    main.deliver(3, "tan@example.edu", "main alone again")
    assert TriggerWatcher(store, mail_service=main.service).look() == 1
    work.deliver(5, "tan@example.edu", "work returns")
    assert both.look() == 1 and queued_events(store, job["id"])[-1]["subject"] == "work returns"


def test_jobs_name_the_mailbox_a_mail_trigger_watches(tmp_path):
    registry = ToolRegistry()
    asked: list[dict] = []

    async def approve(request):
        asked.append(request)
        return "once"

    registry.set_approval_handler(approve)
    register_jobs_tool(registry, home=tmp_path / "home", workdir=lambda: tmp_path,
                       request=lambda: "工作邮箱里导师来信就提醒我", model_key=lambda: "fixture")
    made = asyncio.run(registry.execute("jobs", {"action": "add", "name": "导师来信", "kind": "reminder", "prompt": "x",
                                                 "on_new_mail": {"from": "tan@", "account": "work"}}, call_id="c"))
    job = json.loads(made["output"])["job"]
    assert job["schedule"] == "on new mail from 'tan@' in mailbox 'work'"
    assert asked[-1]["target"].startswith("有新邮件时（work）：发件人含「tan@」")
    output, error = execute_jobs_command(["add", "news", "--reminder", "--prompt", "x", "--on-mail-subject", "deadline",
                                          "--on-mail-account", "work", "--on-mail-folder", "Projects"],
                                         home=tmp_path / "home", workdir=tmp_path)
    assert error == "" and json.loads(output)["spec"]["trigger"] == {
        "type": "mail", "from": "", "subject": "deadline", "folder": "Projects", "account": "work"}


def test_a_mailbox_gets_its_place_even_when_another_fills_the_look(tmp_path):
    store, main, work = JobStore(tmp_path / "home"), Mailbox(tmp_path), Mailbox(tmp_path, "work")
    main.deliver(1, "tan@example.edu", "old in main")
    job = reminder(store, tmp_path, {"type": "mail", "from": "tan@", "subject": ""})
    assert TriggerWatcher(store, mail_service=main.service).look() == 0
    work.deliver(1, "tan@example.edu", "old at work")
    for uid in range(2, 2 + MAX_EVENTS):
        main.deliver(uid, "tan@example.edu", f"new in main {uid}")
    watcher = TriggerWatcher(store, mail_service=lambda: [main.service(), work.service()])
    assert watcher.look() == MAX_EVENTS
    # work was looked at in that full look, and its place taken there: what arrives next is new.
    work.deliver(2, "tan@example.edu", "new at work")
    assert watcher.look() == 1
    assert queued_events(store, job["id"])[-1]["subject"] == "new at work"


def test_an_event_from_a_mailbox_other_than_main_names_it_even_alone(tmp_path):
    store, main, work = JobStore(tmp_path / "home"), Mailbox(tmp_path), Mailbox(tmp_path, "work")
    work.deliver(1, "tan@example.edu", "old at work")
    job = reminder(store, tmp_path, {"type": "mail", "from": "tan@", "subject": ""})
    watcher = TriggerWatcher(store, mail_service=work.service)
    assert watcher.look() == 0
    work.deliver(2, "tan@example.edu", "new at work")
    assert watcher.look() == 1
    [event] = queued_events(store, job["id"])
    assert event["account"] == "work"
    # main is set up before the run reads it: the event still means work's message 2, not main's.
    main.deliver(2, "tan@example.edu", "main's message 2")
    registry = ToolRegistry()
    register_mail_tool(registry, project_root=tmp_path, service=lambda: [main.service(), work.service()])
    text = asyncio.run(registry.execute("mail", {"action": "read", "uid": event["uid"], "folder": event["folder"],
                                                 "account": event["account"]}, call_id="m"))["output"]
    assert "new at work" in text and "main's message" not in text


def test_a_mailbox_whose_cache_cannot_be_opened_does_not_stop_the_others_triggers(tmp_path, monkeypatch):
    import agent.mail163.config as mail_config_module
    import agent.mail163.service as mail_service_module

    store, main = JobStore(tmp_path / "home"), Mailbox(tmp_path)
    main.deliver(1, "tan@example.edu", "old in main")
    job = reminder(store, tmp_path, {"type": "mail", "from": "tan@", "subject": ""})
    work = replace(mail_config(tmp_path), name="work", account="me@work.example",
                   database_path=tmp_path / "mail" / "broken.sqlite3")
    work.database_path.parent.mkdir(parents=True, exist_ok=True)
    work.database_path.write_bytes(b"not a sqlite database")
    # The configured mailboxes as the watcher loads them, each opened on a scripted server.
    monkeypatch.setattr(mail_config_module, "load_mail_accounts", lambda root: [mail_config(tmp_path), work])
    real = mail_service_module.MailService
    monkeypatch.setattr(mail_service_module, "MailService",
                        lambda config: real(config, client_factory=ClientFactory(main.client)))
    watcher = TriggerWatcher(store)
    assert watcher.look() == 0
    main.deliver(2, "tan@example.edu", "new in main")
    assert watcher.look() == 1 and queued_events(store, job["id"])[-1]["subject"] == "new in main"


def test_a_busy_mailbox_does_not_keep_another_mailboxs_older_mail_waiting(tmp_path):
    store, main, work = JobStore(tmp_path / "home"), Mailbox(tmp_path), Mailbox(tmp_path, "work")
    for box in (main, work):
        box.deliver(1, "tan@example.edu", "old", received_at=1.0)
    job = reminder(store, tmp_path, {"type": "mail", "from": "tan@", "subject": ""})
    watcher = TriggerWatcher(store, mail_service=lambda: [main.service(), work.service()])
    assert watcher.look() == 0
    work.deliver(2, "tan@example.edu", "at work, before the rush", received_at=100.0)
    # main, looked at first, has a full look's worth of newer matching mail every time.
    for rush in range(3):
        for uid in range(2 + rush * MAX_EVENTS, 2 + (rush + 1) * MAX_EVENTS):
            main.deliver(uid, "tan@example.edu", f"rush {uid}", received_at=200.0 + uid)
        assert watcher.look() == MAX_EVENTS
    subjects = [event["subject"] for event in queued_events(store, job["id"])]
    # The oldest first, across mailboxes: work's message in the first look, and nothing of main's skipped.
    assert subjects[0] == "at work, before the rush"
    assert subjects[1:] == [f"rush {uid}" for uid in range(2, 2 + 3 * MAX_EVENTS - 1)]
    assert watcher.look() == 1 and queued_events(store, job["id"])[-1]["subject"] == f"rush {1 + 3 * MAX_EVENTS}"


def test_backlogs_in_several_mailboxes_drain_once_each_and_in_order(tmp_path):
    store, main, work = JobStore(tmp_path / "home"), Mailbox(tmp_path), Mailbox(tmp_path, "work")
    for box in (main, work):
        box.deliver(1, "tan@example.edu", "old")
    job = reminder(store, tmp_path, {"type": "mail", "from": "tan@", "subject": ""})
    watcher = TriggerWatcher(store, mail_service=lambda: [main.service(), work.service()])
    assert watcher.look() == 0
    for uid in range(2, 8):
        main.deliver(uid, "news@example.com" if uid == 4 else "tan@example.edu", f"main {uid}", received_at=10.0 * uid)
    for uid in (2, 3):
        work.deliver(uid, "tan@example.edu", f"work {uid}", received_at=10.0 * uid + 5)
    assert [watcher.look() for _ in range(3)] == [MAX_EVENTS, 2, 0]
    assert [event["subject"] for event in queued_events(store, job["id"])] == [
        "main 2", "work 2", "main 3", "work 3", "main 5", "main 6", "main 7"]


def test_newer_mail_left_out_of_a_full_look_is_taken_by_the_next(tmp_path):
    store, main, work = JobStore(tmp_path / "home"), Mailbox(tmp_path), Mailbox(tmp_path, "work")
    for box in (main, work):
        box.deliver(1, "tan@example.edu", "old", received_at=1.0)
    job = reminder(store, tmp_path, {"type": "mail", "from": "tan@", "subject": ""})
    watcher = TriggerWatcher(store, mail_service=lambda: [main.service(), work.service()])
    assert watcher.look() == 0
    for uid in range(2, 2 + MAX_EVENTS):
        main.deliver(uid, "tan@example.edu", f"main {uid}", received_at=10.0 + uid)
    work.deliver(2, "news@example.com", "not watched for", received_at=50.0)
    work.deliver(3, "tan@example.edu", "newer at work", received_at=60.0)
    # main's five fill the look; work's message, newer than all of them, keeps its place for the next one.
    assert watcher.look() == MAX_EVENTS
    assert "newer at work" not in [event["subject"] for event in queued_events(store, job["id"])]
    assert watcher.look() == 1 and queued_events(store, job["id"])[-1]["subject"] == "newer at work"
    assert watcher.look() == 0
