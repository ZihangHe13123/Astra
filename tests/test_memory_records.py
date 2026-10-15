import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from agent.runtime.memory import MemoryStore
from agent.runtime.memory_provider import BuiltinMemoryProvider
from agent.runtime.memory_records import MemoryRecordRepository


def make_store(tmp_path):
    return MemoryStore(path=tmp_path / "memory.db", core_dir=tmp_path / "core")


def _fts_sql(path) -> str:
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name='memory_records_fts'"
        ).fetchone()
    return "" if row is None else str(row[0])


def _canonical_rows(path) -> list[tuple]:
    with sqlite3.connect(path) as connection:
        return connection.execute(
            "SELECT * FROM memory_records ORDER BY id"
        ).fetchall()


def _replace_with_unicode61_fts(path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            DROP TRIGGER IF EXISTS memory_records_ai;
            DROP TRIGGER IF EXISTS memory_records_ad;
            DROP TRIGGER IF EXISTS memory_records_au;
            DROP TABLE IF EXISTS memory_records_fts;
            CREATE VIRTUAL TABLE memory_records_fts USING fts5(
                id UNINDEXED,
                content,
                tags,
                tokenize='unicode61'
            );
            INSERT INTO memory_records_fts(id, content, tags)
            SELECT id, content, tags_json FROM memory_records;
            CREATE TRIGGER memory_records_ai AFTER INSERT ON memory_records BEGIN
                INSERT INTO memory_records_fts(id, content, tags)
                VALUES (new.id, new.content, new.tags_json);
            END;
            CREATE TRIGGER memory_records_ad AFTER DELETE ON memory_records BEGIN
                DELETE FROM memory_records_fts WHERE id = old.id;
            END;
            CREATE TRIGGER memory_records_au AFTER UPDATE ON memory_records BEGIN
                DELETE FROM memory_records_fts WHERE id = old.id;
                INSERT INTO memory_records_fts(id, content, tags)
                VALUES (new.id, new.content, new.tags_json);
            END;
            """
        )


def test_new_repository_creates_trigram_fts(tmp_path):
    path = tmp_path / "new-memory.db"

    repository = MemoryRecordRepository(path)

    assert repository.fts_enabled is True
    assert "trigram" in " ".join(_fts_sql(path).casefold().split())


def test_legacy_unicode61_fts_migrates_without_changing_canonical_rows(tmp_path):
    store = make_store(tmp_path)
    store.add_record(
        kind="observation",
        content="系统已经持久化打开率埋点数据",
        tags=["检索", "反馈"],
    )
    path = tmp_path / "memory.db"
    _replace_with_unicode61_fts(path)
    assert "unicode61" in _fts_sql(path).casefold()
    before = _canonical_rows(path)

    first = MemoryRecordRepository(path)
    first_sql = " ".join(_fts_sql(path).casefold().split())
    after_first = _canonical_rows(path)
    second = MemoryRecordRepository(path)

    assert first.fts_enabled is True
    assert second.fts_enabled is True
    assert "trigram" in first_sql
    assert _canonical_rows(path) == after_first == before
    assert " ".join(_fts_sql(path).casefold().split()) == first_sql


def test_chinese_phrase_uses_fts_without_like_fallback(tmp_path, monkeypatch):
    repository = MemoryRecordRepository(tmp_path / "memory.db")
    record = repository.add(
        kind="observation",
        content="系统已经持久化打开率埋点数据",
        tags=["反馈"],
    )

    def fail_like(*_args, **_kwargs):
        raise AssertionError("LIKE fallback used")

    monkeypatch.setattr(repository, "_recall_like", fail_like)

    recalled = repository.recall("打开率埋点")

    assert [item.record_id for item in recalled] == [record.record_id]


def test_short_chinese_query_reaches_like_fallback(tmp_path, monkeypatch):
    repository = MemoryRecordRepository(tmp_path / "memory.db")
    record = repository.add(kind="observation", content="本地模型已经准备好")
    calls: list[str] = []
    original_fts = repository._recall_fts
    original_like = repository._recall_like

    def observe_fts(*args, **kwargs):
        calls.append("fts")
        return original_fts(*args, **kwargs)

    def observe_like(*args, **kwargs):
        calls.append("like")
        return original_like(*args, **kwargs)

    monkeypatch.setattr(repository, "_recall_fts", observe_fts)
    monkeypatch.setattr(repository, "_recall_like", observe_like)

    recalled = repository.recall("模型")

    assert [item.record_id for item in recalled] == [record.record_id]
    assert calls[-1] == "like"


def test_english_multi_term_query_remains_fts_searchable(tmp_path, monkeypatch):
    repository = MemoryRecordRepository(tmp_path / "memory.db")
    record = repository.add(
        kind="preference",
        content="User prefers concise evidence backed answers",
    )

    def fail_like(*_args, **_kwargs):
        raise AssertionError("LIKE fallback used")

    monkeypatch.setattr(repository, "_recall_like", fail_like)

    assert [
        item.record_id for item in repository.recall("evidence backed")
    ] == [record.record_id]


def test_trigram_fts_triggers_track_update_and_delete(tmp_path):
    path = tmp_path / "memory.db"
    repository = MemoryRecordRepository(path)
    record = repository.add(
        kind="observation",
        content="旧的检索短语不会保留",
        tags=["旧标签"],
    )

    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE memory_records SET content = ?, tags_json = ? WHERE id = ?",
            ("新的打开率埋点已经生效", json.dumps(["新标签"], ensure_ascii=False), record.record_id),
        )
        assert connection.execute(
            "SELECT id FROM memory_records_fts WHERE memory_records_fts MATCH ?",
            ('"打开率埋点"',),
        ).fetchone() == (record.record_id,)
        assert connection.execute(
            "SELECT id FROM memory_records_fts WHERE memory_records_fts MATCH ?",
            ('"旧的检索短语"',),
        ).fetchone() is None
        connection.execute("DELETE FROM memory_records WHERE id = ?", (record.record_id,))
        assert connection.execute(
            "SELECT id FROM memory_records_fts WHERE id = ?",
            (record.record_id,),
        ).fetchone() is None


def test_migration_failure_preserves_legacy_fts_and_canonical_writes(
    tmp_path, monkeypatch
):
    path = tmp_path / "legacy-failure.db"
    repository = MemoryRecordRepository(path)
    original = repository.add(
        kind="observation",
        content="系统已经持久化打开率埋点数据",
    )
    _replace_with_unicode61_fts(path)

    def fail_rebuild(_db):
        raise sqlite3.OperationalError("trigram unavailable")

    monkeypatch.setattr(
        MemoryRecordRepository,
        "_rebuild_fts_trigram",
        staticmethod(fail_rebuild),
    )

    reopened = MemoryRecordRepository(path)
    added = reopened.add(kind="observation", content="新的模型偏好")

    assert reopened.fts_enabled is True
    assert "unicode61" in _fts_sql(path).casefold()
    assert [item.record_id for item in reopened.recall("打开率埋点")] == [
        original.record_id
    ]
    assert reopened.get(added.record_id) is not None


def test_initial_fts_failure_disables_fts_but_keeps_like_and_writes(
    tmp_path, monkeypatch
):
    path = tmp_path / "no-fts.db"
    MemoryRecordRepository(path)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            DROP TRIGGER IF EXISTS memory_records_ai;
            DROP TRIGGER IF EXISTS memory_records_ad;
            DROP TRIGGER IF EXISTS memory_records_au;
            DROP TABLE memory_records_fts;
            """
        )

    def fail_rebuild(_db):
        raise sqlite3.OperationalError("trigram unavailable")

    monkeypatch.setattr(
        MemoryRecordRepository,
        "_rebuild_fts_trigram",
        staticmethod(fail_rebuild),
    )

    repository = MemoryRecordRepository(path)
    record = repository.add(kind="observation", content="fallback model memory")

    assert repository.fts_enabled is False
    assert [item.record_id for item in repository.recall("model")] == [
        record.record_id
    ]


def test_corrupt_fts_detaches_triggers_and_preserves_canonical_writes(tmp_path):
    path = tmp_path / "corrupt-fts.db"
    repository = MemoryRecordRepository(path)
    repository.add(kind="observation", content="record before index corruption")
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE memory_records_fts_idx")

    reopened = MemoryRecordRepository(path)
    added = reopened.add(
        kind="observation",
        content="canonical write survives broken derived search",
    )

    with sqlite3.connect(path) as connection:
        triggers = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND name LIKE 'memory_records_a%'"
        ).fetchall()
    assert reopened.fts_enabled is False
    assert triggers == []
    assert reopened.get(added.record_id) is not None
    assert [item.record_id for item in reopened.recall("survives broken")] == [
        added.record_id
    ]


def test_structured_record_retains_provenance_and_is_not_blindly_injected(tmp_path):
    store = make_store(tmp_path)

    record = store.add_record(
        kind="user_fact",
        content="Test user plans to apply to Northstar University in 2030",
        source_session_id="session-1",
        source_message_id="message-7",
        confidence=0.95,
        salience=0.8,
        tags=["education", "northstar"],
        metadata={"retained_by": "explicit-user-statement"},
    )
    recalled = store.recall_records("Northstar", kinds=["user_fact"])

    assert [item.record_id for item in recalled] == [record.record_id]
    assert recalled[0].source_session_id == "session-1"
    assert recalled[0].source_message_id == "message-7"
    assert recalled[0].confidence == 0.95
    assert recalled[0].tags == ("education", "northstar")
    assert recalled[0].metadata["retained_by"] == "explicit-user-statement"
    # Not pinned: a user fact is a dated line in the saved-memory index.
    prompt = store.format_prompt("session-1")
    index = prompt[prompt.index("<memory-index>"):prompt.index("</memory-index>")]
    assert "Northstar University" in index and record.created_at[:10] in index
    assert "[USER.md]" not in prompt


def test_supersede_marks_old_fact_inactive_and_links_replacement(tmp_path):
    store = make_store(tmp_path)
    old = store.add_record(
        kind="preference",
        content="User prefers verbose answers",
        source_session_id="session-old",
        tags=["response-style"],
    )

    replacement = store.supersede_record(
        old.record_id,
        content="User prefers concise answers by default",
        source_session_id="session-correction",
        source_message_id="correction-1",
    )

    old_after = store.record_store.get(old.record_id)
    assert old_after is not None
    assert old_after.status == "superseded"
    assert old_after.valid_until
    assert replacement.supersedes_id == old.record_id
    assert [item.record_id for item in store.recall_records("concise")] == [replacement.record_id]
    assert store.recall_records("verbose") == []


def test_expired_and_forgotten_records_are_not_recalled(tmp_path):
    store = make_store(tmp_path)
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    expired = store.add_record(
        kind="observation",
        content="Temporary appointment was scheduled for Monday",
        valid_until=yesterday,
    )
    active = store.add_record(kind="observation", content="Current appointment is on Friday")

    assert expired.record_id not in {item.record_id for item in store.recall_records("appointment")}
    assert active.record_id in {item.record_id for item in store.recall_records("appointment")}
    assert store.forget_record(active.record_id) is True
    assert store.recall_records("appointment") == []


def test_recall_has_like_fallback_when_fts5_is_unavailable(tmp_path):
    store = make_store(tmp_path)
    record = store.add_record(kind="episode", content="Browser takeover completed the two factor login")
    store.record_store.fts_enabled = False

    recalled = store.recall_records("two factor")

    assert [item.record_id for item in recalled] == [record.record_id]


def test_structured_record_validation_rejects_secrets_and_invalid_scores(tmp_path):
    store = make_store(tmp_path)

    with pytest.raises(ValueError, match="secret or credential"):
        store.add_record(kind="user_fact", content="api_key=super-secret-value-that-must-not-be-stored")
    with pytest.raises(ValueError, match="confidence must be between 0 and 1"):
        store.add_record(kind="user_fact", content="safe content", confidence=1.5)
    with pytest.raises(ValueError, match="Unknown memory kind"):
        store.add_record(kind="assistant_guess", content="safe content")


def test_builtin_provider_exposes_retain_recall_supersede_and_health(tmp_path):
    store = make_store(tmp_path)
    provider = BuiltinMemoryProvider(store)

    async def scenario():
        original = await provider.retain(
            kind="preference",
            content="User likes evidence-backed answers",
            source_session_id="session-1",
        )
        recalled = await provider.recall("evidence-backed", kinds=["preference"])
        replacement = await provider.supersede(
            original.record_id,
            content="User likes concise evidence-backed answers",
            source_session_id="session-2",
        )
        health = await provider.health()
        return original, recalled, replacement, health

    original, recalled, replacement, health = asyncio.run(scenario())

    assert [item.record_id for item in recalled] == [original.record_id]
    assert replacement.supersedes_id == original.record_id
    assert health.state == "available"
    assert health.capabilities.provenance is True
    assert health.capabilities.reflect is False
    assert "records=2" in health.detail
