"""GUI reads follow one selected branch without recovering or rewriting sources."""

import json
import os
import sqlite3
from pathlib import Path
import pytest

from agent.cli import sessions
from agent.runtime.conversation_branches import BranchConflict, ConversationBranches
from agent.runtime.message_source import message_source_ref
from agent.runtime.session_store import SessionStore
from agent.ui import history_index, queries
from agent.ui.session_actions import act


@pytest.fixture
def branched(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    root.mkdir()
    monkeypatch.setattr(sessions, "SESSION_DIR", root)
    monkeypatch.setenv("AGENT_SESSION_DIR", str(root))
    monkeypatch.setenv("ASTRA_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("AGENT_MEMORY_PATH", str(tmp_path / "missing-memory.db"))
    monkeypatch.setenv("ASTRA_GUI_HISTORY_CACHE", str(tmp_path / "cache"))
    path = root / "demo.json"
    original = [{"role": "user", "content": "question"}, {"role": "assistant", "content": "original answer"}]
    SessionStore(path).save({"messages": original})
    versions = ConversationBranches(path)
    ticket = versions.prepare_retry(message_source_ref(original[1], 1), branch_id="main", expected_revision=0)
    alternate = ticket["seed"]
    alternate["messages"].append({"role": "assistant", "content": "alternate answer"})
    SessionStore(path, branch_id=ticket["candidate_branch_id"]).save(alternate)
    state = versions.finish_candidate(ticket["candidate_branch_id"])
    return path, versions, state


def _request(method, **params):
    result = queries.query({"method": method, "params": {"name": "demo", **params}})
    assert isinstance(result, dict)
    return result


def _select_original(versions):
    state = versions.state()
    group = state["groups"][0]
    original = next(version for version in group["versions"] if version["branch_id"] == "main")
    return versions.select_version(group["id"], original["id"], branch_id=state["branch_id"], expected_revision=state["revision"])


def _source_snapshot(path):
    return {str(file): (file.stat().st_mtime_ns, file.read_bytes())
            for file in SessionStore(path).related_paths() if file.is_file()}


def test_history_and_log_follow_selected_branch_and_keep_source_refs_local(branched):
    path, versions, state = branched
    alternate_id = state["branch_id"]
    page = _request("history", branch_id=alternate_id)
    assert page["branch_id"] == alternate_id
    assert [message["content"] for message in page["messages"]] == ["question", "alternate answer"]
    assert page["response_versions"]["choices"] == state["choices"]
    assert page["response_versions"]["groups"][0]["selected_version"] == state["groups"][0]["selected_version"]
    source = page["messages"][-1]["source_ref"]
    inspected = _request("session_log", branch_id=alternate_id, source_ref=source)
    assert inspected["branch_id"] == alternate_id and inspected["target_status"] == "found"
    assert [json.loads(record["raw"])["content"] for record in inspected["records"]] == ["question", "alternate answer"]
    assert inspected["revision"] == page["revision"]

    _select_original(versions)
    original = _request("history", branch_id="main")
    assert original["branch_id"] == "main" and original["messages"][-1]["content"] == "original answer"
    assert _request("session_log", branch_id="main", source_ref=source)["target_status"] == "stale"
    for method in ("history", "session_log"):
        with pytest.raises(BranchConflict, match="branch changed"):
            _request(method, branch_id=alternate_id)
    assert SessionStore(path).branch_id == "main"


@pytest.mark.parametrize("method", ["history", "session_log"])
@pytest.mark.parametrize("branch_id", ["../escape", "other", 1, True, [], {}])
def test_query_rejects_invalid_branch_identity_without_source_writes(branched, method, branch_id):
    path, _, _ = branched
    before = _source_snapshot(path)
    with pytest.raises(ValueError):
        _request(method, branch_id=branch_id)
    assert _source_snapshot(path) == before


def test_warm_branch_queries_never_load_or_recover_full_session(branched, monkeypatch):
    path, _, state = branched
    _request("history")
    _request("session_log")
    before = _source_snapshot(path)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("A warm branch query attempted full replay or lifecycle recovery")

    monkeypatch.setattr(SessionStore, "load", forbidden)
    monkeypatch.setattr(SessionStore, "recover_interrupted", forbidden)
    monkeypatch.setattr(ConversationBranches, "recover_interrupted", forbidden)
    monkeypatch.setattr(history_index, "_rebuild", forbidden)
    page = _request("history", limit=1, branch_id=state["branch_id"])
    assert page["messages"][-1]["content"] == "alternate answer"
    assert page["response_versions"]["groups"]
    assert _request("session_log", limit=1, branch_id=state["branch_id"])["target_status"] == "none"
    assert _source_snapshot(path) == before


@pytest.mark.parametrize("method", ["history", "session_log"])
def test_branch_selection_during_projection_refuses_stale_response(branched, monkeypatch, method):
    _, versions, state = branched
    rebuild = history_index._rebuild
    changed = []

    def switching(store, db):
        result = rebuild(store, db)
        if not changed:
            _select_original(versions)
            changed.append(True)
        return result

    monkeypatch.setattr(history_index, "_rebuild", switching)
    with pytest.raises(BranchConflict, match="branch changed"):
        _request(method, branch_id=state["branch_id"])
    assert changed
    assert _request("history", branch_id="main")["messages"][-1]["content"] == "original answer"


def test_compacted_history_keeps_existing_version_navigation_metadata(branched):
    path, _, state = branched
    selected = SessionStore(path)
    selected.save({"messages": [{"role": "user", "content": "compacted summary"}]}, snapshot=True)
    before = _source_snapshot(path)
    page = _request("history", branch_id=state["branch_id"])
    assert page["messages"][0]["content"] == "compacted summary"
    group = page["response_versions"]["groups"][0]
    assert group["selected_version"] == state["groups"][0]["selected_version"]
    assert len(group["versions"]) == 2
    assert "source_ref" not in group
    assert _source_snapshot(path) == before


def test_delete_clears_all_branch_files_and_projection_generations_only_for_target(branched):
    path, versions, state = branched
    selected = SessionStore(path)
    _request("history")
    selected_cache = history_index._cache_path(selected)
    _select_original(versions)
    _request("history")
    main_cache = history_index._cache_path(SessionStore(path))
    legacy_cache = history_index._source_cache_path(selected.legacy_path, version=1)
    legacy_cache.write_bytes(b"old private projection")
    orphan = versions.root / "heads" / ("f" * 32 + ".json")
    orphan.write_text('{"messages":[]}')
    orphan_cache = history_index._source_cache_path(orphan)
    orphan_cache.write_bytes(b"orphan private projection")
    other = SessionStore(path.with_name("other.json"))
    other.save({"messages": [{"role": "user", "content": "unrelated"}]})
    other_cache = history_index._cache_path(other)
    other_cache.write_bytes(b"unrelated private projection")
    related = selected.related_paths()

    assert act({"action": "delete", "name": "demo"})["deleted"]
    assert not any(file.exists() for file in related)
    assert not any(file.exists() for file in (main_cache, selected_cache, legacy_cache, orphan_cache))
    assert other.exists and other_cache.read_bytes() == b"unrelated private projection"
    assert not versions.manifest_path.exists()
    assert state["branch_id"] != "main"


def test_delete_refuses_branch_symlink_before_removing_any_owned_source(branched, tmp_path):
    path, versions, _ = branched
    victim = tmp_path / "unrelated.txt"
    victim.write_text("untouched")
    link = versions.root / "heads" / "unexpected-link.json"
    try:
        link.symlink_to(victim)
    except OSError:
        pytest.skip("symlinks are unavailable on this host")
    primary = path.with_suffix(".jsonl")
    before = primary.read_bytes()
    with pytest.raises(OSError, match="regular owned path"):
        act({"action": "delete", "name": "demo"})
    assert primary.read_bytes() == before
    assert versions.manifest_path.exists() and victim.read_text() == "untouched"


def test_cli_rename_refuses_branched_conversation_without_moving_sources(branched):
    path, _, _ = branched
    before = _source_snapshot(path)
    with pytest.raises(ValueError, match="reply versions.*cannot be renamed"):
        sessions.rename_session("demo", "renamed")
    assert _source_snapshot(path) == before
    assert not SessionStore(path.with_name("renamed.json")).exists


def test_cli_delete_clears_branch_sources_caches_and_only_owned_working_scopes(branched):
    from agent.runtime.conversation_branches import branch_scope

    path, _, state = branched
    _request("history")
    cache = history_index._cache_path(SessionStore(path))
    memory_path = Path(os.environ["AGENT_MEMORY_PATH"])
    identities = ["demo", branch_scope(path, state["branch_id"]), "unrelated"]
    with sqlite3.connect(memory_path) as db:
        db.execute("CREATE TABLE working_memories (session_id TEXT PRIMARY KEY, data_json TEXT)")
        db.executemany("INSERT INTO working_memories VALUES (?, '{}')", [(identity,) for identity in identities])
    related = SessionStore(path).related_paths()
    sessions.delete_session("demo")
    assert not any(file.exists() for file in related) and not cache.exists()
    with sqlite3.connect(memory_path) as db:
        assert db.execute("SELECT session_id FROM working_memories").fetchall() == [("unrelated",)]


def test_cli_rename_without_versions_preserves_legacy_worker_journals(tmp_path, monkeypatch):
    monkeypatch.setattr(sessions, "SESSION_DIR", tmp_path)
    source = SessionStore(tmp_path / "before.json")
    source.save({"messages": [{"role": "user", "content": "unchanged"}]})
    worker = SessionStore.for_subagent(source.logical_path, "worker")
    worker.save({"messages": [{"role": "assistant", "content": "worker history"}]})
    sessions.rename_session("before", "after")
    assert not source.exists and not worker.exists
    assert SessionStore(tmp_path / "after.json").load(readonly=True)["messages"][0]["content"] == "unchanged"
    assert SessionStore.for_subagent(tmp_path / "after.json", "worker").load(readonly=True)["messages"][0]["content"] == "worker history"
