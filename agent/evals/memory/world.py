"""Seed one scenario's world through Astra's own storage APIs.

Core entries go through ``MemoryStore.add_core``, records through
``MemoryStore.add_record``/``supersede_record`` and the archive through
``SessionRecall``'s session/logging methods. Those APIs stamp the wall clock,
so seeding runs under a simulated clock that places every write at its
scenario time (relative to the run's local date) without editing rows later.
"""

from __future__ import annotations

import contextlib
import time
import zlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from .fixture import ScenarioSpec

OTHER_WORKSPACE_KEY = "/workspaces/another-project"


@dataclass(frozen=True)
class SeededItem:
    ref: str
    kind: str  # "record" or "message"
    content: str
    fragment: str
    timestamp: float
    identity: str
    status: str = "active"
    role: str = ""

    def local_date(self) -> str:
        return datetime.fromtimestamp(self.timestamp).astimezone().date().isoformat()


@dataclass
class SeededWorld:
    scenario: ScenarioSpec
    now: datetime
    items: dict[str, SeededItem] = field(default_factory=dict)
    core_entries: tuple[str, ...] = ()
    archive_sessions: dict[str, str] = field(default_factory=dict)
    archive_dates: dict[str, str] = field(default_factory=dict)
    workspace_key: str = ""
    workspace_root: str = ""


def scenario_time(now: datetime, days_ago: int, at: str) -> datetime:
    hour, minute = (int(part) for part in at.split(":"))
    local_now = now.astimezone()
    day = local_now.date() - timedelta(days=days_ago)
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=local_now.tzinfo)


@contextlib.contextmanager
def simulated_clock(moment: datetime) -> Iterator[None]:
    """Make the storage APIs stamp ``moment`` instead of the wall clock."""
    from agent.runtime import memory_records, session_recall

    fake_time = SimpleNamespace(time=lambda: moment.timestamp(), sleep=time.sleep)
    iso = moment.astimezone(timezone.utc).isoformat()
    with mock.patch.object(session_recall, "time", fake_time), mock.patch.object(memory_records, "_now", lambda: iso):
        yield


def archive_session_key(scenario_id: str, session_key: str, started: datetime) -> str:
    suffix = zlib.crc32(f"{scenario_id}:{session_key}".encode()) % 90_000 + 10_000
    return f"session_{started:%Y%m%d_%H%M%S}_{suffix}"


def seed_world(
    scenario: ScenarioSpec, *, now: datetime, workspace: Path, without: tuple[str, ...] = (),
) -> SeededWorld:
    """Seed core files, the archive and records (except ``without``) into the configured state paths."""
    from agent.runtime.context_index.workspace import resolve_workspace
    from agent.runtime.memory import MemoryStore
    from agent.runtime.session_recall import SessionRecall

    identity = resolve_workspace(Path(workspace))
    world = SeededWorld(scenario=scenario, now=now, workspace_key=identity.key, workspace_root=identity.root)
    items = scenario.item_contents()
    store = MemoryStore()
    for content in scenario.core_user:
        store.add_core("user", content)
    for content in scenario.core_memory:
        store.add_core("memory", content)
    world.core_entries = tuple(item["content"] for scope in ("memory", "user") for item in store.list_core(scope))

    recall = SessionRecall()
    recall.init_db()
    message_ids: dict[str, int] = {}
    try:
        for session in sorted(scenario.archive, key=lambda item: scenario_time(now, item.days_ago, item.at)):
            started = scenario_time(now, session.days_ago, session.at)
            key = archive_session_key(scenario.id, session.key, started)
            other = session.workspace == "other"
            with simulated_clock(started):
                session_id = recall.get_or_create_session(
                    key, title=key,
                    workspace_key=OTHER_WORKSPACE_KEY if other else identity.key,
                    workspace_root=OTHER_WORKSPACE_KEY if other else identity.root,
                )
            world.archive_sessions[session.key] = session_id
            world.archive_dates[session.key] = started.date().isoformat()
            moment = started
            for index, message in enumerate(session.messages):
                moment = started + timedelta(seconds=75 * index)
                with simulated_clock(moment):
                    message_id = recall.log_message(session_id, message.role, message.content)
                ref = f"msg:{session.key}.{message.key}"
                message_ids[ref] = int(message_id)
                world.items[ref] = SeededItem(
                    ref, "message", message.content, items[ref][1], moment.timestamp(), str(message_id),
                    role=message.role,
                )
            with simulated_clock(moment + timedelta(minutes=2)):
                recall.close_session(session_id)
    finally:
        recall.close()

    record_ids: dict[str, str] = {}
    for record in sorted(scenario.records, key=lambda item: scenario_time(now, item.days_ago, item.at)):
        if record.key in without:
            continue
        moment = scenario_time(now, record.days_ago, record.at)
        source_session = source_message = ""
        if record.source:
            session_key = record.source.split(".", 1)[0]
            source_session = archive_session_key(
                scenario.id, session_key,
                scenario_time(now, *next((s.days_ago, s.at) for s in scenario.archive if s.key == session_key)),
            )
            source_message = str(message_ids[f"msg:{record.source}"])
        with simulated_clock(moment):
            if record.supersedes:
                saved = store.supersede_record(
                    record_ids[record.supersedes], content=record.content,
                    source_session_id=source_session, source_message_id=source_message,
                    metadata={"retained_by": "explicit-user-correction", **dict(record.metadata)},
                )
            else:
                saved = store.add_record(
                    kind=record.kind, content=record.content,
                    source_session_id=source_session, source_message_id=source_message,
                    salience=record.salience, tags=record.tags,
                    metadata={"retained_by": "explicit-user-statement", **dict(record.metadata)},
                )
        record_ids[record.key] = saved.record_id
        world.items[record.ref] = SeededItem(
            record.ref, "record", record.content, items[record.ref][1], moment.timestamp(), saved.record_id,
        )
    for record in scenario.records:
        if record.key in without:
            continue
        stored = store.record_store.get(record_ids[record.key])
        if stored is not None and stored.status != "active":
            item = world.items[record.ref]
            world.items[record.ref] = SeededItem(
                item.ref, item.kind, item.content, item.fragment, item.timestamp, item.identity, status=stored.status,
            )
    return world


__all__ = ["SeededItem", "SeededWorld", "scenario_time", "seed_world", "simulated_clock"]
