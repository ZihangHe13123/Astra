import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent.runtime import activity_store
from agent.runtime.context_index import embedder as embedder_module
from agent.runtime.context_index import sqlite_reader
from agent.runtime.context_index.lexical import LexicalQuery
from agent.runtime.context_index.activity_source import ActivityRecommendationReader
from agent.runtime.context_index.models import SourceLocator
from agent.runtime.context_index.workspace import WorkspaceIdentity
from agent.runtime.tools import activity as activity_tool


@pytest.fixture(autouse=True)
def _isolate_vectors_db(tmp_path, monkeypatch):
    """测试默认无向量库（指向不存在路径）：旧词法用例不得受仓库机器上的
    生产 context-vectors.db 影响；需要向量的用例自行覆盖此 env。"""
    monkeypatch.setenv("ASTRA_CONTEXT_INDEX_VECTORS_DB", str(tmp_path / "absent-vectors.db"))


NOW_DT = datetime(2026, 8, 31, 8, 0, tzinfo=UTC)
NOW = NOW_DT.timestamp()
WORKSPACE = WorkspaceIdentity(
    key="/users/test/astra-master",
    root="/Users/test/astra-master",
    label="astra-master",
)


def _create_activity_database(
    path: Path, *, include_sync_files: bool = True, include_epochs: bool = True
) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    sqlite_reader.set_read_window(connection)
    connection.create_function("context_activity_timestamp", 1, lambda text: datetime.fromisoformat(str(text)).timestamp(), deterministic=True)
    connection.executescript(
        """
        CREATE TABLE activity_events (
            segment_id TEXT NOT NULL,
            event_id INTEGER NOT NULL,
            occurred_at TEXT NOT NULL,
            occurred_at_us INTEGER,
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
            period_end_us INTEGER,
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
                rowid, app_name, window_title, url_search_text, selection_text, searchable_text
            ) VALUES (
                new.rowid, new.app_name, new.window_title, new.url_search_text,
                new.selection_text, new.searchable_text
            );
        END;
        CREATE TRIGGER activity_summaries_ai AFTER INSERT ON activity_summaries BEGIN
            INSERT INTO activity_summaries_fts(rowid, content) VALUES (new.rowid, new.content);
        END;
        """
    )
    if include_epochs:
        connection.execute("CREATE INDEX idx_activity_events_occurred_at_us ON activity_events(occurred_at_us)")
        connection.execute("CREATE INDEX idx_activity_summaries_period_end_us ON activity_summaries(period_end_us)")
    else:
        connection.execute("ALTER TABLE activity_events DROP COLUMN occurred_at_us")
        connection.execute("ALTER TABLE activity_summaries DROP COLUMN period_end_us")
    if include_sync_files:
        connection.execute(
            """CREATE TABLE sync_files (
                source_path TEXT PRIMARY KEY,
                source_kind TEXT NOT NULL,
                source_identity TEXT NOT NULL DEFAULT '',
                byte_offset INTEGER NOT NULL DEFAULT 0,
                observed_size INTEGER NOT NULL DEFAULT 0,
                observed_mtime_ns INTEGER NOT NULL DEFAULT 0,
                last_success_at TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT ''
            )"""
        )
    return connection


def _iso(seconds_before_now: int) -> str:
    return (NOW_DT - timedelta(seconds=seconds_before_now)).isoformat()


def _epoch_us(value: str) -> int:
    return int(datetime.fromisoformat(value).timestamp() * 1_000_000)


def _add_summary(
    connection: sqlite3.Connection,
    summary_id: str,
    content: str,
    *,
    seconds_before_now: int,
) -> None:
    connection.execute(
        """INSERT INTO activity_summaries(
            summary_id, source_path, granularity, period_start, period_end, period_end_us,
            content, content_hash, source_mtime_ns, imported_at
        ) VALUES (?, ?, '10min', ?, ?, ?, ?, ?, 1, ?)""",
        (
            summary_id,
            f"/private/source/{summary_id}.md",
            _iso(seconds_before_now + 600),
            _iso(seconds_before_now),
            _epoch_us(_iso(seconds_before_now)),
            content,
            f"hash-{summary_id}",
            _iso(seconds_before_now),
        ),
    )


def _add_event(
    connection: sqlite3.Connection,
    segment_id: str,
    event_id: int,
    *,
    seconds_before_now: int,
    app_name: str = "Safari",
    window_title: str = "Astra cached activity",
    url: str = "https://example.com/astra/index?token=TOPSECRET#private-fragment",
    selection_text: str = "PRIVATE_SELECTION",
    searchable_text: str = "Astra cached event needle",
    raw_json: str = '{"private":"RAW_SECRET"}',
) -> None:
    connection.execute(
        """INSERT INTO activity_events(
            segment_id, event_id, occurred_at, occurred_at_us, kind, app_name, bundle_id,
            window_title, url, url_search_text, selection_text, searchable_text,
            raw_json, imported_at
        ) VALUES (?, ?, ?, ?, 'selection', ?, 'com.apple.Safari', ?, ?, ?, ?, ?, ?, ?)""",
        (
            segment_id,
            event_id,
            _iso(seconds_before_now),
            _epoch_us(_iso(seconds_before_now)),
            app_name,
            window_title,
            url,
            activity_store.sanitize_url(url),
            selection_text,
            searchable_text,
            raw_json,
            _iso(seconds_before_now),
        ),
    )


@pytest.fixture
def activity_db(tmp_path: Path) -> Path:
    path = tmp_path / "activity.sqlite3"
    connection = _create_activity_database(path)
    _add_summary(
        connection,
        "summary-astra",
        "Astra cached summary Authorization: Bearer SUMMARY_SECRET",
        seconds_before_now=120,
    )
    _add_event(connection, "segment-a", 7, seconds_before_now=60)
    connection.execute(
        """INSERT INTO sync_files(
            source_path, source_kind, last_success_at
        ) VALUES ('/private/events.jsonl', 'events', ?)""",
        ((NOW_DT - timedelta(hours=25)).isoformat(),),
    )
    connection.commit()
    connection.close()
    return path


def test_reader_uses_readonly_uri_never_constructs_store_and_does_not_mutate(
    monkeypatch: pytest.MonkeyPatch, activity_db: Path
) -> None:
    monkeypatch.setattr(
        activity_store,
        "ActivityStore",
        lambda *args, **kwargs: pytest.fail("must not construct ActivityStore"),
    )
    original_connect = sqlite_reader.sqlite3.connect
    opens: list[tuple[object, bool]] = []

    def recording_connect(database, *args, **kwargs):
        opens.append((database, kwargs.get("uri", False)))
        return original_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite_reader.sqlite3, "connect", recording_connect)
    before = (
        activity_db.stat().st_size,
        activity_db.stat().st_mtime_ns,
        activity_db.read_bytes(),
    )

    result = ActivityRecommendationReader(activity_db).recommend("Astra", WORKSPACE, NOW)

    assert result.availability == "available"
    assert result.relevance
    assert opens and all("mode=ro" in str(database) and uri for database, uri in opens)
    assert (
        activity_db.stat().st_size,
        activity_db.stat().st_mtime_ns,
        activity_db.read_bytes(),
    ) == before


def test_absent_activity_database_creates_nothing(tmp_path: Path) -> None:
    path = tmp_path / ".astra" / "activity-history.sqlite3"

    result = ActivityRecommendationReader(path).recommend("Astra", WORKSPACE, NOW)

    assert result.availability == "absent"
    assert not path.parent.exists()


def test_stale_cache_is_metadata_and_never_syncs(
    monkeypatch: pytest.MonkeyPatch, activity_db: Path
) -> None:
    monkeypatch.setattr(
        activity_tool,
        "_sync_activity_store",
        lambda *_: pytest.fail("automatic recommendations must not sync"),
    )

    result = ActivityRecommendationReader(activity_db).recommend("Astra", WORKSPACE, NOW)

    assert result.availability == "available"
    assert result.cache_state == "stale-cache"
    assert result.relevance
    assert all(item.cache_state == "stale-cache" for item in result.all_candidates)


@pytest.mark.parametrize(
    ("hours_old", "expected"),
    [(23, "cached"), (24, "cached"), (25, "stale-cache")],
)
def test_cache_freshness_uses_newest_successful_timestamp(
    tmp_path: Path, hours_old: int, expected: str
) -> None:
    path = tmp_path / f"freshness-{hours_old}.sqlite3"
    connection = _create_activity_database(path)
    _add_summary(connection, "summary", "freshness Astra summary", seconds_before_now=30)
    connection.executemany(
        "INSERT INTO sync_files(source_path, source_kind, last_success_at) VALUES (?, 'events', ?)",
        [
            ("/old", (NOW_DT - timedelta(days=10)).isoformat()),
            ("/new", (NOW_DT - timedelta(hours=hours_old)).isoformat()),
            ("/empty", ""),
        ],
    )
    connection.commit()
    connection.close()

    result = ActivityRecommendationReader(path).recommend("Astra", WORKSPACE, NOW)

    assert result.cache_state == expected
    assert all(item.cache_state == expected for item in result.all_candidates)


def test_missing_sync_files_table_is_stale_metadata_not_source_error(tmp_path: Path) -> None:
    path = tmp_path / "legacy-cache.sqlite3"
    connection = _create_activity_database(path, include_sync_files=False)
    _add_summary(connection, "legacy", "legacy Astra cache", seconds_before_now=60)
    connection.commit()
    connection.close()

    result = ActivityRecommendationReader(path).recommend("Astra", WORKSPACE, NOW)

    assert result.availability == "available"
    assert result.cache_state == "stale-cache"
    assert result.error_category == ""


def test_summary_and_event_relevance_retain_independent_channel_ranks(
    activity_db: Path,
) -> None:
    result = ActivityRecommendationReader(activity_db).recommend("Astra", WORKSPACE, NOW)

    by_kind = {item.locator.kind: item for item in result.relevance}
    assert by_kind["activity_summary"].locator.primary == "summary-astra"
    assert by_kind["activity_event"].locator == SourceLocator("activity_event", "segment-a", 7)
    assert by_kind["activity_summary"].channel_ranks == (("activity-summary", 1),)
    assert by_kind["activity_event"].channel_ranks == (("activity-event", 1),)


def test_fts_relevance_keeps_later_workspace_match_ahead_of_global_crowding(tmp_path: Path) -> None:
    path = tmp_path / "tiered-fts.sqlite3"
    connection = _create_activity_database(path)
    for event_id in range(1, 14):
        _add_event(
            connection,
            "global",
            event_id,
            seconds_before_now=event_id,
            window_title="unrelated history",
            searchable_text="needle global history",
        )
    _add_event(
        connection,
        "workspace",
        14,
        seconds_before_now=100,
        window_title="astra-master exact workspace",
        searchable_text="needle workspace history",
    )
    connection.commit()
    connection.close()

    result = ActivityRecommendationReader(path).recommend("needle", WORKSPACE, NOW)

    assert result.relevance[0].locator == SourceLocator("activity_event", "workspace", 14)
    assert result.relevance[0].workspace_tier == 1
    assert result.relevance[0].native_query_rank != 0.0


def test_mixed_language_query_recovers_partial_term_hits(tmp_path: Path) -> None:
    # 生产实测（2026-09-04）："ranking override 实验" 这类中英混词 query 对英文语料
    # 因 AND-join 必 0 命中——单个落空的 token 拖垮整条查询。AND→OR 后由 bm25
    # 按命中数排序，部分命中恢复；纯中文 query 与英文语料仍无交集（不误报）。
    path = tmp_path / "mixed-lang.sqlite3"
    connection = _create_activity_database(path)
    _add_event(
        connection,
        "seg-mixed",
        1,
        seconds_before_now=10,
        window_title="ranking override ablation notes",
        searchable_text="ranking override ablation notes for the context index",
    )
    connection.commit()
    connection.close()

    hit = ActivityRecommendationReader(path).recommend("ranking override 实验", WORKSPACE, NOW)
    assert any(row.locator.primary == "seg-mixed" for row in hit.relevance)

    miss = ActivityRecommendationReader(path).recommend("帮我修复表单校验", WORKSPACE, NOW)
    assert not any(row.locator.primary == "seg-mixed" for row in miss.relevance)


def test_multi_term_query_rejects_single_term_overlap(tmp_path: Path) -> None:
    # 2-of-N（基线 OR 验收：A3-distinct 噪音行均分 0.5，全是单个 common-term 撞进来的）。
    # 多词 query 至少两词共现才算语义相关；两个分支（FTS 与 <3 字 LIKE 回退）同守。
    path = tmp_path / "two-of-n.sqlite3"
    connection = _create_activity_database(path)
    _add_event(
        connection,
        "seg-one-overlap",
        1,
        seconds_before_now=10,
        window_title="ranking methods survey",
        searchable_text="ranking methods survey notes",
    )
    _add_event(
        connection,
        "seg-two-overlap",
        2,
        seconds_before_now=20,
        window_title="override ranking table audit",
        searchable_text="override ranking table audit log",
    )
    connection.commit()
    connection.close()

    result = ActivityRecommendationReader(path).recommend("ranking override audit", WORKSPACE, NOW)
    identities = [row.locator.primary for row in result.relevance]
    assert "seg-one-overlap" not in identities
    assert "seg-two-overlap" in identities


def test_like_fallback_rejects_single_term_overlap(tmp_path: Path) -> None:
    path = tmp_path / "two-of-n-like.sqlite3"
    connection = _create_activity_database(path)
    _add_event(
        connection,
        "seg-like-one",
        1,
        seconds_before_now=10,
        window_title="ranking methods survey",
        searchable_text="ranking survey notes single overlap bait",
    )
    connection.commit()
    connection.close()

    # "bb" 短词触发 LIKE 回退分支；仅 "ranking" 一词重叠 → 2-of-N 应拒。
    result = ActivityRecommendationReader(path).recommend("ranking bb", WORKSPACE, NOW)
    assert not any(row.locator.primary == "seg-like-one" for row in result.relevance)


def test_one_short_term_scans_within_long_term_postings_without_losing_hits(tmp_path: Path) -> None:
    # One short term among several cannot meet two-of-N alone, so every valid row
    # contains a long term: the long terms' index narrows the literal scan losslessly.
    query = "ci runner deadline"
    assert sum(len(term) < 3 for term in LexicalQuery.from_text(query).terms) == 1
    path = tmp_path / "short-term-prefilter.sqlite3"
    connection = _create_activity_database(path)
    for number, (segment, title) in enumerate((
        ("seg-short-and-long", "ci runner logs"),
        ("seg-two-long", "runner deadline missed"),
        ("seg-short-only", "ci dashboard"),
        ("seg-unrelated", "weekly planning notes"),
    ), 1):
        _add_event(connection, segment, number, seconds_before_now=10 * number,
                   window_title=title, searchable_text=title)
    connection.commit()
    connection.close()

    result = ActivityRecommendationReader(path).recommend(query, WORKSPACE, NOW)
    identities = {row.locator.primary for row in result.relevance}
    assert {"seg-short-and-long", "seg-two-long"} <= identities
    assert not identities & {"seg-short-only", "seg-unrelated"}


def test_embedding_channel_contributes_semantic_candidates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # M2 设计（stage2 §2）：env on 时向量命中并入 relevance——词法看不见的语义位、
    # 低于相似阈值的噪音拒入、与词法重叠的行去重。默认 off 时本通道整体休眠。
    path = tmp_path / "emb-channel.sqlite3"
    connection = _create_activity_database(path)
    _add_summary(connection, "s-semantic", "Deep dive on embedding recall", seconds_before_now=100)
    _add_summary(connection, "s-both", "ranking override notes", seconds_before_now=200)
    _add_summary(connection, "s-noise", "Unrelated grocery list", seconds_before_now=300)
    connection.commit()
    connection.close()

    from agent.runtime.context_index.vector_index import VectorStore

    vectors_db = tmp_path / "vectors.db"
    store = VectorStore(vectors_db)
    store.upsert("s-semantic", "hash-s-semantic", [0.9, 0.1])
    store.upsert("s-both", "hash-s-both", [0.8, 0.2])
    store.upsert("s-noise", "hash-s-noise", [0.1, 0.9])  # 与 query 余弦 ~0.11 < 阈值
    store.close()

    class FakeEmbedder:
        def encode(self, texts):
            return [(1.0, 0.0) for _ in texts]

    monkeypatch.setenv("ASTRA_CONTEXT_INDEX_EMBEDDING", "on")
    monkeypatch.setenv("ASTRA_CONTEXT_INDEX_VECTORS_DB", str(vectors_db))
    monkeypatch.setattr(embedder_module, "ready_embedder", lambda: FakeEmbedder())

    result = ActivityRecommendationReader(path).recommend("ranking", WORKSPACE, NOW)
    identities = [row.locator.primary for row in result.relevance]
    assert "s-semantic" in identities
    assert "s-both" in identities
    assert "s-noise" not in identities
    assert identities.count("s-both") == 1  # 词法+向量双命中只留一条（向量版排前）
    assert result.relevance[0].locator.primary == "s-both"  # Both independent channels contribute an RRF vote.


def test_embedding_channel_skips_without_vectors_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 默认 on 时代的缺库守卫：没建过向量库的机器必须零模型加载、纯词法。
    # （顺序即契约——矩阵检查在 get_embedder 之前，见 _vector_candidates。）
    monkeypatch.setenv("ASTRA_CONTEXT_INDEX_VECTORS_DB", str(tmp_path / "missing-vectors.db"))
    monkeypatch.setattr(embedder_module, "ready_embedder", lambda: pytest.fail("must not load model without vectors"))
    path = tmp_path / "emb-nodb.sqlite3"
    connection = _create_activity_database(path)
    _add_summary(connection, "s-hidden", "Deep dive on embedding recall", seconds_before_now=100)
    _add_summary(connection, "s-lexical", "ranking notes", seconds_before_now=120)
    connection.commit()
    connection.close()

    result = ActivityRecommendationReader(path).recommend("ranking", WORKSPACE, NOW)
    identities = [row.locator.primary for row in result.relevance]
    assert "s-lexical" in identities      # 词法照常
    assert "s-hidden" not in identities   # 无向量命中


def test_embedding_timeout_abandons_vectors_keeps_lexical(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # M4b 并行化契约（208ms 实弹事故）：向量段慢必须「弃本轮向量、保词法全量」，
    # 限时约 100ms——绝不允许串行相加烧穿源预算。
    import time as _time

    path = tmp_path / "emb-timeout.sqlite3"
    connection = _create_activity_database(path)
    _add_summary(connection, "s-lex", "ranking override notes", seconds_before_now=100)
    connection.commit()
    connection.close()

    from agent.runtime.context_index.vector_index import VectorStore

    vectors_db = tmp_path / "vectors.db"
    store = VectorStore(vectors_db)
    store.upsert("s-lex", "h", [1.0, 0.0])
    store.close()

    class SlowEmbedder:
        def encode(self, texts):
            _time.sleep(0.4)
            return [(1.0, 0.0) for _ in texts]

    monkeypatch.setenv("ASTRA_CONTEXT_INDEX_EMBEDDING", "on")
    monkeypatch.setenv("ASTRA_CONTEXT_INDEX_VECTORS_DB", str(vectors_db))
    monkeypatch.setattr(embedder_module, "ready_embedder", lambda: SlowEmbedder())

    reader = ActivityRecommendationReader(path)
    t0 = _time.monotonic()
    result = reader.recommend("ranking", WORKSPACE, NOW)
    elapsed = _time.monotonic() - t0
    identities = [row.locator.primary for row in result.relevance]
    assert elapsed < 0.35  # 弃等而非陪跑 0.4s
    assert "s-lex" in identities  # 词法照常交付


def test_embedding_failure_keeps_lexical_channel_isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 向量段任何异常不得波及词法结果（失败隔离，M4b）。
    path = tmp_path / "emb-boom.sqlite3"
    connection = _create_activity_database(path)
    _add_summary(connection, "s-lex", "ranking override notes", seconds_before_now=100)
    connection.commit()
    connection.close()

    from agent.runtime.context_index.vector_index import VectorStore

    vectors_db = tmp_path / "vectors.db"
    store = VectorStore(vectors_db)
    store.upsert("s-lex", "h", [1.0, 0.0])
    store.close()

    class BoomEmbedder:
        def encode(self, texts):
            raise RuntimeError("metal exploded")

    monkeypatch.setenv("ASTRA_CONTEXT_INDEX_EMBEDDING", "on")
    monkeypatch.setenv("ASTRA_CONTEXT_INDEX_VECTORS_DB", str(vectors_db))
    monkeypatch.setattr(embedder_module, "ready_embedder", lambda: BoomEmbedder())

    result = ActivityRecommendationReader(path).recommend("ranking", WORKSPACE, NOW)
    assert any(row.locator.primary == "s-lex" for row in result.relevance)


def test_fts_relevance_preserves_native_bm25_order_within_workspace_tier(tmp_path: Path) -> None:
    path = tmp_path / "native-rank.sqlite3"
    connection = _create_activity_database(path)
    _add_event(
        connection,
        "global",
        1,
        seconds_before_now=10,
        window_title="short",
        searchable_text="needle",
    )
    _add_event(
        connection,
        "global",
        2,
        seconds_before_now=1,
        window_title="long",
        searchable_text="needle " * 60,
    )
    connection.commit()
    connection.close()

    result = ActivityRecommendationReader(path).recommend("needle", WORKSPACE, NOW)
    global_rows = [row for row in result.relevance if row.locator.primary == "global"]

    assert len(global_rows) == 2
    assert [row.native_query_rank for row in global_rows] == sorted(
        row.native_query_rank for row in global_rows
    )
    assert all(row.native_query_rank != 0.0 for row in global_rows)


def test_recency_parses_offsets_instead_of_sorting_raw_timestamp_text(tmp_path: Path) -> None:
    path = tmp_path / "offset-recency.sqlite3"
    connection = _create_activity_database(path)
    _add_event(
        connection,
        "misleading-lexical",
        1,
        seconds_before_now=1,
        window_title="first",
        searchable_text="first",
    )
    _add_event(
        connection,
        "actual-latest",
        2,
        seconds_before_now=2,
        window_title="second",
        searchable_text="second",
    )
    connection.execute(
        "UPDATE activity_events SET occurred_at = ?, occurred_at_us = ? WHERE segment_id = 'misleading-lexical'",
        ("2026-08-31T09:00:00+14:00", _epoch_us("2026-08-31T09:00:00+14:00")),
    )
    connection.execute(
        "UPDATE activity_events SET occurred_at = ?, occurred_at_us = ? WHERE segment_id = 'actual-latest'",
        ("2026-08-30T20:00:00+00:00", _epoch_us("2026-08-30T20:00:00+00:00")),
    )
    connection.commit()
    connection.close()

    result = ActivityRecommendationReader(path).recommend("unmatched", WORKSPACE, NOW)

    assert result.recency[0].locator.primary == "actual-latest"
    assert all(item.source == "activity" for item in result.relevance)
    assert all(item.trust_label == "untrusted_observation" for item in result.relevance)


def test_summary_recency_parses_offsets_instead_of_raw_lexical_sort(tmp_path: Path) -> None:
    path = tmp_path / "offset-summary-recency.sqlite3"
    connection = _create_activity_database(path)
    _add_summary(connection, "misleading", "older lexical summary", seconds_before_now=1)
    _add_summary(connection, "latest", "latest actual summary", seconds_before_now=2)
    connection.execute(
        "UPDATE activity_summaries SET period_end = ?, period_end_us = ? WHERE summary_id = 'misleading'",
        ("2026-08-31T09:00:00+14:00", _epoch_us("2026-08-31T09:00:00+14:00")),
    )
    connection.execute(
        "UPDATE activity_summaries SET period_end = ?, period_end_us = ? WHERE summary_id = 'latest'",
        ("2026-08-30T20:00:00+00:00", _epoch_us("2026-08-30T20:00:00+00:00")),
    )
    connection.commit()
    connection.close()

    result = ActivityRecommendationReader(path).recommend("unmatched", WORKSPACE, NOW)

    summaries = [item for item in result.recency if item.locator.kind == "activity_summary"]
    assert summaries and summaries[0].locator.primary == "latest"


def test_canonical_summary_epoch_beats_more_than_lexical_recency_cap(tmp_path: Path) -> None:
    path = tmp_path / "summary-overflow.sqlite3"
    connection = _create_activity_database(path)
    older = "2026-08-31T23:59:00+14:00"
    later = "2026-08-31T10:00:00-12:00"
    connection.executemany(
        """INSERT INTO activity_summaries(
            summary_id, source_path, granularity, period_start, period_end, period_end_us,
            content, content_hash, source_mtime_ns, imported_at
        ) VALUES (?, ?, 'day', ?, ?, ?, 'background', ?, 1, 'now')""",
        [
            (f"older-{index}", f"/old/{index}", older, older, _epoch_us(older), f"h{index}")
            for index in range(513)
        ] + [("actual-latest", "/later", later, later, _epoch_us(later), "latest")],
    )
    connection.commit()
    connection.close()

    result = ActivityRecommendationReader(path).recommend("unmatched", WORKSPACE, NOW)

    summaries = [item for item in result.recency if item.locator.kind == "activity_summary"]
    assert summaries and summaries[0].locator.primary == "actual-latest"


def test_legacy_epochless_recency_is_omitted_without_losing_fts_relevance(tmp_path: Path) -> None:
    path = tmp_path / "legacy-overflow.sqlite3"
    connection = _create_activity_database(path, include_epochs=False)
    older = "2026-08-31T23:59:00+14:00"
    later = "2026-08-31T10:00:00-12:00"
    connection.execute(
        """INSERT INTO activity_summaries(
            summary_id, source_path, granularity, period_start, period_end,
            content, content_hash, source_mtime_ns, imported_at
        ) VALUES ('fts-survives', '/fts', 'day', ?, ?, 'needle summary', 'fts', 1, 'now')""",
        (later, later),
    )
    # 97 historical rows make any capped lexical fallback observably unsafe.
    for event_id in range(1, 98):
        connection.execute(
            """INSERT INTO activity_events(
                segment_id, event_id, occurred_at, kind, app_name, bundle_id, window_title,
                url, url_search_text, selection_text, searchable_text, raw_json, imported_at
            ) VALUES ('legacy', ?, ?, 'window', 'Archive', 'bundle', 'older', '', '', '', 'background', '{}', 'now')""",
            (event_id, older),
        )
    connection.execute(
        """INSERT INTO activity_events(
            segment_id, event_id, occurred_at, kind, app_name, bundle_id, window_title,
            url, url_search_text, selection_text, searchable_text, raw_json, imported_at
        ) VALUES ('legacy', 98, ?, 'window', 'Archive', 'bundle', 'actual latest', '', '', '', 'background', '{}', 'now')""",
        (later,),
    )
    connection.commit()
    before = (path.read_bytes(), path.stat().st_size, path.stat().st_mtime_ns)
    connection.close()

    result = ActivityRecommendationReader(path).recommend("needle", WORKSPACE, NOW)

    assert result.availability == "available"
    assert result.error_category == "recency_omitted"
    assert result.diagnostics == ("recency_omitted", "habit_omitted")
    assert any(item.locator.primary == "fts-survives" for item in result.relevance)
    assert result.recency == ()
    assert (path.read_bytes(), path.stat().st_size, path.stat().st_mtime_ns) == before


def test_legacy_activity_omits_adversarial_habit_instead_of_lexical_sampling(tmp_path: Path) -> None:
    path = tmp_path / "legacy-habit.sqlite3"
    connection = _create_activity_database(path, include_epochs=False)
    monday_dates = ("2026-08-17", "2026-08-24", "2026-08-31")
    for date_value in monday_dates:
        connection.execute(
            """INSERT INTO activity_events(segment_id, event_id, occurred_at, kind, app_name,
               bundle_id, window_title, url, url_search_text, selection_text, searchable_text, raw_json, imported_at)
               VALUES (?, ?, ?, 'window', 'TargetApp', '', 'target', '', '', '', 'needle target', '{}', 'now')""",
            (f"target-{date_value}", 1, f"{date_value}T09:15:00+00:00"),
        )
        for index in range(20):
            connection.execute(
                """INSERT INTO activity_events(segment_id, event_id, occurred_at, kind, app_name,
                   bundle_id, window_title, url, url_search_text, selection_text, searchable_text, raw_json, imported_at)
                   VALUES (?, ?, ?, 'window', 'Noise', '', 'noise', '', '', '', 'noise', '{}', 'now')""",
                (f"noise-{date_value}", index + 1, f"{date_value}T23:{index:02d}:00+12:00"),
            )
    connection.execute(
        """INSERT INTO activity_summaries(summary_id, source_path, granularity, period_start, period_end,
           content, content_hash, source_mtime_ns, imported_at)
           VALUES ('fts', '/fts', 'day', '2026-08-31T00:00:00+00:00', '2026-08-31T00:00:00+00:00', 'needle summary', 'h', 1, 'now')"""
    )
    connection.commit()
    connection.close()

    result = ActivityRecommendationReader(path).recommend("needle", WORKSPACE, NOW_DT)

    assert result.habit is None
    assert result.diagnostics == ("recency_omitted", "habit_omitted")
    assert any(item.locator.primary == "fts" for item in result.relevance)


def test_relevance_and_recency_channels_are_capped_at_twelve(tmp_path: Path) -> None:
    path = tmp_path / "bounded.sqlite3"
    connection = _create_activity_database(path)
    for index in range(15):
        _add_event(
            connection,
            "bounded-segment",
            index + 1,
            seconds_before_now=index,
            searchable_text=f"bounded event needle {index}",
        )
    connection.commit()
    connection.close()

    result = ActivityRecommendationReader(path).recommend("bounded event", WORKSPACE, NOW)

    assert len(result.relevance) == 12
    assert len(result.recency) == 12
    assert [item.locator.secondary for item in result.recency[:3]] == [1, 2, 3]


def test_large_legacy_event_archive_keeps_fts_results_when_recency_is_bounded(tmp_path: Path) -> None:
    path = tmp_path / "legacy-large.sqlite3"
    connection = _create_activity_database(path)
    connection.execute("CREATE INDEX idx_legacy_events_occurred_at ON activity_events(occurred_at)")
    _add_summary(connection, "fts-summary", "needle still available through fts", seconds_before_now=1)
    # This emulates a pre-occurred_at_us archive at a production-like cardinality.
    # Its legacy recency path must use the occurred_at index rather than parse all rows.
    rows = []
    for event_id in range(1, 30_001):
        timestamp = _iso(event_id % (14 * 24 * 60 * 60))
        rows.append((
            "legacy", event_id, timestamp, "window", "Archive", "bundle",
            "background", "https://example.test/", "https://example.test/", "", "background", "{}", timestamp,
        ))
    connection.execute("DROP TRIGGER activity_events_ai")
    connection.executemany(
        """INSERT INTO activity_events(
            segment_id, event_id, occurred_at, kind, app_name, bundle_id, window_title,
            url, url_search_text, selection_text, searchable_text, raw_json, imported_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    connection.commit()
    before = (path.read_bytes(), path.stat().st_size, path.stat().st_mtime_ns)
    connection.close()

    result = ActivityRecommendationReader(path, deadline_ms=75).recommend(
        "needle", WORKSPACE, NOW
    )

    assert result.availability == "available"
    assert any(item.locator.primary == "fts-summary" for item in result.relevance)
    assert len(result.recency) <= 12
    assert (path.read_bytes(), path.stat().st_size, path.stat().st_mtime_ns) == before


def test_recommendation_descriptions_expose_only_sanitized_public_fields(
    activity_db: Path,
) -> None:
    result = ActivityRecommendationReader(activity_db).recommend("Astra", WORKSPACE, NOW)
    serialized = json.dumps(
        [
            {
                "description": item.description,
                "project_label": item.project_label,
                "cache_state": item.cache_state,
            }
            for item in result.relevance
        ]
    )

    assert "SUMMARY_SECRET" not in serialized
    assert "TOPSECRET" not in serialized
    assert "private-fragment" not in serialized
    assert "PRIVATE_SELECTION" not in serialized
    assert "RAW_SECRET" not in serialized
    assert "/private/source" not in serialized
    assert "example.com/astra/index" in serialized
    assert "Safari" in serialized
    assert all(len(item.description) <= 240 for item in result.relevance)


def test_open_resolves_exact_summary_and_event_pairs_with_bounded_context(
    activity_db: Path,
) -> None:
    connection = sqlite3.connect(activity_db)
    for event_id in range(1, 15):
        if event_id == 7:
            continue
        _add_event(
            connection,
            "segment-a",
            event_id,
            seconds_before_now=120 - event_id,
            selection_text="Authorization: Bearer EVENT_SECRET " + "x" * 700,
            searchable_text="Astra neighboring event " + "y" * 700,
        )
    _add_event(connection, "segment-b", 7, seconds_before_now=1)
    connection.commit()
    connection.close()
    reader = ActivityRecommendationReader(activity_db)

    summary = reader.open(SourceLocator("activity_summary", "summary-astra"), window=99)
    event = reader.open(SourceLocator("activity_event", "segment-a", 7), window=99)
    wrong_segment = reader.open(SourceLocator("activity_event", "missing", 7), window=2)
    wrong_id = reader.open(SourceLocator("activity_event", "segment-a", 999), window=2)

    assert summary.source == event.source == "activity"
    assert summary.trust_label == event.trust_label == "untrusted_observation"
    assert len(summary.items) == 1
    summary_payload = json.loads(summary.items[0])
    assert summary_payload["evidence_type"] == "summary"
    assert "summary_id" not in summary_payload
    assert len(event.items) == 11
    event_payloads = [json.loads(item) for item in event.items]
    assert event_payloads[0]["url"] == "https://example.com/astra/index"
    assert all("segment_id" not in payload for payload in event_payloads)
    assert all("event_id" not in payload for payload in event_payloads)
    assert all(len(item) <= 500 for item in (*summary.items, *event.items))
    assert "EVENT_SECRET" not in "".join(event.items)
    assert "RAW_SECRET" not in "".join(event.items)
    assert wrong_segment.items == ()
    assert wrong_id.items == ()


def test_open_evidence_serializes_only_the_public_activity_allowlist(tmp_path: Path) -> None:
    path = tmp_path / "public-evidence.sqlite3"
    summary_id = "SUMMARY_ID_DO_NOT_SERIALIZE_012F"
    segment_id = "SEGMENT_ID_DO_NOT_SERIALIZE_34AB"
    event_id = 987654321
    selection_text = "SELECTION_TEXT_DO_NOT_SERIALIZE_56CD"
    searchable_text = "SEARCHABLE_TEXT_DO_NOT_SERIALIZE_78EF"
    connection = _create_activity_database(path)
    _add_summary(
        connection,
        summary_id,
        "Public summary content Authorization: Bearer SUMMARY_SECRET",
        seconds_before_now=120,
    )
    _add_event(
        connection,
        segment_id,
        event_id,
        seconds_before_now=60,
        window_title="Public activity window",
        selection_text=selection_text,
        searchable_text=searchable_text,
    )
    connection.commit()
    connection.close()

    reader = ActivityRecommendationReader(path)
    summary = reader.open(SourceLocator("activity_summary", summary_id), window=1)
    event = reader.open(SourceLocator("activity_event", segment_id, event_id), window=1)
    serialized = json.dumps(
        {
            "summary": {"title": summary.title, "items": summary.items},
            "event": {"title": event.title, "items": event.items},
        },
        ensure_ascii=False,
    )

    assert summary.trust_label == event.trust_label == "untrusted_observation"
    assert "Public summary content" in serialized
    assert "Public activity window" in serialized
    assert "https://example.com/astra/index" in serialized
    assert "SUMMARY_SECRET" not in serialized
    assert summary_id not in serialized
    assert segment_id not in serialized
    assert str(event_id) not in serialized
    assert selection_text not in serialized
    assert searchable_text not in serialized
    assert set(json.loads(summary.items[0])) == {
        "evidence_type",
        "source",
        "untrusted_observation",
        "granularity",
        "period_start",
        "period_end",
        "snippet",
    }
    assert set(json.loads(event.items[0])) == {
        "evidence_type",
        "source",
        "untrusted_observation",
        "occurred_at",
        "kind",
        "app_name",
        "window_title",
        "url",
    }
    assert all(len(item) <= 500 for item in (*summary.items, *event.items))


def test_open_rejects_cross_kind_locators_without_querying_other_identifiers(
    activity_db: Path,
) -> None:
    reader = ActivityRecommendationReader(activity_db)

    summary_as_event = reader.open(
        SourceLocator("activity_event", "summary-astra", 0), window=1
    )
    event_as_summary = reader.open(
        SourceLocator("activity_summary", "segment-a", 7), window=1
    )
    session = reader.open(SourceLocator("session_message", "segment-a", 7), window=1)

    assert summary_as_event.items == ()
    assert event_as_summary.items == ()
    assert session.items == ()


@pytest.mark.parametrize(
    ("database_factory", "expected_category"),
    [
        (lambda path: sqlite3.connect(path).close(), "incompatible_schema"),
        (lambda path: path.write_bytes(b"not a sqlite database"), "database_error"),
    ],
)
def test_recommend_errors_are_independent_and_bounded(
    tmp_path: Path, database_factory, expected_category: str
) -> None:
    broken = tmp_path / f"{expected_category}.sqlite3"
    database_factory(broken)
    healthy = tmp_path / "healthy.sqlite3"
    connection = _create_activity_database(healthy)
    _add_summary(connection, "healthy", "healthy Astra summary", seconds_before_now=1)
    connection.commit()
    connection.close()

    failure = ActivityRecommendationReader(broken).recommend("Astra", WORKSPACE, NOW)
    success = ActivityRecommendationReader(healthy).recommend("Astra", WORKSPACE, NOW)
    opened = ActivityRecommendationReader(broken).open(
        SourceLocator("activity_summary", "anything"), window=1
    )

    assert failure.availability == "error"
    assert failure.error_category == expected_category
    assert str(broken) not in failure.error_category
    assert success.availability == "available" and success.relevance
    assert opened.items == ()


@pytest.mark.parametrize('initially_missing', [False, True])
def test_vector_cache_refreshes_after_index_update(tmp_path, monkeypatch, initially_missing):
    from agent.runtime.context_index.vector_index import VectorStore

    path = tmp_path / 'refresh-vectors.db'
    monkeypatch.setenv('ASTRA_CONTEXT_INDEX_VECTORS_DB', str(path))
    class Fake:
        def encode(self, texts):
            return [[1., 0.]]
    monkeypatch.setattr(embedder_module, 'ready_embedder', lambda: Fake())
    if not initially_missing:
        store = VectorStore(path)
        store.upsert('old', 'h1', [1., 0.])
        store.close()
    reader = ActivityRecommendationReader(tmp_path / 'unused.db')
    assert [h[0] for h in reader._embed_hits('q')] == ([] if initially_missing else ['old'])
    store = VectorStore(path)
    store.upsert('old', 'h2', [0., 1.])
    store.upsert('new', 'h3', [1., 0.])
    store.close()
    assert [h[0] for h in reader._embed_hits('q')] == ['new']
    path.unlink()
    assert reader._embed_hits('q') == []


def test_vector_refresh_does_not_depend_on_filesystem_timestamp_resolution(tmp_path, monkeypatch):
    from agent.runtime.context_index.vector_index import VectorStore
    path = tmp_path / "coarse-clock.db"
    monkeypatch.setenv("ASTRA_CONTEXT_INDEX_VECTORS_DB", str(path))
    class Embedder:
        def encode(self, texts):
            return [[1., 0.]]
    monkeypatch.setattr(embedder_module, "ready_embedder", lambda: Embedder())
    store = VectorStore(path)
    store.upsert("old", "h1", [1., 0.])
    store.close()
    reader = ActivityRecommendationReader(tmp_path / "unused.db")
    assert [hit[0] for hit in reader._embed_hits("q")] == ["old"]
    original_stat, frozen = Path.stat, path.stat()
    monkeypatch.setattr(Path, "stat", lambda file, *args, **kwargs:
                        frozen if file == path else original_stat(file, *args, **kwargs))
    store = VectorStore(path)
    store.upsert("old", "h2", [0., 1.])
    store.upsert("new", "h3", [1., 0.])
    store.close()
    assert [hit[0] for hit in reader._embed_hits("q")] == ["new"]


def test_vector_hit_rejects_changed_summary_hash(tmp_path, monkeypatch):
    from agent.runtime.context_index.vector_index import VectorStore

    path = tmp_path / 'activity.db'
    c = _create_activity_database(path)
    c.row_factory = sqlite3.Row
    _add_summary(c, 'semantic', 'original content', seconds_before_now=100)
    c.commit()
    vectors = tmp_path / 'hash-vectors.db'
    store = VectorStore(vectors)
    store.upsert('semantic', 'hash-semantic', [1., 0.])
    store.close()
    class Fake:
        def encode(self, texts):
            return [[1., 0.]]
    monkeypatch.setenv('ASTRA_CONTEXT_INDEX_VECTORS_DB', str(vectors))
    monkeypatch.setattr(embedder_module, 'ready_embedder', lambda: Fake())
    reader = ActivityRecommendationReader(path)
    hits = reader._embed_hits('q')
    c.create_function('context_activity_summary_tier', 1, lambda _: 2)
    assert len(reader._fetch_vector_rows(c, hits)) == 1
    c.execute("UPDATE activity_summaries SET content='replacement', content_hash='changed'")
    c.commit()
    assert reader._fetch_vector_rows(c, hits) == []
    c.close()


@pytest.mark.parametrize('update_mode', ['wal', 'replace'])
def test_vector_cache_detects_wal_commits_and_file_replacement(tmp_path, monkeypatch, update_mode):
    from agent.runtime.context_index import vector_index

    path = tmp_path / 'revisions.db'
    store = vector_index.VectorStore(path)
    store.upsert('s1', 'old-hash', [1., 0.])
    store.close()
    writer = sqlite3.connect(path)
    if update_mode == 'wal':
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('SELECT * FROM context_index_vectors').fetchall()
    monkeypatch.setenv('ASTRA_CONTEXT_INDEX_VECTORS_DB', str(path))
    reader = ActivityRecommendationReader(tmp_path / 'unused')
    original = vector_index.read_vector_snapshot
    reads = []
    def observe(path):
        reads.append(path)
        return original(path)
    monkeypatch.setattr(vector_index, 'read_vector_snapshot', observe)
    try:
        assert reader._vector_snapshot()[2] == {'s1': 'old-hash'}
        reader._vector_snapshot()
        assert len(reads) == 1  # Unchanged queries do fixed stat work, not full reloads.
        if update_mode == 'wal':
            writer.execute("UPDATE context_index_vectors SET content_hash='new-hash'")
            writer.commit()
        else:
            replacement = tmp_path / 'replacement.db'
            store = vector_index.VectorStore(replacement)
            store.upsert('s1', 'new-hash', [0., 1.])
            store.close()
            writer.close()  # Windows requires writers closed before replacement.
            replacement.replace(path)
        assert reader._vector_snapshot()[2] == {'s1': 'new-hash'}
        assert len(reads) == 2
    finally:
        writer.close()
