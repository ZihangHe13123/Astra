"""Content-verified navigation reads bounded canonical records without resuming."""
import json
import sqlite3

import pytest

from agent.runtime.session_store import SessionStore
from agent.ui import history_index, queries
from agent.ui.session_log import MAX_RECORD_BYTES, message_source_ref


@pytest.fixture
def store(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    root.mkdir()
    monkeypatch.setattr(queries.sessions, "SESSION_DIR", root)
    monkeypatch.setenv("ASTRA_GUI_HISTORY_CACHE", str(tmp_path / "cache"))
    return SessionStore(root / "demo.json")


def write(store, messages):
    store.legacy_path.write_text(json.dumps({"messages": messages}, ensure_ascii=False))


def message(text, role="user", **extra):
    return {"role": role, "content": text, **extra}


def log(**params):
    return queries.query({"method": "session_log", "params": {"name": "demo", **params}})


def test_bidirectional_navigation_uses_source_ref_and_same_visible_position(store):
    messages = [message("hidden", provenance="session_wakeup"), message("hidden answer", "assistant"),
                message("visible"), message("answer", "assistant")]
    write(store, messages)
    page = queries.history("demo")
    assert [m["id"] for m in page["messages"]] == ["work:demo:0", "work:demo:1"]
    selected = page["messages"][1]["source_ref"]
    assert selected == message_source_ref(messages[3], 3)
    inspected = log(source_ref=selected)
    assert inspected["target_status"] == "found"
    record = next(r for r in inspected["records"] if r["source_ref"] == selected)
    assert record["chat_position"] == 1
    assert json.loads(record["raw"]) == messages[3]
    returned = queries.history("demo", source_ref=record["chat_source_ref"])
    assert returned["target_id"] == "work:demo:1"
    assert returned["target_status"] == "found"
    assert returned["revision"] == inspected["revision"]
    assert log(source_ref=message_source_ref(messages[0], 0))["records"][0]["chat_source_ref"] is None


def test_source_shift_is_resolved_only_when_content_is_unique_and_rewrite_is_stale(store):
    first, selected = message("first"), message("selected", "assistant")
    write(store, [first, selected])
    reference = queries.history("demo")["messages"][1]["source_ref"]
    write(store, [selected])
    assert log(source_ref=reference)["target_index"] == 0
    assert queries.history("demo", source_ref=reference)["target_id"] == "work:demo:0"
    write(store, [message("different")])
    assert log(source_ref=reference)["target_status"] == "stale"
    assert log(source_ref=reference)["records"] == []
    assert queries.history("demo", source_ref=reference)["messages"] == []
    write(store, [selected, message("different"), selected])
    assert log(source_ref=reference)["target_status"] == "ambiguous"


def test_tool_call_and_result_link_to_invocation_but_duplicate_call_ids_do_not_guess(store):
    invocation = message("Inspecting", "assistant", tool_calls=[{"id": "call", "function": {"name": "read_file", "arguments": "{}"}}])
    result = message("saved result", "tool", tool_call_id="call")
    write(store, [message("go"), invocation, result])
    selected = log(call_id="call")
    assert selected["target_status"] == "found"
    assert selected["target_index"] == 1
    assert selected["records"][2]["chat_source_ref"] == message_source_ref(invocation, 1)
    page = queries.history("demo", source_ref=message_source_ref(result, 2))
    assert page["target_id"] == "work:demo:1"
    write(store, [invocation, result, invocation, result])
    assert log(call_id="call")["target_status"] == "ambiguous"
    assert log(call_id="call")["records"] == []
    assert log()["records"][-1]["chat_source_ref"] is None
    assert queries.history("demo", source_ref=message_source_ref(result, 3))["target_status"] == "unmapped"


def test_log_reads_are_byte_bounded_read_only_and_warm_reads_do_not_reload_sources(store, monkeypatch):
    write(store, [message("中" * 10000) for _ in range(100)])
    original = store.legacy_path.read_bytes(), store.legacy_path.stat().st_mtime_ns
    inspected = log(limit=50)
    assert len(inspected["records"]) == 50
    assert inspected["has_more"] and inspected["before"] == 50
    for record in inspected["records"]:
        assert len(record["raw"].encode()) <= MAX_RECORD_BYTES
        assert record["raw_truncated"]
    def forbidden(*args, **kwargs):
        raise AssertionError("Inspection must not replay or recover a live session")
    monkeypatch.setattr(SessionStore, "load", forbidden)
    monkeypatch.setattr(SessionStore, "recover_interrupted", forbidden)
    monkeypatch.setattr(history_index, "_rebuild", forbidden)
    assert log(before=50, limit=10)["before"] == 40
    assert original == (store.legacy_path.read_bytes(), store.legacy_path.stat().st_mtime_ns)


def test_log_effective_replace_tail_and_mode_isolation(store):
    write(store, [message("obsolete")])
    replacement = [message("effective")]
    store.jsonl_path.write_text(json.dumps({"type": "replace", "messages": replacement}) + '\n{"type":"message"')
    assert json.loads(log()["records"][0]["raw"])["content"] == "effective"
    bar = queries._session_path("demo", "bar")
    bar.parent.mkdir()
    bar.write_text(json.dumps({"messages": [message("bar only")]}))
    assert "bar only" not in json.dumps(log())
    assert "effective" not in json.dumps(log(mode="bar"))


@pytest.mark.parametrize("params", [
    {"name": "../escape"}, {"mode": "bad"}, {"limit": 0}, {"limit": 51}, {"limit": True},
    {"before": -1}, {"before": True}, {"call_id": ""}, {"call_id": 1},
    {"source_ref": {"index": 0, "digest": "bad"}},
    {"source_ref": {"index": True, "digest": "a" * 64}},
    {"source_ref": {"index": 0, "digest": "a" * 64}, "call_id": "c"},
])
def test_log_validates_request_boundaries(store, params):
    write(store, [])
    with pytest.raises(ValueError):
        log(**params)


def test_missing_session_does_not_create_cache(store):
    with pytest.raises(FileNotFoundError):
        log()
    assert not history_index._cache_path(store).exists()


def test_deleting_session_removes_raw_projection_too(store):
    from agent.ui.session_actions import act
    write(store, [message("private raw data", "tool")])
    assert log()["records"]
    cache = history_index._cache_path(store)
    assert cache.exists()
    assert act({"action": "delete", "name": "demo"})["deleted"]
    assert not cache.exists()


def test_delete_removes_legacy_and_current_cache_only_for_selected_session(store):
    from agent.ui.session_actions import act
    write(store, [message("selected private messages")])
    log()
    legacy = history_index._cache_path(store, version=1)
    with sqlite3.connect(legacy) as db:
        db.execute("CREATE TABLE messages (position INTEGER PRIMARY KEY, payload TEXT NOT NULL)")
        db.execute("INSERT INTO messages VALUES (0, 'legacy private messages')")
    other = SessionStore(store.legacy_path.with_name("other.json"))
    other_paths = [history_index._cache_path(other, version=version) for version in (1, 2)]
    for path in other_paths:
        path.write_bytes(b"other session cache must remain untouched")
    assert act({"action": "delete", "name": "demo"})["deleted"]
    assert not legacy.exists()
    assert not history_index._cache_path(store).exists()
    for path in other_paths:
        assert path.read_bytes() == b"other session cache must remain untouched"
