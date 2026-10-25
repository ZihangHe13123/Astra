"""Wall-clock schedules: a local time of day on chosen weekdays, in a named time zone."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from agent.runtime.json_preferences import update_preferences

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


# Pushes of check results wait out these hours; the inbox and reminders the person timed do not.
DEFAULT_QUIET_HOURS = {"start": "23:00", "end": "08:00"}
PREFERENCES_FILE = "preferences.json"


def _minutes(value: str) -> int:
    match = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", str(value).strip())
    if match is None:
        raise ValueError("Quiet hours are HH:MM on a 24-hour clock.")
    return int(match[1]) * 60 + int(match[2])


def quiet_hours(home: Path) -> dict | None:
    """The person's quiet hours ({"start", "end"}), or None when they switched them off."""
    try:
        data = json.loads((Path(home) / "jobs" / PREFERENCES_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict(DEFAULT_QUIET_HOURS)
    if not isinstance(data, dict) or "quiet_hours" not in data:
        return dict(DEFAULT_QUIET_HOURS)
    window = data["quiet_hours"]
    try:
        return None if window is None else {"start": _clock(window["start"]), "end": _clock(window["end"])}
    except (KeyError, TypeError, ValueError):
        return dict(DEFAULT_QUIET_HOURS)


def _clock(value: str) -> str:
    minutes = _minutes(value)
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def set_quiet_hours(home: Path, window: dict | None) -> dict | None:
    if window is not None:
        window = {"start": _clock(window["start"]), "end": _clock(window["end"])}
    update_preferences(Path(home) / "jobs" / PREFERENCES_FILE, lambda data: data.__setitem__("quiet_hours", window))
    return quiet_hours(home)


def quiet_until(window: dict | None, now: float, tz: str = "") -> float | None:
    """When the quiet hours around ``now`` end, or None outside them."""
    if not window:
        return None
    start, end = _minutes(window["start"]), _minutes(window["end"])
    local = datetime.fromtimestamp(now, zone(tz or local_timezone_name()))
    minute = local.hour * 60 + local.minute
    inside = start <= minute < end if start < end else start != end and (minute >= start or minute < end)
    if not inside:
        return None
    day = local.date() if minute < end else local.date() + timedelta(days=1)
    return datetime(day.year, day.month, day.day, end // 60, end % 60, tzinfo=local.tzinfo).timestamp()
