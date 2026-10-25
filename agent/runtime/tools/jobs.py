"""Reminders and read-only checks the person asks for, scheduled by the model and approved by the person."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from agent.runtime.jobs.calendar import WEEKDAYS, calendar_schedule, describe, local_timezone_name, zone
from agent.runtime.jobs.store import NOTIFY_POLICIES, PUSH_TARGETS, JobStore
from agent.runtime.tools.registry import ToolDef, ToolRegistry

ACTIONS = ("add", "list", "update", "pause", "resume", "remove", "run")
# Listing and pausing only narrow what runs; everything else is shown to the person first.
_UNGUARDED = frozenset({"list", "pause"})
_SCHEDULE_KEYS = ("time", "days", "every_minutes", "at", "after_minutes")

DESCRIPTION = (
    "Schedule reminders and read-only checks that run later without the person, and manage them. Use only when "
    "the person asks for something at a time or on a schedule (remind me at 3pm, every morning, each Monday). "
    "A check runs in a fresh read-only session: workspace files, plus web search and fetch when web is true. It "
    "cannot write, send, browse with logins or use the computer. A reminder delivers its text as written. "
    "add, update, resume, remove and run are each shown to the person for approval. Times are the person's "
    "local time; schedule with time (+days), every_minutes, at, or after_minutes."
)

PARAMETERS = {"type": "object", "properties": {
    "action": {"type": "string", "enum": list(ACTIONS)},
    "job_id": {"type": "string", "description": "The job to change, from list."},
    "name": {"type": "string", "maxLength": 120, "description": "Short name the person will recognise."},
    "kind": {"type": "string", "enum": ["check", "reminder"], "default": "check"},
    "prompt": {"type": "string", "maxLength": 4000,
               "description": "A check: what to look at and what to report. A reminder: the text to deliver."},
    "time": {"type": "string", "description": "Local time of day, HH:MM on a 24-hour clock; repeats on days."},
    "days": {"type": "array", "items": {"type": "string", "enum": list(WEEKDAYS)},
             "description": "With time: the weekdays to run on. Omit for every day."},
    "every_minutes": {"type": "number", "minimum": 1, "description": "Repeat at this fixed interval."},
    "at": {"type": "string", "description": "One run (the first, with every_minutes) at YYYY-MM-DD HH:MM local "
                                            "time, or ISO 8601 with an offset."},
    "after_minutes": {"type": "number", "minimum": 1, "description": "One run this many minutes from now."},
    "notify": {"type": "string", "enum": sorted(NOTIFY_POLICIES),
               "description": "on_change: tell the person only when a check finds something new."},
    "web": {"type": "boolean", "description": "Let a check search and fetch web pages."},
    "targets": {"type": "array", "items": {"type": "string", "enum": list(PUSH_TARGETS)},
                "description": "Where results are pushed besides the inbox. Omit for both; [] for the inbox only."},
}, "required": ["action"]}


def _instant(text: str, now: float) -> float:
    value = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=zone(local_timezone_name()))
    instant = value.timestamp()
    if instant <= now:
        raise ValueError("The time 'at' has already passed.")
    return instant


def _schedule(args: dict, now: float) -> dict[str, Any] | None:
    """The schedule the arguments ask for, or None when they name none."""
    given = [key for key in _SCHEDULE_KEYS if args.get(key) not in (None, "", [])]
    if not given:
        return None
    if args.get("days") and not args.get("time"):
        raise ValueError("days applies to time.")
    if args.get("time"):
        if len(given) > (2 if args.get("days") else 1):
            raise ValueError("Use time (with days) on its own.")
        return {"calendar": calendar_schedule(str(args["time"]), args.get("days") or None)}
    if args.get("after_minutes"):
        if len(given) > 1:
            raise ValueError("Use after_minutes on its own.")
        return {"at": now + float(args["after_minutes"]) * 60}
    at = _instant(str(args["at"]), now) if args.get("at") else None
    if args.get("every_minutes"):
        interval = float(args["every_minutes"]) * 60
        return {"interval_seconds": interval, "at": at if at is not None else now + interval}
    return {"at": at}


def _local(instant: float, job_spec: dict) -> str:
    calendar = job_spec.get("calendar")
    place = zone(calendar["tz"]) if calendar else zone(local_timezone_name())
    return datetime.fromtimestamp(instant, place).strftime("%Y-%m-%d %H:%M %Z").strip()


def _when(job: dict) -> str:
    spec = job["spec"]
    if spec.get("calendar"):
        return describe(spec["calendar"])
    if job["interval_seconds"]:
        minutes = job["interval_seconds"] / 60
        return f"every {minutes:g} minutes from {_local(job['next_at'], spec)}"
    return f"once at {_local(job['next_at'], spec)}"


_TARGET_NAMES = {"desktop": "桌面通知", "weixin": "微信"}


def _where(targets) -> str:
    names = [_TARGET_NAMES[target] for target in targets]
    return ("发到" + "和".join(names) + "，收件箱里也有一份") if names else "只放进收件箱"


def _summary(job: dict) -> dict[str, Any]:
    spec = job["spec"]
    return {"job_id": job["id"], "name": spec["name"], "kind": spec["kind"], "state": job["state"],
            "paused_reason": job.get("paused_reason") or "", "schedule": _when(job),
            "next_run": _local(job["next_at"], spec) if job["state"] == "enabled" else "",
            "notify": spec.get("notify", "always"), "web": spec.get("web", False),
            "targets": spec.get("targets", list(PUSH_TARGETS)),
            "last_outcome": job.get("last_outcome") or "", "prompt": spec["prompt"]}


def _zh(calendar: dict | None, interval: float, first: float | None) -> str:
    """The schedule as the person reads it on the approval card."""
    if calendar:
        days = calendar["days"]
        names = "一二三四五六日"
        when = ("每天" if len(days) == 7 else "工作日" if days == list(WEEKDAYS[:5]) else "周末" if days == ["sat", "sun"]
                else "每周" + "、".join(names[WEEKDAYS.index(day)] for day in days))
        return f"{when} {calendar['time']}（{calendar['tz']}）"
    start = _local(first, {}) if first else ""
    if interval:
        return f"每 {interval / 60:g} 分钟，从 {start} 开始"
    return f"{start} 运行一次" if start else ""


def _plan(args: dict, now: float) -> str:
    """What a schedule would read as before it exists, for the approval card."""
    schedule = _schedule(args, now) or {}
    return _zh(schedule.get("calendar"), schedule.get("interval_seconds", 0), schedule.get("at"))


def register_jobs_tool(registry: ToolRegistry, *, home: Path, workdir: Callable[[], Path],
                       request: Callable[[], str], model_key: Callable[[], str]) -> None:
    """``request`` returns the person's own latest message: the authority a job records."""

    def store() -> JobStore:
        return JobStore(home)

    def validate(args: dict) -> None:
        action = args.get("action")
        if action not in ACTIONS:
            raise ValueError("Unknown action.")
        now = time.time()
        if action == "add":
            if not str(args.get("name") or "").strip() or not str(args.get("prompt") or "").strip():
                raise ValueError("add needs a name and a prompt.")
            if _schedule(args, now) is None:
                raise ValueError("add needs a schedule: time (+days), every_minutes, at, or after_minutes.")
            if args.get("kind") == "reminder" and args.get("web"):
                raise ValueError("A reminder does not use the web.")
        elif action != "list":
            if not args.get("job_id"):
                raise ValueError(f"{action} needs a job_id from list.")
            store().job(str(args["job_id"]))
            if action == "update":
                _schedule(args, now)

    def permission(args: dict) -> dict[str, Any] | None:
        action = str(args.get("action") or "")
        if action in _UNGUARDED:
            return None
        now = time.time()
        if action == "add":
            kind = args.get("kind") or "check"
            reads = ("一次只读检查：工作区文件" + ("和联网搜索" if args.get("web") else "")) if kind == "check" else "一条提醒"
            title = f"新建定时任务：{str(args.get('name') or '').strip()}"
            target = f"{_plan(args, now)}；{reads}"
            where = _where(args["targets"] if args.get("targets") is not None else PUSH_TARGETS)
            effect = f"到点在本机运行{reads}，结果{where}。不会写入文件、替你发消息或操作电脑。"
            question = "要创建这个定时任务吗？"
        else:
            job = store().job(str(args.get("job_id") or ""))
            name = job["spec"]["name"]
            target = (_plan(args, now) if action == "update" else "") or _zh(
                job["spec"].get("calendar"), job["interval_seconds"], job["next_at"])
            title, effect, question = {
                "update": (f"修改定时任务：{name}", "之后的运行按新的设置进行" + (
                    f"，结果{_where(args['targets'])}" if args.get("targets") is not None else "") + "。", "要这样修改吗？"),
                "resume": (f"恢复定时任务：{name}", "从下一个时间点起重新运行；暂停期间错过的不补跑。", "要恢复吗？"),
                "remove": (f"删除定时任务：{name}", "以后不再运行；历史结果保留。", "要删除吗？"),
                "run": (f"立即运行定时任务：{name}", "调度器下一次检查时额外运行一次，不改变原来的时间表。", "要现在运行一次吗？"),
            }[action]
        # Each change is its own decision: no "allow for this session".
        return {"reason": title, "approval_title": title, "target": target, "approval_summary": target,
                "approval_effect": effect, "approval_question": question, "choices": ["once", "deny"]}

    async def jobs(action: str, job_id: str = "", name: str = "", kind: str = "check", prompt: str = "",
                   time: str = "", days: list[str] | None = None, every_minutes: float | None = None,
                   at: str = "", after_minutes: float | None = None, notify: str | None = None,
                   web: bool | None = None, targets: list[str] | None = None) -> str:
        args = {"time": time, "days": days, "every_minutes": every_minutes, "at": at, "after_minutes": after_minutes}
        jobs_store = store()
        now = _now()
        if action == "list":
            return json.dumps({"jobs": [_summary(job) for job in jobs_store.jobs() if job["state"] != "removed"]},
                              ensure_ascii=False)
        authority = request().strip()
        if action not in _UNGUARDED and not authority:
            raise ValueError("Scheduling needs the person's own request in this conversation.")
        if action == "add":
            schedule = _schedule(args, now) or {}
            check = kind != "reminder"
            interval = schedule.get("interval_seconds", 0)
            job = jobs_store.add(
                name=name, prompt=prompt, original_request=authority[:8000], workdir=workdir(),
                next_at=schedule.get("at"), interval_seconds=interval,
                calendar=schedule.get("calendar"), notify=notify or "always",
                targets=PUSH_TARGETS if targets is None else targets,
                # The same catch-up windows as `astra jobs add`.
                grace_seconds=3600 if schedule.get("calendar") else min(interval / 2, 3600) if interval else 300,
                kind="check" if check else "reminder", model=model_key() if check else "",
                web=bool(web) if check else False, now=now)
        elif action == "update":
            schedule = _schedule(args, now) or {}
            job = jobs_store.update(
                job_id, request=authority, name=name or None, prompt=prompt or None,
                interval_seconds=schedule.get("interval_seconds", 0) if schedule else None,
                calendar=schedule.get("calendar"), at=schedule.get("at"), notify=notify, web=web,
                targets=targets, now=now)
        elif action == "run":
            job = jobs_store.request_run(job_id, now=now)
        else:
            job = jobs_store.set_state(job_id, {"pause": "paused", "resume": "enabled", "remove": "removed"}[action],
                                       now=now)
        return json.dumps({"job": _summary(job), "delivery": (
            "Results are pushed to the listed targets and kept in the Astra inbox (/jobs inbox). Jobs run while "
            "the Astra desktop app is open, or while `astra jobs serve` runs.")}, ensure_ascii=False)

    registry.register(ToolDef(
        name="jobs", description=DESCRIPTION, parameters=PARAMETERS, fn=jobs, risk="write", group="schedule",
        cache_results=False, argument_validator=validate, permission_check=permission,
        permission_authoritative=True, permission_grant=lambda _args, _request, _decision: None,
    ))


def _now() -> float:
    return time.time()
