"""Recover complete conversation records without rewriting damaged source bytes."""

import asyncio
import json

import pytest

from agent.runtime.context import AgentContext
from agent.runtime.conversation_branches import ConversationBranches
from agent.runtime.message_source import message_source_ref
from agent.runtime.session_store import SessionStore


def saved_conversation(tmp_path, branched):
    path = tmp_path / "conversation.json"
    data = {"messages": [{"role": "user", "content": "original request"},
                         {"role": "assistant", "content": "original answer"}]}
    store = SessionStore(path)
    store.save(data)
    if branched:
        branches = ConversationBranches(path)
        ticket = branches.prepare_retry(message_source_ref(data["messages"][-1], 1),
                                        branch_id="main", expected_revision=0)
        data = ticket["seed"]
        data["messages"].append({"role": "assistant", "content": "alternate answer"})
        store = SessionStore(path, branch_id=ticket["candidate_branch_id"])
        store.save(data)
        branches.finish_candidate(store.branch_id)
    return store, data


@pytest.mark.parametrize("branched", [False, True])
@pytest.mark.parametrize("tail", [b'{"type":"message","index":2',
                                  b'{"type":"message","message":{"content":"\xe4\xb8'])
def test_damaged_tail_loads_readonly_then_recovers_and_saves_without_swallowing_records(tmp_path, monkeypatch, branched, tail):
    store, data = saved_conversation(tmp_path, branched)
    store.record_end("fixture")
    dead_start = {"type": "session_started", "run_id": "dead-owner", "pid": 999999999}
    with store.jsonl_path.open("ab") as handle:
        handle.write(json.dumps(dead_start).encode() + b"\n" + tail)
    damaged = store.jsonl_path.read_bytes()
    sources = {file: (file.read_bytes(), file.stat().st_mtime_ns)
               for file in store.related_paths() if file.is_file()}
    monkeypatch.setattr(SessionStore, "_process_alive", staticmethod(lambda pid: pid != 999999999))

    reader = SessionStore(store.logical_path)
    assert reader.load(readonly=True)["messages"] == data["messages"]
    assert {file: (file.read_bytes(), file.stat().st_mtime_ns) for file in sources} == sources

    resumed = AgentContext()
    resumed.set_session(str(store.logical_path))
    assert resumed.load()
    assert resumed.messages == data["messages"]
    resumed.add_user("request after recovery")
    resumed.add_assistant("answer after recovery")
    asyncio.run(resumed.save_async())
    resumed.record_session_end("shutdown")

    after = SessionStore(store.logical_path)
    assert after.branch_id == store.branch_id
    assert after.load()["messages"] == resumed.messages
    events = after.lifecycle_events()
    assert [event["type"] for event in events[-4:]] == [
        "session_started", "session_interrupted", "session_started", "session_ended",
    ]
    assert events[-3]["run_id"] == "dead-owner"
    assert after.jsonl_path.read_bytes().startswith(damaged + b"\n")


@pytest.mark.parametrize("tail", [b'{"type":"message","index":2', b'\xe4\xb8'])
def test_existing_writer_preserves_new_message_after_damaged_tail(tmp_path, tail):
    store, data = saved_conversation(tmp_path, False)
    with store.jsonl_path.open("ab") as handle:
        handle.write(tail)
    before = store.jsonl_path.read_bytes()
    data["messages"].append({"role": "user", "content": "new complete message"})
    store.save(data, append_from=2)
    assert SessionStore(store.logical_path).load(readonly=True)["messages"] == data["messages"]
    assert store.jsonl_path.read_bytes().startswith(before + b"\n")


def test_complete_final_record_without_newline_is_preserved_before_append(tmp_path):
    store, data = saved_conversation(tmp_path, False)
    store.jsonl_path.write_bytes(store.jsonl_path.read_bytes().rstrip(b"\n"))
    before = store.jsonl_path.read_bytes()
    data["messages"].append({"role": "user", "content": "new complete message"})
    store.save(data, append_from=2)
    assert SessionStore(store.logical_path).load()["messages"] == data["messages"]
    assert store.jsonl_path.read_bytes().startswith(before + b"\n")


def test_cleared_guidance_does_not_reappear_from_old_snapshot(tmp_path):
    store = SessionStore(tmp_path / "guidance.json")
    data = {"messages": [{"role": "user", "content": "hello"}],
            "guidance_snapshot": {"version": 1, "catalog": "old guidance"}}
    store.save(data)
    data.pop("guidance_snapshot")
    store.save(data, append_from=1)
    assert SessionStore(store.logical_path).load(readonly=True).get("guidance_snapshot") == {}
