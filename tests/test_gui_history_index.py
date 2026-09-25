"""History pages agree with SessionStore replay without full-memory warm reads."""

import json
import io
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from agent.cli import sessions
from agent.cli.images import message_display_text
from agent.runtime.session_store import SessionStore
from agent.runtime.session_wakeup import visible_wakeup_history
from agent.ui import history_index, queries

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("chunk", range(1, 15))
def test_numeric_fraction_and_exponent_chunk_boundaries(monkeypatch, chunk):
    monkeypatch.setattr(history_index, "_CHUNK", chunk)
    text = '{"a":1.25,"b":-1.23e+20,"c":1e-12,"d":true,"messages":[]}'
    parsed = history_index._JSONStream(io.StringIO(text)).object(lambda: None, lambda _: None)
    assert parsed == {**json.loads(text), "messages": True}


@pytest.fixture
def store(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    root.mkdir()
    monkeypatch.setattr(sessions, "SESSION_DIR", root)
    monkeypatch.setenv("ASTRA_GUI_HISTORY_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("AGENT_SESSION_DIR", str(root))
    monkeypatch.setenv("ASTRA_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("ASTRA_ENV_FILE", str(tmp_path / "missing.env"))
    return SessionStore(root / "demo.json")


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _events(store, events):
    store.jsonl_path.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n", encoding="utf-8")


def _message(text, role="user", **kwargs):
    return {"role": role, "content": text, **kwargs}


def _oracle(store, before=None, limit=200):
    messages = visible_wakeup_history(store.load(readonly=True)["messages"])
    end = max(0, min(len(messages), before if before is not None else len(messages)))
    start = max(0, end - min(max(1, limit), 1000))
    visible = [{"id": f"work:demo:{i}", "role": m["role"],
                "content": message_display_text(m.get("display_command", m.get("content", ""))),
                "timestamp": m.get("timestamp")}
               for i, m in enumerate(messages) if start <= i < end
               and m.get("role") in ("user", "assistant")
               and m.get("_meta", {}).get("type") != "reasoning_context"]
    return {"session_id": "demo", "mode": "work", "messages": visible,
            "before": start, "has_more": start > 0, "total": len(messages)}


def _same(store, **params):
    expected = _oracle(store, **params)
    actual = queries.history("demo", **params)
    assert actual.pop("revision")
    assert actual.pop("delegates") == []
    assert actual == expected
    return actual


def test_legacy_streaming_and_cursor_match_with_tiny_chunk_boundaries(store, monkeypatch):
    monkeypatch.setattr(history_index, "_CHUNK", 7)
    messages = [_message(f"中文 {i}\\\"\n", timestamp=123456.789) for i in range(125)]
    messages += [_message("hidden request", provenance="session_wakeup"), _message("hidden reply", "assistant"),
                 _message("notice", "assistant", provenance="wakeup_notification"),
                 _message("visible again", display_command="/retry"), _message("tool", "tool"),
                 _message("reasoning", "assistant", _meta={"type": "reasoning_context"}),
                 _message([{"type": "text", "text": "rich text"}], "assistant")]
    _write(store.legacy_path, {"system_prompt": "not messages", "messages": messages, "trailer": 123456})
    for before, limit in ((None, 10), (125, 7), (5, 200), (0, 200), (9999, 1000)):
        _same(store, before=before, limit=limit)


def test_snapshot_precedence_jsonl_replay_replace_and_header_override(store):
    _write(store.legacy_path, {"messages": [_message("obsolete legacy")]})
    _write(store.snapshot_path, {"messages": [_message("snapshot")]})
    _events(store, [
        {"type": "message", "index": 0, "message": _message("duplicate")},
        {"type": "message", "index": 300, "message": _message("gap appends")},
        {"messages": [_message("new"), _message("reply", "assistant")], "type": "replace"},
        {"type": "state", "state": {"messages": [_message("ignored state messages")]}},
        {"type": "message", "index": "noninteger", "message": _message("last")},
    ])
    _same(store)
    _write(store.header_path, {"messages": [_message("header wins")]})
    assert _same(store)["messages"][0]["content"] == "header wins"


def test_append_compaction_shrink_and_same_size_rewrite_invalidate_cache(store):
    _write(store.legacy_path, {"messages": [_message("first")]})
    _same(store)
    _events(store, [{"type": "message", "index": 1, "message": _message("added")}])
    _same(store)
    old = store.jsonl_path.stat()
    store.jsonl_path.write_text(store.jsonl_path.read_text().replace("added", "other"))
    os.utime(store.jsonl_path, ns=(old.st_atime_ns, old.st_mtime_ns))
    assert _same(store)["messages"][-1]["content"] == "other"
    _write(store.snapshot_path, {"messages": [_message("compacted")]})
    store.jsonl_path.write_text("")
    _same(store)
    _events(store, [{"type": "replace", "messages": []}])
    assert _same(store)["total"] == 0
    store.snapshot_path.unlink()
    store.jsonl_path.unlink()
    assert _same(store)["messages"][0]["content"] == "first"


def test_real_session_store_save_and_snapshot_are_compatible(store):
    data = {"messages": [_message("first"), _message("reply", "assistant")]}
    store.save(data)
    _same(store)
    data["messages"].append(_message("appended"))
    store.save(data, append_from=2)
    _same(store)
    data["messages"] = [_message("rewrite")]
    store.save(data)
    _same(store)
    store.save(data, snapshot=True)
    _same(store)


def test_invalid_source_and_partial_jsonl_record_never_publish_partial_replacement(store):
    _write(store.legacy_path, {"messages": [_message("legacy")]})
    store.snapshot_path.write_text('{"messages":[{"role":"user","content":"half"}],broken}')
    store.jsonl_path.write_text('garbage\n' + json.dumps({"type": "message", "index": 0, "message": _message("valid")})
                               + '\n{"type":"replace","messages":[{"role":"user","content":"half"}')
    assert _same(store)["messages"][0]["content"] == "valid"
    with store.jsonl_path.open("a") as handle:
        handle.write("]}\n")
    assert _same(store)["messages"][0]["content"] == "half"


def test_duplicate_message_keys_match_json_last_key_semantics(store):
    store.legacy_path.write_text('{"messages":[{"role":"user","content":"old"}],'
                                 '"messages":[{"role":"user","content":"new"}]}')
    assert _same(store)["messages"][0]["content"] == "new"


def test_warm_pages_do_not_read_or_hash_source_and_never_recover_lifecycle(store, monkeypatch):
    _write(store.legacy_path, {"messages": [_message(str(i)) for i in range(1500)]})
    before = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in store.related_paths() if p.exists()}
    _same(store, limit=200)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Warm history attempted complete source replay")

    monkeypatch.setattr(SessionStore, "load", forbidden)
    monkeypatch.setattr(SessionStore, "recover_interrupted", forbidden)
    monkeypatch.setattr(history_index, "_rebuild", forbidden)
    page = queries.history("demo", before=1300, limit=200)
    assert page["messages"][0]["content"] == "1100"
    assert page["messages"][-1]["content"] == "1299"
    assert before == {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in store.related_paths() if p.exists()}


def test_source_mutation_during_build_retries_before_returning(store, monkeypatch):
    _write(store.legacy_path, {"messages": [_message("before")]})
    original = history_index._rebuild
    called = []

    def changing(current, db):
        result = original(current, db)
        if not called:
            _write(store.legacy_path, {"messages": [_message("after")]})
        called.append(True)
        return result

    monkeypatch.setattr(history_index, "_rebuild", changing)
    result = queries.history("demo")
    assert len(called) == 2
    assert result["messages"][0]["content"] == "after"


def test_deleted_session_never_returns_cached_history(store):
    _write(store.legacy_path, {"messages": [_message("deleted")]})
    _same(store)
    store.legacy_path.unlink()
    with pytest.raises(FileNotFoundError):
        queries.history("demo")


def test_corrupt_cache_is_rebuilt_and_session_delete_removes_private_projection(store):
    from agent.ui.session_actions import act

    _write(store.legacy_path, {"messages": [_message("private cached message")]})
    _same(store)
    cache = history_index._cache_path(store)
    assert cache.exists()
    cache.write_bytes(b"interrupted cache file")
    _same(store)
    assert act({"action": "delete", "name": "demo"})["deleted"]
    assert not cache.exists()
    assert not store.exists


def test_corrupt_message_leaf_page_rebuilds_cache_without_touching_source(store):
    _write(store.legacy_path, {"messages": [_message("x" * 1000 + str(i)) for i in range(50)]})
    _same(store)
    source = store.legacy_path.read_bytes()
    cache = history_index._cache_path(store)
    with sqlite3.connect(cache) as db:
        size = db.execute("PRAGMA page_size").fetchone()[0]
        page = db.execute("SELECT rootpage FROM sqlite_master WHERE name='messages'").fetchone()[0]
    with cache.open("r+b") as handle:
        # Follow the rightmost table b-tree child to a data leaf. Using the
        # on-disk header avoids requiring SQLite's optional dbstat extension.
        while True:
            handle.seek((page - 1) * size)
            header = handle.read(12)
            if header[0] != 0x05:
                assert header[0] == 0x0d
                break
            page = int.from_bytes(header[8:12], "big")
        handle.seek((page - 1) * size)
        handle.write(b"\xff")
    with sqlite3.connect(cache) as db:
        assert db.execute("SELECT total FROM metadata").fetchone()[0] == 50
        with pytest.raises(sqlite3.DatabaseError, match="malformed"):
            db.execute("SELECT payload FROM messages ORDER BY position DESC").fetchall()
    _same(store)
    assert store.legacy_path.read_bytes() == source


@pytest.mark.parametrize("code", [sqlite3.SQLITE_BUSY, sqlite3.SQLITE_READONLY])
def test_locked_or_readonly_cache_is_not_discarded(store, monkeypatch, code):
    _write(store.legacy_path, {"messages": [_message("retained")]})
    _same(store)
    cache = history_index._cache_path(store)
    original = cache.read_bytes()
    calls = []

    def fail(*args, **kwargs):
        calls.append(True)
        error = sqlite3.OperationalError("locked or readonly")
        error.sqlite_errorcode = code
        raise error

    monkeypatch.setattr(history_index, "_read_page", fail)
    with pytest.raises(sqlite3.OperationalError, match="locked or readonly"):
        queries.history("demo")
    assert len(calls) == 1
    assert cache.read_bytes() == original


def test_corrupt_cache_recovery_retries_only_once(store, monkeypatch):
    _write(store.legacy_path, {"messages": [_message("retained")]})
    _same(store)
    calls = []

    def corrupt(*args, **kwargs):
        calls.append(True)
        error = sqlite3.DatabaseError("broken cache")
        error.sqlite_errorcode = sqlite3.SQLITE_CORRUPT
        raise error

    monkeypatch.setattr(history_index, "_read_page", corrupt)
    with pytest.raises(sqlite3.DatabaseError, match="broken cache"):
        queries.history("demo")
    assert len(calls) == 2


def test_query_worker_handles_multiple_requests_and_exits_at_eof(store):
    _write(store.legacy_path, {"messages": [_message("hello")]})
    requests = [{"id": "one", "method": "history", "params": {"name": "demo"}},
                {"id": "bad", "method": "missing"}, {"id": "two", "method": "commands"}]
    result = subprocess.run([sys.executable, "-m", "agent.ui.queries", "--serve"],
                            cwd=ROOT, env=os.environ.copy(), text=True, capture_output=True,
                            input="\n".join(map(json.dumps, requests)) + "\n", timeout=15, check=True)
    responses = [json.loads(line) for line in result.stdout.splitlines()]
    assert [r["id"] for r in responses] == ["one", "bad", "two"]
    assert responses[0]["result"]["messages"][0]["content"] == "hello"
    assert responses[1]["ok"] is False
    assert len(responses[2]["result"]) == 51


def test_history_includes_only_the_selected_sessions_public_delegate_reports(store):
    _write(store.legacy_path, {"messages": [_message("hello")]})
    store.append_subagent_event({"type": "started", "process_id": "child", "goal": "Inspect", "recorded_at": 10})
    store.append_subagent_event({"type": "assistant", "process_id": "child", "content": "not a final report", "recorded_at": 11})
    store.append_subagent_event({"type": "terminal", "process_id": "child", "status": "completed", "result": "Final report", "recorded_at": 12})
    before = store.subagent_path.read_bytes()
    result = queries.history("demo")
    assert result["messages"][0]["content"] == "hello"
    assert result["delegates"][0]["result"] == "Final report"
    assert result["delegates"][0]["status"] == "completed"
    assert result["delegates"][0]["session_id"] == "demo"
    assert store.subagent_path.read_bytes() == before
