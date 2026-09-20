"""Synthetic relevance pairs: literal targets and independent query evidence."""

from dataclasses import replace
import sqlite3

import pytest

from agent.runtime.context_index.models import SourceResult
from agent.runtime.context_index.query import plan_query
from agent.runtime.context_index.selection import select_rows
from agent.runtime.context_index.lexical import LexicalQuery
from tests.test_context_index_lexical import NOW, _read_source
from tests.test_context_index_native import candidate


@pytest.mark.parametrize("text", [
    "早上好", "早上好呀～", "早安", "早安呀！", "上午好", "中午好", "午安",
    "下午好", "晚上好", "晚安", "你好呀！", "您好啊", "Good morning!",
    "Good afternoon.", "Good evening", "Good night!", "  GOOD MORNING!  ", "hello!",
])
def test_greeting_only_turn_does_not_recall_prior_context(text):
    plan = plan_query(text, NOW, recent_text="Astra 检索问题", task_text="Astra 优化计划")
    assert not plan.should_recall
    assert not plan.include_recent


@pytest.mark.parametrize("text", [
    "早上好，帮我查Astra检索问题", "早上好是什么语言", "早安的英文是什么",
    "晚安之后继续Astra优化", "你好呀，刚才的方案还有哪些风险",
    "Good morning, recall the Astra plan", "What does good morning mean?",
    "Good evening — check the retrieval tests", "早上好呀呀呀", "Good morning Astra",
])
def test_greeting_words_do_not_suppress_substantive_or_unrecognized_turns(text):
    assert plan_query(text, NOW).should_recall


@pytest.mark.parametrize("text", ["刚才你说的Astra优化", "早上好，刚才你说的Astra优化"])
def test_recent_anaphora_survives_greeting_filter(text):
    plan = plan_query(text, NOW)
    assert plan.should_recall and plan.include_recent


def test_distinct_evidence_from_same_session_keeps_both_locators():
    schema = candidate("schema", "Astra optimization: optional paging fields must stay optional", rank=0)
    receipt = candidate("receipt", "Astra optimization: uncertain browser writes require observation", rank=1)
    schema = replace(schema, topic_key="same-session",
                     locator=replace(schema.locator, primary="same-session", secondary=1))
    receipt = replace(receipt, topic_key="same-session",
                      locator=replace(receipt.locator, primary="same-session", secondary=2))

    rows = select_rows(
        SourceResult("available", relevance=(schema, receipt)),
        SourceResult("absent"), plan_query("Astra optimization", NOW),
    )

    assert {row.identity for row in rows} == {"schema", "receipt"}
    assert {row.locator.primary for row in rows} == {"same-session"}
    assert {row.locator.secondary for row in rows} == {1, 2}


def _limit_sql_work(connection, *, instructions=25000):
    """Bound query work without depending on runner clock speed."""
    steps = 0

    def work_budget():
        nonlocal steps
        steps += 1000
        return int(steps >= instructions)

    connection.set_progress_handler(work_budget, 1000)


@pytest.mark.parametrize("source", ["session", "memory"])
@pytest.mark.parametrize("target", ["NUS", "nus", "ＮＵＳ"])
def test_named_target_survives_newer_overlapping_gram_noise(tmp_path, source, target):
    useful = "NUS 的课程已确认，讲师是课程负责人。" + "课程背景说明。" * 160
    noise = "虚构人物向课程讲师致意。" + "故事背景说明。" * 160
    result = _read_source(tmp_path, source, f"{target} 课程讲师是谁", [
        useful, *[noise + str(i) for i in range(80)],
    ])
    assert not result.error_category
    assert [item.private_text for item in result.relevance] == [useful]


@pytest.mark.parametrize("source", ["session", "memory"])
def test_identifier_match_has_ascii_boundaries(tmp_path, source):
    useful = "NUS 的这门课程，讲师是课程负责人。"
    result = _read_source(tmp_path, source, "NUS 课程讲师是谁", [
        useful, "sinus 课程讲师是谁只是示例文本", "NUS2 课程讲师是谁只是另一示例",
    ])
    assert [item.private_text for item in result.relevance] == [useful]


@pytest.mark.parametrize("source", ["session", "memory"])
def test_independent_chinese_evidence_beats_overlapping_grams_before_limit(tmp_path, source):
    useful = "数据库设置已检查；重试超时需要记录。"
    noise = "连接池故障正在虚构故事里出现。"
    result = _read_source(tmp_path, source, "数据库连接池故障重试超时", [
        useful, *[noise + str(i) for i in range(80)],
    ])
    assert not result.error_category
    assert result.relevance[0].private_text == useful


@pytest.mark.parametrize("source", ["session", "memory"])
def test_a_short_chinese_phrase_keeps_partial_recall(tmp_path, source):
    useful = "记忆推荐支持关键词检索。"
    result = _read_source(tmp_path, source, "记忆推荐", [useful])
    assert [item.private_text for item in result.relevance] == [useful]


def test_named_target_index_avoids_scanning_common_chinese_hits(tmp_path):
    from agent.runtime.context_index.session_source import SessionRecommendationSource
    from agent.runtime.context_index.sqlite_reader import open_readonly, set_read_window
    from tests.test_context_index_session_source import _add_session, _create_recall_database
    from tests.test_context_index_lexical import WORKSPACE

    path = tmp_path / "sessions.db"
    db = _create_recall_database(path)
    db.execute("CREATE INDEX sessions_workspace ON sessions(workspace_key)")
    db.execute("CREATE INDEX idx_messages_session ON messages(session_id, msg_index)")
    ids = _add_session(db, "history", "History", WORKSPACE.key, [
        ("user", "NUS 的课程，讲师是课程负责人。", NOW.timestamp() - 1),
        *[("assistant", "课程讲师在无关故事里出现。" * 20, NOW.timestamp() - 2) for _ in range(5000)],
    ])
    db.commit()
    db.close()

    with open_readonly(path, deadline_ms=5000) as reader:
        set_read_window(reader, NOW.timestamp())
        _limit_sql_work(reader)
        rows = SessionRecommendationSource(path)._native_relevance_rows(
            reader, "NUS 课程讲师是谁", WORKSPACE, "current", None, frozenset(), True,
        )
    assert [row["id"] for row in rows] == [ids[0]]


def test_fusion_rejects_wrong_entity_even_with_strong_vector_or_recent_votes():
    useful = candidate("useful", "NUS 的课程，讲师是课程负责人。", rank=20, tier=2)
    noise = replace(candidate("noise", "虚构人物向课程讲师致意。", source="activity"),
                    channel_ranks=(("lexical", 1), ("vector", 1)))
    rows = select_rows(
        SourceResult("available", relevance=(useful,)),
        SourceResult("available", relevance=(noise,), recency=(noise,)),
        plan_query("最近 NUS 课程讲师是谁", NOW),
    )
    assert [item.identity for item in rows] == ["useful"]


def test_fusion_uses_independent_query_coverage_for_equal_channel_ranks():
    useful = candidate("z-useful", "数据库设置已检查；重试超时需要记录。")
    noise = candidate("a-noise", "连接池故障正在虚构故事里出现。")
    plan = replace(plan_query("数据库连接池故障重试超时", NOW), max_items=1)
    rows = select_rows(SourceResult("available", relevance=(noise, useful)), SourceResult("absent"), plan)
    assert [item.identity for item in rows] == ["z-useful"]


@pytest.mark.parametrize("text,expected", [
    ("甲乙丙丁", 4 / 5),
    ("甲乙丙", 3 / 5),
    ("甲乙丙；丙丁戊", 1.0),
    ("不相关内容", 0.0),
])
def test_overlapping_query_characters_count_once_in_sql_and_fusion(text, expected):
    lexical = LexicalQuery.from_text("甲乙丙丁戊")
    mask, params = lexical.mask_sql("content")
    db = sqlite3.connect(":memory:")
    try:
        value = db.execute(
            f"SELECT ({lexical.coverage_sql('bits')}) / 5.0 FROM "
            f"(SELECT ({mask}) AS bits FROM (SELECT ? AS content))", (*params, text),
        ).fetchone()[0]
    finally:
        db.close()
    assert value == pytest.approx(expected)
    assert lexical.coverage(text) == pytest.approx(expected)


@pytest.mark.parametrize("source", ["session", "memory"])
def test_explicit_target_is_retained_after_long_chinese_query_hits_term_cap(tmp_path, source):
    query = "数据库连接池故障重试超时重新检查恢复流程 NUS"
    useful = "NUS 的数据库设置已确认。"
    result = _read_source(tmp_path, source, query, [useful, query.replace(" NUS", "")])
    assert [item.private_text for item in result.relevance] == [useful]
    assert len(LexicalQuery.from_text(query).terms) <= 12


@pytest.mark.parametrize("source", ["session", "memory"])
@pytest.mark.parametrize("second", ["NTU", "AI"])
def test_multiple_named_targets_allow_complementary_evidence(tmp_path, source, second):
    contents = ["NUS 课程讲师负责教学。", f"{second} 课程讲师负责指导。"]
    result = _read_source(tmp_path, source, f"NUS {second} 课程讲师对比", contents)
    assert {item.private_text for item in result.relevance} == set(contents)


def test_english_request_word_is_not_a_literal_target():
    assert not LexicalQuery.from_text("Please 帮我看看记忆推荐").anchors


@pytest.mark.parametrize("query", [
    "把今天的技巧和经验更新进skill，然后梳理优化项",
    "把今天的经验整理成 README",
    "Summarize today's notes into README",
    "把今天的经验写入 docs/README.md",
    "Save today's notes to `docs/session notes.md`",
    "把今天的经验写入 README.md 和 CHANGELOG.md",
    "把今天的经验写入 README.md、docs/notes.md",
    "把今天的经验写入 README.md 以及 `docs/session notes.md`",
    "把今天的经验写入 README.md、CHANGELOG.md 和 docs/notes.md",
    "Summarize today's notes into README.md and CHANGELOG.md",
    "Save today's notes to `docs/session notes.md`, CHANGELOG.md and README.md",
])
def test_output_destination_does_not_filter_the_history_to_be_summarized(query):
    plan = plan_query(query, NOW)
    recent = candidate("experience", "记录了输入兼容性的修复经验。")
    rows = select_rows(SourceResult("available", recency=(recent,)), SourceResult("absent"), plan)
    assert [row.identity for row in rows] == ["experience"]


def test_output_destination_is_distinct_from_the_named_subject():
    assert LexicalQuery.from_text("把 NUS 的课程记录整理成 README").anchors == ("nus",)
    assert LexicalQuery.from_text("Look into SQLite lock failures").anchors == ("sqlite",)


@pytest.mark.parametrize("query", [
    "把今天的经验写入 README.md 和 CHANGELOG.md，再检查 NUS 的课程",
    "Save today's notes to README.md and CHANGELOG.md, then inspect NUS courses",
])
def test_output_file_list_stops_before_a_new_subject(query):
    assert LexicalQuery.from_text(query).anchors == ("nus",)


@pytest.mark.parametrize("source", ["session", "memory"])
def test_output_file_list_cannot_replace_the_named_subject(tmp_path, source):
    useful = "NUS 的课程安排已经确认。"
    result = _read_source(tmp_path, source, "把今天 NUS 的课程安排写入 README.md 和 CHANGELOG.md", [
        useful, "CHANGELOG.md 中记录其他大学的课程安排。",
    ])
    assert not result.error_category
    assert [item.private_text for item in result.relevance] == [useful]


@pytest.mark.parametrize("kind", ["summary", "event"])
@pytest.mark.parametrize("query", ["NUS 课程讲师是谁", "NUS 课程", "AI NUS 课程讲师对比"])
def test_activity_target_filter_precedes_fts_and_like_limits(tmp_path, monkeypatch, kind, query):
    from agent.runtime.context_index.activity_source import ActivityRecommendationReader
    from tests.test_context_index_activity_source import NOW_DT, WORKSPACE, _add_summary, _add_event, _create_activity_database

    monkeypatch.setenv("ASTRA_CONTEXT_INDEX_EMBEDDING", "off")
    path = tmp_path / "activity.db"
    db = _create_activity_database(path)
    for i in range(81):
        target = "AI" if query.startswith("AI") else "NUS"
        content = f"{target} 的课程讲师是课程负责人。" if i == 0 else "虚构课程讲师正在登场。"
        if kind == "summary":
            _add_summary(db, str(i), content, seconds_before_now=100 - i)
        else:
            _add_event(db, str(i), i, searchable_text=content, seconds_before_now=100 - i)
    db.commit()
    db.close()
    plan = plan_query(query, NOW_DT)
    result = ActivityRecommendationReader(path).recommend(query, WORKSPACE, NOW_DT, plan)
    assert not result.error_category
    assert [item.locator.primary for item in result.relevance] == ["0"]


@pytest.mark.parametrize("kind", ["summary", "event"])
@pytest.mark.parametrize("query", ["NUS 课程讲师是谁", "NUS 课程"])
def test_activity_named_target_index_avoids_scanning_common_chinese_hits(tmp_path, kind, query):
    from agent.runtime.context_index.activity_source import ActivityRecommendationReader
    from agent.runtime.context_index.sqlite_reader import set_read_window
    from tests.test_context_index_activity_source import NOW_DT, WORKSPACE, _add_summary, _add_event, _create_activity_database

    db = _create_activity_database(tmp_path / "activity.db")
    try:
        for i in range(5001):
            content = "NUS 的课程安排，讲师是课程负责人。" if i == 0 else "课程讲师在无关故事里出现。" * 20
            if kind == "summary":
                _add_summary(db, str(i), content, seconds_before_now=10)
            else:
                _add_event(db, str(i), i, searchable_text=content, seconds_before_now=10)
        db.commit()
        set_read_window(db, NOW_DT.timestamp())
        db.row_factory = sqlite3.Row
        db.create_function("context_activity_summary_tier", 1, lambda _: 0, deterministic=True)
        db.create_function("context_activity_event_tier", 4, lambda *_: 0, deterministic=True)
        _limit_sql_work(db)
        lexical = LexicalQuery.from_text(query)
        if kind == "summary":
            rows = ActivityRecommendationReader._summary_relevance(db, lexical.terms, WORKSPACE, lexical)
            identities = [row["summary_id"] for _, row in rows]
        else:
            rows = ActivityRecommendationReader._event_relevance(db, lexical.terms, WORKSPACE, lexical)
            identities = [row["segment_id"] for _, row in rows]
        assert identities == ["0"]
    finally:
        db.close()


def test_activity_fusion_checks_searchable_evidence_beyond_the_compact_preview(tmp_path, monkeypatch):
    from agent.runtime.context_index.activity_source import ActivityRecommendationReader
    from tests.test_context_index_activity_source import NOW_DT, WORKSPACE, _add_summary, _create_activity_database

    monkeypatch.setenv("ASTRA_CONTEXT_INDEX_EMBEDDING", "off")
    path = tmp_path / "activity.db"
    db = _create_activity_database(path)
    _add_summary(db, "course", "课程背景说明。" * 60 + "NUS 的课程讲师负责教学。", seconds_before_now=10)
    db.commit()
    db.close()
    plan = plan_query("NUS 课程讲师是谁", NOW_DT)
    result = ActivityRecommendationReader(path).recommend(plan.query, WORKSPACE, NOW_DT, plan)
    assert result.relevance and "NUS" not in result.relevance[0].description
    rows = select_rows(SourceResult("absent"), result, plan)
    assert [item.locator.primary for item in rows] == ["course"]
