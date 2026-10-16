"""Behavioral contracts for Astra's dependency-free memory recommendation."""

from datetime import UTC, datetime
import asyncio
import json
import sqlite3
from dataclasses import replace

from agent.runtime.context_index.broker import ContextIndexBroker
from agent.runtime.context_index.feedback import FeedbackStore, item_key, scope_key
from agent.runtime.context_index.record_source import RecordSource
from agent.runtime.context_index.workspace import WorkspaceIdentity
from agent.runtime.memory import MemoryStore

from agent.runtime.context_index.models import RecommendationCandidate, SourceLocator, SourceResult
from agent.runtime.context_index.query import plan_query
from agent.runtime.context_index.selection import select_rows


NOW = datetime(2026, 9, 8, 10, tzinfo=UTC)


def candidate(key, text, *, source="session", rank=0, tier=0, timestamp=None):
    return RecommendationCandidate(
        source=source,
        identity=key,
        description=text,
        timestamp=NOW.timestamp() - 60 if timestamp is None else timestamp,
        workspace_tier=tier,
        native_query_rank=rank,
        topic_key=key,
        trust_label="historical_context" if source == "session" else "untrusted_observation",
        locator=SourceLocator("session_message" if source == "session" else "activity_summary", key),
        private_text=text,
    )


def test_resume_query_uses_recent_goal_but_independent_question_does_not():
    plan = plan_query("继续昨天的修改", NOW, recent_text="Astra 修复数据库锁超时", task_text="完善事务恢复")
    assert plan.intent == "resume"
    assert "数据库" in plan.query and "事务恢复" in plan.query
    assert plan.time_start == datetime(2026, 9, 7, tzinfo=UTC).timestamp()
    assert plan.time_end == datetime(2026, 9, 8, tzinfo=UTC).timestamp()
    assert not plan_query("谢谢", NOW, task_text="Astra 数据库").should_recall
    independent = plan_query("解释 Python 生成器", NOW, recent_text="Astra 数据库")
    assert "数据库" not in independent.query
    assert not independent.include_recent


def test_relevance_can_use_four_slots_without_activity_filler():
    sessions = SourceResult(
        "available",
        relevance=tuple(
            candidate(str(i), text, rank=i)
            for i, text in enumerate(
                (
                    "SQLite transactions retry failed commits",
                    "Astra worker shutdown joins threads",
                    "Memory evidence remains private to the request",
                    "Windows paths use native separators",
                )
            )
        ),
    )
    activities = SourceResult("available", recency=(candidate("noise", "music player", source="activity"),))
    rows = select_rows(sessions, activities, plan_query("Astra architecture", NOW))
    assert len(rows) == 4
    assert all(row.source == "session" for row in rows)
    assert [row.slot for row in rows] == ["R1", "R2", "R3", "R4"]


def test_no_recency_injection_without_recall_intent_and_no_future_candidates():
    noise = candidate("recent", "Unrelated window")
    future = candidate("future", "Astra answer written tomorrow", timestamp=NOW.timestamp() + 1)
    rows = select_rows(
        SourceResult("available", relevance=(future,), recency=(noise,)),
        SourceResult("absent"),
        plan_query("Astra memory", NOW),
    )
    assert rows == ()


def test_near_duplicates_do_not_displace_complementary_evidence():
    a = candidate("a", "Astra database recovery: retry transactions after a lock timeout", rank=0)
    b = candidate("b", "Astra database recovery: retry transactions after a lock timeout.", source="activity", rank=0)
    c = candidate("c", "The shutdown worker must join before the SQLite connection closes", rank=1)
    rows = select_rows(
        SourceResult("available", relevance=(a, c)),
        SourceResult("available", relevance=(b,)),
        plan_query("Astra recovery", NOW),
    )
    assert len(rows) == 2
    assert any(row.identity == "c" for row in rows)


def test_time_window_and_cold_start_are_deterministic():
    recent = candidate("today", "Astra notes")
    yesterday = candidate("yesterday", "Astra retry constraints", timestamp=NOW.timestamp() - 86400)
    plan = plan_query("昨天 Astra 的限制", NOW)
    rows = select_rows(SourceResult("available", relevance=(recent, yesterday)), SourceResult("absent"), plan)
    assert [row.identity for row in rows] == ["yesterday"]


def test_explicit_recent_desktop_recall_is_not_restricted_to_repository():
    item = candidate("browser", "Reading a research paper in the browser", source="activity", tier=2)
    result = SourceResult("available", recency=(item,))
    assert select_rows(SourceResult("absent"), result, plan_query("刚才看的网页", NOW))
    assert not select_rows(SourceResult("absent"), result, plan_query("继续仓库修改", NOW))


def test_rrf_rewards_two_independent_channels_without_comparing_raw_scores():
    a = replace(candidate("a", "Astra transaction rollback", rank=-10000), channel_ranks=(("lexical", 1),))
    b = replace(candidate("b", "Astra transaction retry", rank=-0.4), channel_ranks=(("lexical", 2), ("vector", 1)))
    rows = select_rows(
        SourceResult("available", relevance=(a, b)), SourceResult("absent"), plan_query("Astra transaction", NOW)
    )
    assert rows[0].identity == "b"
    duplicated = replace(a, channel_ranks=(("lexical", 1),) * 10)
    rows = select_rows(
        SourceResult("available", relevance=(duplicated, b)),
        SourceResult("absent"),
        plan_query("Astra transaction", NOW),
    )
    assert rows[0].identity == "b"


def test_feedback_is_explicit_scoped_versioned_and_idempotent(tmp_path):
    feedback = FeedbackStore(tmp_path / "feedback.db")
    item = candidate("private-id", "private Astra notes")
    scope = scope_key("/private/workspace", "lookup")
    key = item_key(item)
    assert feedback.adjustments(scope, (key,)) == {}
    assert not feedback.path.exists()
    assert not feedback.record(scope, key, "event", "opened")
    assert feedback.record(scope, key, "event", "useful")
    value = feedback.adjustments(scope, (key,))[key]
    assert 0 < value < 0.1
    assert feedback.record(scope, key, "event", "useful")
    assert feedback.adjustments(scope, (key,))[key] == value
    assert feedback.adjustments(scope_key("other", "lookup"), (key,)) == {}
    assert feedback.adjustments(scope, (item_key(replace(item, revision="new")),)) == {}
    assert feedback.record(scope, key, "event", "outdated")
    assert feedback.adjustments(scope, (key,))[key] < 0
    assert b"private" not in feedback.path.read_bytes()


def test_native_records_skip_pinned_conflicts_and_reject_changed_versions(tmp_path):
    store = MemoryStore(path=tmp_path / "memory.db", core_dir=tmp_path / "core")
    record = store.add_record(kind="observation", content="Astra native recall keeps useful project constraints")
    store.add_record(kind="observation", content="Astra pinned duplicate", metadata={"pinned": True})
    store.add_record(kind="observation", content="Astra unresolved conflict", metadata={"conflict_state": "pending"})
    reader = RecordSource(store.path)
    plan = plan_query("Astra", datetime.now().astimezone())
    result = reader.recommend(plan, WorkspaceIdentity("astra", "", "astra"), frozenset())
    assert [row.identity for row in result.relevance] == [record.record_id]
    assert record.content in reader.open(result.relevance[0].locator, 2).items[0]
    store.supersede_record(record.record_id, content="Astra corrected project constraint")
    assert reader.open(result.relevance[0].locator, 2).items == ()


def test_native_records_keep_a_project_memory_to_its_project(tmp_path):
    store = MemoryStore(path=tmp_path / "memory.db", core_dir=tmp_path / "core")
    record, _ = store.remember(kind="observation", scope="project", workspace_key="repo-a",
                               summary="Astra release builds need the notarization profile")
    reader = RecordSource(store.path)
    plan = plan_query("Astra notarization profile", datetime.now().astimezone())

    def found(key):
        return [row.identity for row in reader.recommend(plan, WorkspaceIdentity(key, "", key), frozenset()).relevance]

    assert found("repo-a") == [record.record_id]
    assert found("repo-b") == []
    # A handle taken in one project does not open in another.
    locator = reader.recommend(plan, WorkspaceIdentity("repo-a", "", "repo-a"), frozenset()).relevance[0].locator
    reader.recommend(plan, WorkspaceIdentity("repo-b", "", "repo-b"), frozenset())
    assert reader.open(locator, 0).items == ()
    assert found("repo-a") == [record.record_id]

def test_native_record_date_filter_precedes_pool_limit(tmp_path):
    store = MemoryStore(path=tmp_path / "memory.db", core_dir=tmp_path / "core")
    yesterday = store.add_record(kind="observation", content="Astra prior recovery constraint")
    newer = [store.add_record(kind="observation", content=f"Astra newer observation {i}") for i in range(45)]
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE memory_records SET created_at = ?, valid_from = ?, last_confirmed_at = ?",
                   ("2026-09-01T00:00:00+00:00", "2026-09-01T00:00:00+00:00", "2026-09-08T08:00:00+00:00"))
        db.execute("UPDATE memory_records SET last_confirmed_at = ? WHERE id = ?",
                   ("2026-09-07T10:00:00+00:00", yesterday.record_id))
    reader = RecordSource(store.path)
    result = reader.recommend(plan_query("昨天 Astra", NOW), WorkspaceIdentity("astra", "", "astra"), frozenset())
    assert [item.identity for item in result.relevance] == [yesterday.record_id]
    assert len(newer) > 40
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE memory_records SET valid_until = ? WHERE id = ?",
                   ("2026-09-07T12:00:00+00:00", yesterday.record_id))
    assert reader.open(result.relevance[0].locator, 0).items == ()


def test_session_time_filter_runs_before_limit_and_open_cannot_read_future(tmp_path):
    from tests.test_context_index_session_source import _create_recall_database, _add_session
    from agent.runtime.context_index.session_source import SessionRecommendationSource

    path = tmp_path / "sessions.db"
    db = _create_recall_database(path)
    workspace = WorkspaceIdentity("astra", "", "astra")
    ids = _add_session(
        db,
        "history",
        "Astra recovery",
        "astra",
        [
            ("user", "Astra recovery prior constraint", NOW.timestamp() - 100),
            ("assistant", "Astra recovery future answer", NOW.timestamp() + 1),
            *[("assistant", f"Astra recovery future {i}", NOW.timestamp() + 2 + i) for i in range(100)],
        ],
    )
    db.commit()
    db.close()
    reader = SessionRecommendationSource(path)
    plan = plan_query("Astra recovery", NOW)
    result = reader.recommend(plan.query, workspace, "current", frozenset(), NOW.timestamp(), plan)
    assert [row.locator.secondary for row in result.relevance] == [ids[0]]
    evidence = reader.open(result.relevance[0].locator, 5, plan)
    assert len(evidence.items) == 1 and "future" not in evidence.items[0]


def test_replay_identity_excludes_later_messages_with_identical_timestamps(tmp_path):
    from tests.test_context_index_session_source import _create_recall_database, _add_session
    from agent.runtime.context_index.session_source import SessionRecommendationSource

    path = tmp_path / "sessions.db"
    db = _create_recall_database(path)
    ids = _add_session(db, "current", "Astra recovery", "astra", [
        ("user", "Astra recovery prior constraint", NOW.timestamp() - 1),
        ("user", "Astra recovery", NOW.timestamp()),
        *[("assistant", f"Astra recovery future answer {i}", NOW.timestamp()) for i in range(100)],
    ])
    db.commit()
    db.close()
    reader = SessionRecommendationSource(path)
    plan = replace(plan_query("Astra recovery", NOW), message_id=ids[1])
    result = reader.recommend(plan.query, WorkspaceIdentity("astra", "", "astra"), "current", frozenset(), NOW.timestamp(), plan)
    assert [item.locator.secondary for item in result.relevance] == [ids[0]]
    assert len(reader.open(result.relevance[0].locator, 5, plan).items) == 1


def test_native_habit_replay_excludes_later_imports_and_separates_cached_cutoffs(tmp_path, monkeypatch):
    from datetime import timedelta
    from tests.test_context_index_habits import _create_activity_database, _add_event
    from agent.runtime.context_index.activity_source import ActivityRecommendationReader

    monkeypatch.setenv("ASTRA_CONTEXT_INDEX_EMBEDDING", "off")
    path = tmp_path / "activity.db"
    db = _create_activity_database(path)
    for weeks in (1, 2, 3):
        _add_event(db, NOW - timedelta(weeks=weeks))
    db.execute("UPDATE activity_events SET imported_at = ?", (NOW.isoformat(),))
    db.commit()
    db.close()
    reader = ActivityRecommendationReader(path, local_zone=UTC)
    workspace = WorkspaceIdentity("astra", "", "astra")
    after = NOW + timedelta(minutes=1)
    later = reader.recommend("Astra habit", workspace, after, plan_query("Astra habit", after))
    assert later.habit is not None
    assert reader.open(later.habit.locator, 0).items
    before = NOW - timedelta(minutes=1)
    earlier = reader.recommend("Astra habit", workspace, before, plan_query("Astra habit", before))
    assert earlier.habit is None
    assert earlier.relevance == ()


def test_chinese_question_recalls_local_records_without_word_segmentation_dependency(tmp_path):
    store = MemoryStore(path=tmp_path / "memory.db", core_dir=tmp_path / "core")
    fact = store.add_record(kind="observation", content="数据库锁超时需要回滚事务并重试")
    store.add_record(kind="observation", content="用户喜欢喝咖啡")
    reader = RecordSource(store.path)
    plan = plan_query("上次数据库锁超时是怎么处理的", datetime.now().astimezone())
    result = reader.recommend(plan, WorkspaceIdentity("astra", "", "astra"), frozenset())
    assert [item.identity for item in result.relevance] == [fact.record_id]


def test_broker_submissions_and_actual_previews_are_separate_from_builds(tmp_path):
    from tests.test_context_index_broker import FakeSource
    from agent.runtime.context_index.ranking import _escape

    broker = ContextIndexBroker(
        "session", 400, FakeSource("session"), FakeSource("activity"), feedback_path=tmp_path / "feedback.db"
    )
    events = []
    broker.set_feedback_sink(events.append)
    pack = asyncio.run(
        broker.build("continue", "r", "current", WorkspaceIdentity("astra", "", "astra"), NOW, frozenset())
    )
    assert [event["type"] for event in events] == ["context_index_built"]
    assert all(_escape(row.description) in pack.rendered for row in pack.rows)
    broker.mark_submitted([{"role": "user", "content": "omitted index"}])
    assert len(events) == 1
    broker.mark_submitted([{"role": "user", "content": pack.rendered}])
    broker.mark_submitted([{"role": "user", "content": pack.rendered}])
    assert [event["type"] for event in events] == ["context_index_built", "context_index_submitted"]
    assert "Feedback saved" in broker.record_feedback(pack.rows[0].slot, "useful")
    broker.complete_request("r")
    assert "Feedback saved" in broker.record_feedback(pack.rows[0].slot, "useful")
    assert "description" not in json.dumps(events)


def test_broker_abstains_before_source_io_on_trivial_turn(tmp_path):
    class NeverRead:
        def recommend(self, *args):
            raise AssertionError("trivial turns must not read databases")

    broker = ContextIndexBroker("all", 900, NeverRead(), NeverRead())
    pack = asyncio.run(
        broker.build(
            "谢谢", "r", "s", WorkspaceIdentity("a", "", "a"), NOW, frozenset(), memory_path=tmp_path / "missing.db"
        )
    )
    assert pack.rendered == "" and broker.last_trace.decision == "not-needed"
    assert not (tmp_path / "missing.db").exists()


def test_shared_open_budget_applies_to_actual_returned_unicode(tmp_path):
    from tests.test_context_index_broker import FakeSource
    from agent.runtime.context_index.models import EvidenceResult
    from agent.runtime.token_estimator import estimate_value_tokens

    class LongSource(FakeSource):
        def open(self, locator, window, plan=None):
            return EvidenceResult("session", "historical_context", "长证据", ("正文&<标记>" * 1500,))

    broker = ContextIndexBroker("session", 900, LongSource("session"), FakeSource("activity"))
    pack = asyncio.run(broker.build("continue", "r", "s", WorkspaceIdentity("a", "", "a"), NOW, frozenset()))
    outputs = [broker.open([pack.rows[0].handle], 2) for _ in range(2)]
    evidence = [output for output in outputs if output.startswith("<context-evidence>")]
    assert sum(estimate_value_tokens(output) for output in evidence) <= 2000
    for output in evidence:
        assert output.endswith("</context-evidence>")
        assert output.count("<evidence ") == output.count("</evidence>")
        assert "<标记>" not in output


def test_native_index_shows_astra_records_and_counts_them_as_used(tmp_path):
    from tests.test_context_index_runtime import DoneLLM
    from tests.test_context_index_broker import FakeSource
    from agent.runtime.memory_router import MemoryRouter
    from agent.runtime.react import ReActAgent
    from agent.runtime.tools.registry import ToolRegistry
    from agent.core.msg import ContentBlock, Msg

    store = MemoryStore(path=tmp_path / "memory.db", core_dir=tmp_path / "core")
    record = store.add_record(kind="observation", content="Astra recovery requires an atomic SQLite transaction")

    router = MemoryRouter(store)
    llm = DoneLLM()
    empty = SourceResult("absent")
    broker = ContextIndexBroker(
        "session", 900, FakeSource("session", result=empty), FakeSource("activity", result=empty)
    )
    agent = ReActAgent(
        "agent",
        llm,
        ToolRegistry(),
        memory_store=store,
        memory_router=router,
        context_index_broker=broker,
        max_iterations=1,
        timing_log_enabled=False,
    )
    agent.context.set_session(str(tmp_path / "session.json"))
    before = store.format_core_prompt()
    asyncio.run(agent.reply(Msg(content=[ContentBlock.text("Astra recovery constraints")], id="native-request")))
    prompt = json.dumps(llm.calls[0].messages, ensure_ascii=False)
    assert record.content in prompt
    assert "<retrieved-memory>" not in prompt
    assert broker.last_trace.submitted
    assert [row.source for row in broker.last_trace.displayed] == ["memory"]
    assert store.format_core_prompt() == before
    assert len(llm.calls) == 1
    # A record the index puts in front of the model counts as used.
    assert store.record_store.usage()[record.record_id][0] == 1
