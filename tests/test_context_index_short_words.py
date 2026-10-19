"""Two-character Chinese words: kept whole in questions, found through the archive's pair index."""

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from agent.runtime.context_index.query import plan_query, retrieval_terms
from agent.runtime.context_index.session_source import SessionRecommendationSource
from agent.runtime.context_index.sqlite_reader import open_readonly, set_read_window
from agent.runtime.context_index.workspace import WorkspaceIdentity
from agent.runtime.session_recall import CJK_PAIRS_TABLE, SessionRecall

WORKSPACE = WorkspaceIdentity("astra", "", "astra")


def _archive(tmp_path, contents):
    recall = SessionRecall(tmp_path / "sessions.db")
    recall.init_db()
    session = recall.create_session("History", workspace_key=WORKSPACE.key)
    ids = [recall.log_message(session, "assistant", content) for content in contents]
    return recall, session, ids


def _found(path, query):
    now = datetime.now(UTC) + timedelta(seconds=5)
    plan = plan_query(query, now)
    result = SessionRecommendationSource(path).recommend(query, WORKSPACE, "current", frozenset(), now.timestamp(), plan)
    assert not result.error_category
    return [item.private_text for item in result.relevance]


def _pair_rows(recall, word):
    return [row[0] for row in recall._get_conn().execute(f"SELECT rowid FROM {CJK_PAIRS_TABLE}(?)", (f'"{word}"',))]


@pytest.mark.parametrize("question,words", [
    ("牙医是哪天？", ("牙医",)),
    ("车位在哪？", ("车位",)),
    ("膝盖还疼吗？上次说的。", ("膝盖",)),
    ("上次说的语音是哪个模型", ("语音", "模型")),
])
def test_question_words_do_not_cut_two_character_words(question, words):
    assert retrieval_terms(question) == words


def test_sources_without_a_pair_index_keep_trigram_terms():
    assert retrieval_terms("牙医是哪天？", word_split=False) == ("牙医是", "医是哪", "是哪天")
    assert retrieval_terms("你觉得呢") == ()


@pytest.mark.parametrize("question,useful", [
    ("牙医是哪天？", "周三下午三点去看牙医。"),
    ("车位在哪？", "车位换到 B2 层 47 号了。"),
    ("京都交通怎么解决？", "在京都坐巴士和地铁，交通卡用 ICOCA。"),
])
def test_a_short_question_finds_the_message_with_its_word(tmp_path, question, useful):
    _archive(tmp_path, [useful, "今天的天气不错。", "周末去看了电影。"])
    assert _found(tmp_path / "sessions.db", question) == [useful]


def test_pair_lookup_reads_the_index_instead_of_every_message(tmp_path):
    useful = "周三下午三点去看牙医。"
    recall, _, ids = _archive(tmp_path, [useful, *[f"第{i}次故事背景说明，人物在城市里散步聊天。" * 8 for i in range(3000)]])
    path = tmp_path / "sessions.db"

    def rows(connection_path):
        steps = 0

        def budget():
            nonlocal steps
            steps += 1000
            return int(steps >= 25000)

        with open_readonly(connection_path) as reader:
            reader.row_factory = sqlite3.Row
            set_read_window(reader, datetime.now(UTC).timestamp() + 5)
            reader.set_progress_handler(budget, 1000)
            return [row["id"] for row in SessionRecommendationSource(connection_path)._native_relevance_rows(
                reader, "牙医是哪天？", WORKSPACE, "current", None, frozenset(), True,
            )]

    assert rows(path) == [ids[0]]
    # The same lookup without the index scans every message and runs out of budget.
    recall._get_conn().execute(f"DROP TABLE {CJK_PAIRS_TABLE}")
    recall._get_conn().commit()
    with pytest.raises(sqlite3.OperationalError, match="interrupt"):
        rows(path)


def test_messages_written_without_the_index_are_found_and_then_indexed(tmp_path):
    recall, session, _ = _archive(tmp_path, ["今天的天气不错。"])
    connection = recall._get_conn()
    # An older Astra, or an import, writes messages without the pair index.
    connection.execute(
        "INSERT INTO messages (session_id, role, content, timestamp, msg_index) VALUES (?, 'user', ?, ?, 9)",
        (session, "周三下午三点去看牙医。", datetime.now(UTC).timestamp()),
    )
    connection.commit()
    imported = connection.execute("SELECT max(id) FROM messages").fetchone()[0]
    assert _pair_rows(recall, "牙医") == []
    assert _found(tmp_path / "sessions.db", "牙医是哪天？") == ["周三下午三点去看牙医。"]

    latest = recall.log_message(session, "assistant", "好的。")
    assert latest == connection.execute("SELECT max(id) FROM messages").fetchone()[0] != imported
    assert _pair_rows(recall, "牙医") == [imported]


def test_deleted_and_edited_messages_leave_the_pair_index_exact(tmp_path):
    recall, session, (dentist, parking) = _archive(tmp_path, ["周三下午三点去看牙医。", "车位在 B1 层。"])
    connection = recall._get_conn()
    connection.execute("DELETE FROM messages WHERE id = ?", (dentist,))
    connection.execute("UPDATE messages SET content = '车位换到膝盖诊所旁边。' WHERE id = ?", (parking,))
    connection.commit()
    assert _pair_rows(recall, "牙医") == [] and _pair_rows(recall, "车位") == []
    # The edited message is read literally until the next write indexes it again.
    assert _found(tmp_path / "sessions.db", "膝盖还疼吗") == ["车位换到膝盖诊所旁边。"]
    recall.log_message(session, "user", "好的。")
    assert _pair_rows(recall, "膝盖") == [parking]


def test_sqlite_without_contentless_delete_keeps_a_stored_pair_table(tmp_path):
    class OlderSQLite:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, sql, *args):
            if "contentless_delete" in sql:
                raise sqlite3.OperationalError("unrecognized option: contentless_delete")
            return self.connection.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self.connection, name)

    recall = SessionRecall(tmp_path / "sessions.db")
    recall.init_db()
    connection = recall._get_conn()
    connection.execute(f"DROP TABLE {CJK_PAIRS_TABLE}")
    connection.commit()
    assert SessionRecall._ensure_cjk_pairs(OlderSQLite(connection))  # type: ignore[arg-type]
    assert "detail=none" in connection.execute(
        "SELECT sql FROM sqlite_master WHERE name = ?", (CJK_PAIRS_TABLE,)).fetchone()[0]
    session = recall.create_session("History", workspace_key=WORKSPACE.key)
    dentist = recall.log_message(session, "user", "周三下午三点去看牙医。")
    assert _pair_rows(recall, "牙医") == [dentist]
    connection.execute("DELETE FROM messages WHERE id = ?", (dentist,))
    connection.commit()
    assert _pair_rows(recall, "牙医") == []
