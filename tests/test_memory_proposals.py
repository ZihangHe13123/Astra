"""Idle consolidation proposes saved-memory changes; nothing changes until the user accepts."""

import asyncio
import json
from datetime import datetime, timezone


from agent.cli.memory_commands import execute_memory_command
from agent.runtime.memory import MemoryStore
from agent.runtime.memory_proposals import (
    IDLE_SECONDS, ConsolidationResult, ConsolidationScheduler, consolidate,
)
from agent.runtime.session_recall import SessionRecall


class ScriptedModel:
    def __init__(self, reply):
        self.reply, self.requests = reply, []

    async def chat_limited(self, messages, tools=None, **options):
        self.requests.append(messages)
        reply = self.reply(json.loads(messages[-1]["content"])) if callable(self.reply) else self.reply
        return {"content": reply if isinstance(reply, str) else json.dumps(reply)}


def _world(tmp_path, lines):
    store = MemoryStore(tmp_path / "memory.db", core_dir=tmp_path / "core")
    changes = []
    store.on_change = changes.append
    recall = SessionRecall(tmp_path / "sessions.db")
    recall.init_db()
    session = recall.get_or_create_session("chat-1", title="chat-1", workspace_key="repo-a")
    ids = {}
    for key, role, text in lines:
        ids[key] = recall.log_message(session, role, text)
    return store, tmp_path / "sessions.db", ids, changes


def _run(store, archive, model, **options):
    return asyncio.run(consolidate(store, archive, lambda: (model, "scripted"), **options))


CHAT = [
    ("u1", "user", "以后前端项目都用 pnpm，别再用 npm 了。"),
    ("a1", "assistant", "好的，之后用 pnpm。"),
    ("u2", "user", "车位换了，现在是 B2 层 47 号。"),
    ("a2", "assistant", "记下了。"),
    ("u3", "user", "帮我看看这段报错。"),
    ("u4", "user", "谢谢，没事了。"),
]


def test_a_pass_proposes_changes_that_only_apply_when_accepted(tmp_path):
    store, archive, ids, changes = _world(tmp_path, CHAT)
    parking, _ = store.remember(kind="user_fact", summary="车位在 B1 层 12 号")
    changes.clear()
    model = ScriptedModel(lambda request: {"proposals": [
        {"action": "remember", "kind": "preference", "scope": "project", "summary": "前端项目用 pnpm",
         "content": "前端项目用 pnpm，不用 npm。", "evidence": [ids["u1"]], "reason": "stated as a rule"},
        {"action": "revise", "memory_id": parking.record_id[:8], "summary": "车位在 B2 层 47 号",
         "content": "车位换到 B2 层 47 号。", "evidence": [ids["u2"]], "reason": "the user moved"},
    ]})
    result = _run(store, archive, model)
    assert [item["action"] for item in result.proposed] == ["remember", "revise"]
    request = json.loads(model.requests[0][-1]["content"])
    assert [item["id"] for item in request["saved_memories"]] == [parking.record_id[:8]]
    assert [item["id"] for item in request["messages"]] == list(ids.values())
    # Nothing changed yet.
    assert changes == [] and store.resolve_record(parking.record_id, active_only=True) is not None

    listing, _ = execute_memory_command(store, ["proposals"], session_id="s")
    assert "前端项目用 pnpm" in listing and "以后前端项目都用 pnpm" in listing and "车位在 B1 层 12 号" in listing
    first, second = (item["id"] for item in store.record_store.proposals())
    output, error = execute_memory_command(store, ["accept", "all"], session_id="s")
    assert not error and "Saved" in output and "Revised" in output
    saved = next(record for record in store.record_store.list_records(status="active")
                 if record.metadata.get("summary") == "前端项目用 pnpm")
    assert saved.source_session_id == "chat-1" and saved.source_message_id == f"archive:{ids['u1']}"
    assert saved.metadata["scope"] == "project" and saved.metadata["workspace_key"] == "repo-a"
    assert store.record_store.get(parking.record_id).status == "superseded"
    assert [change["event"] for change in changes] == ["saved", "revised"]
    assert store.record_store.proposals() == [] and {item["id"] for item in store.record_store.proposals(
        status="accepted")} == {first, second}
    # The next pass starts after what was reviewed.
    assert _run(store, archive, model).skipped and len(model.requests) == 1


def test_proposals_need_the_users_own_words_and_never_keep_secrets(tmp_path):
    store, archive, ids, _ = _world(tmp_path, CHAT)
    store.remember(kind="preference", summary="Prefers dark themes")
    model = ScriptedModel({"proposals": [
        {"action": "remember", "kind": "preference", "summary": "Agrees to pnpm", "evidence": [ids["a1"]]},
        {"action": "remember", "kind": "user_fact", "summary": "API key sk-live-0123456789abcdef0123456789",
         "evidence": [ids["u3"]]},
        {"action": "remember", "kind": "preference", "summary": "prefers DARK themes", "evidence": [ids["u1"]]},
        {"action": "revise", "memory_id": "deadbeef", "content": "x", "evidence": [ids["u2"]]},
        {"action": "remember", "kind": "mood", "summary": "Happy today", "evidence": [ids["u4"]]},
        {"action": "remember", "kind": "preference", "summary": "Uses pnpm", "evidence": [ids["u1"]]},
    ]})
    result = _run(store, archive, model)
    assert [item["summary"] for item in result.proposed] == ["Uses pnpm"]


def test_an_unreadable_reply_or_a_missing_key_reviews_the_same_messages_again(tmp_path):
    store, archive, _, _ = _world(tmp_path, CHAT)
    assert _run(store, archive, ScriptedModel("I could not decide.")).skipped
    unavailable = asyncio.run(consolidate(store, archive, lambda: (None, "DEEPSEEK_API_KEY is not set")))
    assert unavailable.skipped == "DEEPSEEK_API_KEY is not set"
    retry = ScriptedModel({"proposals": []})
    assert not _run(store, archive, retry).skipped and len(json.loads(retry.requests[0][-1]["content"])["messages"]) == 6


def test_few_new_messages_wait_unless_asked_and_a_first_pass_skips_old_history(tmp_path):
    store, archive, ids, _ = _world(tmp_path, CHAT[:3])
    model = ScriptedModel({"proposals": []})
    assert "2 new messages" in _run(store, archive, model).skipped and not model.requests
    assert not _run(store, archive, model, force=True).skipped and len(model.requests) == 1

    later = tmp_path / "later"
    later.mkdir()
    store, archive, ids, _ = _world(later, CHAT)
    # Messages older than a few days are not read on a first pass.
    old = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
    recall = SessionRecall(archive)
    recall._get_conn().execute("UPDATE messages SET timestamp = ? WHERE id <= ?", (old, ids["a2"]))
    recall._get_conn().commit()
    model = ScriptedModel({"proposals": []})
    _run(store, archive, model, force=True)
    assert [item["id"] for item in json.loads(model.requests[0][-1]["content"])["messages"]] == [ids["u3"], ids["u4"]]


def test_a_proposal_for_a_memory_changed_since_is_dismissed_not_applied(tmp_path):
    store, archive, ids, _ = _world(tmp_path, CHAT)
    parking, _ = store.remember(kind="user_fact", summary="车位在 B1 层 12 号")
    _run(store, archive, ScriptedModel({"proposals": [
        {"action": "forget", "memory_id": parking.record_id[:8], "evidence": [ids["u2"]], "reason": "moved"}]}))
    store.revise(parking.record_id, content="车位在 B3 层")
    [proposal] = store.record_store.proposals()
    output, _ = execute_memory_command(store, ["accept", f"P{proposal['id']}"], session_id="s")
    assert "dismissed" in output and store.record_store.proposals(status="dismissed")
    assert any(record.content == "车位在 B3 层" for record in store.record_store.list_records(status="active"))


def test_dismissing_leaves_memory_unchanged(tmp_path):
    store, archive, ids, changes = _world(tmp_path, CHAT)
    _run(store, archive, ScriptedModel({"proposals": [
        {"action": "remember", "kind": "preference", "summary": "Uses pnpm", "evidence": [ids["u1"]]}]}))
    output, error = execute_memory_command(store, ["dismiss", "all"], session_id="s")
    assert not error and output.startswith("Dismissed P")
    assert not store.record_store.list_records(status="active") and changes == []
    assert execute_memory_command(store, ["accept", "all"], session_id="s")[1]


def test_the_scheduler_waits_for_an_idle_backend_and_announces_proposals(tmp_path):
    store = MemoryStore(tmp_path / "memory.db", core_dir=tmp_path / "core")
    now = [1000.0]
    runs, notices = [], []

    async def run(store, archive, client, *, force=False):
        runs.append(force)
        return ConsolidationResult([{"id": 1, "action": "remember", "summary": "Uses pnpm"}])

    scheduler = ConsolidationScheduler(store, tmp_path / "sessions.db", lambda: (None, ""), enabled=True,
                                       notify=notices.append, clock=lambda: now[0], run=run)

    async def ticks():
        scheduler.tick(busy=True)
        now[0] += IDLE_SECONDS - 1
        scheduler.tick(busy=False)
        assert scheduler.task is None
        now[0] += 2
        scheduler.tick(busy=False)
        await scheduler.task
        scheduler.tick(busy=False)  # the next pass waits for the interval
        assert scheduler.task.done() and runs == [False]
        scheduler.enabled = False
        scheduler.request()
        scheduler.tick(busy=False)
        await scheduler.task

    asyncio.run(ticks())
    assert runs == [False, True]
    assert notices[0] == {"type": "memory_notice", "event": "proposed", "count": 1, "summary": "Uses pnpm"}


def test_a_failed_pass_never_reaches_the_conversation(tmp_path):
    store = MemoryStore(tmp_path / "memory.db", core_dir=tmp_path / "core")

    async def run(*args, **kwargs):
        raise RuntimeError("network down")

    now = [0.0]
    scheduler = ConsolidationScheduler(store, tmp_path / "sessions.db", lambda: (None, ""), enabled=True,
                                       notify=lambda event: None, clock=lambda: now[0], run=run)

    async def once():
        now[0] += IDLE_SECONDS
        scheduler.tick(busy=False)
        await scheduler.task

    asyncio.run(once())
    assert "failed: RuntimeError" in scheduler.status()


def test_turning_consolidation_off_is_saved(tmp_path, monkeypatch):
    from agent.cli.memory_consolidation_preferences import load_memory_consolidation_enabled

    monkeypatch.setenv("AGENT_SETTINGS_PATH", str(tmp_path / "settings.json"))
    store = MemoryStore(tmp_path / "memory.db", core_dir=tmp_path / "core")
    scheduler = ConsolidationScheduler(store, tmp_path / "sessions.db", lambda: (None, ""),
                                       enabled=load_memory_consolidation_enabled(), notify=lambda event: None)
    assert scheduler.enabled
    output, _ = execute_memory_command(store, ["consolidation", "off"], session_id="s", consolidation=scheduler)
    assert "Memory consolidation: off" in output and not load_memory_consolidation_enabled()
    status, _ = execute_memory_command(store, [], session_id="s", consolidation=scheduler)
    assert "Memory consolidation: off" in status


def test_only_one_astra_reviews_at_a_time(tmp_path):
    from agent.runtime.instance_lock import InstanceLock

    store, archive, _, _ = _world(tmp_path, CHAT)
    with InstanceLock(store.path.with_name(store.path.name + ".consolidation.lock")):
        model = ScriptedModel({"proposals": []})
        assert "another Astra" in _run(store, archive, model, force=True).skipped and not model.requests
