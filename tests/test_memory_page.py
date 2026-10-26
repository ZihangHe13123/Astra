"""The desktop's memory page: what it shows, and the person's own changes, through the same stores as the agent."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from agent.runtime.memory import MemoryStore
from agent.ui.memory_page import apply, memory_overview
from agent.ui.queries import query

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AGENT_MEMORY_PATH", str(tmp_path / "home" / "memory.db"))
    return tmp_path / "home"


def test_an_empty_home_shows_empty_memory_and_creates_nothing(home):
    view = memory_overview()
    assert [file["file"] for file in view["core"]] == ["USER.md", "MEMORY.md"]
    assert view["saved"] == [] and view["proposals"] == []
    assert not home.exists()


def test_overview_shows_core_room_saved_memories_their_use_and_history(home):
    store = MemoryStore()
    store.add_core("user", "称呼：小林")
    record, _ = store.remember(kind="preference", summary="喜欢简短的回答", content="回答尽量简短，先说结论。")
    old, _ = store.remember(kind="user_fact", summary="住在新加坡", content="住在新加坡。")
    store.revise(old.record_id, content="搬到了吉隆坡。", summary="住在吉隆坡")
    store.record_store.note_use([record.record_id], read=True)
    view = query({"method": "memory"})
    user = view["core"][0]
    assert user["entries"][0]["content"] == "称呼：小林" and user["usage"]["limit"] == 1375
    by_summary = {item["summary"]: item for item in view["saved"]}
    assert set(by_summary) == {"喜欢简短的回答", "住在吉隆坡"}
    assert by_summary["喜欢简短的回答"]["uses"] == 1 and by_summary["喜欢简短的回答"]["saved_by_astra"]
    history = {item["summary"]: item["status"] for item in query({"method": "memory", "params": {"history": True}})["saved"]}
    assert history["住在新加坡"] == "superseded" and history["住在吉隆坡"] == "active"


def test_the_persons_changes_reach_the_stores_the_agent_reads(home):
    store = MemoryStore()
    apply("core-add", ["user", "时区：Asia/Singapore"])
    [entry] = store.list_core("user")
    apply("core-edit", [entry["id"], entry["content"], "时区：Asia/Kuala_Lumpur"])
    assert [item["content"] for item in store.list_core("user")] == ["时区：Asia/Kuala_Lumpur"]
    with pytest.raises(ValueError, match="changed"):
        apply("core-edit", [entry["id"], entry["content"], "stale edit"])
    apply("core-remove", [store.list_core("user")[0]["id"]])
    assert store.list_core("user") == []
    record, _ = store.remember(kind="user_fact", summary="养了一只猫", content="养了一只猫，叫团子。")
    apply("correct", [record.record_id, "养了一只猫，叫豆子。"])
    current = [item for item in memory_overview()["saved"]]
    assert [item["content"] for item in current] == ["养了一只猫，叫豆子。"]
    apply("forget", [current[0]["id"]])
    assert memory_overview()["saved"] == []
    assert {item["status"] for item in memory_overview(history=True)["saved"]} == {"superseded", "forgotten"}
    with pytest.raises(ValueError):
        apply("core-add", ["user", "x" * 2000])
    with pytest.raises(ValueError):
        apply("drop-table", [])


def test_proposals_are_listed_and_decided_from_the_page(home):
    store = MemoryStore()
    ids = store.record_store.add_proposals([
        ({"action": "remember", "kind": "preference", "summary": "早上不喝咖啡", "content": "早上不喝咖啡。",
          "scope": "global", "reason": "said so"}, [{"date": "2026-10-25", "text": "我早上不喝咖啡"}]),
        ({"action": "remember", "kind": "preference", "summary": "喜欢跑步", "content": "喜欢跑步。",
          "scope": "global", "reason": "said so"}, [{"date": "2026-10-25", "text": "我喜欢跑步"}]),
    ], model="fixture")
    [first, second] = memory_overview()["proposals"]
    assert first["summary"] == "早上不喝咖啡" and first["evidence"] == [{"date": "2026-10-25", "text": "我早上不喝咖啡"}]
    apply("accept", [str(ids[0])])
    apply("dismiss", [str(ids[1])])
    assert memory_overview()["proposals"] == []
    assert [item["summary"] for item in memory_overview()["saved"]] == ["早上不喝咖啡"]


def test_the_command_line_reports_each_change_as_json(home, tmp_path):
    env = {"ASTRA_HOME": str(home), "AGENT_MEMORY_PATH": str(home / "memory.db"), "PATH": "/usr/bin:/bin",
           "PYTHONPATH": str(ROOT)}
    ok = subprocess.run([sys.executable, "-m", "agent.ui.memory_page", "core-add", "memory", "Astra 在 Mac 上运行"],
                        cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    assert ok.returncode == 0 and json.loads(ok.stdout.strip().splitlines()[-1])["ok"]
    bad = subprocess.run([sys.executable, "-m", "agent.ui.memory_page", "forget", "nope"],
                         cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    assert bad.returncode == 1 and json.loads(bad.stdout.strip().splitlines()[-1])["ok"] is False
