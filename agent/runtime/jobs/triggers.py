"""Event-started jobs: new mail that matches a filter, or a new file in a folder.

The background host looks every few minutes. A trigger's first successful look only notes where things
stand, an empty folder included, so mail and files that were already there never start a run. Each look
queues at most ``MAX_EVENTS`` runs per job, oldest first, and moves the job's cursor past what it queued
or passed over in the same transaction; what is left waits for the next look, so nothing new is dropped.
A file is new when its name was not in the folder at the last look, whatever its time says.
A mail trigger watches its folder in one mailbox, or in every configured mailbox, each with its own place;
a look takes the oldest new mail across them, so a busy mailbox does not keep another's waiting.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent.mail163.config import MAIN_ACCOUNT
from agent.runtime.instance_lock import InstanceAlreadyRunning, InstanceLock

from .store import INACTIVE_SECONDS, JobStore, last_interaction

logger = logging.getLogger(__name__)

MAX_EVENTS = 5
LOOK_SECONDS = 300.0
# Mail is read past the cursor this many messages at a time, oldest first.
MAIL_PAGE = 100
# A file cursor is the names in the folder at the last look. Earlier cursors were a time, or a time and a
# name, with every file at or before it seen; one before names were kept counted every file at its time.
_NAMES = "names:"
# A mail cursor is, for each mailbox a job watches, its folder's generation and the newest message number
# looked at ("7:120"). One that holds only the "main" mailbox is that pair alone, as before there were several.
_MAILBOXES = "mailboxes:"
_EVERY_NAME = "\U0010ffff"


def _matches(item: dict[str, Any], trigger: dict[str, str]) -> bool:
    sender, subject = trigger.get("from", "").casefold(), trigger.get("subject", "").casefold()
    return ((not sender or sender in str(item.get("sender") or "").casefold())
            and (not subject or subject in str(item.get("subject") or "").casefold()))


class TriggerWatcher:
    def __init__(self, store: JobStore, *, mail_service: Callable[[], Any] | None = None):
        self.store = store
        self._mail_service = mail_service

    def look(self, *, now: float | None = None) -> int:
        """Look once at every enabled event-started job; return how many runs were queued."""
        guard = InstanceLock(self.store.root / "triggers.lock")
        try:
            guard.acquire()
        except InstanceAlreadyRunning:
            return 0
        try:
            return self._look(time.time() if now is None else now)
        finally:
            guard.release()

    def _look(self, now: float) -> int:
        jobs = [job for job in self.store.jobs() if job["state"] == "enabled" and job["spec"].get("trigger")]
        if not jobs:
            return 0
        interaction = last_interaction(self.store.home)
        mail_jobs, queued = [], 0
        for job in jobs:
            if interaction is not None and now - max(interaction, job["active_since"]) > INACTIVE_SECONDS:
                self.store.pause_inactive(job["id"], now=now)
            elif job["spec"]["trigger"]["type"] == "mail":
                mail_jobs.append(job)
            else:
                queued += self._look_at_folder(job, now)
        if mail_jobs:
            queued += self._look_at_mail(mail_jobs, now)
        return queued

    def _look_at_folder(self, job: dict, now: float) -> int:
        folder, pattern = Path(job["spec"]["workdir"]), job["spec"]["trigger"]["pattern"]
        cursor = self.store.trigger_cursor(job["id"])
        try:
            files = {entry.name: entry.stat().st_mtime for entry in folder.iterdir()
                     if entry.is_file() and not entry.name.startswith(".") and fnmatch.fnmatch(entry.name, pattern)}
        except OSError:
            return 0
        if cursor is None:
            return self.store.fire(job["id"], [], _names_cursor(files), now=now)
        seen = _seen(cursor, files)
        # A copy, a move or an unpacked file keeps an old time and is still new; a change to a file already
        # there is not. Oldest first, by time and then name.
        fresh = sorted((when, name) for name, when in files.items() if name not in seen)[:MAX_EVENTS]
        events = [{"type": "file", "path": str(folder / name), "name": name} for _, name in fresh]
        # A file removed since is forgotten, so one that comes back is new again.
        kept = (seen & files.keys()) | {name for _, name in fresh}
        return self.store.fire(job["id"], events, _names_cursor(kept), now=now)

    def _look_at_mail(self, jobs: list[dict], now: float) -> int:
        try:
            boxes, several = self._mailboxes(jobs)
        except Exception:  # noqa: BLE001 - not set up: look again next time
            logger.info("mail triggers could not open the mailboxes", exc_info=True)
            return 0
        watched = {job["id"]: [box for box in boxes if _watches(job, box.config)] for job in jobs}
        looked = []
        for box in boxes:
            folders = sorted({job["spec"]["trigger"]["folder"] for job in jobs if box in watched[job["id"]]})
            if not folders:
                continue
            try:
                box.sync(folders)
            except Exception:  # noqa: BLE001 - unreachable: look again next time
                logger.info("mail triggers could not look at mailbox %s", box.config.name, exc_info=True)
                continue
            looked.append(box)
        queued = 0
        for job in jobs:
            try:
                queued += self._look_at_job_mail([box for box in watched[job["id"]] if box in looked], job, now,
                                                 several)
            except Exception:  # noqa: BLE001 - this folder could not be read now: look again next time
                logger.info("mail trigger %s could not look at its folder", job["id"], exc_info=True)
        return queued

    def _mailboxes(self, jobs: list[dict]) -> tuple[list[Any], bool]:
        """The mailboxes some job watches, opened, and whether more than one is configured."""
        if self._mail_service is not None:
            opened = self._mail_service()
            boxes = list(opened) if isinstance(opened, (list, tuple)) else [opened]
            return boxes, len(boxes) > 1
        from agent.mail163.config import load_mail_accounts
        from agent.mail163.service import MailService

        configs = load_mail_accounts(Path(__file__).resolve().parents[3])
        boxes = []
        for config in configs:
            if not any(_watches(job, config) for job in jobs):
                continue
            try:
                boxes.append(MailService(config))
            except Exception:  # noqa: BLE001 - this mailbox's cache cannot be opened: the others are still looked at
                logger.info("mail triggers could not open mailbox %s", config.name, exc_info=True)
        return boxes, len(configs) > 1

    def _look_at_job_mail(self, boxes: list[Any], job: dict, now: float, several: bool) -> int:
        trigger = job["spec"]["trigger"]
        marks = _mail_marks(self.store.trigger_cursor(job["id"]))
        scans: list[_Scan] = []
        for box in boxes:
            try:
                scan = _scan(box, trigger, marks.get(box.config.name))
            except Exception:  # noqa: BLE001 - this mailbox's folder could not be read now: look again next time
                logger.info("mail trigger %s could not look at mailbox %s", job["id"], box.config.name, exc_info=True)
                continue
            if scan is not None:
                scans.append(scan)
        if not scans:
            return 0
        # The oldest new mail first, across mailboxes, each mailbox's in its own order.
        events: list[dict] = []
        while len(events) < MAX_EVENTS:
            waiting = [scan for scan in scans if scan.taken < len(scan.candidates)]
            if not waiting:
                break
            scan = min(waiting, key=lambda each: (_received(each.candidates[each.taken]), scans.index(each)))
            events.append(_event(scan.box, scan.candidates[scan.taken], trigger, several))
            scan.taken += 1
        for scan in scans:
            if scan.taken == len(scan.candidates):
                place = scan.reached  # Everything it looked at is passed: the first place, or all its new mail.
            elif scan.taken:
                place = int(scan.candidates[scan.taken - 1]["uid"])  # Up to the last one taken; the rest wait.
            else:
                continue  # None of its new mail was taken: it stays where it was.
            marks[scan.box.config.name] = f"{scan.validity}:{place}"
        return self.store.fire(job["id"], events, _mail_cursor(marks), now=now)


@dataclass
class _Scan:
    """What one look found in one mailbox's folder: matching mail after its place, oldest first (at most
    ``MAX_EVENTS``), and the newest number it looked at; a first place has no candidates."""

    box: Any
    validity: str
    reached: int
    candidates: list[dict] = field(default_factory=list)
    taken: int = 0


def _scan(box: Any, trigger: dict, seen: str | None) -> _Scan | None:
    """None when the mailbox has no place to start from yet and cannot be given one now."""
    state = box.folder_state(trigger["folder"])
    seen_validity, _, seen_uid = (seen or "").partition(":")
    if state is None or not state.uidvalidity:
        return None  # Never looked at successfully: no baseline yet, and nothing taken for new.
    validity = str(state.uidvalidity)
    if seen is None or seen_validity != validity:
        if state.last_error_code:
            return None  # Its last look failed: a baseline waits for one that succeeds.
        # The first look, or a mailbox that renumbered its messages: start from where it stands, empty or not.
        return _Scan(box, validity, box.newest_uid(trigger["folder"], uidvalidity=state.uidvalidity))
    last, candidates = int(seen_uid or 0), []
    while len(candidates) < MAX_EVENTS:
        page = box.after(trigger["folder"], last, uidvalidity=state.uidvalidity, limit=MAIL_PAGE)
        for item in page:
            last = int(item["uid"])
            if _matches(item, trigger):
                candidates.append(item)
                if len(candidates) == MAX_EVENTS:
                    break
        if len(page) < MAIL_PAGE:
            break
    return _Scan(box, validity, last, candidates)


def _received(item: dict) -> float:
    received = item.get("received_at")
    return float(received) if isinstance(received, (int, float)) else 0.0


def _event(box: Any, item: dict, trigger: dict, several: bool) -> dict:
    return {"type": "mail", "folder": item.get("folder") or trigger["folder"], "uid": int(item["uid"]),
            "from": str(item.get("sender") or ""), "subject": str(item.get("subject") or ""),
            # Every mailbox but main is named, so the event means the same mailbox later.
            **({"account": box.config.name} if several or box.config.name != MAIN_ACCOUNT else {})}


def _watches(job: dict, config: Any) -> bool:
    wanted = str(job["spec"]["trigger"].get("account") or "").casefold()
    return not wanted or wanted in {config.name, str(config.account).casefold(), str(config.address).casefold()}


def _mail_marks(cursor: str | None) -> dict[str, str]:
    if cursor is None:
        return {}
    if cursor.startswith(_MAILBOXES):
        return {str(name): str(mark) for name, mark in json.loads(cursor[len(_MAILBOXES):]).items()}
    return {MAIN_ACCOUNT: cursor}


def _mail_cursor(marks: dict[str, str]) -> str:
    if set(marks) == {MAIN_ACCOUNT}:
        return marks[MAIN_ACCOUNT]
    return _MAILBOXES + json.dumps(marks, sort_keys=True)


def _names_cursor(names: Iterable[str]) -> str:
    return _NAMES + json.dumps(sorted(names), ensure_ascii=False)


def _seen(cursor: str, files: dict[str, float]) -> set[str]:
    if cursor.startswith(_NAMES):
        return set(json.loads(cursor[len(_NAMES):]))
    since_text, _, since_name = cursor.partition("|")
    since = (float(since_text), since_name if "|" in cursor else _EVERY_NAME)
    return {name for name, when in files.items() if (when, name) <= since}
