import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from agent.runtime.context_index.activity_source import ActivityRecommendationReader
from agent.runtime.context_index.habits import HabitAggregator
from agent.runtime.context_index.workspace import WorkspaceIdentity

pytestmark = pytest.mark.usefixtures("unhurried_context_readers")

UTC_WORKSPACE = WorkspaceIdentity(
    key="/users/test/astra-master",
    root="/Users/test/astra-master",
    label="astra-master",
)
EMPTY_WORKSPACE = WorkspaceIdentity(key="", root="", label="")
MONDAY_0900 = datetime(2026, 8, 31, 9, 0, tzinfo=UTC)
UTC_ZONE = ZoneInfo("UTC")


def _create_activity_database(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE activity_events (
            segment_id TEXT NOT NULL,
            event_id INTEGER NOT NULL,
            occurred_at TEXT NOT NULL,
            occurred_at_us INTEGER NOT NULL,
            kind TEXT NOT NULL,
            app_name TEXT NOT NULL DEFAULT '',
            bundle_id TEXT NOT NULL DEFAULT '',
            window_title TEXT NOT NULL DEFAULT '',
            url TEXT NOT NULL DEFAULT '',
            url_search_text TEXT NOT NULL,
            selection_text TEXT NOT NULL DEFAULT '',
            searchable_text TEXT NOT NULL DEFAULT '',
            raw_json TEXT NOT NULL,
            imported_at TEXT NOT NULL,
            PRIMARY KEY(segment_id, event_id)
        );
        CREATE TABLE activity_summaries (
            summary_id TEXT PRIMARY KEY,
            source_path TEXT NOT NULL,
            granularity TEXT NOT NULL,
            period_start TEXT NOT NULL DEFAULT '',
            period_end TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            source_mtime_ns INTEGER NOT NULL,
            imported_at TEXT NOT NULL
        );
        CREATE VIRTUAL TABLE activity_events_fts USING fts5(
            app_name, window_title, url_search_text, selection_text, searchable_text,
            content='activity_events', content_rowid='rowid', tokenize='trigram'
        );
        CREATE VIRTUAL TABLE activity_summaries_fts USING fts5(
            content, content='activity_summaries', content_rowid='rowid', tokenize='trigram'
        );
        CREATE TRIGGER activity_events_ai AFTER INSERT ON activity_events BEGIN
            INSERT INTO activity_events_fts(
                rowid, app_name, window_title, url_search_text, selection_text,
                searchable_text
            ) VALUES (
                new.rowid, new.app_name, new.window_title, new.url_search_text,
                new.selection_text, new.searchable_text
            );
        END;
        CREATE TRIGGER activity_events_ad AFTER DELETE ON activity_events BEGIN
            INSERT INTO activity_events_fts(
                activity_events_fts, rowid, app_name, window_title, url_search_text,
                selection_text, searchable_text
            ) VALUES (
                'delete', old.rowid, old.app_name, old.window_title,
                old.url_search_text, old.selection_text, old.searchable_text
            );
        END;
        """
    )
    connection.execute("CREATE INDEX idx_activity_events_occurred_at_us ON activity_events(occurred_at_us)")
    return connection


@pytest.fixture
def activity_connection(tmp_path: Path):
    connection = _create_activity_database(tmp_path / "activity.sqlite3")
    try:
        yield connection
    finally:
        connection.close()


def _add_event(
    connection: sqlite3.Connection,
    occurred_at: datetime | str,
    *,
    app: str = "Xcode",
    title: str = "Astra review",
    url: str = "https://example.com/review?token=PRIVATE#fragment",
    searchable: str = "Astra project",
    selection: str = "PRIVATE_SELECTION_TEXT",
) -> None:
    next_id = int(connection.execute("SELECT COUNT(*) FROM activity_events").fetchone()[0]) + 1
    if isinstance(occurred_at, datetime):
        occurred_at = occurred_at.astimezone(UTC).isoformat()
    connection.execute(
        """INSERT INTO activity_events(
               segment_id, event_id, occurred_at, occurred_at_us, kind, app_name, bundle_id,
               window_title, url, url_search_text, selection_text, searchable_text,
               raw_json, imported_at
           ) VALUES (?, ?, ?, ?, 'window', ?, '', ?, ?, ?, ?, ?, ?, ?)""",
        (
            f"PRIVATE_SEGMENT_{next_id}",
            next_id,
            occurred_at,
            int(datetime.fromisoformat(occurred_at).timestamp() * 1_000_000),
            app,
            title,
            url,
            url.split("?", 1)[0],
            selection,
            searchable,
            '{"private":"RAW_SECRET"}',
            occurred_at,
        ),
    )
    connection.commit()


def _add_local_dates(
    connection: sqlite3.Connection,
    dates: list[str],
    *,
    zone: ZoneInfo = UTC_ZONE,
    hour: int = 9,
    copies_per_date: int = 1,
    **event_fields: str,
) -> None:
    for value in dates:
        for copy in range(copies_per_date):
            local = datetime.fromisoformat(value).replace(
                hour=hour,
                minute=min(copy, 59),
                tzinfo=zone,
            )
            _add_event(connection, local, **event_fields)


def test_habit_requires_three_distinct_local_dates(activity_connection) -> None:
    _add_local_dates(
        activity_connection,
        ["2026-08-03", "2026-08-10"],
        copies_per_date=20,
    )
    aggregator = HabitAggregator(ZoneInfo("UTC"))

    assert aggregator.candidate(activity_connection, UTC_WORKSPACE, MONDAY_0900, 1) is None

    _add_local_dates(activity_connection, ["2026-08-17"])
    candidate = aggregator.candidate(activity_connection, UTC_WORKSPACE, MONDAY_0900, 2)

    assert candidate is not None
    assert candidate.support_days == 3


def test_habit_uses_a_rolling_six_week_horizon_including_boundaries(
    activity_connection,
) -> None:
    _add_local_dates(
        activity_connection,
        ["2026-07-13", "2026-07-20", "2026-07-27", "2026-08-03"],
    )

    candidate = HabitAggregator(ZoneInfo("UTC")).candidate(activity_connection, EMPTY_WORKSPACE, MONDAY_0900, 1)

    assert candidate is not None
    assert candidate.support_days == 3
    assert candidate.support_weeks == 3


def test_habit_workspace_match_survives_sixteen_global_events_per_bucket(activity_connection) -> None:
    dates = ["2026-08-03", "2026-08-10", "2026-08-17"]
    for value in dates:
        _add_local_dates(
            activity_connection,
            [value],
            copies_per_date=16,
            app="Global",
            title="unrelated stream",
            searchable="unrelated stream",
        )
        _add_local_dates(
            activity_connection,
            [value],
            hour=8,
            app="Workspace",
            title="astra-master review",
            searchable="astra-master review",
        )

    candidate = HabitAggregator(UTC_ZONE).candidate(
        activity_connection, UTC_WORKSPACE, MONDAY_0900, 11
    )

    assert candidate is not None
    assert candidate.workspace_tier == 0
    assert candidate.support_days == 3


def test_habit_inferred_workspace_token_survives_sixteen_global_events_per_bucket(
    activity_connection,
) -> None:
    for value in ("2026-08-03", "2026-08-10", "2026-08-17"):
        _add_local_dates(
            activity_connection, [value], copies_per_date=16, app="Global",
            title="unrelated stream", searchable="unrelated stream",
        )
        _add_local_dates(
            activity_connection, [value], hour=8, app="Workspace",
            title="astra review", searchable="astra review",
        )

    candidate = HabitAggregator(UTC_ZONE).candidate(activity_connection, UTC_WORKSPACE, MONDAY_0900, 12)

    assert candidate is not None
    assert candidate.workspace_tier == 1
    assert candidate.support_days == 3


def test_historical_offsets_choose_local_weekday_and_bucket(
    activity_connection,
) -> None:
    zone = ZoneInfo("America/Los_Angeles")
    for timestamp in (
        "2026-03-01T09:30:00+00:00",
        "2026-03-08T09:30:00+00:00",
        "2026-03-15T08:30:00+00:00",
    ):
        _add_event(activity_connection, timestamp, searchable="")

    candidate = HabitAggregator(zone).candidate(
        activity_connection,
        EMPTY_WORKSPACE,
        datetime(2026, 3, 22, 1, 0, tzinfo=zone),
        3,
    )

    assert candidate is not None
    assert candidate.support_days == 3


def test_local_midnight_controls_weekday_not_the_utc_date(activity_connection) -> None:
    zone = ZoneInfo("Asia/Singapore")
    for timestamp in (
        "2026-08-02T16:30:00+00:00",
        "2026-08-09T16:30:00+00:00",
        "2026-08-16T16:30:00+00:00",
    ):
        _add_event(activity_connection, timestamp, searchable="")

    candidate = HabitAggregator(zone).candidate(
        activity_connection,
        EMPTY_WORKSPACE,
        datetime(2026, 8, 24, 0, 30, tzinfo=zone),
        1,
    )

    assert candidate is not None
    assert candidate.support_days == 3


def test_exact_then_inferred_then_global_workspace_tiers_drive_ranking(
    activity_connection,
) -> None:
    _add_local_dates(
        activity_connection,
        ["2026-08-03", "2026-08-10", "2026-08-17"],
        app="ExactApp",
        title="astra-master review",
        searchable="",
    )
    _add_local_dates(
        activity_connection,
        ["2026-07-27", "2026-08-03", "2026-08-10", "2026-08-17"],
        app="InferredApp",
        title="Astra review",
        searchable="",
    )
    _add_local_dates(
        activity_connection,
        ["2026-07-20", "2026-07-27", "2026-08-03", "2026-08-10", "2026-08-17"],
        app="GlobalApp",
        title="Unrelated review",
        searchable="",
    )

    exact = HabitAggregator(ZoneInfo("UTC")).candidate(activity_connection, UTC_WORKSPACE, MONDAY_0900, 1)
    inferred = HabitAggregator(ZoneInfo("UTC")).candidate(
        activity_connection,
        WorkspaceIdentity("/project/astra-tools", "/project/astra-tools", "astra-tools"),
        MONDAY_0900,
        1,
    )

    assert exact is not None and exact.workspace_tier == 0
    assert "ExactApp" in exact.description
    assert inferred is not None and inferred.workspace_tier == 1
    assert "InferredApp" in inferred.description


def test_changed_data_version_invalidates_cached_aggregate_and_source_clear(
    activity_connection,
) -> None:
    aggregator = HabitAggregator(ZoneInfo("UTC"))
    first = aggregator.candidate(activity_connection, UTC_WORKSPACE, MONDAY_0900, 10)
    _add_local_dates(
        activity_connection,
        ["2026-08-03", "2026-08-10", "2026-08-17"],
    )
    second = aggregator.candidate(activity_connection, UTC_WORKSPACE, MONDAY_0900, 11)
    assert first is None and second is not None

    activity_connection.execute("DELETE FROM activity_events")
    activity_connection.commit()
    cleared = aggregator.candidate(activity_connection, UTC_WORKSPACE, MONDAY_0900, 12)

    assert cleared is None
    assert aggregator.open(second.locator).items == ()


def test_cache_key_separates_local_slots_and_is_bounded(activity_connection) -> None:
    aggregator = HabitAggregator(ZoneInfo("UTC"))
    assert aggregator.candidate(activity_connection, EMPTY_WORKSPACE, MONDAY_0900, 1) is None
    _add_local_dates(
        activity_connection,
        ["2026-08-03", "2026-08-10", "2026-08-17"],
        hour=11,
    )

    assert (
        aggregator.candidate(
            activity_connection,
            EMPTY_WORKSPACE,
            MONDAY_0900.replace(hour=11),
            1,
        )
        is not None
    )
    assert aggregator.candidate(activity_connection, EMPTY_WORKSPACE, MONDAY_0900, 1) is None

    locators = []
    for index in range(33):
        candidate = aggregator.candidate(
            activity_connection,
            WorkspaceIdentity(f"workspace-{index}", "", ""),
            MONDAY_0900.replace(hour=11),
            1,
        )
        assert candidate is not None
        locators.append(candidate.locator)
    assert aggregator.open(locators[0]).items == ()
    assert aggregator.open(locators[-1]).items


def test_open_is_bounded_inferred_and_uses_only_safe_activity_fields(
    activity_connection,
) -> None:
    _add_local_dates(
        activity_connection,
        [
            "2026-07-20",
            "2026-07-27",
            "2026-08-03",
            "2026-08-10",
            "2026-08-17",
            "2026-08-24",
        ],
        copies_per_date=2,
        app="Safari Authorization: Bearer APP_SECRET",
        title="Astra token=TITLE_SECRET review",
    )
    aggregator = HabitAggregator(ZoneInfo("UTC"))
    candidate = aggregator.candidate(activity_connection, UTC_WORKSPACE, MONDAY_0900, 1)

    assert candidate is not None
    evidence = aggregator.open(candidate.locator)
    serialized = json.dumps({"title": evidence.title, "items": evidence.items}, ensure_ascii=False)
    support = json.loads(evidence.items[0])

    assert candidate.source == "habit"
    assert candidate.trust_label == evidence.trust_label == "inferred_pattern"
    assert "inferred" in candidate.description.casefold()
    assert "Around this time, activity often shifts to" in candidate.description
    assert support["support_days"] == 6
    assert support["represented_weeks"] == 6
    assert len(support["example_local_dates"]) == 5
    assert len(evidence.items) == 4
    assert "certain" not in serialized.casefold()
    assert "will repeat" not in serialized.casefold()
    assert "PRIVATE_SEGMENT" not in serialized
    assert "PRIVATE_SELECTION_TEXT" not in serialized
    assert "RAW_SECRET" not in serialized
    assert "APP_SECRET" not in serialized
    assert "TITLE_SECRET" not in serialized
    assert "token=PRIVATE" not in serialized
    assert "#fragment" not in serialized
    for item in evidence.items[1:]:
        assert set(json.loads(item)) == {
            "evidence_type",
            "source",
            "untrusted_observation",
            "occurred_at",
            "kind",
            "app_name",
            "window_title",
            "url",
        }


def test_aggregation_makes_no_database_or_schema_writes(activity_connection) -> None:
    _add_local_dates(
        activity_connection,
        ["2026-08-03", "2026-08-10", "2026-08-17"],
    )
    before_changes = activity_connection.total_changes
    before_schema = activity_connection.execute(
        "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
    ).fetchall()

    HabitAggregator(ZoneInfo("UTC")).candidate(activity_connection, UTC_WORKSPACE, MONDAY_0900, 1)

    assert activity_connection.total_changes == before_changes
    assert (
        activity_connection.execute("SELECT type, name, sql FROM sqlite_master ORDER BY type, name").fetchall()
        == before_schema
    )


def test_activity_reader_integrates_habit_and_invalidates_after_clear(
    activity_connection,
) -> None:
    path = Path(activity_connection.execute("PRAGMA database_list").fetchone()[2])
    _add_local_dates(
        activity_connection,
        ["2026-08-03", "2026-08-10", "2026-08-17"],
    )
    # Cache invalidation is the contract here; latency has a separate gate.
    # Its 20% habit slice must tolerate host scheduling during this fixture.
    reader = ActivityRecommendationReader(path, deadline_ms=1000, local_zone=ZoneInfo("UTC"))

    first = reader.recommend("", UTC_WORKSPACE, MONDAY_0900)

    assert first.habit is not None, (first.error_category, first.stages)
    opened = reader.open(first.habit.locator, window=99)
    assert opened.source == "habit"
    assert opened.trust_label == "inferred_pattern"

    activity_connection.execute("DELETE FROM activity_events")
    activity_connection.commit()
    second = reader.recommend("", UTC_WORKSPACE, MONDAY_0900 + timedelta(minutes=1))

    assert second.habit is None
    assert reader.open(first.habit.locator, window=1).items == ()


def test_unchanged_source_recomputes_when_the_six_week_horizon_rolls(
    activity_connection,
) -> None:
    _add_local_dates(
        activity_connection,
        ["2026-07-20", "2026-07-27", "2026-08-03"],
    )
    aggregator = HabitAggregator(ZoneInfo("UTC"))

    first = aggregator.candidate(activity_connection, EMPTY_WORKSPACE, MONDAY_0900, 7)
    one_week_later = aggregator.candidate(
        activity_connection,
        EMPTY_WORKSPACE,
        MONDAY_0900 + timedelta(weeks=1),
        7,
    )

    assert first is not None and first.support_days == 3
    assert one_week_later is None


def test_cache_recomputes_after_exact_six_week_boundary_on_same_local_date(
    activity_connection,
) -> None:
    _add_local_dates(
        activity_connection,
        ["2026-07-20", "2026-07-27", "2026-08-03"],
    )
    aggregator = HabitAggregator(ZoneInfo("UTC"))

    at_boundary = aggregator.candidate(activity_connection, EMPTY_WORKSPACE, MONDAY_0900, 7)
    after_boundary = aggregator.candidate(
        activity_connection,
        EMPTY_WORKSPACE,
        MONDAY_0900 + timedelta(minutes=1),
        7,
    )

    assert at_boundary is not None and at_boundary.support_days == 3
    assert after_boundary is None


def test_habit_open_refuses_cached_evidence_immediately_after_source_clear(
    activity_connection,
) -> None:
    path = Path(activity_connection.execute("PRAGMA database_list").fetchone()[2])
    _add_local_dates(
        activity_connection,
        ["2026-08-03", "2026-08-10", "2026-08-17"],
    )
    reader = ActivityRecommendationReader(path, local_zone=ZoneInfo("UTC"))
    result = reader.recommend("", EMPTY_WORKSPACE, MONDAY_0900)
    assert result.habit is not None

    activity_connection.execute("DELETE FROM activity_events")
    activity_connection.commit()

    assert reader.open(result.habit.locator, window=1).items == ()


def _closed_habit_database(path: Path, *, app: str = "Xcode") -> None:
    connection = _create_activity_database(path)
    _add_local_dates(
        connection,
        ["2026-08-03", "2026-08-10", "2026-08-17"],
        app=app,
        searchable="",
    )
    connection.close()


def test_habit_open_refuses_cached_evidence_when_source_disappears(
    tmp_path: Path,
) -> None:
    path = tmp_path / "missing.sqlite3"
    _closed_habit_database(path)
    reader = ActivityRecommendationReader(path, local_zone=ZoneInfo("UTC"))
    result = reader.recommend("", EMPTY_WORKSPACE, MONDAY_0900)
    assert result.habit is not None

    path.unlink()

    assert reader.open(result.habit.locator, window=1).items == ()


@pytest.mark.parametrize("replacement_kind", ["incompatible", "corrupt"])
def test_habit_open_refuses_incompatible_or_corrupt_replacement(
    tmp_path: Path,
    replacement_kind: str,
) -> None:
    path = tmp_path / f"{replacement_kind}.sqlite3"
    _closed_habit_database(path)
    reader = ActivityRecommendationReader(path, local_zone=ZoneInfo("UTC"))
    result = reader.recommend("", EMPTY_WORKSPACE, MONDAY_0900)
    assert result.habit is not None

    replacement = tmp_path / f"{replacement_kind}-replacement.sqlite3"
    if replacement_kind == "incompatible":
        connection = sqlite3.connect(replacement)
        connection.execute("CREATE TABLE unrelated(value TEXT)")
        connection.commit()
        connection.close()
    else:
        replacement.write_bytes(b"not a sqlite database")
    os.replace(replacement, path)

    assert reader.open(result.habit.locator, window=1).items == ()


def test_habit_open_detects_valid_replacement_with_preserved_size_and_mtime(
    tmp_path: Path,
) -> None:
    path = tmp_path / "activity.sqlite3"
    replacement = tmp_path / "replacement.sqlite3"
    _closed_habit_database(path, app="Xcode")
    _closed_habit_database(replacement, app="Emacs")
    original_stat = path.stat()
    assert replacement.stat().st_size == original_stat.st_size
    os.utime(
        replacement,
        ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
    )
    reader = ActivityRecommendationReader(path, local_zone=ZoneInfo("UTC"))
    result = reader.recommend("", EMPTY_WORKSPACE, MONDAY_0900)
    assert result.habit is not None

    os.replace(replacement, path)
    os.utime(path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    replaced_stat = path.stat()
    assert replaced_stat.st_size == original_stat.st_size
    assert replaced_stat.st_mtime_ns == original_stat.st_mtime_ns

    assert reader.open(result.habit.locator, window=1).items == ()


def test_habit_open_detects_wal_only_source_clear(activity_connection) -> None:
    path = Path(activity_connection.execute("PRAGMA database_list").fetchone()[2])
    activity_connection.execute("PRAGMA journal_mode=WAL")
    activity_connection.execute("PRAGMA wal_autocheckpoint=0")
    _add_local_dates(
        activity_connection,
        ["2026-08-03", "2026-08-10", "2026-08-17"],
    )
    reader = ActivityRecommendationReader(path, local_zone=ZoneInfo("UTC"))
    result = reader.recommend("", EMPTY_WORKSPACE, MONDAY_0900)
    assert result.habit is not None
    main_before = (path.stat().st_size, path.stat().st_mtime_ns)

    activity_connection.execute("DELETE FROM activity_events")
    activity_connection.commit()

    assert (path.stat().st_size, path.stat().st_mtime_ns) == main_before
    assert reader.open(result.habit.locator, window=1).items == ()


def test_same_key_tier_and_display_are_independent_of_row_order(
    tmp_path: Path,
) -> None:
    def candidate_for(path: Path, *, exact_first: bool):
        connection = _create_activity_database(path)
        for date_value in ("2026-08-03", "2026-08-10", "2026-08-17"):
            variants = (
                (
                    "ASTRA review",
                    "astra-master exact affinity",
                ),
                (
                    "Astra Review",
                    "astra inferred affinity",
                ),
            )
            if not exact_first:
                variants = tuple(reversed(variants))
            for title, searchable in variants:
                _add_local_dates(
                    connection,
                    [date_value],
                    title=title,
                    searchable=searchable,
                )
        candidate = HabitAggregator(ZoneInfo("UTC")).candidate(connection, UTC_WORKSPACE, MONDAY_0900, 1)
        connection.close()
        assert candidate is not None
        return candidate

    exact_first = candidate_for(tmp_path / "exact-first.sqlite3", exact_first=True)
    inferred_first = candidate_for(tmp_path / "inferred-first.sqlite3", exact_first=False)

    assert exact_first.workspace_tier == inferred_first.workspace_tier == 0
    assert exact_first.description == inferred_first.description
    assert "astra review" in exact_first.description
