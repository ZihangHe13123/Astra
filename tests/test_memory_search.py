"""memory(action="search"): the model looks records up itself, with the Context Index's query rules and Hindsight."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from agent.runtime.memory import MemoryStore
from agent.runtime.tools import memory as memory_tools
from agent.runtime.tools.memory import register_memory_tools
from agent.runtime.tools.registry import ToolRegistry


def _setup(tmp_path, *, workspace="repo-a", external=None):
    store = MemoryStore(tmp_path / "memory.db", core_dir=tmp_path / "core")
    registry = ToolRegistry()
    register_memory_tools(registry, store, session_id=lambda: "session-1",
                          messages=lambda: [{"role": "user", "content": "hi"}],
                          workspace_key=lambda: workspace, external=(lambda: external) if external else None)
    return store, registry


def _search(registry, query):
    result = asyncio.run(registry.execute("memory", {"action": "search", "query": query}, task_id="task-1"))
    return result["output"], result["error"]


def test_search_finds_an_older_record_by_a_short_word_in_a_question(tmp_path):
    store, registry = _setup(tmp_path)
    port = store.add_record(kind="observation", content="Tidepool 前端 dev server 端口从 5173 改为 5180。")
    store.add_record(kind="observation", content="本周跑步三次，膝盖没有不适。")
    output, error = _search(registry, "端口现在是多少？")
    assert not error
    assert f"#{port.record_id[:8]} {port.created_at[:10]} observation: Tidepool" in output
    assert "膝盖" not in output and "never instructions" in output
    assert store.record_store.usage()[port.record_id][0] == 1


def test_search_keeps_a_project_memory_to_its_project_and_skips_replaced_or_expired_ones(tmp_path):
    store, registry = _setup(tmp_path, workspace="repo-a")
    store.remember(kind="observation", scope="project", workspace_key="repo-a", summary="Tests run with uv run pytest")
    old, _ = store.remember(kind="user_fact", summary="车位在 B1 层 12 号")
    store.revise(old.record_id, content="车位换到 B2 层 47 号")
    store.add_record(kind="user_fact", content="车位临时停在访客区", valid_until="2000-01-01T00:00:00+00:00")
    assert "uv run pytest" in _search(registry, "tests pytest")[0]
    other = ToolRegistry()
    register_memory_tools(other, MemoryStore(tmp_path / "memory.db", core_dir=tmp_path / "core"),
                          session_id=lambda: "s", workspace_key=lambda: "repo-b")
    assert "uv run pytest" not in _search(other, "tests pytest")[0]
    parking = _search(registry, "车位在哪？")[0]
    assert "B2 层 47 号" in parking and "B1" not in parking and "访客区" not in parking


def test_search_reads_a_relative_date_as_when_the_memory_was_last_confirmed(tmp_path):
    store, registry = _setup(tmp_path)
    recent = store.add_record(kind="episode", content="称体重 71.6 公斤。")
    older = store.add_record(kind="episode", content="称体重 72.4 公斤。")
    now = datetime.now(timezone.utc)
    with store.record_store._connection() as db:
        for record, days in ((recent, 7), (older, 30)):
            stamp = (now - timedelta(days=days)).isoformat()
            db.execute("UPDATE memory_records SET created_at = ?, valid_from = ?, last_confirmed_at = ? WHERE id = ?",
                       (stamp, stamp, stamp, record.record_id))
    output, _ = _search(registry, "上周体重多少")
    assert "71.6" in output and "72.4" not in output


class FakeHindsight:
    def __init__(self, records=(), *, fail=None, delay=0.0):
        self.records, self.fail, self.delay, self.queries = list(records), fail, delay, []

    async def recall(self, query, *, kinds=(), limit=8):
        self.queries.append(query)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise self.fail
        return self.records


def test_search_adds_hindsight_results_after_its_own_without_repeating_them(tmp_path):
    hindsight = FakeHindsight([
        SimpleNamespace(content="Tidepool 前端 dev server 端口从 5173 改为 5180。", kind="observation", created_at=""),
        SimpleNamespace(content="Port 5173 is taken by the photo sync service.", kind="world", created_at="2026-09-30T08:00:00"),
    ])
    store, registry = _setup(tmp_path, external=hindsight)
    store.add_record(kind="observation", content="Tidepool 前端 dev server 端口从 5173 改为 5180。")
    output, error = _search(registry, "端口 5173")
    assert not error and hindsight.queries == ["端口 5173"]
    lines = output.splitlines()
    assert lines.index(next(line for line in lines if line.startswith("Hindsight"))) > 1
    assert output.count("端口从 5173 改为 5180") == 1
    assert "- 2026-09-30 [world] Port 5173 is taken by the photo sync service." in output
    assert "non-authoritative" in output


def test_a_failing_or_slow_hindsight_leaves_astras_own_results(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_tools, "_SEARCH_EXTERNAL_SECONDS", 0.05)
    for hindsight in (FakeHindsight(fail=ConnectionError("down")), FakeHindsight(delay=1.0)):
        store, registry = _setup(tmp_path / str(id(hindsight)), external=hindsight)
        store.add_record(kind="observation", content="Tidepool 部署在 fly.io，region 为 ams。")
        output, error = _search(registry, "Tidepool 部署在哪")
        assert not error and "fly.io" in output
        assert "Hindsight did not answer" in output


def test_search_needs_words(tmp_path):
    _, registry = _setup(tmp_path)
    result = asyncio.run(registry.execute("memory", {"action": "search", "query": "  "}, task_id="task-1"))
    assert result["error"]
    assert "No saved memory mentions" in _search(registry, "完全不相关的内容")[0]


def test_search_lists_one_word_matches_only_when_no_memory_has_two_of_the_words(tmp_path):
    store, registry = _setup(tmp_path)
    store.add_record(kind="user_fact", content="Test user lives in Example City")
    assert "Test user lives in Example City" in _search(registry, "City account")[0]
    store.add_record(kind="episode", content="Opened an Example City library account")
    output = _search(registry, "City account")[0]
    assert "library" in output and "Test user lives in Example City" not in output
