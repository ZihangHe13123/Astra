import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.runtime.context_index import session_source as session_source_module
from agent.runtime.context_index.models import SourceLocator
from agent.runtime.context_index.session_source import (
    SessionRecommendationSource,
    active_context_fingerprints,
    content_fingerprint,
)
from agent.runtime.context_index.sqlite_reader import open_readonly, set_read_window
from agent.runtime.context_index.workspace import WorkspaceIdentity
from agent.runtime.session_recall import SessionRecall

NOW = 1_800_000_000.0
WORKSPACE = WorkspaceIdentity(
    key="/work/astra-master",
    root="/work/astra-master",
    label="astra-master",
)


def _create_recall_database(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    set_read_window(connection)
    connection.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            title TEXT DEFAULT '',
            started_at REAL NOT NULL,
            ended_at REAL,
            message_count INTEGER DEFAULT 0,
            personality TEXT DEFAULT '',
            source_session_key TEXT,
            workspace_key TEXT,
            workspace_root TEXT
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL REFERENCES sessions(id),
            role TEXT NOT NULL,
            content TEXT DEFAULT '',
            tool_name TEXT DEFAULT '',
            timestamp REAL NOT NULL,
            msg_index INTEGER NOT NULL
        );
        CREATE VIRTUAL TABLE messages_fts USING fts5(
            content, content=messages, content_rowid=id, tokenize='trigram'
        );
        CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
            INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
        END;
        """
    )
    return connection


def _add_session(
    connection: sqlite3.Connection,
    session_id: str,
    title: str,
    workspace_key: str | None,
    messages: list[tuple[str, str, float]],
) -> list[int]:
    started_at = min(timestamp for _, _, timestamp in messages)
    connection.execute(
        "INSERT INTO sessions "
        "(id, title, started_at, message_count, workspace_key, workspace_root) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            session_id,
            title,
            started_at,
            len(messages),
            workspace_key,
            workspace_key,
        ),
    )
    ids = []
    for index, (role, content, timestamp) in enumerate(messages, start=1):
        cursor = connection.execute(
            "INSERT INTO messages "
            "(session_id, role, content, timestamp, msg_index) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, role, content, timestamp, index),
        )
        ids.append(int(cursor.lastrowid))
    return ids


def _create_broad_recall_database(path: Path, *, rows: int) -> Path:
    connection = _create_recall_database(path)
    messages_per_session = 100
    for session_index in range(rows // messages_per_session):
        _add_session(
            connection,
            f"broad-{session_index}",
            "Broad history",
            WORKSPACE.key if session_index == 0 else None,
            [
                (
                    "user",
                    f"the common result 的 {session_index}-{message_index}",
                    NOW - session_index * messages_per_session - message_index,
                )
                for message_index in range(messages_per_session)
            ],
        )
    connection.commit()
    connection.close()
    return path


@pytest.fixture
def recall_db(tmp_path):
    path = tmp_path / "sessions.db"
    connection = _create_recall_database(path)
    _add_session(
        connection,
        "exact",
        "Memory indexing",
        WORKSPACE.key,
        [("user", "memory index exact workspace", NOW - 40)],
    )
    _add_session(
        connection,
        "inferred",
        "Astra master planning",
        None,
        [("assistant", "memory index inferred project", NOW - 30)],
    )
    _add_session(
        connection,
        "global",
        "Unrelated legacy",
        None,
        [("user", "memory index global fallback", NOW - 20)],
    )
    _add_session(
        connection,
        "current",
        "Current session",
        WORKSPACE.key,
        [
            ("assistant", "retained historical detail", NOW - 11),
            ("user", "current needle", NOW - 10),
        ],
    )
    _add_session(
        connection,
        "visible-copy",
        "Copied session",
        None,
        [("assistant", "  Already   VISIBLE ", NOW - 5)],
    )
    connection.commit()
    connection.close()
    return path


def test_missing_database_is_not_created(tmp_path):
    path = tmp_path / "missing" / "sessions.db"

    result = SessionRecommendationSource(path).recommend(
        "Astra", WORKSPACE, "current", frozenset(), NOW
    )

    assert result.availability == "absent"
    assert not path.exists() and not path.parent.exists()


@pytest.mark.parametrize(
    ("failed_method", "diagnostic", "surviving_field"),
    [
        ("_recency_rows", "recency_omitted", "relevance"),
        ("_relevance_rows", "relevance_omitted", "recency"),
    ],
)
def test_session_channel_failure_preserves_other_channel(
    recall_db, monkeypatch, failed_method, diagnostic, surviving_field
):
    source = SessionRecommendationSource(recall_db)

    def interrupted(*_args, **_kwargs):
        raise sqlite3.OperationalError("interrupted")

    monkeypatch.setattr(source, failed_method, interrupted)
    result = source.recommend("memory index", WORKSPACE, "current", frozenset(), NOW)

    assert result.availability == "available"
    assert getattr(result, surviving_field)
    assert result.diagnostics == (diagnostic,)
    assert result.error_category == "database_error"


def test_relevance_exhausting_total_deadline_keeps_completed_recency(
    recall_db, monkeypatch
):
    source = SessionRecommendationSource(recall_db, deadline_ms=75)
    original_recency = source._recency_rows
    clock = [0.0]
    monkeypatch.setattr(
        session_source_module, "time", SimpleNamespace(monotonic=lambda: clock[0])
    )

    def progress_checkpoint(connection):
        # Run enough VM instructions to check the real SQLite progress handler.
        connection.execute(
            "WITH RECURSIVE ticks(n) AS (SELECT 1 UNION ALL "
            "SELECT n + 1 FROM ticks WHERE n < 1000) SELECT sum(n) FROM ticks"
        ).fetchone()

    def relevance_exhausts_budget(connection, *_args, **_kwargs):
        # A worker can resume after its deadline, even if it was allocated
        # only part of the source budget. No wall-clock sleep is needed here.
        clock[0] = 0.076
        progress_checkpoint(connection)
        raise AssertionError("relevance must stop at the unchanged 75ms deadline")

    def checked_recency(connection, *args, **kwargs):
        progress_checkpoint(connection)
        return original_recency(connection, *args, **kwargs)

    monkeypatch.setattr(source, "_relevance_rows", relevance_exhausts_budget)
    monkeypatch.setattr(source, "_recency_rows", checked_recency)
    result = source.recommend("no matching words", WORKSPACE, "current", frozenset(), NOW)

    assert result.availability == "available"
    assert result.recency
    assert not result.relevance
    assert result.error_category == "deadline"
    assert result.diagnostics == ("relevance_omitted",)


def test_session_channel_failures_mark_source_error_with_deadline_diagnostics(
    recall_db, monkeypatch
):
    source = SessionRecommendationSource(recall_db)

    def interrupted_deadline(*_args, **_kwargs):
        error = sqlite3.OperationalError("interrupted")
        error.sqlite_errorcode = sqlite3.SQLITE_INTERRUPT
        raise error

    def interrupted_database(*_args, **_kwargs):
        raise sqlite3.OperationalError("database busy")

    monkeypatch.setattr(source, "_relevance_rows", interrupted_deadline)
    monkeypatch.setattr(source, "_recency_rows", interrupted_database)
    result = source.recommend("memory index", WORKSPACE, "current", frozenset(), NOW)

    assert result.availability == "error"
    assert result.diagnostics == ("relevance_omitted", "recency_omitted")
    assert result.error_category == "deadline"


@pytest.mark.parametrize("query", ["legacy memory", "legacy me"])
def test_legacy_sessions_without_workspace_columns_are_read_only_and_global(tmp_path, query):
    path = tmp_path / "legacy-sessions.db"
    connection = _create_recall_database(path)
    connection.execute("ALTER TABLE sessions DROP COLUMN workspace_key")
    connection.execute("ALTER TABLE sessions DROP COLUMN workspace_root")
    connection.execute(
        "INSERT INTO sessions(id, title, started_at, message_count) VALUES ('legacy', 'old archive', ?, 1)",
        (NOW - 1,),
    )
    connection.execute(
        "INSERT INTO messages(session_id, role, content, timestamp, msg_index) VALUES ('legacy', 'user', 'legacy memory needle', ?, 1)",
        (NOW - 1,),
    )
    connection.commit()
    before = (path.read_bytes(), path.stat().st_size, path.stat().st_mtime_ns)
    connection.close()

    result = SessionRecommendationSource(path).recommend(
        query, WORKSPACE, "current", frozenset(), NOW
    )

    assert result.availability == "available"
    assert result.error_category == ""
    assert result.relevance and result.relevance[0].workspace_tier == 2
    assert (path.read_bytes(), path.stat().st_size, path.stat().st_mtime_ns) == before


def test_open_readonly_handles_uri_characters_and_forbids_writes(tmp_path):
    path = tmp_path / "dir with spaces # and %" / "sessions.db"
    connection = _create_recall_database(path)
    connection.commit()
    connection.close()
    before = (path.stat().st_size, path.stat().st_mtime_ns, path.read_bytes())

    with open_readonly(path) as readonly:
        assert readonly.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            readonly.execute(
                "INSERT INTO sessions (id, started_at) VALUES ('write', 0)"
            )

    with sqlite3.connect(path) as check:
        assert check.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    assert (path.stat().st_size, path.stat().st_mtime_ns, path.read_bytes()) == before


def test_readonly_connection_observes_wal_commits_and_clear(tmp_path):
    path = tmp_path / "live.db"
    with sqlite3.connect(path) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE evidence(content TEXT)")
        writer.execute("INSERT INTO evidence VALUES ('original')")
        writer.commit()
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        with open_readonly(path, deadline_ms=2000) as reader:
            assert reader.execute("SELECT content FROM evidence").fetchone()[0] == "original"
            writer.execute("UPDATE evidence SET content='committed update'")
            writer.commit()
            assert Path(str(path) + "-wal").stat().st_size > 0
            assert reader.execute("SELECT content FROM evidence").fetchone()[0] == "committed update"
            writer.execute("DELETE FROM evidence")
            writer.commit()
            assert reader.execute("SELECT content FROM evidence").fetchall() == []


def test_exact_workspace_precedes_inferred_and_legacy_global(recall_db, monkeypatch):
    # This test checks candidate ordering, independent of CI scheduling.
    # Deadline interruption and latency have dedicated regression/acceptance tests.
    monkeypatch.setattr(
        session_source_module, "time", SimpleNamespace(monotonic=lambda: 0.0)
    )
    result = SessionRecommendationSource(recall_db).recommend(
        "memory index", WORKSPACE, "current", frozenset(), NOW
    )

    assert result.availability == "available"
    assert result.error_category == ""
    assert [item.workspace_tier for item in result.relevance[:3]] == [0, 1, 2]
    assert [item.project_label for item in result.relevance[:3]] == [
        WORKSPACE.label,
        WORKSPACE.label,
        "",
    ]


def test_current_session_and_in_context_messages_are_excluded(recall_db):
    result = SessionRecommendationSource(recall_db).recommend(
        "current needle",
        WORKSPACE,
        "current",
        {content_fingerprint("already visible")},
        NOW,
    )

    assert all(
        content_fingerprint(item.private_text) != content_fingerprint("current needle")
        for item in result.all_candidates
    )
    assert all(
        content_fingerprint(item.private_text)
        != content_fingerprint("already visible")
        for item in result.all_candidates
    )


def test_source_session_key_excludes_only_persisted_current_user_row(tmp_path):
    path = tmp_path / "source-key-current.db"
    connection = _create_recall_database(path)
    older_id = _add_session(
        connection,
        "older",
        "Older repeated request",
        WORKSPACE.key,
        [("user", "repeat request", NOW - 2)],
    )[0]
    current_id = _add_session(
        connection,
        "canonical-current",
        "Current repeated request",
        WORKSPACE.key,
        [("user", "repeat request", NOW - 1)],
    )[0]
    connection.execute(
        "UPDATE sessions SET source_session_key = ? WHERE id = ?",
        ("session_20260901_001333_51259", "canonical-current"),
    )
    connection.commit()
    connection.close()

    result = SessionRecommendationSource(path).recommend(
        "repeat request",
        WORKSPACE,
        "session_20260901_001333_51259",
        frozenset({content_fingerprint("repeat request")}),
        NOW,
    )

    ids = {item.locator.secondary for item in result.relevance}
    assert current_id not in ids
    assert older_id in ids


def test_source_session_key_wins_when_it_collides_with_another_canonical_id(
    tmp_path,
):
    path = tmp_path / "source-key-collision.db"
    connection = _create_recall_database(path)
    _add_session(
        connection,
        "path-stem",
        "Canonical id collision",
        WORKSPACE.key,
        [("user", "unrelated historical row", NOW - 3)],
    )
    current_id = _add_session(
        connection,
        "canonical-current",
        "Actual current session",
        WORKSPACE.key,
        [("user", "current collision request", NOW - 1)],
    )[0]
    connection.execute(
        "UPDATE sessions SET source_session_key = ? WHERE id = ?",
        ("path-stem", "canonical-current"),
    )
    connection.commit()
    connection.close()

    result = SessionRecommendationSource(path).recommend(
        "current collision request",
        WORKSPACE,
        "path-stem",
        frozenset({content_fingerprint("current collision request")}),
        NOW,
    )

    assert current_id not in {
        item.locator.secondary for item in result.all_candidates
    }


def test_unknown_source_session_key_does_not_hide_older_identical_message(tmp_path):
    path = tmp_path / "unknown-source-key.db"
    connection = _create_recall_database(path)
    older_id = _add_session(
        connection,
        "older",
        "Older repeated request",
        WORKSPACE.key,
        [("user", "repeat request", NOW - 2)],
    )[0]
    connection.commit()
    connection.close()

    result = SessionRecommendationSource(path).recommend(
        "repeat request",
        WORKSPACE,
        "missing-source-key",
        frozenset({content_fingerprint("repeat request")}),
        NOW,
    )

    assert older_id in {item.locator.secondary for item in result.relevance}


def test_legacy_schema_without_source_session_key_uses_canonical_id(tmp_path):
    path = tmp_path / "legacy-current-id.db"
    connection = _create_recall_database(path)
    current_id = _add_session(
        connection,
        "canonical-current",
        "Legacy current",
        WORKSPACE.key,
        [("user", "legacy current request", NOW - 1)],
    )[0]
    connection.execute("ALTER TABLE sessions DROP COLUMN source_session_key")
    connection.commit()
    connection.close()

    result = SessionRecommendationSource(path).recommend(
        "legacy current request",
        WORKSPACE,
        "canonical-current",
        frozenset({content_fingerprint("legacy current request")}),
        NOW,
    )

    assert current_id not in {
        item.locator.secondary for item in result.all_candidates
    }


def test_active_session_history_not_in_visible_context_remains_eligible(recall_db):
    result = SessionRecommendationSource(recall_db).recommend(
        "retained historical",
        WORKSPACE,
        "current",
        {content_fingerprint("current needle")},
        NOW,
    )

    assert any(
        item.locator.primary == "current"
        and item.private_text == "retained historical detail"
        for item in result.relevance
    )


def test_only_latest_matching_user_row_is_inferred_as_current(tmp_path):
    path = tmp_path / "exact-current.db"
    connection = _create_recall_database(path)
    matching_ids = _add_session(
        connection,
        "current",
        "Repeated request",
        WORKSPACE.key,
        [
            ("user", "repeat this request", NOW - 3),
            ("assistant", "repeat this request", NOW - 2),
            ("user", "  REPEAT   this request  ", NOW - 1),
        ],
    )
    mismatched_ids = _add_session(
        connection,
        "mismatched-current",
        "Earlier request",
        WORKSPACE.key,
        [
            ("user", "repeat this request", NOW - 3),
            ("user", "a different latest request", NOW - 1),
        ],
    )
    connection.commit()
    connection.close()
    visible_current_fingerprint = {content_fingerprint("repeat this request")}
    source = SessionRecommendationSource(path)

    matched = source.recommend(
        "repeat this request",
        WORKSPACE,
        "current",
        visible_current_fingerprint,
        NOW,
    )
    mismatched = source.recommend(
        "repeat this request",
        WORKSPACE,
        "mismatched-current",
        visible_current_fingerprint,
        NOW,
    )

    matched_ids = {item.locator.secondary for item in matched.relevance}
    mismatched_result_ids = {item.locator.secondary for item in mismatched.relevance}
    assert matching_ids[2] not in matched_ids
    assert matching_ids[0] in matched_ids
    assert matching_ids[1] in matched_ids
    assert mismatched_ids[0] in mismatched_result_ids


def test_current_and_visible_exclusions_happen_before_channel_limit(tmp_path):
    path = tmp_path / "pre-limit-exclusions.db"
    connection = _create_recall_database(path)
    _add_session(
        connection,
        "current",
        "Current",
        WORKSPACE.key,
        [("user", "crowding needle", NOW)],
    )
    visible_contents = []
    for index in range(11):
        content = f"crowding needle visible {index}"
        visible_contents.append(content)
        _add_session(
            connection,
            f"visible-{index}",
            f"Visible {index}",
            WORKSPACE.key,
            [("assistant", content, NOW - index - 1)],
        )
    eligible_ids = []
    for index in range(12):
        eligible_ids.extend(
            _add_session(
                connection,
                f"eligible-{index}",
                f"Eligible {index}",
                WORKSPACE.key,
                [("user", f"crowding needle eligible {index}", NOW - index - 20)],
            )
        )
    connection.commit()
    connection.close()

    result = SessionRecommendationSource(path).recommend(
        "crowding needle",
        WORKSPACE,
        "current",
        frozenset(content_fingerprint(content) for content in visible_contents),
        NOW,
    )

    assert {item.locator.secondary for item in result.relevance} == set(eligible_ids)
    assert {item.locator.secondary for item in result.recency} == set(eligible_ids)


def test_recency_applies_workspace_priority_before_limit(tmp_path):
    path = tmp_path / "workspace-priority.db"
    connection = _create_recall_database(path)
    for index in range(12):
        _add_session(
            connection,
            f"global-{index}",
            f"Global {index}",
            None,
            [("user", f"global recent {index}", NOW - index)],
        )
    inferred_id = _add_session(
        connection,
        "inferred",
        "Astra master planning",
        None,
        [("assistant", "inferred older", NOW - 100)],
    )[0]
    exact_ids = _add_session(
        connection,
        "exact",
        "Exact workspace",
        WORKSPACE.key,
        [
            ("user", "exact older first", NOW - 200),
            ("assistant", "exact older second", NOW - 200),
        ],
    )
    connection.commit()
    connection.close()

    result = SessionRecommendationSource(path).recommend(
        "unmatched query", WORKSPACE, "current", frozenset(), NOW
    )

    assert [item.locator.secondary for item in result.recency[:2]] == sorted(
        exact_ids,
        key=lambda message_id: session_source_module._identity("exact", message_id),
    )
    assert result.recency[2].locator.secondary == inferred_id
    assert [item.workspace_tier for item in result.recency[:3]] == [0, 0, 1]


def test_recency_pools_keep_older_workspace_rows_without_unbounded_tiering(
    tmp_path, monkeypatch
):
    path = tmp_path / "bounded-recency.db"
    connection = _create_recall_database(path)
    for index in range(2_000):
        _add_session(
            connection,
            f"global-{index}",
            "Global history",
            None,
            [("user", f"global row {index}", NOW - index)],
        )
    inferred_id = _add_session(
        connection,
        "inferred",
        "Astra master planning",
        None,
        [("assistant", "older inferred row", NOW - 3_000)],
    )[0]
    exact_id = _add_session(
        connection,
        "exact",
        "Exact workspace",
        WORKSPACE.key,
        [("user", "oldest exact row", NOW - 4_000)],
    )[0]
    connection.commit()
    connection.close()

    calls = 0
    original = session_source_module._workspace_tier

    def counted(*args):
        nonlocal calls
        calls += 1
        return original(*args)

    monkeypatch.setattr(session_source_module, "_workspace_tier", counted)
    result = SessionRecommendationSource(path).recommend(
        "no-match-token", WORKSPACE, "current", frozenset(), NOW
    )

    assert result.availability == "available"
    assert [item.locator.secondary for item in result.recency[:2]] == [exact_id, inferred_id]
    assert calls <= 256


@pytest.mark.parametrize("exact_session_count", [0, 1, 100])
def test_indexed_recency_pool_stops_after_top_workspace_candidates(tmp_path, exact_session_count):
    """A common workspace must not sort its entire archive for twelve rows."""
    path = tmp_path / "indexed-recency.db"
    connection = _create_recall_database(path)
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE INDEX idx_messages_timestamp ON messages(timestamp DESC, id DESC);
        CREATE INDEX idx_messages_session ON messages(session_id, msg_index);
        CREATE INDEX idx_sessions_workspace_key ON sessions(workspace_key);
        """
    )
    expected_ids = []
    for session_index in range(100):
        ids = _add_session(
            connection,
            f"session-{session_index}",
            "Historical discussion",
            WORKSPACE.key if session_index < exact_session_count else "different-workspace",
            [
                ("user", f"history {session_index} {index}", NOW - session_index * 100 - index)
                for index in range(100)
            ],
        )
        if session_index == 1:
            expected_ids = ids[:12]
    connection.commit()
    instructions = 0

    def work_budget():
        nonlocal instructions
        instructions += 1_000
        return int(instructions >= 25_000)

    # Bound SQLite work, not wall time: a join that scans and sorts all 10,000
    # messages exceeds this budget even on a fast host. The indexed reader
    # only needs the first eligible history after excluding the live session.
    connection.set_progress_handler(work_budget, 1_000)
    try:
        rows = SessionRecommendationSource(path)._recency_rows(
            connection,
            WORKSPACE,
            "session-0",
            None,
            frozenset(),
            has_workspace_key=True,
        )
        assert [row.id for row in rows] == expected_ids
    finally:
        connection.close()


@pytest.mark.parametrize("old_workspace_rows", [0, 1, 20])
def test_old_sparse_workspace_recency_does_not_scan_unrelated_archive(tmp_path, old_workspace_rows):
    path = tmp_path / "old-workspace-recency.db"
    connection = _create_recall_database(path)
    connection.row_factory = sqlite3.Row
    connection.executescript("""
        CREATE INDEX idx_messages_timestamp ON messages(timestamp DESC, id DESC);
        CREATE INDEX idx_messages_session ON messages(session_id, msg_index);
        CREATE INDEX idx_messages_session_timestamp ON messages(session_id, timestamp DESC, id DESC);
        CREATE INDEX idx_sessions_workspace_key ON sessions(workspace_key);
    """)
    global_ids = _add_session(
        connection, "global", "Unrelated discussion", "other-workspace",
        [("user", f"global history {index}", NOW - index) for index in range(10_000)],
    )
    old_ids = _add_session(
        connection, "old", "Historical discussion", WORKSPACE.key,
        [("user", f"old history {index}", NOW - 20_000 - index)
         for index in range(old_workspace_rows)]
        or [("tool", "no eligible history", NOW - 20_000)],
    )
    connection.commit()
    instructions = 0

    def work_budget():
        nonlocal instructions
        instructions += 1_000
        return int(instructions >= 25_000)

    # The bounded recent probe plus the workspace index can answer both an
    # empty historical workspace and older workspaces below/above pool size.
    # A timestamp scan of the unrelated 10,000 rows exhausts this VM budget.
    connection.set_progress_handler(work_budget, 1_000)
    try:
        rows = SessionRecommendationSource(path)._recency_rows(
            connection, WORKSPACE, "current", None, frozenset(), has_workspace_key=True,
        )
        expected = old_ids if old_workspace_rows else []
        assert [row.id for row in rows] == (expected + global_ids)[:12]
    finally:
        connection.close()


def test_recency_equal_timestamps_order_by_private_identity_not_message_id(tmp_path):
    path = tmp_path / "identity-ordering.db"
    connection = _create_recall_database(path)
    exact_ids = _add_session(
        connection,
        "exact",
        "Exact workspace",
        WORKSPACE.key,
        [
            ("user", "first same timestamp", NOW - 1),
            ("assistant", "second same timestamp", NOW - 1),
        ],
    )
    connection.commit()
    connection.close()

    result = SessionRecommendationSource(path).recommend(
        "no-match-token", WORKSPACE, "current", frozenset(), NOW
    )
    expected = sorted(
        exact_ids,
        key=lambda message_id: session_source_module._identity("exact", message_id),
    )

    assert expected != sorted(exact_ids, reverse=True)
    assert [item.locator.secondary for item in result.recency[:2]] == expected


def test_candidate_descriptions_sanitize_paths_sql_and_exceptions(tmp_path):
    path = tmp_path / "sanitized-descriptions.db"
    connection = _create_recall_database(path)
    unsafe_contents = [
        r"review paths /Users/alice/private/project.py and C:\Users\alice\secret.py",
        "SELECT review_secret FROM private_table WHERE enabled = 1",
        "review ordinary recovery notes",
    ]
    session_rows = [
        ("path", "Review paths", unsafe_contents[0]),
        ("sql", "Database review", unsafe_contents[1]),
        ("exception", "ValueError: review_title_secret", unsafe_contents[2]),
        ("ordinary", "Migration review", "review the useful migration summary"),
    ]
    for session_id, title, content in session_rows:
        _add_session(
            connection,
            session_id,
            title,
            WORKSPACE.key,
            [("assistant", content, NOW - len(session_rows))],
        )
    connection.commit()
    connection.close()

    result = SessionRecommendationSource(path).recommend(
        "review", WORKSPACE, "current", frozenset(), NOW
    )
    descriptions = {item.private_text: item.description for item in result.relevance}

    path_description = descriptions[unsafe_contents[0]]
    assert "/Users/alice/private/project.py" not in path_description
    assert r"C:\Users\alice\secret.py" not in path_description
    assert "Review paths" in path_description
    assert "SELECT" not in descriptions[unsafe_contents[1]]
    assert "review_secret" not in descriptions[unsafe_contents[1]]
    assert "ValueError" not in descriptions[unsafe_contents[2]]
    assert "review_title_secret" not in descriptions[unsafe_contents[2]]
    assert "useful migration summary" in descriptions["review the useful migration summary"]
    assert all(item.project_label == WORKSPACE.label for item in result.relevance)


def test_candidate_descriptions_sanitize_embedded_sensitive_signatures(tmp_path):
    path = tmp_path / "embedded-sensitive-descriptions.db"
    connection = _create_recall_database(path)
    unsafe_contents = {
        "windows-forward-slashes": "review C:/Users/alice/private/project.py now",
        "sql-mid-sentence": (
            "review query SELECT secret_column FROM private_table before merge"
        ),
        "exception-after-punctuation": "failure=ValueError: private detail",
    }
    for session_id, content in unsafe_contents.items():
        _add_session(
            connection,
            session_id,
            f"Review {session_id}",
            WORKSPACE.key,
            [("assistant", content, NOW - 1)],
        )
    connection.commit()
    connection.close()

    source = SessionRecommendationSource(path)
    for query, content, sensitive in (
        (
            "review",
            unsafe_contents["windows-forward-slashes"],
            "C:/Users/alice/private/project.py",
        ),
        (
            "review",
            unsafe_contents["sql-mid-sentence"],
            "SELECT secret_column FROM private_table",
        ),
        (
            "failure",
            unsafe_contents["exception-after-punctuation"],
            "ValueError: private detail",
        ),
    ):
        result = source.recommend(query, WORKSPACE, "current", frozenset(), NOW)
        description = next(
            item.description for item in result.relevance if item.private_text == content
        )
        assert sensitive not in description


def test_candidate_descriptions_preserve_ordinary_select_from_text(tmp_path):
    path = tmp_path / "ordinary-select-descriptions.db"
    connection = _create_recall_database(path)
    ordinary_contents = (
        "Select a model from the menu",
        "Select the best option from these alternatives",
        "Select a model from the menu, then compare the results",
    )
    for index, content in enumerate(ordinary_contents):
        _add_session(
            connection,
            f"ordinary-{index}",
            "Ordinary selection guidance",
            WORKSPACE.key,
            [("assistant", content, NOW - index - 1)],
        )
    connection.commit()
    connection.close()

    result = SessionRecommendationSource(path).recommend(
        "select", WORKSPACE, "current", frozenset(), NOW
    )
    descriptions = {item.private_text: item.description for item in result.relevance}

    assert descriptions[ordinary_contents[0]].endswith(ordinary_contents[0])
    assert descriptions[ordinary_contents[1]].endswith(ordinary_contents[1])
    assert descriptions[ordinary_contents[2]].endswith(ordinary_contents[2])


def test_active_context_fingerprints_use_only_ordered_multimodal_text():
    data_url = "data:image/png;base64," + "A" * 10_000
    fingerprints = active_context_fingerprints(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "First"},
                    {"type": "image_url", "image_url": {"url": data_url}},
                    {"type": "input_text", "text": " second  "},
                ],
            },
            {"role": "assistant", "content": "Plain response"},
            {"role": "tool", "content": "ignore tool text"},
        ]
    )

    assert fingerprints == {
        content_fingerprint("First second"),
        content_fingerprint("Plain response"),
    }
    assert content_fingerprint(data_url) not in fingerprints


def test_cjk_fts_and_short_query_fallback_remain_available(tmp_path):
    path = tmp_path / "cjk.db"
    connection = _create_recall_database(path)
    _add_session(
        connection,
        "cjk",
        "中文讨论",
        None,
        [("user", "推荐系统课程和图搜索", NOW - 1)],
    )
    connection.commit()
    connection.close()
    source = SessionRecommendationSource(path)

    assert source.recommend(
        "推荐系统", WORKSPACE, "current", frozenset(), NOW
    ).relevance
    assert source.recommend(
        "图", WORKSPACE, "current", frozenset(), NOW
    ).relevance


def test_rare_like_query_does_not_rescan_workspace_history(tmp_path):
    path = _create_broad_recall_database(tmp_path / "rare-like.db", rows=10_000)
    connection = sqlite3.connect(path)
    set_read_window(connection)
    connection.execute("CREATE INDEX idx_messages_session ON messages(session_id, msg_index)")
    connection.execute("CREATE INDEX idx_sessions_workspace_key ON sessions(workspace_key)")
    connection.execute("UPDATE sessions SET workspace_key = ?", (WORKSPACE.key,))
    target = _add_session(
        connection, "target", "Target", WORKSPACE.key,
        [("user", "specificneedle 图", NOW + 1)],
    )[0]
    _add_session(
        connection, "false-positive", "False positive", WORKSPACE.key,
        [("user", "specificneedle without the short term", NOW + 2)],
    )
    connection.commit()
    connection.close()
    with open_readonly(path) as reader:
        vm_calls = 0

        def limit_work():
            nonlocal vm_calls
            vm_calls += 1
            return int(vm_calls >= 50)

        reader.set_progress_handler(limit_work, 1000)
        rows = SessionRecommendationSource(path)._like_rows(
            reader, ["specificneedle", "图"],
            WorkspaceIdentity(key=WORKSPACE.key, root="", label=""),
            "current", None, frozenset(), True,
        )

    assert [row["id"] for row in rows] == [target]
    assert vm_calls < 50


def test_like_results_do_not_depend_on_fts_index_completeness(tmp_path):
    path = tmp_path / "incomplete-fts.db"
    connection = _create_recall_database(path)
    ids = _add_session(
        connection, "history", "History", WORKSPACE.key,
        [("user", "needle 图 old", NOW - 2), ("user", "needle 图 new", NOW - 1)],
    )
    connection.execute(
        "INSERT INTO messages_fts(messages_fts, rowid, content) VALUES ('delete', ?, ?)",
        (ids[1], "needle 图 new"),
    )
    assert [row[0] for row in connection.execute(
        "SELECT rowid FROM messages_fts WHERE messages_fts MATCH 'needle'"
    )] == [ids[0]]
    connection.commit()
    connection.close()

    result = SessionRecommendationSource(path).recommend(
        "needle 图", WorkspaceIdentity(key=WORKSPACE.key, root="", label=""),
        "current", frozenset(), NOW,
    )

    assert not result.error_category
    assert [item.locator.secondary for item in result.relevance] == ids[::-1]


def test_complete_like_pool_preserves_public_workspace_order(tmp_path):
    path = tmp_path / "like-seed-order.db"
    connection = _create_recall_database(path)
    exact = _add_session(
        connection, "exact", "Exact", WORKSPACE.key,
        [("user", "needle 图 exact", NOW - 3)],
    )[0]
    inferred = _add_session(
        connection, "inferred", "Astra master planning", None,
        [("user", "needle 图 inferred", NOW - 2)],
    )[0]
    global_id = _add_session(
        connection, "global", "Other", "/other",
        [("user", "needle 图 global", NOW - 1)],
    )[0]
    connection.commit()
    connection.close()

    result = SessionRecommendationSource(path).recommend(
        "needle 图", WORKSPACE, "current", frozenset(), NOW,
    )

    assert not result.error_category
    assert [item.locator.secondary for item in result.relevance] == [exact, inferred, global_id]


def test_full_like_pool_does_not_hide_older_workspace_matches(tmp_path):
    path = tmp_path / "like-pool-bound.db"
    connection = _create_recall_database(path)
    newer = _add_session(
        connection, "global", "Other", "/other",
        [("user", f"needle 图 global {index}", NOW - index) for index in range(13)],
    )
    exact = _add_session(
        connection, "exact", "Exact", WORKSPACE.key,
        [("user", "needle 图 older workspace", NOW - 100)],
    )[0]
    connection.row_factory = sqlite3.Row
    rows = SessionRecommendationSource(path)._like_rows(
        connection, ["needle", "图"], WORKSPACE, "current", None, frozenset(), True,
    )
    connection.close()

    assert [row["id"] for row in rows] == [exact, *newer]


def test_empty_and_like_pool_keeps_or_fallback_and_active_exclusions(tmp_path):
    path = tmp_path / "like-or-fallback.db"
    connection = _create_recall_database(path)
    exact = _add_session(
        connection, "exact", "Exact", WORKSPACE.key,
        [("user", "needle without short term", NOW - 3)],
    )[0]
    global_id = _add_session(
        connection, "global", "Other", "/other",
        [("user", "图 without long term", NOW - 2)],
    )[0]
    _add_session(
        connection, "visible", "Visible", None,
        [("user", "needle visible", NOW - 1)],
    )
    connection.commit()
    connection.close()

    result = SessionRecommendationSource(path).recommend(
        "needle 图", WORKSPACE, "current", frozenset({content_fingerprint("needle visible")}), NOW,
    )

    assert not result.error_category
    assert [item.locator.secondary for item in result.relevance] == [exact, global_id]


@pytest.mark.parametrize("query", ["the", "的"])
def test_broad_relevance_terms_return_bounded_candidates(tmp_path, query, monkeypatch):
    path = _create_broad_recall_database(
        tmp_path / f"broad-{ord(query[0])}.db", rows=10_000
    )
    # Check complete candidate selection independently of host scheduling.
    # Deadline degradation and wall-clock performance have separate regressions.
    monkeypatch.setattr(
        session_source_module, "time", SimpleNamespace(monotonic=lambda: 0.0)
    )

    result = SessionRecommendationSource(path, deadline_ms=75).recommend(
        query, WORKSPACE, "current", frozenset(), NOW
    )

    assert result.availability == "available"
    assert result.error_category == ""
    assert len(result.relevance) == 12
    assert len(result.recency) == 12


def test_relevance_pools_preserve_tier_before_native_rank(tmp_path):
    path = tmp_path / "relevance-tier.db"
    connection = _create_recall_database(path)
    for index in range(40):
        _add_session(
            connection,
            f"global-{index}",
            "Global",
            None,
            [("user", "needle needle needle global", NOW - index)],
        )
    inferred_id = _add_session(
        connection,
        "inferred",
        "Astra master planning",
        None,
        [("user", "needle inferred", NOW - 100)],
    )[0]
    exact_ids = _add_session(
        connection,
        "exact",
        "Exact",
        WORKSPACE.key,
        [
            ("user", "needle exact", NOW - 200),
            ("assistant", "needle needle exact", NOW - 201),
        ],
    )
    connection.commit()
    connection.close()

    result = SessionRecommendationSource(path).recommend(
        "needle", WORKSPACE, "current", frozenset(), NOW
    )

    assert [row.workspace_tier for row in result.relevance[:3]] == [0, 0, 1]
    assert {row.locator.secondary for row in result.relevance[:2]} == set(exact_ids)
    assert result.relevance[2].locator.secondary == inferred_id
    assert result.relevance[0].native_query_rank <= result.relevance[1].native_query_rank


def test_content_inferred_cutoff_uses_user_only_bm25_across_more_than_64_sessions(
    tmp_path,
):
    path = tmp_path / "content-inferred-cutoff.db"
    connection = _create_recall_database(path)
    for index in range(20):
        _add_session(
            connection,
            f"stronger-global-{index}",
            "Global",
            f"/work/other-{index}",
            [
                (
                    "user",
                    ("needle " * 80) + f"global {index}",
                    NOW - index,
                )
            ],
        )
    content_inferred_ids = []
    for index in range(80):
        content_inferred_ids.extend(
            _add_session(
                connection,
                f"compound-favored-{index}",
                "Unrelated",
                None,
                [
                    (
                        "user",
                        "needle " + ("astra-master " * 40) + str(index),
                        NOW - 100 - index,
                    )
                ],
            )
        )
    user_rank_first_id = _add_session(
        connection,
        "user-rank-first",
        "Unrelated",
        None,
        [
            (
                "user",
                ("needle " * 12) + "astra-master target",
                NOW - 1_000,
            )
        ],
    )[0]
    connection.commit()

    candidate_ids = (*content_inferred_ids, user_rank_first_id)
    placeholders = ", ".join("?" for _ in candidate_ids)
    user_query = session_source_module._fts_query("needle")
    workspace_query = session_source_module._fts_query(WORKSPACE.label)
    user_order = [
        int(row[0])
        for row in connection.execute(
            "SELECT m.id FROM messages_fts "
            "JOIN messages AS m ON m.rowid = messages_fts.rowid "
            f"WHERE messages_fts MATCH ? AND m.id IN ({placeholders}) "
            "ORDER BY bm25(messages_fts) ASC, m.timestamp DESC, m.id DESC",
            (user_query, *candidate_ids),
        )
    ]
    compound_order = [
        int(row[0])
        for row in connection.execute(
            "SELECT m.id FROM messages_fts "
            "JOIN messages AS m ON m.rowid = messages_fts.rowid "
            f"WHERE messages_fts MATCH ? AND m.id IN ({placeholders}) "
            "ORDER BY bm25(messages_fts) ASC, m.timestamp DESC, m.id DESC",
            (f"({user_query}) AND ({workspace_query})", *candidate_ids),
        )
    ]
    connection.close()

    assert len(candidate_ids) > 64
    assert user_order[0] == user_rank_first_id
    assert compound_order.index(user_rank_first_id) >= 64

    result = SessionRecommendationSource(path).recommend(
        "needle", WORKSPACE, "current", frozenset(), NOW
    )

    assert result.relevance[0].locator.secondary == user_rank_first_id
    assert result.relevance[0].workspace_tier == 1


def test_content_inferred_rows_use_user_only_bm25_for_tier_ordering(tmp_path):
    path = tmp_path / "content-inferred-rank.db"
    connection = _create_recall_database(path)
    for index in range(64):
        _add_session(
            connection,
            f"newer-workspace-{index}",
            "Unrelated",
            None,
            [("user", f"astra-master filler {index}", NOW - index)],
        )
    for index in range(1_000):
        _add_session(
            connection,
            f"unrelated-{index}",
            "Unrelated",
            None,
            [("user", f"unrelated corpus {index}", NOW - 2_000 - index)],
        )
    title_inferred_id = _add_session(
        connection,
        "title-inferred",
        "Astra master planning",
        None,
        [("user", "needle needle title", NOW - 500)],
    )[0]
    user_rank_first_id = _add_session(
        connection,
        "user-rank-first",
        "Unrelated",
        None,
        [("user", "needle astra-master other", NOW - 1_000)],
    )[0]
    compound_rank_first_id = _add_session(
        connection,
        "compound-rank-first",
        "Unrelated",
        None,
        [
            (
                "user",
                "needle astra-master astra-master",
                NOW - 1_001,
            )
        ],
    )[0]
    global_id = _add_session(
        connection,
        "global",
        "Global",
        None,
        [("user", "needle needle needle needle global", NOW - 10)],
    )[0]
    connection.commit()

    candidate_ids = (
        title_inferred_id,
        user_rank_first_id,
        compound_rank_first_id,
    )
    placeholders = ", ".join("?" for _ in candidate_ids)
    user_query = session_source_module._fts_query("needle")
    workspace_query = session_source_module._fts_query(WORKSPACE.label)
    user_ranks = {
        int(row[0]): float(row[1])
        for row in connection.execute(
            "SELECT m.id, bm25(messages_fts) FROM messages_fts "
            "JOIN messages AS m ON m.rowid = messages_fts.rowid "
            f"WHERE messages_fts MATCH ? AND m.id IN ({placeholders})",
            (user_query, *candidate_ids),
        )
    }
    compound_order = [
        int(row[0])
        for row in connection.execute(
            "SELECT m.id FROM messages_fts "
            "JOIN messages AS m ON m.rowid = messages_fts.rowid "
            f"WHERE messages_fts MATCH ? AND m.id IN ({placeholders}) "
            "ORDER BY bm25(messages_fts) ASC, m.timestamp DESC, m.id DESC",
            (f"({user_query}) AND ({workspace_query})", *candidate_ids),
        )
    ]
    connection.close()

    expected = sorted(
        candidate_ids,
        key=lambda message_id: (
            1,
            user_ranks[message_id],
            -{
                title_inferred_id: NOW - 500,
                user_rank_first_id: NOW - 1_000,
                compound_rank_first_id: NOW - 1_001,
            }[message_id],
            session_source_module._identity(
                {
                    title_inferred_id: "title-inferred",
                    user_rank_first_id: "user-rank-first",
                    compound_rank_first_id: "compound-rank-first",
                }[message_id],
                message_id,
            ),
        ),
    )
    assert compound_order != [
        message_id
        for message_id in expected
        if message_id in {user_rank_first_id, compound_rank_first_id}
    ]

    result = SessionRecommendationSource(path).recommend(
        "needle", WORKSPACE, "current", frozenset(), NOW
    )
    actual = [
        item.locator.secondary
        for item in result.relevance
        if item.locator.secondary in candidate_ids
    ]

    assert actual == expected
    assert all(
        item.workspace_tier == 1
        and item.native_query_rank == pytest.approx(user_ranks[item.locator.secondary])
        for item in result.relevance
        if item.locator.secondary in candidate_ids
    )
    assert next(item for item in result.relevance if item.locator.secondary == global_id).workspace_tier == 2


def test_default_blank_workspace_key_is_missing_for_title_inferred_recency(tmp_path):
    path = tmp_path / "blank-key-title-recency.db"
    recall = SessionRecall(path)
    recall.init_db()
    inferred_session_id = recall.create_session("Astra master planning")
    inferred_message_id = recall.log_message(
        inferred_session_id,
        "assistant",
        "older inferred planning row",
    )
    for index in range(80):
        session_id = recall.create_session(
            f"Newer global {index}",
            workspace_key=f"/work/other-{index}",
        )
        recall.log_message(session_id, "user", f"newer global row {index}")
    assert recall._get_conn().execute(
        "SELECT workspace_key FROM sessions WHERE id = ?",
        (inferred_session_id,),
    ).fetchone()[0] == ""
    recall.close()

    result = SessionRecommendationSource(path).recommend(
        "definitely-no-match-token", WORKSPACE, "current", frozenset(), NOW
    )

    assert result.recency[0].locator.secondary == inferred_message_id
    assert result.recency[0].workspace_tier == 1


def test_default_blank_workspace_key_is_missing_for_content_inferred_relevance(
    tmp_path,
):
    path = tmp_path / "blank-key-content-relevance.db"
    recall = SessionRecall(path)
    recall.init_db()
    inferred_session_id = recall.create_session("Unrelated")
    inferred_message_id = recall.log_message(
        inferred_session_id,
        "user",
        "needle astra-master",
    )
    for index in range(80):
        session_id = recall.create_session(
            f"Newer global {index}",
            workspace_key=f"/work/other-{index}",
        )
        recall.log_message(
            session_id,
            "user",
            ("needle " * 80) + f"global {index}",
        )
    assert recall._get_conn().execute(
        "SELECT workspace_key FROM sessions WHERE id = ?",
        (inferred_session_id,),
    ).fetchone()[0] == ""
    recall.close()

    result = SessionRecommendationSource(path).recommend(
        "needle", WORKSPACE, "current", frozenset(), NOW
    )

    assert result.relevance[0].locator.secondary == inferred_message_id
    assert result.relevance[0].workspace_tier == 1


def test_broad_relevance_hashes_only_bounded_candidate_union(tmp_path, monkeypatch):
    path = _create_broad_recall_database(tmp_path / "bounded-hashes.db", rows=10_000)
    # Exercise the full candidate union to measure hash work. Host scheduling
    # must not short-circuit this semantic bound via the source deadline.
    monkeypatch.setattr(
        session_source_module, "time", SimpleNamespace(monotonic=lambda: 0.0)
    )
    calls = 0
    original = session_source_module.content_fingerprint

    def counted(value):
        nonlocal calls
        calls += 1
        return original(value)

    monkeypatch.setattr(session_source_module, "content_fingerprint", counted)
    result = SessionRecommendationSource(path).recommend(
        "the", WORKSPACE, "current", frozenset(), NOW
    )

    assert result.availability == "available"
    assert result.error_category == ""
    assert result.relevance
    assert calls <= 256


def test_channels_are_capped_at_twelve_after_exclusions(tmp_path):
    path = tmp_path / "bounded.db"
    connection = _create_recall_database(path)
    for index in range(20):
        _add_session(
            connection,
            f"session-{index}",
            f"Candidate {index}",
            None,
            [("user", f"bounded needle candidate {index}", NOW - index)],
        )
    connection.commit()
    connection.close()

    result = SessionRecommendationSource(path).recommend(
        "bounded needle", WORKSPACE, "current", frozenset(), NOW
    )

    assert len(result.relevance) == 12
    assert len(result.recency) == 12


def test_open_validates_pair_and_bounds_neighbor_window_and_content(tmp_path):
    path = tmp_path / "open.db"
    connection = _create_recall_database(path)
    message_ids = _add_session(
        connection,
        "session-a",
        "Evidence title",
        WORKSPACE.key,
        [
            ("user", "before", NOW - 3),
            ("assistant", "A" * 700, NOW - 2),
            ("user", "after", NOW - 1),
        ],
    )
    _add_session(
        connection,
        "session-b",
        "Other",
        None,
        [("user", "other", NOW)],
    )
    connection.commit()
    connection.close()
    source = SessionRecommendationSource(path)

    evidence = source.open(
        SourceLocator("session_message", "session-a", message_ids[1]), window=1
    )
    mismatched = source.open(
        SourceLocator("session_message", "session-b", message_ids[1]), window=5
    )

    assert evidence.source == "session"
    assert evidence.trust_label == "historical_context"
    assert evidence.title == "Evidence title"
    assert len(evidence.items) == 3
    assert "A" * 500 in evidence.items[1]
    assert "A" * 501 not in evidence.items[1]
    assert mismatched.items == ()


@pytest.mark.parametrize(
    ("database_factory", "expected_category"),
    [
        (lambda path: sqlite3.connect(path).close(), "incompatible_schema"),
        (lambda path: path.write_bytes(b"not a sqlite database"), "database_error"),
    ],
)
def test_database_failures_use_bounded_categories(
    tmp_path, database_factory, expected_category
):
    path = tmp_path / "broken.db"
    database_factory(path)

    result = SessionRecommendationSource(path).recommend(
        "needle", WORKSPACE, "current", frozenset(), NOW
    )

    assert result.availability == "error"
    assert result.error_category == expected_category
    assert str(path) not in result.error_category


def test_progress_deadline_is_categorized_and_connection_cleanup_is_safe(tmp_path):
    path = tmp_path / "large.db"
    connection = _create_recall_database(path)
    for index in range(2_000):
        _add_session(
            connection,
            f"session-{index}",
            "Large fixture",
            None,
            [("user", f"deadline needle {index}", NOW - index)],
        )
    connection.commit()
    connection.close()

    result = SessionRecommendationSource(path, deadline_ms=0).recommend(
        "deadline needle", WORKSPACE, "current", frozenset(), NOW
    )

    assert result.availability == "error"
    assert result.error_category == "deadline"
    with open_readonly(path) as reopened:
        assert reopened.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2_000


def _seed_current_and_other_sessions(connection: sqlite3.Connection) -> None:
    _add_session(
        connection,
        "current-session",
        "Current work",
        WORKSPACE.key,
        [
            ("user", "current history alpha message", NOW - 30),
            ("assistant", "current history beta response", NOW - 20),
            ("user", "unique relevance needle xyzzy", NOW - 10),
        ],
    )
    _add_session(
        connection,
        "older-session",
        "Other work",
        WORKSPACE.key,
        [("user", "older session plain content", NOW - 100)],
    )
    connection.commit()


def test_recency_candidates_exclude_current_session_rows(tmp_path):
    path = tmp_path / "sessions.db"
    connection = _create_recall_database(path)
    _seed_current_and_other_sessions(connection)
    connection.close()

    result = SessionRecommendationSource(path).recommend(
        "", WORKSPACE, "current-session", frozenset(), NOW
    )

    assert result.availability == "available"
    assert result.recency, "recency channel must still surface other sessions"
    assert {item.locator.primary for item in result.recency} == {"older-session"}


def test_relevance_candidates_still_recover_unseen_current_session_rows(tmp_path):
    path = tmp_path / "sessions.db"
    connection = _create_recall_database(path)
    _seed_current_and_other_sessions(connection)
    connection.close()

    result = SessionRecommendationSource(path).recommend(
        "xyzzy", WORKSPACE, "current-session", frozenset(), NOW
    )

    assert result.availability == "available"
    assert any(
        item.locator.primary == "current-session" for item in result.relevance
    ), "a real query match may still recover live-session history outside the visible window"


def test_blank_message_rows_are_never_recommended(tmp_path):
    path = tmp_path / "sessions.db"
    connection = _create_recall_database(path)
    _add_session(
        connection,
        "noisy-session",
        "Noisy work",
        WORKSPACE.key,
        [
            ("user", "normal content line", NOW - 60),
            ("user", "", NOW - 50),
            ("user", " ", NOW - 40),
        ],
    )
    connection.commit()
    connection.close()

    result = SessionRecommendationSource(path).recommend(
        "", WORKSPACE, "current-session", frozenset(), NOW
    )

    assert result.availability == "available"
    for item in result.all_candidates:
        assert item.private_text.strip(), (
            "blank message rows must not consume recommendation slots"
        )


def test_missing_current_session_mapping_keeps_rows_recommendable(tmp_path):
    path = tmp_path / "sessions.db"
    connection = _create_recall_database(path)
    _seed_current_and_other_sessions(connection)
    connection.close()

    result = SessionRecommendationSource(path).recommend(
        "", WORKSPACE, "", frozenset(), NOW
    )

    assert result.availability == "available"
    assert {item.locator.primary for item in result.recency} == {
        "current-session",
        "older-session",
    }
