"""Event-started jobs: new mail that matches a filter, or a new file in a folder.

The background host looks every few minutes. A trigger's first successful look only notes where things
stand, an empty folder included, so mail and files that were already there never start a run. Each look
queues at most ``MAX_EVENTS`` runs per job, oldest first, and moves the job's cursor past what it queued
or passed over in the same transaction; what is left waits for the next look, so nothing new is dropped.
"""

from __future__ import annotations

import fnmatch
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .store import INACTIVE_SECONDS, JobStore, last_interaction

logger = logging.getLogger(__name__)

MAX_EVENTS = 5
LOOK_SECONDS = 300.0
# Mail is read past the cursor this many messages at a time, oldest first.
MAIL_PAGE = 100
# A file cursor before files were told apart by name: every file at its time counted as seen.
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
        now = time.time() if now is None else now
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
            files = [(entry.stat().st_mtime, entry) for entry in folder.iterdir()
                     if entry.is_file() and not entry.name.startswith(".") and fnmatch.fnmatch(entry.name, pattern)]
        except OSError:
            return 0
        if cursor is None:
            return self.store.fire(job["id"], [], repr(now), now=now)
        # Files in (time, name) order: those sharing a time are told apart, so a look that stops among
        # them goes on from the right one next time.
        since_text, _, since_name = cursor.partition("|")
        since = (float(since_text), since_name if "|" in cursor else _EVERY_NAME)
        fresh = sorted((when, entry.name, entry) for when, entry in files if (when, entry.name) > since)[:MAX_EVENTS]
        if not fresh:
            return self.store.fire(job["id"], [], cursor, now=now)
        events = [{"type": "file", "path": str(entry), "name": name} for _, name, entry in fresh]
        last_when, last_name, _ = fresh[-1]
        return self.store.fire(job["id"], events, f"{last_when!r}|{last_name}", now=now)

    def _look_at_mail(self, jobs: list[dict], now: float) -> int:
        try:
            service = self._mail_service() if self._mail_service is not None else _default_mail_service()
            folders = sorted({job["spec"]["trigger"]["folder"] for job in jobs})
            service.sync(folders)
        except Exception:  # noqa: BLE001 - not set up or unreachable: look again next time
            logger.info("mail triggers could not look at the mailbox", exc_info=True)
            return 0
        queued = 0
        for job in jobs:
            try:
                queued += self._look_at_folder_mail(service, job, now)
            except Exception:  # noqa: BLE001 - this folder could not be read now: look again next time
                logger.info("mail trigger %s could not look at its folder", job["id"], exc_info=True)
        return queued

    def _look_at_folder_mail(self, service: Any, job: dict, now: float) -> int:
        trigger = job["spec"]["trigger"]
        state = service.folder_state(trigger["folder"])
        cursor = self.store.trigger_cursor(job["id"])
        seen_validity, _, seen_uid = (cursor or "").partition(":")
        if state is None or not state.uidvalidity:
            return 0  # Never looked at successfully: no baseline yet, and nothing taken for new.
        validity = str(state.uidvalidity)
        if cursor is None or seen_validity != validity:
            if state.last_error_code:
                return 0  # Its last look failed: a baseline waits for one that succeeds.
            # The first look, or a mailbox that renumbered its messages: start from where it stands, empty or not.
            newest = service.newest_uid(trigger["folder"], uidvalidity=state.uidvalidity)
            return self.store.fire(job["id"], [], f"{validity}:{newest}", now=now)
        last, events = int(seen_uid or 0), []
        while len(events) < MAX_EVENTS:
            page = service.after(trigger["folder"], last, uidvalidity=state.uidvalidity, limit=MAIL_PAGE)
            for item in page:
                last = int(item["uid"])
                if _matches(item, trigger):
                    events.append({"type": "mail", "folder": item.get("folder") or trigger["folder"], "uid": last,
                                   "from": str(item.get("sender") or ""), "subject": str(item.get("subject") or "")})
                    if len(events) == MAX_EVENTS:
                        break
            if len(page) < MAIL_PAGE:
                break
        return self.store.fire(job["id"], events, f"{validity}:{last}", now=now)


def _default_mail_service() -> Any:
    from agent.mail163.config import load_mail_config
    from agent.mail163.service import MailService

    return MailService(load_mail_config(Path(__file__).resolve().parents[3]))
