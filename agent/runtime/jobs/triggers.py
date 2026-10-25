"""Event-started jobs: new mail that matches a filter, or a new file in a folder.

The background host looks every few minutes. A trigger's first look only notes where things stand, so
mail and files that were already there never start a run. Each look queues at most ``MAX_EVENTS``
runs per job, oldest first, and moves the job's cursor past what it saw in the same transaction.
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
        since = float(cursor)
        fresh = sorted((when, entry) for when, entry in files if when > since)[-MAX_EVENTS:]
        newest = max([since, *(when for when, _ in fresh)])
        events = [{"type": "file", "path": str(entry), "name": entry.name} for _, entry in fresh]
        return self.store.fire(job["id"], events, repr(newest), now=now)

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
            trigger = job["spec"]["trigger"]
            try:
                items = list(service.recent(folder=trigger["folder"], limit=50, refresh=False)["items"])
            except Exception:  # noqa: BLE001
                continue
            if not items:
                continue
            validity = str(items[0].get("uidvalidity"))
            newest = max(int(item["uid"]) for item in items)
            cursor = self.store.trigger_cursor(job["id"])
            seen_validity, _, seen_uid = (cursor or "").partition(":")
            if cursor is None or seen_validity != validity:
                # The first look, or a mailbox that renumbered its messages: start from here.
                queued += self.store.fire(job["id"], [], f"{validity}:{newest}", now=now)
                continue
            fresh = sorted((item for item in items if int(item["uid"]) > int(seen_uid or 0) and _matches(item, trigger)),
                           key=lambda item: int(item["uid"]))[-MAX_EVENTS:]
            events = [{"type": "mail", "folder": item.get("folder") or trigger["folder"], "uid": int(item["uid"]),
                       "from": str(item.get("sender") or ""), "subject": str(item.get("subject") or "")} for item in fresh]
            queued += self.store.fire(job["id"], events, f"{validity}:{max(newest, int(seen_uid or 0))}", now=now)
        return queued


def _default_mail_service() -> Any:
    from agent.mail163.config import load_mail_config
    from agent.mail163.service import MailService

    return MailService(load_mail_config(Path(__file__).resolve().parents[3]))
