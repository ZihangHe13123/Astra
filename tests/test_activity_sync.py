import contextlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent.runtime.activity_store import ActivityStore
from agent.runtime.activity_sync import (
    ActivitySynchronizer,
    SourceDiscoveryError,
    SourceRoots,
    normalize_event,
    resolve_source_roots,
    run_scheduled_sync,
    source_is_recent,
    fcntl,
)

requires_posix_lock = pytest.mark.skipif(fcntl is None, reason="activity archive imports require POSIX flock")


def test_activity_tool_starts_without_fcntl_and_sync_reports_unsupported_platform(tmp_path: Path):
    script = """
import builtins
import json
from pathlib import Path

original_import = builtins.__import__

def without_fcntl(name, globals=None, locals=None, fromlist=(), level=0):
    if name == "fcntl":
        raise ModuleNotFoundError("No module named 'fcntl'")
    return original_import(name, globals, locals, fromlist, level)

builtins.__import__ = without_fcntl
from agent.runtime import activity_sync
from agent.runtime.tools import activity

roots = activity_sync.SourceRoots(Path("events"), Path("summaries"))
activity.resolve_source_roots = lambda: roots

class Store:
    path = Path("private/activity.sqlite3")

report = activity._sync_activity_store(Store())
print(json.dumps({"status": report.status, "error": report.error}))
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "status": "source_unavailable",
        "error": "unsupported_platform",
    }


def build_computer_history_fixture(
    tmp_path: Path,
    *,
    events: list[dict[str, object]],
    summaries: dict[str, str],
) -> SourceRoots:
    """Create the minimal local Computer History and summary roots used by integration tests."""
    event_root = tmp_path / "computer-history"
    summary_root = tmp_path / "summaries"
    segment = event_root / "segments" / "2026-08-26T06-40-00Z"
    segment.mkdir(parents=True)
    summary_root.mkdir()
    (segment / "events.jsonl").write_bytes(
        b"".join(json.dumps(event, ensure_ascii=False).encode("utf-8") + b"\n" for event in events)
    )
    (event_root / "metadata.json").write_text("{}", encoding="utf-8")
    for name, content in summaries.items():
        (summary_root / name).write_text(content, encoding="utf-8")
    return SourceRoots(event_root=event_root, summary_root=summary_root)


@requires_posix_lock
def test_activity_archive_end_to_end_and_clear_cutoff(tmp_path: Path):
    roots = build_computer_history_fixture(
        tmp_path,
        events=[{
            "id": 1, "kind": "selection", "timestamp": "2026-08-26T06:41:00Z",
            "app": {"name": "Safari", "bundleIdentifier": "com.apple.Safari"},
            "window": {"title": "Astra Activity Store",
                       "url": "https://example.com/design?token=private#review"},
            "selection": {"target": "activity archive"},
        }],
        summaries={"2026-08-26T06-40-00-abcd-10min-work.md":
                   "Designed a local Astra activity archive."},
    )
    with ActivityStore(tmp_path / "activity.sqlite3") as store:
        sync = ActivitySynchronizer(store, roots, tmp_path / "sync.lock")
        report = sync.sync(force=True)
        assert (report.events_imported, report.summaries_imported) == (1, 1)
        results = store.search("Astra activity", limit=10)
        assert {item["evidence_type"] for item in results} == {"summary", "raw_event"}
        serialized = json.dumps(results, ensure_ascii=False)
        assert "token=private" not in serialized and "raw_json" not in serialized
        store.clear(before="2026-08-26T07:00:00+00:00")
        event_path = next(roots.event_root.glob("segments/*/events.jsonl"))
        replacement_event = {
            "id": 2, "kind": "selection", "timestamp": "2026-08-26T06:42:00Z",
            "app": {"name": "Safari", "bundleIdentifier": "com.apple.Safari"},
            "window": {"title": "Astra Activity Store",
                       "url": "https://example.com/design?token=reimport"},
            "selection": {"target": "activity archive"},
        }
        replacement_event_path = event_path.with_suffix(".replacement")
        replacement_event_path.write_bytes(
            json.dumps(replacement_event, ensure_ascii=False).encode("utf-8") + b"\n"
        )
        replacement_event_path.replace(event_path)
        summary_path = next(roots.summary_root.glob("*.md"))
        summary_path.write_text("Rewritten pre-cutoff Astra activity archive.", encoding="utf-8")

        report = sync.sync(force=True)
        assert (report.events_imported, report.summaries_imported) == (0, 0)
        assert store.search("Astra activity", limit=10) == []


def test_normalize_event_preserves_raw_and_extracts_search_fields():
    payload = {
        "id": 12, "kind": "selection", "timestamp": "2026-08-26T06:41:00Z",
        "app": {"name": "Safari", "bundleIdentifier": "com.apple.Safari"},
        "window": {"title": "Astra Activity", "url": "https://example.com/work?q=private"},
        "selection": {"selectedItems": ["activity archive"], "target": "document"},
    }
    normalized = normalize_event("2026-08-26T06-40-00Z", payload)
    assert normalized is not None
    assert normalized.event_id == 12
    assert normalized.app_name == "Safari"
    assert "activity archive" in normalized.searchable_text
    assert json.loads(normalized.raw_json) == payload


def test_normalize_event_rejects_non_dict_payload():
    assert normalize_event("2026-08-26T06-40-00Z", ["not", "an", "event"]) is None  # type: ignore[arg-type]


def test_normalize_event_rejects_malformed_timestamp():
    assert normalize_event("2026-08-26T06-40-00Z", {"id": 1, "kind": "focus", "timestamp": "not-a-timestamp"}) is None


@pytest.mark.parametrize("timestamp", ("0001-01-01T00:00:00+23:59", "9999-12-31T23:59:59-23:59"))
def test_normalize_event_rejects_timestamp_conversion_overflow(timestamp: str):
    assert normalize_event("2026-08-26T06-40-00Z", {"id": 1, "kind": "focus", "timestamp": timestamp}) is None


def test_sync_skips_overflow_timestamps_and_continues(sync_fixture: "SyncFixture"):
    sync_fixture.write_events([
        {"id": 1, "kind": "focus", "timestamp": "0001-01-01T00:00:00+23:59"},
        {"id": 2, "kind": "focus", "timestamp": "9999-12-31T23:59:59-23:59"},
        {"id": 3, "kind": "focus", "timestamp": "2026-08-26T06:40:03Z"},
    ])
    report = sync_fixture.sync()
    assert (report.malformed_lines, report.events_imported) == (2, 1)


@requires_posix_lock
def test_sync_resumes_after_complete_line_and_defers_partial_tail(tmp_path: Path):
    segment = tmp_path / "segments" / "2026-08-26T06-40-00Z"
    segment.mkdir(parents=True)
    events = segment / "events.jsonl"
    first = json.dumps({"id": 1, "kind": "focus", "timestamp": "2026-08-26T06:40:01Z"}) + "\n"
    second = json.dumps({"id": 2, "kind": "focus", "timestamp": "2026-08-26T06:40:02Z"})
    events.write_text(first + second[:20], encoding="utf-8")
    roots = SourceRoots(tmp_path, tmp_path / "summaries")
    with ActivityStore(tmp_path / "activity.sqlite3") as store:
        sync = ActivitySynchronizer(store, roots, tmp_path / "sync.lock")
        assert sync.sync_events().events_imported == 1
        assert store.get_cursor(str(events)).byte_offset == len(first.encode())
        with events.open("a", encoding="utf-8") as handle:
            handle.write(second[20:] + "\n")
        assert sync.sync_events().events_imported == 1
        assert store.stats()["events"] == 2


@dataclass
class SyncFixture:
    root: Path
    store: ActivityStore
    source: Path
    lock_path: Path

    def write_events(self, events: list[dict[str, object]]) -> None:
        self.write_raw(b"".join(json.dumps(event).encode("utf-8") + b"\n" for event in events))

    def replace_events(self, events: list[dict[str, object]]) -> None:
        replacement = self.source.with_suffix(".replacement")
        replacement.write_bytes(b"".join(json.dumps(event).encode("utf-8") + b"\n" for event in events))
        replacement.replace(self.source)

    def write_raw(self, content: bytes) -> None:
        self.source.write_bytes(content)

    def sync(self):
        return self.synchronizer.sync_events()

    @property
    def synchronizer(self) -> ActivitySynchronizer:
        return ActivitySynchronizer(self.store, SourceRoots(self.root, self.root / "summaries"), self.lock_path)

    @contextlib.contextmanager
    def hold_lock(self):
        if fcntl is None:
            pytest.skip("activity archive imports require POSIX flock")
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@pytest.fixture
def sync_fixture(tmp_path: Path):
    if fcntl is None:
        pytest.skip("activity archive imports require POSIX flock")
    source = tmp_path / "segments" / "2026-08-26T06-40-00Z" / "events.jsonl"
    source.parent.mkdir(parents=True)
    fixture = SyncFixture(tmp_path, ActivityStore(tmp_path / "activity.sqlite3"), source, tmp_path / "sync.lock")
    try:
        yield fixture
    finally:
        fixture.store.close()


def test_replaced_file_rescans_without_duplicate_rows(sync_fixture: SyncFixture):
    sync_fixture.write_events([{"id": 1, "kind": "focus", "timestamp": "2026-08-26T06:40:01Z"}])
    assert sync_fixture.sync().events_imported == 1
    sync_fixture.replace_events([
        {"id": 1, "kind": "focus", "timestamp": "2026-08-26T06:40:01Z"},
        {"id": 2, "kind": "focus", "timestamp": "2026-08-26T06:40:02Z"},
    ])
    report = sync_fixture.sync()
    assert (report.events_imported, report.events_duplicate) == (1, 1)
    assert sync_fixture.store.stats()["events"] == 2


def test_truncated_same_inode_rescans_after_deferred_tail(sync_fixture: SyncFixture):
    first = {"id": 1, "kind": "focus", "timestamp": "2026-08-26T06:40:01Z"}
    sync_fixture.write_raw(json.dumps(first).encode() + b"\n" + b"{" * 200)
    assert sync_fixture.sync().events_imported == 1

    sync_fixture.write_events([{"id": 2, "kind": "focus", "timestamp": "2026-08-26T06:40:02Z"}])
    report = sync_fixture.sync()
    assert (report.events_imported, report.events_duplicate) == (1, 0)
    assert sync_fixture.store.stats()["events"] == 2


def test_truncated_partial_replacement_persists_reset_before_it_grows(sync_fixture: SyncFixture):
    first = {"id": 1, "kind": "focus", "timestamp": "2026-08-26T06:40:01Z"}
    sync_fixture.write_raw(json.dumps(first).encode() + b"\n" + b"{" * 200)
    assert sync_fixture.sync().events_imported == 1

    replacement = {
        "id": 2, "kind": "focus", "timestamp": "2026-08-26T06:40:02Z",
        "selection": {"selectedItems": ["x" * 400]},
    }
    replacement_bytes = json.dumps(replacement).encode()
    sync_fixture.write_raw(replacement_bytes[:20])
    assert sync_fixture.sync().deferred_lines == 1
    assert sync_fixture.store.get_cursor(str(sync_fixture.source)).byte_offset == 0

    with sync_fixture.source.open("ab") as handle:
        handle.write(replacement_bytes[20:] + b"\n")
    report = sync_fixture.sync()
    assert (report.events_imported, report.events_duplicate) == (1, 0)
    assert sync_fixture.store.stats()["events"] == 2


def test_malformed_line_does_not_block_later_event(sync_fixture: SyncFixture):
    sync_fixture.write_raw(b"{broken}\n" + json.dumps({
        "id": 3, "kind": "focus", "timestamp": "2026-08-26T06:40:03Z"
    }).encode() + b"\n")
    report = sync_fixture.sync()
    assert (report.malformed_lines, report.events_imported) == (1, 1)


def test_second_lock_holder_returns_already_running(sync_fixture: SyncFixture):
    with sync_fixture.hold_lock():
        report = sync_fixture.sync()
    assert report.already_running is True
    assert report.events_imported == 0


@requires_posix_lock
def test_summary_sync_lock_contention_returns_already_running_without_writes(tmp_path: Path):
    summary_root = tmp_path / "summaries"
    summary_root.mkdir()
    (summary_root / "2026-08-26T06-40-00-abcd-10min-work.md").write_text(
        "PRIVATE SUMMARY CONTENT",
        encoding="utf-8",
    )
    lock_path = tmp_path / "activity-sync.lock"
    with ActivityStore(tmp_path / "activity.sqlite3") as store:
        synchronizer = ActivitySynchronizer(
            store,
            SourceRoots(tmp_path / "events", summary_root),
            lock_path,
        )
        with lock_path.open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                report = synchronizer.sync_summaries()
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

        assert report.already_running is True
        assert report.summaries_imported == 0
        assert store.stats()["summaries"] == 0


@requires_posix_lock
def test_combined_sync_lock_contention_precedes_integrity_and_all_writes(tmp_path: Path):
    roots = build_computer_history_fixture(
        tmp_path,
        events=[{"id": 1, "kind": "focus", "timestamp": "2026-08-26T06:40:01Z"}],
        summaries={
            "2026-08-26T06-40-00-abcd-10min-work.md": "PRIVATE SUMMARY CONTENT",
        },
    )
    lock_path = tmp_path / "activity-sync.lock"
    with ActivityStore(tmp_path / "activity.sqlite3") as store:
        integrity_checks = 0
        original_integrity_status = store.integrity_status

        def tracked_integrity_status():
            nonlocal integrity_checks
            integrity_checks += 1
            return original_integrity_status()

        store.integrity_status = tracked_integrity_status  # type: ignore[method-assign]
        synchronizer = ActivitySynchronizer(store, roots, lock_path)
        with lock_path.open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                report = synchronizer.sync(force=True)
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

        assert report.already_running is True
        assert integrity_checks == 0
        assert store.stats()["events"] == 0
        assert store.stats()["summaries"] == 0


def test_explicit_root_wins_and_ambiguous_auto_discovery_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    explicit = tmp_path / "chosen"
    (explicit / "segments").mkdir(parents=True)
    monkeypatch.setenv("ASTRA_COMPUTER_HISTORY_ROOT", str(explicit))
    assert resolve_source_roots(summary_root=tmp_path / "summaries").event_root == explicit
    monkeypatch.delenv("ASTRA_COMPUTER_HISTORY_ROOT")
    with pytest.raises(SourceDiscoveryError, match="multiple"):
        resolve_source_roots(
            summary_root=tmp_path / "summaries",
            candidates=[tmp_path / "first", tmp_path / "second"],
        )


def test_source_discovery_precedence_is_argument_then_environment_then_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    argument = tmp_path / "argument"
    environment = tmp_path / "environment"
    candidate = tmp_path / "candidate"
    for root in (argument, environment, candidate):
        root.mkdir()
    cases = (
        (argument, environment, [candidate], argument),
        (None, environment, [candidate], environment),
        (None, None, [candidate], candidate),
    )
    for explicit_root, environment_root, candidates, expected_root in cases:
        with monkeypatch.context() as context:
            if environment_root is None:
                context.delenv("ASTRA_COMPUTER_HISTORY_ROOT", raising=False)
            else:
                context.setenv("ASTRA_COMPUTER_HISTORY_ROOT", str(environment_root))
            assert resolve_source_roots(
                event_root=explicit_root,
                summary_root=tmp_path / "summaries",
                candidates=candidates,
            ).event_root == expected_root


@requires_posix_lock
def test_summary_sync_is_hash_incremental_and_links_time_range(tmp_path: Path):
    resources = tmp_path / "resources"
    resources.mkdir()
    summary = resources / "2026-08-26T06-40-00-abcd-10min-work.md"
    summary.write_text("Worked on Astra activity archive.", encoding="utf-8")
    with ActivityStore(tmp_path / "activity.sqlite3") as store:
        sync = ActivitySynchronizer(
            store, SourceRoots(tmp_path / "skysight", resources), tmp_path / "sync.lock"
        )
        assert sync.sync_summaries().summaries_imported == 1
        assert sync.sync_summaries().summaries_imported == 0
        saved = store.get_summary_by_source(str(summary))
        assert saved is not None
        assert saved.granularity == "10min"
        assert saved.period_start == "2026-08-26T06:40:00+00:00"
        assert saved.period_end == "2026-08-26T06:50:00+00:00"


@requires_posix_lock
def test_summary_sync_derives_six_hour_period(tmp_path: Path):
    resources = tmp_path / "resources"
    resources.mkdir()
    summary = resources / "2026-08-26T06-40-00-abcd-6h-work.md"
    summary.write_text("Six hour summary.", encoding="utf-8")
    with ActivityStore(tmp_path / "activity.sqlite3") as store:
        sync = ActivitySynchronizer(
            store, SourceRoots(tmp_path / "skysight", resources), tmp_path / "sync.lock"
        )
        assert sync.sync_summaries().summaries_imported == 1
        saved = store.get_summary_by_source(str(summary))
        assert saved is not None
        assert saved.period_start == "2026-08-26T06:40:00+00:00"
        assert saved.period_end == "2026-08-26T12:40:00+00:00"


@requires_posix_lock
def test_summary_sync_ignores_non_summary_filenames(tmp_path: Path):
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "notes.md").write_text("not a Skysight summary", encoding="utf-8")
    with ActivityStore(tmp_path / "activity.sqlite3") as store:
        report = ActivitySynchronizer(
            store, SourceRoots(tmp_path / "skysight", resources), tmp_path / "sync.lock"
        ).sync_summaries()
    assert (report.summaries_imported, report.summaries_ignored) == (0, 1)


@pytest.fixture
def fixed_now() -> datetime:
    return datetime(2026, 8, 26, 7, 0, tzinfo=timezone.utc)


@pytest.fixture
def source_roots(tmp_path: Path) -> SourceRoots:
    events = tmp_path / "segments" / "2026-08-26T06-40-00Z" / "events.jsonl"
    metadata = events.with_name("metadata.json")
    events.parent.mkdir(parents=True)
    events.write_text("{}\n", encoding="utf-8")
    metadata.write_text("{}", encoding="utf-8")
    for path in (events, metadata):
        os.utime(path, (0, 0))
    return SourceRoots(tmp_path, tmp_path / "summaries")


def set_source_mtime(roots: SourceRoots, timestamp: float) -> None:
    for path in roots.event_root.glob("segments/*/events.jsonl"):
        os.utime(path, (timestamp, timestamp))
    for path in roots.event_root.glob("segments/*/metadata.json"):
        os.utime(path, (timestamp, timestamp))


@pytest.mark.parametrize("age,expected", [(899, True), (900, True), (901, False)])
def test_source_freshness_boundary(source_roots: SourceRoots, fixed_now: datetime, age: int, expected: bool):
    set_source_mtime(source_roots, fixed_now.timestamp() - age)
    assert source_is_recent(source_roots, now=fixed_now, max_age_seconds=900) is expected


def test_scheduled_sync_does_not_construct_store_for_stale_source(source_roots: SourceRoots):
    opened: list[bool] = []
    result = run_scheduled_sync(
        source_roots, store_factory=lambda: opened.append(True),
        now=datetime(2026, 8, 26, 7, 0, tzinfo=timezone.utc),
    )
    assert result.status == "inactive"
    assert opened == []


def test_manual_sync_scans_stale_source(sync_fixture: SyncFixture):
    sync_fixture.write_events([{"id": 9, "kind": "focus", "timestamp": "2026-08-20T00:00:00Z"}])
    os.utime(sync_fixture.source, (0, 0))
    assert sync_fixture.synchronizer.sync(force=True).events_imported == 1


def test_sync_is_partial_when_events_succeed_but_summary_root_is_missing(sync_fixture: SyncFixture):
    sync_fixture.write_events([{"id": 10, "kind": "focus", "timestamp": "2026-08-26T00:00:00Z"}])
    report = sync_fixture.synchronizer.sync(force=True)
    assert report.events_imported == 1
    assert report.status == "partial"
    assert report.error == "summary_root_unavailable"


@pytest.mark.parametrize("recursion_limit", (None, 5_000), ids=("default_limit", "raised_limit"))
def test_complete_adversarial_records_are_malformed_and_cursor_reaches_following_event(
    sync_fixture: SyncFixture,
    recursion_limit: int | None,
):
    deeply_nested = (
        b'{"id":1,"kind":"focus","timestamp":"2026-08-26T06:40:01Z","nested":'
        + (b"[" * 1_500)
        + b"0"
        + (b"]" * 1_500)
        + b"}\n"
    )
    outside_sqlite_integer = json.dumps({
        "id": 2**63,
        "kind": "focus",
        "timestamp": "2026-08-26T06:40:02Z",
    }).encode("utf-8") + b"\n"
    valid = json.dumps({
        "id": 2**63 - 1,
        "kind": "focus",
        "timestamp": "2026-08-26T06:40:03Z",
    }).encode("utf-8") + b"\n"
    payload = deeply_nested + outside_sqlite_integer + valid
    sync_fixture.write_raw(payload)

    # Python 3.11 stops json.loads at sys.getrecursionlimit() (importing jedi raises it to 3000);
    # 3.12+ ignore that limit, so the 1,500-level line can decode and must still be rejected.
    previous_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(recursion_limit or previous_limit)
    try:
        report = sync_fixture.sync()
    finally:
        sys.setrecursionlimit(previous_limit)

    assert (report.malformed_lines, report.events_imported) == (2, 1)
    assert sync_fixture.store.get_cursor(str(sync_fixture.source)).byte_offset == len(payload)
    assert sync_fixture.store.stats()["events"] == 1


@pytest.mark.parametrize("entry_point", ("sync_events", "sync_summaries", "sync"))
@requires_posix_lock
def test_every_sync_entry_point_gates_unhealthy_integrity_before_writes(
    tmp_path: Path,
    entry_point: str,
):
    event_root = tmp_path / "source"
    summary_root = tmp_path / "summaries"
    (event_root / "segments" / "segment").mkdir(parents=True)
    summary_root.mkdir()
    (event_root / "segments" / "segment" / "events.jsonl").write_text(
        '{"id":1,"kind":"focus","timestamp":"2026-08-26T06:40:01Z"}\n',
        encoding="utf-8",
    )
    (summary_root / "2026-08-26T06-40-00-abcd-10min-work.md").write_text(
        "PRIVATE SUMMARY CONTENT",
        encoding="utf-8",
    )

    class UnhealthyStore:
        path = tmp_path / "activity.sqlite3"

        def __init__(self):
            self.integrity_checks = 0

        def integrity_status(self):
            self.integrity_checks += 1
            return {"integrity_status": "ok", "fts_status": "error"}

        def __getattr__(self, name):
            raise AssertionError(f"write path reached after failed integrity gate: {name}")

    store = UnhealthyStore()
    synchronizer = ActivitySynchronizer(
        store,  # type: ignore[arg-type]
        SourceRoots(event_root, summary_root),
        tmp_path / "activity-sync.lock",
    )

    if entry_point == "sync":
        report = synchronizer.sync(force=True)
    else:
        report = getattr(synchronizer, entry_point)()

    assert report.status == "integrity_error"
    assert report.error == "integrity_error"
    assert store.integrity_checks == 1


def test_public_scheduled_and_cli_sync_use_same_database_parent_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    from agent.cli import activity_commands
    from agent.runtime import activity_sync

    roots = SourceRoots(tmp_path / "source", tmp_path / "summaries")
    roots.event_root.mkdir()
    roots.summary_root.mkdir()
    database = tmp_path / "private" / "archive.sqlite3"
    captured_locks: list[Path] = []

    class FakeStore:
        path = database

        def close(self):
            pass

    class CapturingSynchronizer:
        def __init__(self, _store, _roots, lock_path):
            captured_locks.append(lock_path)

        def sync(self, force=False):
            assert force is True
            return activity_sync.SyncReport(status="ok")

    monkeypatch.setattr(activity_sync, "source_is_recent", lambda _roots, now=None: True)
    monkeypatch.setattr(activity_sync, "ActivitySynchronizer", CapturingSynchronizer)
    scheduled = activity_sync.run_scheduled_sync(roots, store_factory=FakeStore)

    monkeypatch.setattr(activity_commands, "resolve_source_roots", lambda: roots)
    monkeypatch.setattr(activity_commands, "ActivitySynchronizer", CapturingSynchronizer)
    code = activity_commands.execute_activity_command(["sync"], store_factory=FakeStore)
    capsys.readouterr()

    expected = database.parent / "activity-sync.lock"
    assert scheduled.status == "ok"
    assert code == 0
    assert captured_locks == [expected, expected]


def test_selection_flattening_charges_separators_and_caps_final_text_exactly():
    normalized = normalize_event(
        "segment",
        {
            "id": 1,
            "kind": "selection",
            "timestamp": "2026-08-26T06:40:01Z",
            "selection": ["x" * 64 for _ in range(64)],
        },
    )

    assert normalized is not None
    assert len(normalized.selection_text) == 4_096
