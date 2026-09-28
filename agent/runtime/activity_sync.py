"""Incremental, local-only synchronization of Codex Computer History events."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

if sys.platform == "win32":
    fcntl = None
else:
    try:
        import fcntl
    except ImportError:  # pragma: no cover - exercised by a clean subprocess startup test.
        fcntl = None

from agent.runtime.activity_store import ActivityEvent, ActivityStore, ActivitySummary, SourceCursor, is_valid_timestamp

_MAX_SELECTION_DEPTH = 6
_MAX_SELECTION_VALUES = 64
_MAX_SELECTION_CHARS = 4096
_MAX_EVENT_ID = 2**63 - 1
_MAX_EVENT_DEPTH = 256
_SUMMARY_FILENAME = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2})-"
    r"[A-Za-z0-9]+-(?P<granularity>10min|6h)-.+\.md$"
)


class SourceDiscoveryError(RuntimeError):
    """Raised when the local Computer History archive cannot be identified."""


@dataclass(frozen=True)
class SourceRoots:
    event_root: Path
    summary_root: Path


@dataclass(frozen=True)
class SyncReport:
    status: str
    events_imported: int = 0
    events_duplicate: int = 0
    summaries_imported: int = 0
    summaries_ignored: int = 0
    malformed_lines: int = 0
    deferred_lines: int = 0
    already_running: bool = False
    source_active: bool = False
    error: str = ""


def _imported_timestamp(imported_at: str | None) -> str:
    return imported_at or datetime.now(timezone.utc).isoformat()


def resolve_source_roots(
    *,
    event_root: Path | None = None,
    summary_root: Path | None = None,
    group_container_root: Path | None = None,
    candidates: Sequence[Path] | None = None,
) -> SourceRoots:
    """Resolve the one permitted local Computer History archive source."""
    configured_event_root = event_root or _environment_path("ASTRA_COMPUTER_HISTORY_ROOT")
    resolved_summary_root = (summary_root or (
        Path.home() / ".codex" / "memories" / "extensions" / "skysight" / "resources"
    )).expanduser()
    if configured_event_root is not None:
        return SourceRoots(configured_event_root.expanduser(), resolved_summary_root)

    if candidates is None:
        group_root = (group_container_root or (Path.home() / "Library" / "Group Containers")).expanduser()
        source_candidates = sorted(group_root.glob("*/Library/Caches/ComputerUse/Skysight"))
    else:
        source_candidates = sorted(candidate.expanduser() for candidate in candidates)
    if len(source_candidates) == 1:
        return SourceRoots(source_candidates[0], resolved_summary_root)
    if len(source_candidates) > 1:
        raise SourceDiscoveryError("multiple local Computer History sources found")
    raise SourceDiscoveryError("no local Computer History source found")


def _environment_path(name: str) -> Path | None:
    configured = os.getenv(name, "").strip()
    return Path(configured) if configured else None


def _source_activity_paths(roots: SourceRoots) -> list[Path]:
    if not roots.event_root.is_dir():
        return []
    try:
        return sorted({*roots.event_root.rglob("events.jsonl"), *roots.event_root.rglob("metadata.json")})
    except OSError:
        return []


def source_is_recent(
    roots: SourceRoots,
    *,
    now: datetime | None = None,
    max_age_seconds: int = 900,
) -> bool:
    """Return whether a local archive source was updated within the inclusive age limit."""
    source_paths = _source_activity_paths(roots)
    if not source_paths:
        return False
    try:
        newest_mtime = max(path.stat().st_mtime for path in source_paths)
    except OSError:
        return False
    timestamp = (now or datetime.now(timezone.utc)).timestamp()
    return timestamp - newest_mtime <= max_age_seconds


def _string_field(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _selection_strings(value: Any, *, depth: int = 0) -> Iterator[str]:
    if depth > _MAX_SELECTION_DEPTH:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _selection_strings(item, depth=depth + 1)
    elif isinstance(value, list):
        for item in value:
            yield from _selection_strings(item, depth=depth + 1)


def _selection_text(value: Any) -> str:
    selected: list[str] = []
    total = 0
    for item in _selection_strings(value):
        if len(selected) >= _MAX_SELECTION_VALUES or total >= _MAX_SELECTION_CHARS:
            break
        separator_chars = 1 if selected else 0
        remaining = _MAX_SELECTION_CHARS - total - separator_chars
        if remaining <= 0:
            break
        bounded = item[:remaining]
        if bounded:
            selected.append(bounded)
            total += separator_chars + len(bounded)
    return " ".join(selected)


def _nesting_exceeds(value: Any, limit: int) -> bool:
    """Check nesting iteratively; json.loads' own depth limit varies by Python version and recursion limit."""
    pending = [(value, 1)]
    while pending:
        item, depth = pending.pop()
        if isinstance(item, (dict, list)):
            if depth > limit:
                return True
            children = item.values() if isinstance(item, dict) else item
            pending.extend((child, depth + 1) for child in children)
    return False


def normalize_event(segment_id: str, payload: dict[str, Any], imported_at: str | None = None) -> ActivityEvent | None:
    """Normalize the stable searchable fields while preserving raw local evidence."""
    if not isinstance(payload, dict):
        return None
    event_id = payload.get("id")
    occurred_at = payload.get("timestamp")
    if (
        isinstance(event_id, bool)
        or not isinstance(event_id, int)
        or event_id <= 0
        or event_id > _MAX_EVENT_ID
    ):
        return None
    if not isinstance(occurred_at, str) or not is_valid_timestamp(occurred_at):
        return None
    if _nesting_exceeds(payload, _MAX_EVENT_DEPTH):
        return None

    app = payload.get("app")
    window = payload.get("window")
    app_data = app if isinstance(app, dict) else {}
    window_data = window if isinstance(window, dict) else {}
    app_name = _string_field(app_data.get("name"))
    bundle_id = _string_field(app_data.get("bundleIdentifier"))
    window_title = _string_field(window_data.get("title"))
    url = _string_field(window_data.get("url"))
    selection_text = _selection_text(payload.get("selection"))
    searchable_text = " ".join(part for part in (app_name, bundle_id, window_title, selection_text) if part)
    return ActivityEvent(
        segment_id=segment_id,
        event_id=event_id,
        occurred_at=occurred_at,
        kind=_string_field(payload.get("kind")),
        app_name=app_name,
        bundle_id=bundle_id,
        window_title=window_title,
        url=url,
        selection_text=selection_text,
        searchable_text=searchable_text,
        raw_json=json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
        imported_at=_imported_timestamp(imported_at),
    )


class ActivitySynchronizer:
    def __init__(self, store: ActivityStore, roots: SourceRoots, lock_path: Path):
        self.store = store
        self.roots = roots
        self.lock_path = lock_path

    def sync_events(self) -> SyncReport:
        return self._run_locked(self._sync_events_unlocked)

    def _run_locked(self, operation: Callable[[], SyncReport]) -> SyncReport:
        if fcntl is None:
            return SyncReport(status="source_unavailable", error="unsupported_platform")
        self.lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock_path.parent.chmod(0o700)
        with self.lock_path.open("a+") as lock_handle:
            try:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return SyncReport(status="ok", already_running=True)
            try:
                integrity_error = self._integrity_error()
                if integrity_error is not None:
                    return integrity_error
                return operation()
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

    def _sync_events_unlocked(self) -> SyncReport:
        return self._sync_locked()

    def sync_summaries(self) -> SyncReport:
        return self._run_locked(self._sync_summaries_unlocked)

    def _sync_summaries_unlocked(self) -> SyncReport:
        if not self.roots.summary_root.is_dir():
            return SyncReport(status="source_unavailable", error="summary_root_unavailable")

        imported = ignored = 0
        source_active = False
        for path in sorted(self.roots.summary_root.glob("*.md")):
            summary = _parse_summary(path)
            if summary is None:
                ignored += 1
                continue
            source_active = True
            try:
                content_bytes = path.read_bytes()
                content = content_bytes.decode("utf-8")
                stat = path.stat()
            except (OSError, UnicodeDecodeError) as exc:
                self.store.record_source_error(str(path), "summary_read_error", type(exc).__name__)
                return SyncReport(
                    status="partial", summaries_imported=imported, summaries_ignored=ignored,
                    source_active=source_active, error="summary_read_error",
                )
            existing = self.store.get_summary_by_source(str(path.resolve()))
            if existing is not None and existing.content_hash == hashlib.sha256(content_bytes).hexdigest():
                continue
            stored = ActivitySummary(
                summary_id=summary.summary_id,
                source_path=summary.source_path,
                granularity=summary.granularity,
                period_start=summary.period_start,
                period_end=summary.period_end,
                content=content,
                content_hash=hashlib.sha256(content_bytes).hexdigest(),
                source_mtime_ns=stat.st_mtime_ns,
                imported_at=_imported_timestamp(None),
            )
            imported += int(self.store.upsert_summary(stored))
        return SyncReport(
            status="ok", summaries_imported=imported, summaries_ignored=ignored,
            source_active=source_active,
        )

    def sync(self, force: bool = False) -> SyncReport:
        if not force and not source_is_recent(self.roots):
            return SyncReport(status="inactive")
        return self._run_locked(self._sync_all_unlocked)

    def _sync_all_unlocked(self) -> SyncReport:
        event_report = self._sync_events_unlocked()
        summary_report = self._sync_summaries_unlocked()
        return SyncReport(
            status="ok" if event_report.status == summary_report.status == "ok" else "partial",
            events_imported=event_report.events_imported,
            events_duplicate=event_report.events_duplicate,
            summaries_imported=summary_report.summaries_imported,
            summaries_ignored=summary_report.summaries_ignored,
            malformed_lines=event_report.malformed_lines,
            deferred_lines=event_report.deferred_lines,
            source_active=event_report.source_active or summary_report.source_active,
            error=event_report.error or summary_report.error,
        )

    def _integrity_error(self) -> SyncReport | None:
        try:
            status = self.store.integrity_status()
        except sqlite3.DatabaseError:
            return SyncReport(status="integrity_error", error="integrity_error")
        if status.get("integrity_status") != "ok" or status.get("fts_status") != "ok":
            return SyncReport(status="integrity_error", error="integrity_error")
        return None

    def _sync_locked(self) -> SyncReport:
        if not self.roots.event_root.is_dir():
            return SyncReport(status="source_unavailable", error="event_root_unavailable")

        imported = duplicates = malformed = deferred = 0
        event_paths = sorted(self.roots.event_root.glob("segments/*/events.jsonl"))
        for path in event_paths:
            try:
                file_report = self._sync_file(path)
            except OSError as exc:
                self.store.record_source_error(str(path), "source_read_error", type(exc).__name__)
                return SyncReport(
                    status="partial",
                    events_imported=imported,
                    events_duplicate=duplicates,
                    malformed_lines=malformed,
                    deferred_lines=deferred,
                    source_active=True,
                    error="source_read_error",
                )
            imported += file_report.events_imported
            duplicates += file_report.events_duplicate
            malformed += file_report.malformed_lines
            deferred += file_report.deferred_lines
        return SyncReport(
            status="ok",
            events_imported=imported,
            events_duplicate=duplicates,
            malformed_lines=malformed,
            deferred_lines=deferred,
            source_active=bool(event_paths),
        )

    def _sync_file(self, path: Path) -> SyncReport:
        stat = path.stat()
        identity = f"{stat.st_dev}:{stat.st_ino}"
        previous = self.store.get_cursor(str(path))
        offset = previous.byte_offset if previous else 0
        reset_required = bool(previous and (
            previous.source_identity != identity
            or stat.st_size < offset
            or stat.st_size < previous.observed_size
        ))
        if reset_required:
            offset = 0

        with path.open("rb") as handle:
            handle.seek(offset)
            chunk = handle.read()
        complete_length = chunk.rfind(b"\n") + 1
        complete, trailing = chunk[:complete_length], chunk[complete_length:]
        if not complete:
            if reset_required:
                self.store.add_event_batch([], SourceCursor(
                    source_path=str(path),
                    source_kind="events",
                    source_identity=identity,
                    byte_offset=0,
                    observed_size=stat.st_size,
                    observed_mtime_ns=stat.st_mtime_ns,
                    last_success_at=_imported_timestamp(None),
                    last_error="",
                ))
            return SyncReport(status="ok", deferred_lines=1 if trailing else 0, source_active=True)

        events: list[ActivityEvent] = []
        malformed = 0
        last_error = ""
        line_offset = offset
        imported_at = _imported_timestamp(None)
        for raw_line in complete.splitlines(keepends=True):
            line_length = len(raw_line)
            line = raw_line[:-1]
            try:
                decoded = line.decode("utf-8")
                payload = json.loads(decoded)
                if not isinstance(payload, dict):
                    raise ValueError("non_object")
                event = normalize_event(path.parent.name, payload, imported_at)
                if event is None:
                    raise ValueError("invalid_event")
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError, OverflowError) as exc:
                malformed += 1
                last_error = f"{type(exc).__name__}:{line_offset}"
            else:
                events.append(event)
            line_offset += line_length

        cursor = SourceCursor(
            source_path=str(path),
            source_kind="events",
            source_identity=identity,
            byte_offset=offset + complete_length,
            observed_size=stat.st_size,
            observed_mtime_ns=stat.st_mtime_ns,
            last_success_at=_imported_timestamp(None),
            last_error=last_error,
        )
        inserted = self.store.add_event_batch(events, cursor)
        return SyncReport(
            status="ok",
            events_imported=inserted,
            events_duplicate=len(events) - inserted,
            malformed_lines=malformed,
            deferred_lines=1 if trailing else 0,
            source_active=True,
        )


def _parse_summary(path: Path) -> ActivitySummary | None:
    match = _SUMMARY_FILENAME.fullmatch(path.name)
    if match is None:
        return None
    try:
        period_start = datetime.strptime(match.group("timestamp"), "%Y-%m-%dT%H-%M-%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    granularity = match.group("granularity")
    period_end = period_start + (timedelta(minutes=10) if granularity == "10min" else timedelta(hours=6))
    absolute_path = path.resolve()
    return ActivitySummary(
        summary_id=hashlib.sha256(str(absolute_path).encode("utf-8")).hexdigest(),
        source_path=str(absolute_path),
        granularity=granularity,
        period_start=period_start.isoformat(),
        period_end=period_end.isoformat(),
        content="",
        content_hash="",
        source_mtime_ns=0,
        imported_at="",
    )


def run_scheduled_sync(
    roots: SourceRoots,
    *,
    store_factory: Callable[[], ActivityStore] = ActivityStore,
    now: datetime | None = None,
) -> SyncReport:
    """Synchronize only a recently active local archive without opening storage otherwise."""
    if not source_is_recent(roots, now=now):
        return SyncReport(status="inactive")
    store = store_factory()
    try:
        lock_path = activity_sync_lock_path(store.path)
        return ActivitySynchronizer(store, roots, lock_path).sync(force=True)
    finally:
        store.close()


def activity_sync_lock_path(database_path: str | Path) -> Path:
    """Return the one owner-private lock shared by every sync entry point."""

    return Path(database_path).expanduser().parent / "activity-sync.lock"
