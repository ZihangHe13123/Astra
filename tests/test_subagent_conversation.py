"""Durable canonical subagent history, separate from audit transcripts."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from agent.runtime.instance_lock import InstanceAlreadyRunning
from agent.runtime.session_store import SessionStore
from agent.runtime.subagent_conversation import ConversationCorrupt, SubagentConversation


def conversation(tmp_path, key="agent_123"):
    return SubagentConversation(SessionStore.for_subagent(tmp_path / "session.json", key))


def message(content):
    return {"role": "user", "content": content}


def test_factory_isolated_paths_and_safe_identity(tmp_path):
    first = SessionStore.for_subagent(tmp_path / "first.json", "agent_1")
    second = SessionStore.for_subagent(tmp_path / "second.json", "agent_1")
    assert first.jsonl_path != second.jsonl_path
    assert first.jsonl_path == tmp_path / ".artifacts" / "first.agent_1.conv.jsonl"
    for key in ("", "../escape", "a/b", "a\\b", ".", "a.json", "a\n", "x" * 129):
        with pytest.raises(ValueError):
            SessionStore.for_subagent(tmp_path / "session.json", key)


def test_load_save_require_lease_and_empty_is_valid(tmp_path):
    conv = conversation(tmp_path)
    with pytest.raises(RuntimeError, match="lease"):
        conv.load()
    with pytest.raises(RuntimeError, match="lease"):
        conv.save([], {})
    conv.acquire()
    try:
        assert conv.load() == {"messages": [], "state": {}}
    finally:
        conv.close()


def test_checkpoint_appends_only_changed_suffix_and_metadata_atomically(tmp_path):
    conv = conversation(tmp_path)
    conv.acquire()
    try:
        messages = [message("one")]
        state = {"turns_used": 1, "pending": [{"id": "tool1"}]}
        conv.save(messages, state)
        messages[0]["content"] = "mutated outside checkpoint"
        state["pending"][0]["id"] = "mutated"
        assert conv.load() == {"messages": [message("one")], "state": {"turns_used": 1, "pending": [{"id": "tool1"}]}}
        loaded = conv.load()
        loaded["messages"][0]["content"] = "caller mutation"
        conv.save([message("one"), message("two")], {"turns_used": 2})
        events = [json.loads(line) for line in conv.store.jsonl_path.read_text().splitlines()]
        assert [event["index"] for event in events] == [0, 1]
        assert events[-1]["messages"] == [message("two")]
        assert events[-1]["state"] == {"turns_used": 2}
        assert all(event["type"] == "subagent_checkpoint" and event["version"] == 1 for event in events)
        assert not conv.store.snapshot_path.exists()
        assert not conv.store.header_path.exists()
    finally:
        conv.close()
    reader = SubagentConversation(conv.store)
    reader.acquire()
    try:
        assert reader.load() == {"messages": [message("one"), message("two")], "state": {"turns_used": 2}}
        ordinary = reader.store.load(readonly=True)
        assert ordinary["messages"] == [message("one"), message("two")]
        assert ordinary["subagent_state"] == {"turns_used": 2}
    finally:
        reader.close()


def test_rewrite_shrink_and_metadata_only_checkpoint(tmp_path):
    conv = conversation(tmp_path)
    conv.acquire()
    try:
        conv.save([message("one"), message("two")], {"n": 1})
        conv.save([message("one"), message("replacement")], {"n": 2})
        conv.save([message("one")], {"n": 3})
        conv.save([message("one")], {"n": 4})
        conv.save([], {"n": 5})
        records = [json.loads(line) for line in conv.store.jsonl_path.read_text().splitlines()]
        assert [record["index"] for record in records] == [0, 1, 1, 1, 0]
        assert [record["messages"] for record in records[2:]] == [[], [], []]
        assert conv.store.load(readonly=True)["messages"] == []
        assert conv.store.load(readonly=True)["subagent_state"] == {"n": 5}
    finally:
        conv.close()


@pytest.mark.parametrize("tail", [b'{"type":"subagent_check', b'\xff'])
def test_torn_tail_repaired_before_next_append(tmp_path, tail):
    conv = conversation(tmp_path)
    conv.acquire()
    conv.save([message("committed")], {"cursor": 1})
    conv.close()
    with conv.store.jsonl_path.open("ab") as handle:
        handle.write(tail)
    assert conv.store.load(readonly=True)["messages"] == [message("committed")]
    conv.acquire()
    try:
        assert conv.load() == {"messages": [message("committed")], "state": {"cursor": 1}}
        conv.save([message("committed"), message("after crash")], {"cursor": 2})
        records = [json.loads(line) for line in conv.store.jsonl_path.read_text().splitlines()]
        assert len(records) == 2
        assert conv.store.load(readonly=True)["subagent_state"] == {"cursor": 2}
    finally:
        conv.close()


def test_complete_final_record_without_newline_can_continue(tmp_path):
    conv = conversation(tmp_path)
    conv.acquire()
    conv.save([message("one")], {"n": 1})
    conv.close()
    path = conv.store.jsonl_path
    path.write_bytes(path.read_bytes().rstrip(b"\n"))
    conv.acquire()
    try:
        assert conv.load()["state"] == {"n": 1}
        conv.save([message("one"), message("two")], {"n": 2})
        assert len(path.read_text().splitlines()) == 2
    finally:
        conv.close()


@pytest.mark.parametrize("bad", [
    b'not json\n', b'{}\n', b'[]\n', b'\n', b'\xff\n', b'{}',
    b'{"type":"subagent_checkpoint","version":2,"index":0,"messages":[],"state":{}}\n',
    b'{"type":"subagent_checkpoint","version":1,"index":2,"messages":[],"state":{}}\n',
    b'{"type":"subagent_checkpoint","version":1,"index":true,"messages":[],"state":{}}\n',
    b'{"type":"subagent_checkpoint","version":1,"index":0,"messages":[{"content":"missing role"}],"state":{}}\n',
])
def test_corrupt_complete_records_fail_closed(tmp_path, bad):
    conv = conversation(tmp_path)
    conv.store.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    conv.store.jsonl_path.write_bytes(bad)
    conv.acquire()
    try:
        with pytest.raises(ConversationCorrupt):
            conv.load()
        with pytest.raises(ConversationCorrupt):
            conv.save([], {})
        assert conv.store.jsonl_path.read_bytes() == bad
    finally:
        conv.close()


def test_invalid_middle_record_not_treated_as_torn_tail(tmp_path):
    conv = conversation(tmp_path)
    conv.acquire()
    conv.save([message("one")], {})
    conv.close()
    record = conv.store.jsonl_path.read_bytes()
    conv.store.jsonl_path.write_bytes(record + b"bad\n" + record)
    conv.acquire()
    try:
        with pytest.raises(ConversationCorrupt):
            conv.load()
    finally:
        conv.close()


def test_pending_tool_call_preserved_and_invalid_values_rejected(tmp_path):
    conv = conversation(tmp_path)
    conv.acquire()
    try:
        pending = {"role": "assistant", "content": None, "tool_calls": [{"id": "call1", "type": "function", "function": {"name": "write_file", "arguments": '{"path":"x"}'}}]}
        conv.save([pending], {"pending": True})
        assert conv.load()["messages"] == [pending]
        for messages, state in (([message(float("nan"))], {}), ([message(object())], {}), ([message("ok")], {"bad": object()}), ([message("ok")], {1: "integer key"})):
            with pytest.raises((TypeError, ValueError)):
                conv.save(messages, state)
        assert conv.load()["messages"] == [pending]
    finally:
        conv.close()


def test_live_lease_excludes_other_handles_and_processes(tmp_path):
    conv = conversation(tmp_path)
    conv.acquire()
    try:
        other = SubagentConversation(conv.store)
        with pytest.raises(InstanceAlreadyRunning):
            other.acquire()
        script = "from agent.runtime.instance_lock import InstanceLock, InstanceAlreadyRunning; import sys\ntry:\n InstanceLock(sys.argv[1]).acquire()\nexcept InstanceAlreadyRunning:\n sys.exit(23)\n"
        result = subprocess.run([sys.executable, "-c", script, str(conv.store.jsonl_path) + ".lock"], capture_output=True)
        assert result.returncode == 23, result.stderr.decode()
    finally:
        conv.close()
    other.acquire()
    other.close()


def test_killed_process_releases_conversation_lease(tmp_path):
    path = tmp_path / "session.json"
    script = "from agent.runtime.session_store import SessionStore; from agent.runtime.subagent_conversation import SubagentConversation; import sys, time\nc=SubagentConversation(SessionStore.for_subagent(sys.argv[1], 'agent_123')); c.acquire(); print('ready', flush=True); time.sleep(60)\n"
    process = subprocess.Popen([sys.executable, "-c", script, str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == "ready"
        conv = conversation(tmp_path)
        with pytest.raises(InstanceAlreadyRunning):
            conv.acquire()
        process.kill()
        process.wait(timeout=5)
        conv.acquire()
        conv.close()
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


def test_save_does_not_reread_entire_file_and_fsyncs(tmp_path, monkeypatch):
    conv = conversation(tmp_path)
    conv.acquire()
    try:
        conv.save([message("one")], {})
        original_open = Path.open
        def no_reread(path, mode="r", *args, **kwargs):
            if path == conv.store.jsonl_path and mode == "rb":
                pytest.fail("reread entire file")
            return original_open(path, mode, *args, **kwargs)
        monkeypatch.setattr(Path, "open", no_reread)
        calls = []
        monkeypatch.setattr("agent.runtime.subagent_conversation.os.fsync", lambda fd: calls.append(fd))
        conv.save([message("one"), message("two")], {"n": 2})
        assert calls
    finally:
        conv.close()


def test_torn_checkpoint_does_not_advance_messages_or_state(tmp_path):
    conv = conversation(tmp_path)
    conv.acquire()
    conv.save([message("known")], {"turns_used": 4, "cursor": 9})
    conv.close()
    newer = json.dumps({"type": "subagent_checkpoint", "version": 1, "index": 1,
                        "messages": [message("uncommitted")],
                        "state": {"turns_used": 5, "cursor": 10}}).encode()
    with conv.store.jsonl_path.open("ab") as handle:
        handle.write(newer[:-6])
    conv.acquire()
    try:
        assert conv.load() == {"messages": [message("known")], "state": {"turns_used": 4, "cursor": 9}}
    finally:
        conv.close()


def test_failed_partial_write_reloads_and_repairs_before_retry(tmp_path, monkeypatch):
    conv = conversation(tmp_path)
    conv.acquire()
    conv.save([message("one")], {"cursor": 1})
    original_open = Path.open

    class PartialWrite:
        def __init__(self, handle):
            self.handle = handle
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.handle.close()
        def write(self, data):
            self.handle.write(data[:len(data) // 2])
            self.handle.flush()
            raise OSError("simulated disk failure")

    def partial_open(path, mode="r", *args, **kwargs):
        handle = original_open(path, mode, *args, **kwargs)
        if path == conv.store.jsonl_path and mode == "ab":
            return PartialWrite(handle)
        return handle

    try:
        with monkeypatch.context() as failing:
            failing.setattr(Path, "open", partial_open)
            with pytest.raises(OSError, match="disk failure"):
                conv.save([message("one"), message("uncommitted")], {"cursor": 2})
        assert conv.load() == {"messages": [message("one")], "state": {"cursor": 1}}
        conv.save([message("one"), message("retried")], {"cursor": 3})
        assert conv.store.load(readonly=True)["messages"] == [message("one"), message("retried")]
        assert len(conv.store.jsonl_path.read_text().splitlines()) == 2
    finally:
        conv.close()
