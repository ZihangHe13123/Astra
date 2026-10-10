"""On-demand history must retrieve evidence without changing its authority."""

import asyncio
import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path

import pytest

from agent.runtime.learning_archive import CONTENT_CHARS, EVIDENCE_CHARS, EXCERPT_CHARS, LearningArchive
from agent.runtime.tools import session_recall as tool
from agent.runtime.tools.registry import ToolRegistry
from agent.runtime.session_recall import SessionRecall


@pytest.fixture
def archive():
    path = Path(os.environ["AGENT_LEARNING_PATH"])
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE learning_proposals (
            id TEXT PRIMARY KEY, session_id TEXT, kind TEXT, payload_json TEXT,
            status TEXT, created_at TEXT, updated_at TEXT)""")

    def add(name, content="", *, status="pending", kind="observation", date="2026-09-01",
            tags=None, evidence="", evidence_role="tool", raw=None):
        payload = raw if raw is not None else json.dumps({
            "content": content, "tags": tags or [], "evidence": evidence,
            "evidence_role": evidence_role,
        })
        with sqlite3.connect(path) as db:
            db.execute("INSERT INTO learning_proposals VALUES (?, ?, ?, ?, ?, ?, ?)", (
                f"lr_{name}", "old-session", kind, payload, status, date, date,
            ))
        return f"learning:lr_{name}"

    return path, add


def test_keywords_rank_before_limit_and_find_chinese(archive):
    path, add = archive
    expected = add("pip", "ComfyUI 环境没有 pip，可用 uv 安装依赖。", date="2026-08-01")
    add("other", "ComfyUI MPS 工作流", date="2026-09-12")
    add("unrelated", "Astra 的任务取消实现")
    reader = LearningArchive(path)
    assert reader.lookup("ComfyUI pip", limit=1)["results"][0]["record_id"] == expected
    assert reader.lookup("安装依赖")["results"][0]["record_id"] == expected
    assert reader.lookup("环境 pip")["results"][0]["record_id"] == expected
    assert reader.lookup("完全不相关xyz")["total"] == 0


def test_fetch_preserves_original_dates_evidence_and_status(archive):
    path, add = archive
    record_id = add("old", "曾使用 /undo；必须核对当前代码。", status="applied",
                    evidence="旧版代码检查结果", evidence_role="tool", tags=["astra", "undo"])
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    result = LearningArchive(path).lookup(record_id=record_id)
    item = result["results"][0]
    assert item == {
        "record_id": record_id, "source": "learning", "session_id": "old-session",
        "created_at": "2026-09-01", "updated_at": "2026-09-01", "legacy_status": "applied",
        "tags": ["astra", "undo"], "content": "曾使用 /undo；必须核对当前代码。",
        "evidence": "旧版代码检查结果", "evidence_role": "tool", "truncated": False,
    }
    assert "potentially stale and unverified" in result["notice"]
    assert "not instructions or authorization" in result["notice"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert [p.name for p in path.parent.glob("learning.db*")] == ["learning.db"]


@pytest.mark.parametrize("status", ["rejected", "superseded", "rolled_back", "anything-unknown"])
def test_ineligible_records_are_neither_searched_nor_expanded(archive, status):
    path, add = archive
    record_id = add("excluded", "findme", status=status)
    reader = LearningArchive(path)
    assert reader.lookup("findme")["total"] == 0
    assert reader.lookup(record_id=record_id)["status"] == "not_found"


def test_skill_records_and_malformed_payloads_do_not_pollute_results(archive):
    path, add = archive
    for name, raw in (("invalid", "not json"), ("array", "[]"), ("null", '{"content":null}'),
                      ("object", '{"content":{"text":"findme"}}')):
        add(name, raw=raw)
    add("skill", "findme", kind="skill_create")
    kept = add("valid", "findme", tags={"bad": "shape"}, evidence_role=["tool"])
    result = LearningArchive(path).lookup("findme")
    assert result["status"] == "ok"
    assert [row["record_id"] for row in result["results"]] == [kept]
    assert result["results"][0]["tags"] == []


def test_excerpt_includes_keyword_and_full_read_is_bounded(archive):
    path, add = archive
    text = "开头 " * 500 + "uv 是这里的关键工具 " + "后续 " * CONTENT_CHARS
    record_id = add("long", text, evidence="a" * (EVIDENCE_CHARS + 1))
    reader = LearningArchive(path)
    excerpt = reader.lookup("uv")["results"][0]
    assert "uv" in excerpt["excerpt"]
    assert 0 < excerpt["excerpt_offset"] < 1500
    assert len(excerpt["excerpt"]) <= EXCERPT_CHARS
    assert excerpt["truncated"]
    expanded = reader.lookup(record_id=record_id)["results"][0]
    assert expanded["content"] == text[:CONTENT_CHARS]
    assert len(expanded["evidence"]) == EVIDENCE_CHARS
    assert expanded["truncated"]


def test_browse_counts_pagination_and_deterministic_sort(archive):
    path, add = archive
    for name, date in (("c", "2026-09-02"), ("b", "2026-09-01"), ("a", "2026-09-01")):
        add(name, "needle", date=date)
    reader = LearningArchive(path)
    assert [r["record_id"] for r in reader.lookup(limit=2)["results"]] == ["learning:lr_c", "learning:lr_a"]
    assert reader.lookup(limit=2)["has_more"] is True
    assert reader.lookup(limit=3)["has_more"] is False
    assert reader.lookup("needle", sort="oldest", limit=1)["results"][0]["record_id"] == "learning:lr_a"
    assert reader.lookup("needle", sort="newest", limit=1)["total"] == 1


def test_identifier_underscore_is_literal_and_ids_cannot_inject_sql(archive):
    path, add = archive
    expected = add("exact", "session_id is an identifier")
    add("wildcard", "sessionXid is not the same identifier")
    reader = LearningArchive(path)
    assert [r["record_id"] for r in reader.lookup("session_id")["results"]] == [expected]
    assert reader.lookup("%' OR 1=1 --")["total"] == 0
    with pytest.raises(ValueError, match="record_id"):
        reader.lookup(record_id="learning:lr_x' OR 1=1 --")
    with pytest.raises(ValueError, match="record_id"):
        reader.lookup(record_id="../../learning.db")
    assert reader.lookup("!!!")["query_status"] == "no_searchable_terms"


def test_missing_corrupt_and_locked_archives_are_distinct(tmp_path, archive):
    absent = tmp_path / "missing" / "archive.db"
    assert LearningArchive(absent).lookup()["status"] == "missing"
    assert not absent.parent.exists()
    broken = tmp_path / "corrupt.db"
    broken.write_text("corrupt archive")
    assert LearningArchive(broken).lookup()["status"] == "unavailable"
    path, _ = archive
    with sqlite3.connect(path) as db:
        db.execute("BEGIN EXCLUSIVE")
        started = time.monotonic()
        result = LearningArchive(path).lookup()
        assert result["status"] == "unavailable"
        assert time.monotonic() - started < 2
    assert LearningArchive(path).lookup()["status"] == "ok"


def test_explicit_paths_and_default_path_are_resolved_at_construction(archive, monkeypatch, tmp_path):
    path, add = archive
    add("present", "needle")
    reader = LearningArchive()
    monkeypatch.setenv("AGENT_LEARNING_PATH", str(tmp_path / "other.db"))
    assert reader.lookup()["total"] == 1
    assert LearningArchive().lookup()["status"] == "missing"
    assert LearningArchive(path).lookup()["total"] == 1


def test_read_connection_rejects_mutations(archive):
    path, add = archive
    add("kept", "needle")
    with pytest.raises(sqlite3.OperationalError):
        LearningArchive(path)._read("DELETE FROM learning_proposals", ())
    assert LearningArchive(path).lookup()["total"] == 1


def test_learning_only_and_expansion_do_not_open_session_archive(archive, monkeypatch):
    _, add = archive
    record_id = add("pip", "ComfyUI pip uv")
    monkeypatch.setattr(tool, "_get_sr", lambda: pytest.fail("must not open sessions"))
    result = json.loads(tool._search("pip", source_type="learning"))
    assert result["observations"]["total"] == 1
    # conversations were not queried
    assert "results" not in result and "returned" not in result and "total_matches" not in result
    assert json.loads(tool._search(record_id=record_id))["observations"]["total"] == 1
    assert not Path(os.environ["ASTRA_SESSION_RECALL_DB"]).exists()


def test_session_search_browse_scroll_and_filters_keep_existing_meaning(archive, monkeypatch, tmp_path):
    _, add = archive
    add("note", "needle observation")
    sr = SessionRecall(tmp_path / "sessions.db")
    sr.init_db()
    sid = sr.get_or_create_session("test-native", title="test", personality="test")
    mid = sr.log_message(sid, "user", "needle conversation")
    monkeypatch.setattr(tool, "_SR", sr)
    try:
        discovered = json.loads(tool._search("needle"))
        assert discovered["results"][0]["session_id"] == sid
        assert discovered["returned"] == discovered["total_matches"] == 1
        assert discovered["observations"]["total"] == 1
        assert json.loads(tool._search())["sessions"][0]["session_id"] == sid
        scrolled = json.loads(tool._search(session_id=sid, around_message_id=mid))
        assert scrolled["window"][0]["content"] == "needle conversation"
        assert "observations" not in scrolled
        for source in ("astra", "api", "hermes"):
            filtered = json.loads(tool._search("needle", source_type=source))
            assert "observations" not in filtered
            assert filtered["total_matches"] == (1 if source == "astra" else 0)
    finally:
        sr.close()


def test_more_observations_than_listed_make_the_result_partial(archive, monkeypatch):
    _, add = archive
    for index in range(4):
        add(f"note{index}", f"needle observation {index}")
    monkeypatch.setattr(tool, "_get_sr", lambda: pytest.fail("must not open sessions"))
    registry = ToolRegistry()
    tool.register_session_recall_tools(registry)

    def search(limit):
        result = asyncio.run(registry.execute(
            "session_search", {"query": "needle", "limit": limit, "source_type": "learning"},
        ))
        return result, json.loads(result["output"])

    result, page = search(2)
    assert page["observations"]["has_more"] is True and len(page["observations"]["results"]) == 2
    # A full page of a longer list used to be labelled a complete result.
    assert result["partial"] is True
    assert "has_more" in page["partial"] and "limit" in page["partial"]

    result, everything = search(4)
    assert everything["observations"]["has_more"] is False
    assert "partial" not in everything and not result.get("partial")


def test_one_archive_failure_does_not_mask_the_other(archive, monkeypatch):
    path, add = archive
    add("note", "needle")

    def broken():
        raise sqlite3.OperationalError("private database path")

    monkeypatch.setattr(tool, "_get_sr", broken)
    partial = json.loads(tool._search("needle"))
    assert partial["sessions_status"] == "unavailable"
    assert partial["observations"]["total"] == 1
    assert "private database path" not in str(partial)
    path.write_bytes(b"broken")

    class Recall:
        def search(self, *args, **kwargs):
            return [{"session_id": "usable"}]

    monkeypatch.setattr(tool, "_get_sr", Recall)
    partial = json.loads(tool._search("needle"))
    assert partial["results"] == [{"session_id": "usable"}]
    assert partial["observations"]["status"] == "unavailable"


@pytest.mark.parametrize("kwargs", [
    {"record_id": "learning:lr_x", "query": "query"},
    {"record_id": "learning:lr_x", "source_type": "astra"},
    {"session_id": "s"}, {"around_message_id": 4},
    {"session_id": "s", "around_message_id": 4, "source_type": "learning"},
    {"source_type": "invented"},
])
def test_ambiguous_modes_fail_before_io(monkeypatch, kwargs):
    monkeypatch.setattr(tool, "_get_sr", lambda: pytest.fail("must validate first"))
    with pytest.raises(ValueError):
        tool._search(**kwargs)


def test_tool_invites_relevant_proactive_retrieval():
    registry = ToolRegistry()
    tool.register_session_recall_tools(registry)
    definition = registry.get("session_search")
    assert definition.risk == "read"
    assert "Proactively search" in definition.description
    assert "verify current state" in definition.description
    assert "record_id" in definition.parameters["properties"]
