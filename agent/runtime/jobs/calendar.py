"""Wall-clock schedules: a local time of day on chosen weekdays, in a named time zone."""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_OFFSET = re.compile(r"UTC([+-])(\d{2}):(\d{2})")


def local_timezone_name() -> str:
    """This computer's IANA zone, or its current UTC offset when no name is known."""
    candidates = [value.strip() for value in (os.getenv("AGENT_TIMEZONE", ""), os.getenv("TZ", "")) if value.strip()]
    # macOS and Linux name the zone through the /etc/localtime link.
    target = os.path.realpath("/etc/localtime")
    if "zoneinfo/" in target:
        candidates.append(target.split("zoneinfo/", 1)[1])
    for name in candidates:
        try:
            ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            continue
        return name
    offset = datetime.now().astimezone().utcoffset() or timedelta()
    minutes = int(offset.total_seconds() // 60)
    return f"UTC{'+' if minutes >= 0 else '-'}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"


def zone(name: str) -> tzinfo:
    match = _OFFSET.fullmatch(name)
    if match:
        sign = 1 if match[1] == "+" else -1
        return timezone(sign * timedelta(hours=int(match[2]), minutes=int(match[3])))
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"Unknown time zone: {name}.") from exc


def calendar_schedule(time_of_day: str, days: list[str] | tuple[str, ...] | None = None, tz: str = "") -> dict:
    """A validated schedule: {"time": "HH:MM", "days": [...], "tz": name}. No days means every day."""
    match = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", str(time_of_day).strip())
    if match is None:
        raise ValueError("The time of day must be HH:MM on a 24-hour clock.")
    chosen = {str(day).strip().lower()[:3] for day in (days or WEEKDAYS)}
    if not chosen or not chosen <= set(WEEKDAYS):
        raise ValueError("Days must be weekday names: " + ", ".join(WEEKDAYS) + ".")
    name = tz.strip() or local_timezone_name()
    zone(name)
    return {"time": f"{int(match[1]):02d}:{match[2]}", "days": [day for day in WEEKDAYS if day in chosen], "tz": name}


def next_slot(schedule: dict, after: float) -> float:
    """The first scheduled instant strictly after ``after``.

    A local time skipped by a daylight-saving change runs at the same offset as
    before the change (an hour later on the clock); a repeated one runs once.
    """
    place = zone(schedule["tz"])
    hour, minute = (int(part) for part in schedule["time"].split(":"))
    days = {WEEKDAYS.index(day) for day in schedule["days"]}
    start = datetime.fromtimestamp(after, place).date()
    for offset in range(8):
        day = start + timedelta(days=offset)
        if day.weekday() not in days:
            continue
        instant = datetime(day.year, day.month, day.day, hour, minute, tzinfo=place).timestamp()
        if instant > after:
            return instant
    raise ValueError("The schedule has no weekday.")


def describe(schedule: dict) -> str:
    days = schedule["days"]
    when = "every day" if len(days) == 7 else "weekdays" if days == list(WEEKDAYS[:5]) else ", ".join(days)
    return f"{when} at {schedule['time']} ({schedule['tz']})"
