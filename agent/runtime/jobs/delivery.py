"""Pushes of scheduled-job results to where the person is: a desktop notification and WeChat.

The local inbox always has every result; a push only tells the person. Each push is tried a
few times with growing pauses, then given up; one older than ``MAX_AGE`` is not sent at all.
Check results and notices wait out the quiet hours. A reminder is sent when it was set for.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from .calendar import quiet_hours, quiet_until
from .store import ROUNDS_USED_UP, JobStore

logger = logging.getLogger(__name__)

RETRY_SECONDS = (60, 300, 900, 1800)
MAX_AGE = 12 * 3600
TEXT_LIMIT = 1800
INBOX_NOTE = "…（完整结果在 Astra 的定时任务收件箱里）"

DesktopNotify = Callable[[dict], None]
# Sends one text to the person's WeChat and keeps it in that chat's conversation.
WeixinPush = Callable[[str], Awaitable[None]]


def push_message(push: dict) -> tuple[str, str]:
    """The title and text the person reads for one result."""
    spec = push["spec"]
    name = spec["name"]
    state = push["run_state"]
    if state == "completed":
        title = f"提醒：{name}" if spec["kind"] == "reminder" and not spec.get("trigger") else f"定时任务：{name}"
        if push["detail"] == ROUNDS_USED_UP:
            title += "（轮数用完，可能不全）"
        body = push["output"].strip()
    elif state == "skipped":
        title = f"定时任务已暂停：{name}"
        body = ("你已经 7 天没找我了，我先把它暂停了。要继续就跟我说一声。"
                if push["paused_reason"] == "inactive" else push["detail"])
    else:
        reason = push["detail"].strip() or state
        if push["job_state"] == "paused" and push["paused_reason"] == "failures" and reason.startswith("Paused after"):
            title = f"定时任务已暂停：{name}"
            body = f"连续 3 次没能完成，我先暂停了。最近一次的情况：{reason.split('. ', 1)[-1].strip() or state}"
        else:
            title = f"定时任务没能完成：{name}"
            body = f"这次没能完成：{reason}"
    if len(body) > TEXT_LIMIT:
        body = body[:TEXT_LIMIT].rstrip() + INBOX_NOTE
    return title, body


class JobDelivery:
    """Sends due pushes. ``weixin`` is None when WeChat is not set up here."""

    def __init__(self, store: JobStore, *, desktop: DesktopNotify | None, weixin: WeixinPush | None):
        self.store = store
        self.desktop = desktop
        self.weixin = weixin

    def _home(self) -> Path:
        return self.store.home

    async def deliver_due(self, *, now: float | None = None) -> int:
        """Send what is due; return how many pushes were sent."""
        now = time.time() if now is None else now
        due = await asyncio.to_thread(self.store.due_pushes, now=now)
        if not due:
            return 0
        quiet = quiet_until(await asyncio.to_thread(quiet_hours, self._home()), now)
        sent = 0
        for push in due:
            run_id, target = push["run_id"], push["target"]
            # Measured from when the run started: a day-old result is no longer news.
            if now - push["created_at"] > MAX_AGE:
                await asyncio.to_thread(self.store.mark_push, run_id, target, "failed", detail="expired", now=now)
                continue
            # A reminder set for a time is sent then; one started by mail or a file waits like a check result.
            timed = (push["spec"]["kind"] == "reminder" and push["run_state"] == "completed"
                     and not push["spec"].get("trigger"))
            if quiet is not None and not timed:
                await asyncio.to_thread(self.store.mark_push, run_id, target, "pending", next_try=quiet,
                                        attempted=False, detail="quiet hours", now=now)
                continue
            title, body = push_message(push)
            try:
                if target == "desktop":
                    if self.desktop is None:
                        raise LookupError("no desktop is attached")
                    self.desktop({"type": "job_notification", "title": title, "body": body[:300],
                                  "job_id": push["job_id"], "run_id": run_id})
                elif self.weixin is None:
                    raise LookupError("WeChat is not set up")
                else:
                    await self.weixin(f"【{title}】\n{body}")
            except LookupError as exc:
                await asyncio.to_thread(self.store.mark_push, run_id, target, "failed", detail=str(exc), now=now)
                continue
            except Exception as exc:
                attempt = push["attempts"] + 1
                logger.warning("job push to %s failed (attempt %s): %s", target, attempt, type(exc).__name__)
                if attempt > len(RETRY_SECONDS):
                    await asyncio.to_thread(self.store.mark_push, run_id, target, "failed",
                                            detail=f"{type(exc).__name__}: {exc}", now=now)
                else:
                    await asyncio.to_thread(self.store.mark_push, run_id, target, "pending",
                                            next_try=now + RETRY_SECONDS[attempt - 1],
                                            detail=f"{type(exc).__name__}: {exc}", now=now)
                continue
            await asyncio.to_thread(self.store.mark_push, run_id, target, "sent", now=now)
            sent += 1
        return sent
