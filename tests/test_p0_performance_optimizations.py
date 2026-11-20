"""Regression tests for P0 startup, session storage, listing, and filesystem policy optimizations."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.cli import sessions
from agent.runtime import llm as llm_module
from agent.runtime.conversation_branches import ConversationBranches
from agent.runtime.message_source import message_source_ref
from agent.runtime.session_store import SessionStore
from agent.runtime.tools import web as web_module
from agent.runtime.tools.files import FilesystemPolicy, FilesystemRoot
from agent.ui import queries


def test_p0_1_backend_import_does_not_eagerly_import_heavy_optional_sdks():
    """Importing llm, web, and backend must not pull in openai, aiohttp, bs4, or markdownify."""
    code = (
        "import sys; "
        "import agent.runtime.llm, agent.runtime.tools.web, agent.cli.backend; "
        "from agent.runtime.llm import LLMConfig, OpenAICompatibleProvider; "
        "OpenAICompatibleProvider(LLMConfig(api_key='k')); "
        "OpenAICompatibleProvider(LLMConfig(api_key='', connection_required=True)); "
        "leaked = [m for m in ('openai', 'aiohttp', 'bs4', 'markdownify') if m in sys.modules]; "
        "assert not leaked, f'eagerly imported: {leaked}'"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_p0_1_openai_lazy_loader_preserves_monkeypatches_and_recovers_after_teardown(monkeypatch):
    """Monkeypatched SDK symbols must not be clobbered, and real ones still bind afterward."""

    class FakeError(Exception):
        pass

    fake_module = SimpleNamespace(
        APIError=type("RealAPIError", (Exception,), {}),
        APITimeoutError=type("RealAPITimeoutError", (Exception,), {}),
        BadRequestError=type("RealBadRequestError", (Exception,), {}),
        RateLimitError=type("RealRateLimitError", (Exception,), {}),
        AsyncOpenAI=object(),
        DefaultAsyncHttpxClient=object(),
    )

    with monkeypatch.context() as inner:
        inner.setattr(llm_module, "_OPENAI_MODULE", fake_module)
        inner.setattr(llm_module, "APIError", llm_module._UnloadedAPIError)
        inner.setattr(llm_module, "APITimeoutError", llm_module._UnloadedAPITimeoutError)
        inner.setattr(llm_module, "BadRequestError", FakeError)
        inner.setattr(llm_module, "RateLimitError", llm_module._UnloadedRateLimitError)
        inner.setattr(llm_module, "AsyncOpenAI", llm_module._OPENAI_UNSET)
        inner.setattr(llm_module, "DefaultAsyncHttpxClient", llm_module._OPENAI_UNSET)

        llm_module._ensure_openai_loaded()
        assert llm_module.BadRequestError is FakeError
        assert llm_module.APIError is fake_module.APIError
        assert llm_module.AsyncOpenAI is fake_module.AsyncOpenAI

        # Simulate monkeypatch teardown restoring BadRequestError to its unloaded sentinel.
        inner.setattr(llm_module, "BadRequestError", llm_module._UnloadedBadRequestError)
        llm_module._ensure_openai_loaded()
        assert llm_module.BadRequestError is fake_module.BadRequestError


def test_p0_1_openai_concurrent_partial_import_never_raises_attribute_error(monkeypatch):
    """A half-initialized sys.modules['openai'] or in-progress import must not break concurrent callers."""
    partial_module = types.ModuleType("openai")

    with monkeypatch.context() as inner:
        inner.setattr(llm_module, "_OPENAI_MODULE", llm_module._OPENAI_UNSET)
        inner.setattr(llm_module, "APIError", llm_module._UnloadedAPIError)
        inner.setattr(llm_module, "APITimeoutError", llm_module._UnloadedAPITimeoutError)
        inner.setattr(llm_module, "BadRequestError", llm_module._UnloadedBadRequestError)
        inner.setattr(llm_module, "RateLimitError", llm_module._UnloadedRateLimitError)
        inner.setattr(llm_module, "AsyncOpenAI", llm_module._OPENAI_UNSET)
        inner.setattr(llm_module, "DefaultAsyncHttpxClient", llm_module._OPENAI_UNSET)
        inner.setitem(sys.modules, "openai", partial_module)

        # Case 1: module exists in sys.modules but attributes are not populated yet.
        llm_module._ensure_openai_loaded(only_if_imported=True)
        assert llm_module._OPENAI_MODULE is llm_module._OPENAI_UNSET
        assert llm_module.APIError is llm_module._UnloadedAPIError

        # Case 2: another thread holds _OPENAI_LOAD_LOCK while importing openai.
        entered = threading.Event()
        release = threading.Event()

        def hold_lock():
            with llm_module._OPENAI_LOAD_LOCK:
                entered.set()
                release.wait(timeout=5)

        holder = threading.Thread(target=hold_lock)
        holder.start()
        try:
            assert entered.wait(timeout=5)
            # Must return immediately without blocking or raising AttributeError.
            llm_module._ensure_openai_loaded(only_if_imported=True)
            assert llm_module.APIError is llm_module._UnloadedAPIError
        finally:
            release.set()
            holder.join(timeout=5)

        # Once initialization completes, symbols bind cleanly.
        for attr in llm_module._OPENAI_REQUIRED_ATTRS:
            setattr(partial_module, attr, type(attr, (Exception,), {}))
        llm_module._ensure_openai_loaded(only_if_imported=True)
        assert llm_module.APIError is partial_module.APIError


def test_p0_1_web_lazy_html_parsers_respect_none_monkeypatch(monkeypatch):
    """Monkeypatching BeautifulSoup/markdownify_html to None must fall back cleanly."""
    monkeypatch.setattr(web_module, "BeautifulSoup", None)
    monkeypatch.setattr(web_module, "markdownify_html", None)
    assert web_module._get_beautiful_soup() is None
    assert web_module._get_markdownify_html() is None


def test_p0_2_session_store_save_uses_o1_cache_and_detects_external_changes(tmp_path, monkeypatch):
    """Steady-state appends skip re-reading JSONL, while external edits invalidate the cache."""
    store = SessionStore(tmp_path / "chat.json")
    data = {"messages": [{"role": "user", "content": "one"}]}
    store.save(data, append_from=0)
    # Prime steady-state cache after initial snapshot creation.
    data = store.load()

    def forbidden_load(*_args, **_kwargs):
        raise AssertionError("save() re-read the journal despite matching stat signature")

    monkeypatch.setattr(store, "load", forbidden_load)
    data["messages"].append({"role": "assistant", "content": "two"})
    store.save(data, append_from=1)
    data["messages"].append({"role": "user", "content": "three"})
    store.save(data, append_from=2)

    # Verify on-disk replay from a fresh reader matches all 3 messages.
    monkeypatch.undo()
    assert [m["content"] for m in SessionStore(tmp_path / "chat.json").load(readonly=True)["messages"]] == [
        "one", "two", "three",
    ]

    # Simulate an external writer appending a 4th message, changing size/mtime.
    with store.jsonl_path.open("ab") as handle:
        handle.write(
            json.dumps({"type": "message", "index": 3, "message": {"role": "assistant", "content": "external"}}).encode()
            + b"\n"
        )

    # Saving a shorter history with append_from=2 must detect the external edit and emit a replace.
    rewritten = {"messages": [{"role": "user", "content": "one"}, {"role": "assistant", "content": "two"}]}
    store.save(rewritten, append_from=2)
    assert [m["content"] for m in SessionStore(tmp_path / "chat.json").load(readonly=True)["messages"]] == [
        "one", "two",
    ]


def test_p0_2_transient_jsonl_read_error_invalidates_cache_and_never_loses_next_save(tmp_path, monkeypatch):
    """If JSONL read hits PermissionError while stat succeeds, load() must not cache incomplete count."""
    path = tmp_path / "locked.json"
    seed = SessionStore(path)
    seed.save({"messages": [{"role": "user", "content": "snap-0"}]})
    seed.save(
        {"messages": [{"role": "user", "content": "snap-0"}, {"role": "assistant", "content": "jsonl-1"}]},
        append_from=1,
    )

    reader = SessionStore(path)
    orig_open = open
    lock_active = [True]

    def locked_open(file, mode="r", *args, **kwargs):
        if lock_active[0] and Path(file) == reader.jsonl_path and "r" in mode:
            raise PermissionError("transient exclusive lock on JSONL")
        return orig_open(file, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", locked_open)
    partial = reader.load(readonly=True)
    assert [m["content"] for m in partial["messages"]] == ["snap-0"]

    # Lock releases; saving a new reply from index 1 must not rely on the incomplete cached count.
    lock_active[0] = False
    monkeypatch.undo()
    partial["messages"].append({"role": "assistant", "content": "saved-after-unlock"})
    reader.save(partial, append_from=1)

    reopened = SessionStore(path).load(readonly=True)["messages"]
    assert [m["content"] for m in reopened] == ["snap-0", "saved-after-unlock"]


def test_p0_2_mid_replay_oserror_after_replace_preserves_recovered_history(tmp_path, monkeypatch):
    """If reading JSONL fails with OSError after a valid replace, already-replayed messages must be kept."""
    path = tmp_path / "mid_replay_fail.json"
    seed = SessionStore(path)
    seed.save_snapshot({"messages": [{"role": "user", "content": "old"}]})
    seed.jsonl_path.write_bytes(
        (
            json.dumps({"type": "replace", "messages": [{"role": "user", "content": "updated"}]}).encode() + b"\n"
            + json.dumps({"type": "message", "index": 1, "message": {"role": "assistant", "content": "tail"}}).encode()
            + b"\n"
            + json.dumps({"type": "message", "index": 2, "message": {"role": "user", "content": "unreached"}}).encode()
            + b"\n"
        )
    )

    store = SessionStore(path)
    orig_open = open
    injected = [True]

    class InterruptingReader:
        def __init__(self, inner):
            self._inner = inner
            self._yielded = 0

        def fileno(self):
            return self._inner.fileno()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self._inner.__exit__(*args)

        def __iter__(self):
            return self

        def __next__(self):
            if self._yielded >= 2:
                raise OSError("mid-stream read error")
            line = self._inner.readline()
            if not line:
                raise StopIteration
            self._yielded += 1
            return line

    def flaky_open(file, mode="r", *args, **kwargs):
        handle = orig_open(file, mode, *args, **kwargs)
        if injected[0] and Path(file) == store.jsonl_path and mode == "rb":
            return InterruptingReader(handle)
        return handle

    monkeypatch.setattr("builtins.open", flaky_open)
    loaded = store.load(readonly=True)
    assert [m["content"] for m in loaded["messages"]] == ["updated", "tail"]

    # Fault clears; appending a new message and reopening must preserve [updated, tail, next].
    injected[0] = False
    monkeypatch.undo()
    loaded["messages"].append({"role": "user", "content": "next"})
    store.save(loaded, append_from=2)

    reopened = SessionStore(path).load(readonly=True)["messages"]
    assert [m["content"] for m in reopened] == ["updated", "tail", "next"]


def test_p0_2_mmap_lifecycle_recovery_handles_many_messages_mentioning_lifecycle_markers(tmp_path, monkeypatch):
    """Reverse line scan in mmap recovery must find real lifecycle records cleanly even with marker text in messages."""
    path = tmp_path / "tool_logs.json"
    store = SessionStore(path)
    monkeypatch.setattr(SessionStore, "_MMAP_MIN_BYTES", 0)
    monkeypatch.setattr(SessionStore, "_process_alive", staticmethod(lambda pid: pid != 666666))

    lines = [
        json.dumps({"type": "session_started", "run_id": "dead-run", "pid": 666666}),
        json.dumps({"type": "replace", "messages": [{"role": "user", "content": "seed"}]}),
    ]
    for i in range(1, 201):
        lines.append(
            json.dumps({
                "type": "message",
                "index": i,
                "message": {"role": "tool", "content": f"log {i}: session_started seen in tool output"},
            })
        )
    store.jsonl_path.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))

    loaded = store.load(readonly=False)
    assert len(loaded["messages"]) == 201
    events = SessionStore(path).lifecycle_events()
    assert [e["type"] for e in events] == ["session_started", "session_interrupted"]
    assert events[1]["run_id"] == "dead-run"


def test_p0_2_save_post_write_cache_rejects_interleaved_jsonl_and_snapshot_writes(tmp_path, monkeypatch):
    """Interleaving another writer before _append_jsonl or after save_snapshot must not corrupt next save."""
    # Window A: another writer appends to JSONL after existing_count is decided and before our write.
    path_a = tmp_path / "window_a.json"
    seed_a = SessionStore(path_a)
    seed_a.save({"messages": [{"role": "user", "content": "m0"}]})

    writer_a = SessionStore(path_a)
    writer_b = SessionStore(path_a)
    a_data = writer_a.load()
    orig_write_header = writer_a._write_header
    injected_a = [False]

    def header_then_b_appends(header):
        orig_write_header(header)
        if not injected_a[0]:
            injected_a[0] = True
            b_data = writer_b.load(readonly=True)
            b_data["messages"].append({"role": "assistant", "content": "b1"})
            b_data["messages"].append({"role": "user", "content": "b2"})
            writer_b.save(b_data, append_from=1)

    monkeypatch.setattr(writer_a, "_write_header", header_then_b_appends)
    a_data["messages"].append({"role": "assistant", "content": "a1"})
    writer_a.save(a_data, append_from=1)

    # Next uncontested save from writer_a must not trust a polluted cache and must preserve a2.
    monkeypatch.undo()
    a_data["messages"].append({"role": "user", "content": "a2"})
    writer_a.save(a_data, append_from=2)
    reopened_a = SessionStore(path_a).load(readonly=True)["messages"]
    assert reopened_a[-1]["content"] == "a2"

    # Window B: another writer replaces snapshot right after save_snapshot() finishes.
    path_b = tmp_path / "window_b.json"
    writer_c = SessionStore(path_b)
    c_data = {"messages": [{"role": "user", "content": "c0"}]}
    orig_save_snapshot = writer_c.save_snapshot

    def snapshot_then_external_replace(data):
        orig_save_snapshot(data)
        SessionStore(path_b).save_snapshot(
            {
                "messages": [
                    {"role": "user", "content": "ext0"},
                    {"role": "assistant", "content": "ext1"},
                    {"role": "user", "content": "ext2"},
                ]
            }
        )

    monkeypatch.setattr(writer_c, "save_snapshot", snapshot_then_external_replace)
    writer_c.save(c_data, append_from=0, snapshot=True)

    # Next uncontested save from writer_c must detect the external snapshot and preserve c1.
    monkeypatch.undo()
    c_data["messages"].append({"role": "assistant", "content": "c1"})
    writer_c.save(c_data, append_from=1)
    reopened_b = SessionStore(path_b).load(readonly=True)["messages"]
    assert [m["content"] for m in reopened_b] == ["c0", "c1"]


def test_p0_2_interleaved_crash_after_load_scan_is_not_masked_as_recovered(tmp_path, monkeypatch):
    """If another process starts and dies right after A scans an ended log, A must still record interruption."""
    path = tmp_path / "interleaved_crash.json"
    seed = SessionStore(path)
    seed.save({"messages": [{"role": "user", "content": "seed"}]})
    seed.record_end("clean")

    store_a = SessionStore(path)
    orig_recover_state = store_a._recover_from_lifecycle_event_state
    injected = [False]

    def recover_state_while_b_crashes(last_event, *, checked_jsonl_sig):
        result = orig_recover_state(last_event, checked_jsonl_sig=checked_jsonl_sig)
        if not injected[0]:
            injected[0] = True
            with store_a.jsonl_path.open("ab") as handle:
                handle.write(
                    json.dumps({"type": "session_started", "run_id": "b-crashed", "pid": 777777}).encode()
                    + b"\n"
                )
        return result

    monkeypatch.setattr(store_a, "_recover_from_lifecycle_event_state", recover_state_while_b_crashes)
    monkeypatch.setattr(
        SessionStore,
        "_process_alive",
        staticmethod(lambda pid: pid != 777777),
    )

    data = store_a.load()
    data["messages"].append({"role": "assistant", "content": "after-b-crash"})
    store_a.save(data, append_from=1)

    events = SessionStore(path).lifecycle_events()
    assert [e["type"] for e in events[-3:]] == [
        "session_started",
        "session_interrupted",
        "session_started",
    ]
    assert events[-2]["run_id"] == "b-crashed"


def test_p0_2_interleaved_writer_during_load_does_not_drop_saved_message(tmp_path, monkeypatch):
    """If writer B commits between A's replay and end of load(), A must not bind stale count to B's signature."""
    path = tmp_path / "interleaved.json"
    seed = SessionStore(path)
    seed.save({"messages": [{"role": "user", "content": "seed"}]})

    writer_a = SessionStore(path)
    writer_b = SessionStore(path)
    orig_replay = writer_a._replay_jsonl_with_lifecycle
    interleaved = False

    def replay_then_b_commits(data, *, track_lifecycle=False):
        nonlocal interleaved
        result = orig_replay(data, track_lifecycle=track_lifecycle)
        if not interleaved:
            interleaved = True
            b_data = writer_b.load(readonly=True)
            b_data["messages"].append({"role": "assistant", "content": "from-B"})
            writer_b.save(b_data, append_from=1)
        return result

    monkeypatch.setattr(writer_a, "_replay_jsonl_with_lifecycle", replay_then_b_commits)

    a_data = writer_a.load()
    assert [m["content"] for m in a_data["messages"]] == ["seed"]
    a_data["messages"].append({"role": "assistant", "content": "from-A"})
    writer_a.save(a_data, append_from=1)

    reopened = SessionStore(path).load(readonly=True)["messages"]
    assert [m["content"] for m in reopened] == ["seed", "from-A"]


def test_p0_2_live_owner_exiting_between_load_and_ensure_started_records_interrupted(tmp_path, monkeypatch):
    """If a previous PID was alive during load() and exits before ensure_started(), interruption must be recorded."""
    path = tmp_path / "crash_after_load.json"
    store = SessionStore(path)
    store.jsonl_path.write_bytes(
        (
            json.dumps({"type": "session_started", "run_id": "prev-run", "pid": 888888}) + "\n"
            + json.dumps({"type": "message", "index": 0, "message": {"role": "user", "content": "hi"}}) + "\n"
        ).encode("utf-8")
    )
    prev_alive = [True]
    monkeypatch.setattr(
        SessionStore,
        "_process_alive",
        staticmethod(lambda pid: prev_alive[0] if pid == 888888 else True),
    )

    data = store.load()
    assert [e["type"] for e in store.lifecycle_events()] == ["session_started"]

    # Previous owner exits without touching the journal.
    prev_alive[0] = False
    data["messages"].append({"role": "assistant", "content": "recovered"})
    store.save(data, append_from=1)

    events = store.lifecycle_events()
    assert [e["type"] for e in events] == [
        "session_started",
        "session_interrupted",
        "session_started",
    ]
    assert events[1]["run_id"] == "prev-run"


@pytest.mark.parametrize("threshold", [256 * 1024, 0])
def test_p0_2_session_store_skips_pre_replace_lines_preserves_state_and_handles_torn_replace(
    tmp_path, monkeypatch, threshold
):
    """load() skips obsolete pre-replace messages, keeps pre-replace state, and survives a torn replace tail."""
    monkeypatch.setattr(SessionStore, "_MMAP_MIN_BYTES", threshold)
    path = tmp_path / "multi_replace.json"
    store = SessionStore(path)
    lines = [
        json.dumps({"type": "session_started", "run_id": "r1", "pid": os.getpid()}),
        json.dumps({"type": "state", "state": {"system_prompt": "v1", "custom_flag": "kept"}}),
        json.dumps({"type": "message", "index": 0, "message": {"role": "user", "content": "obsolete-0"}}),
        json.dumps({"type": "replace", "messages": [{"role": "user", "content": "old-replace"}]}),
        json.dumps({"type": "state", "state": {"system_prompt": "v2"}}),
        json.dumps({"type": "replace", "messages": [{"role": "user", "content": "final-replace"}]}),
        json.dumps({"type": "message", "index": 1, "message": {"role": "assistant", "content": "after-replace"}}),
        '{"type": "replace", "messages": [{"role": "user", "content": "torn',
    ]
    store.jsonl_path.write_bytes(("\n".join(lines)).encode("utf-8"))

    loaded = store.load(readonly=True)
    assert loaded["system_prompt"] == "v2"
    assert loaded["custom_flag"] == "kept"
    assert [m["content"] for m in loaded["messages"]] == ["final-replace", "after-replace"]


def test_p0_2_lifecycle_events_ignores_message_body_mentions_and_handles_reordered_keys(tmp_path):
    """Fast byte-filtered lifecycle scan skips message payloads mentioning lifecycle strings."""
    store = SessionStore(tmp_path / "lifecycle.json")
    lines = [
        json.dumps({"pid": 12345, "run_id": "reordered", "type": "session_started"}),
        json.dumps({
            "type": "message",
            "index": 0,
            "message": {"role": "user", "content": 'talking about "session_ended" and "session_interrupted"'},
        }),
        json.dumps({
            "type": "replace",
            "messages": [{"role": "user", "content": "session_started inside replace"}],
        }),
        json.dumps({"type": "session_ended", "run_id": "reordered", "pid": 12345, "reason": "done"}),
    ]
    store.jsonl_path.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))

    events = store.lifecycle_events()
    assert [e["type"] for e in events] == ["session_started", "session_ended"]
    assert events[0]["run_id"] == "reordered"


def test_p0_3_fast_session_listing_matches_modes_excludes_orphan_headers_and_tracks_branches(
    tmp_path, monkeypatch
):
    """scandir-based session listing preserves exact mode/branch semantics without per-session SessionStore."""
    monkeypatch.setattr(sessions, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(sessions, "SESSION_DEFAULT", tmp_path / "default.json")

    # Work sessions: legacy .json, .jsonl, .snapshot.json, plus an orphan .header.json and subdir.
    legacy = tmp_path / "legacy_work.json"
    legacy.write_text('{"messages": []}', encoding="utf-8")
    os.utime(legacy, (1000, 1000))

    jsonl = tmp_path / "recent_work.jsonl"
    jsonl.write_text('{"type": "message", "index": 0, "message": {"role": "user", "content": "hi"}}\n', encoding="utf-8")
    os.utime(jsonl, (2000, 2000))

    snap = tmp_path / "snap_only.snapshot.json"
    snap.write_text('{"messages": []}', encoding="utf-8")
    os.utime(snap, (1500, 1500))

    (tmp_path / "orphan_only.header.json").write_text('{"system_prompt": ""}', encoding="utf-8")
    (tmp_path / "not_a_session.json").mkdir()

    # Bar session with newer artifact file.
    bar_dir = tmp_path / "bar"
    bar_dir.mkdir()
    bar_old = bar_dir / "bar_a.jsonl"
    bar_old.write_text("", encoding="utf-8")
    os.utime(bar_old, (1100, 1100))
    bar_new = bar_dir / "bar_b.jsonl"
    bar_new.write_text("", encoding="utf-8")
    os.utime(bar_new, (1200, 1200))
    bar_artifacts = bar_dir / ".artifacts"
    bar_artifacts.mkdir()
    bar_art = bar_artifacts / "bar_a.subagents.jsonl"
    bar_art.write_text("", encoding="utf-8")
    os.utime(bar_art, (5000, 5000))

    # Branched work session whose active branch head is newer than its main .jsonl.
    branched_path = tmp_path / "branched.json"
    base_data = {
        "messages": [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "a1"},
        ]
    }
    SessionStore(branched_path).save(base_data)
    branches = ConversationBranches(branched_path)
    ticket = branches.prepare_retry(message_source_ref(base_data["messages"][-1], 1), branch_id="main", expected_revision=0)
    alt = ticket["seed"]
    alt["messages"].append({"role": "assistant", "content": "a2"})
    alt_store = SessionStore(branched_path, branch_id=ticket["candidate_branch_id"])
    alt_store.save(alt)
    branches.finish_candidate(alt_store.branch_id)
    for main_file in (tmp_path / "branched.jsonl", tmp_path / "branched.snapshot.json"):
        if main_file.exists():
            os.utime(main_file, (900, 900))
    for branch_file in (alt_store.jsonl_path, alt_store.legacy_path, alt_store.snapshot_path):
        if branch_file.exists():
            os.utime(branch_file, (9000, 9000))

    # Track which sessions actually construct SessionStore during queries.list_sessions().
    constructed: list[str] = []
    orig_init = SessionStore.__init__

    def spy_init(self, path, branch_id=None):
        constructed.append(Path(path).stem)
        return orig_init(self, path, branch_id=branch_id)

    monkeypatch.setattr(SessionStore, "__init__", spy_init)

    listed = queries.list_sessions()
    names_by_mode = [(item["mode"], item["name"]) for item in listed]
    assert ("work", "orphan_only") not in names_by_mode
    assert ("work", "not_a_session") not in names_by_mode
    assert listed[0] == {"name": "branched", "mode": "work", "modified": pytest.approx(9000.0)}
    assert [item["name"] for item in listed] == [
        "branched", "recent_work", "snap_only", "bar_b", "bar_a", "legacy_work",
    ]
    # Only the single branched session should have needed SessionStore construction.
    assert constructed == ["branched"]

    # CLI list_bar_sessions includes artifact mtime, so bar_a (artifact at 5000) sorts before bar_b (1200).
    assert sessions.list_bar_sessions() == ["bar_a", "bar_b"]
    assert sessions.list_sessions() == ["branched", "legacy_work", "recent_work", "snap_only"]

    # Sessions with pre-epoch (<= -1.0) mtimes must still be listed.
    old_file = tmp_path / "pre_epoch.jsonl"
    old_file.write_text("", encoding="utf-8")
    orig_entry_mtime = sessions._entry_mtime
    monkeypatch.setattr(
        sessions,
        "_entry_mtime",
        lambda entry: -5.0 if entry.name == "pre_epoch.jsonl" else orig_entry_mtime(entry),
    )
    assert "pre_epoch" in sessions.list_sessions()
    pre_epoch_item = next(item for item in queries.list_sessions() if item["name"] == "pre_epoch")
    assert pre_epoch_item["modified"] == pytest.approx(-5.0)


def test_p0_3_session_listing_tracks_nested_subagent_diagnostics_and_windows_case(tmp_path, monkeypatch):
    """Nested subagent diagnostic files update parent mtime, and Windows suffix matching is case-insensitive."""
    monkeypatch.setattr(sessions, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(sessions, "SESSION_DEFAULT", tmp_path / "default.json")

    bar_dir = tmp_path / "bar"
    bar_dir.mkdir()
    parent_a = bar_dir / "parent_a.jsonl"
    parent_b = bar_dir / "parent_b.jsonl"
    parent_a.write_text("", encoding="utf-8")
    parent_b.write_text("", encoding="utf-8")
    os.utime(parent_a, (1000, 1000))
    os.utime(parent_b, (2000, 2000))

    sub_store = SessionStore.for_subagent(bar_dir / "parent_a.json", "worker_1")
    sub_store.save({"messages": [{"role": "user", "content": "task"}]})
    for file in (sub_store.jsonl_path, sub_store.snapshot_path, sub_store.header_path):
        if file.exists():
            os.utime(file, (1500, 1500))
    diag = sub_store.append_diagnostic({"error": "boom"})
    os.utime(diag, (8000, 8000))

    assert sessions.list_bar_sessions() == ["parent_a", "parent_b"]

    if os.name == "nt":
        upper = tmp_path / "imported.JSONL"
        upper.write_text('{"type": "message", "index": 0, "message": {"role": "user", "content": "hi"}}\n', encoding="utf-8")
        assert "imported" in sessions.list_sessions()
        assert any(item["name"] == "imported" and item["mode"] == "work" for item in queries.list_sessions())

        # Mixed-case branch and diagnostic association on case-insensitive Windows NTFS.
        notes_path = tmp_path / "Notes.json"
        notes_data = {
            "messages": [
                {"role": "user", "content": "q"},
                {"role": "assistant", "content": "a1"},
            ]
        }
        SessionStore(notes_path).save(notes_data)
        lower_branches = ConversationBranches(tmp_path / "notes.json")
        ticket = lower_branches.prepare_retry(
            message_source_ref(notes_data["messages"][-1], 1),
            branch_id="main",
            expected_revision=0,
        )
        alt_notes = ticket["seed"]
        alt_notes["messages"].append({"role": "assistant", "content": "a2"})
        alt_store = SessionStore(tmp_path / "notes.json", branch_id=ticket["candidate_branch_id"])
        alt_store.save(alt_notes)
        lower_branches.finish_candidate(alt_store.branch_id)
        for main_file in (tmp_path / "Notes.jsonl", tmp_path / "Notes.snapshot.json"):
            if main_file.exists():
                os.utime(main_file, (1000, 1000))
        for branch_file in (alt_store.jsonl_path, alt_store.legacy_path, alt_store.snapshot_path):
            if branch_file.exists():
                os.utime(branch_file, (9000, 9000))
        notes_entry = next(
            item for item in queries.list_sessions() if item["mode"] == "work" and item["name"].casefold() == "notes"
        )
        assert notes_entry["modified"] == pytest.approx(9000.0)

        # Mixed-case sibling files (.jsonl vs .snapshot.json) must share the same max mtime on NTFS.
        mixed_dir = tmp_path / "minimal"
        mixed_dir.mkdir()
        upper_jsonl = mixed_dir / "MixCase.jsonl"
        lower_snap = mixed_dir / "mixcase.snapshot.json"
        upper_jsonl.write_text("", encoding="utf-8")
        lower_snap.write_text('{"messages": []}', encoding="utf-8")
        os.utime(upper_jsonl, (1000, 1000))
        os.utime(lower_snap, (5000, 5000))
        mix_entries = [
            item for item in queries.list_sessions()
            if item["mode"] == "minimal" and os.path.normcase(item["name"]) == "mixcase"
        ]
        assert mix_entries and all(item["modified"] == pytest.approx(5000.0) for item in mix_entries)

        # Distinct NTFS filenames "Straße" and "Strasse" must not share diagnostic mtimes.
        eszett = mixed_dir / "Straße.jsonl"
        double_s = mixed_dir / "Strasse.jsonl"
        eszett.write_text("", encoding="utf-8")
        double_s.write_text("", encoding="utf-8")
        os.utime(eszett, (1000, 1000))
        os.utime(double_s, (2000, 2000))
        m_art = mixed_dir / ".artifacts"
        m_art.mkdir()
        eszett_diag = m_art / "Straße.tool-failures.jsonl"
        eszett_diag.write_text("", encoding="utf-8")
        os.utime(eszett_diag, (7000, 7000))
        listed_min = [name for name in sessions.list_minimal_sessions() if name in {"Straße", "Strasse"}]
        assert listed_min == ["Straße", "Strasse"]
        # Flip: update Strasse diagnostic to 9000; Straße must stay at 7000 and sort second.
        double_s_diag = m_art / "Strasse.tool-failures.jsonl"
        double_s_diag.write_text("", encoding="utf-8")
        os.utime(double_s_diag, (9000, 9000))
        listed_min = [name for name in sessions.list_minimal_sessions() if name in {"Straße", "Strasse"}]
        assert listed_min == ["Strasse", "Straße"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows UNC/WSL path semantics")
def test_p0_4_filesystem_policy_skips_eager_unc_resolve_and_enforces_unc_roots(tmp_path, monkeypatch):
    """UNC/WSL roots must not be resolved during startup, local accesses, or unrelated UNC share accesses."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    local_file = workspace / "hello.txt"
    local_file.write_text("hi", encoding="utf-8")

    unc_ro = Path(r"\\wsl.localhost\Ubuntu\home\tester")
    unc_rw = Path(r"\\wsl.localhost\Ubuntu\home\tester\.local\bin")
    smb_ro = Path(r"\\fileserver\share\docs")

    resolved_calls: list[str] = []
    orig_resolve = Path.resolve

    def spy_resolve(self, strict=False):
        raw = str(self)
        if raw.startswith("\\\\"):
            resolved_calls.append(raw)
            raise AssertionError(f"Unexpected eager UNC resolve: {raw}")
        return orig_resolve(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", spy_resolve)

    policy = FilesystemPolicy(
        workspace,
        [FilesystemRoot(unc_ro, "ro"), FilesystemRoot(unc_rw, "rw"), FilesystemRoot(smb_ro, "ro")],
    )
    rebound = policy.for_workspace(workspace)
    assert rebound.resolve(str(local_file)) == local_file.resolve()
    assert resolved_calls == []

    # Accessing an SMB share must NOT resolve unrelated WSL UNC roots.
    unc_resolutions: list[str] = []

    def track_unc_resolve(root: Path) -> Path:
        unc_resolutions.append(str(root))
        return root

    monkeypatch.setattr(policy, "_candidate", lambda raw: Path(os.path.normpath(raw)))
    monkeypatch.setattr(policy, "_resolve_unc_root", track_unc_resolve)

    assert policy.resolve(r"\\fileserver\share\docs\spec.md", write=False) == Path(
        r"\\fileserver\share\docs\spec.md"
    )
    assert unc_resolutions == [r"\\fileserver\share\docs"]

    assert policy.resolve(r"\\wsl.localhost\Ubuntu\home\tester\notes.txt", write=False) == Path(
        r"\\wsl.localhost\Ubuntu\home\tester\notes.txt"
    )
    with pytest.raises(PermissionError, match="read-only"):
        policy.resolve(r"\\wsl.localhost\Ubuntu\home\tester\notes.txt", write=True)
    assert policy.resolve(r"\\wsl.localhost\Ubuntu\home\tester\.local\bin\tool", write=True) == Path(
        r"\\wsl.localhost\Ubuntu\home\tester\.local\bin\tool"
    )
    with pytest.raises(ValueError, match="outside allowed filesystem roots"):
        policy.resolve(r"\\wsl.localhost\Ubuntu\etc\passwd", write=False)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows UNC/WSL path semantics")
def test_p0_4_unc_alias_root_does_not_override_approved_grant(tmp_path, monkeypatch):
    """An approved grant on a resolved UNC path must suppress approval prompts despite an alias root."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    alias_root = Path(r"\\server\share\long-alias-link")
    real_root = Path(r"\\server\share\real")
    target_file = r"\\server\share\real\notes.txt"

    policy = FilesystemPolicy(workspace, [FilesystemRoot(alias_root, "ro")])
    orig_exists = Path.exists
    monkeypatch.setattr(
        Path,
        "exists",
        lambda self: False if str(self).startswith("\\\\") else orig_exists(self),
    )
    monkeypatch.setattr(policy, "_candidate", lambda raw: Path(os.path.normpath(raw)))
    monkeypatch.setattr(
        policy,
        "_resolve_unc_root",
        lambda root: real_root if str(root).casefold() == str(alias_root).casefold() else root,
    )

    # Before grant: configured alias root requires explicit approval.
    assert policy.permission_request(target_file, write=False, operation="read_file") is not None

    # After approving read access on the real UNC directory, reads must not prompt again.
    grant = policy.grant(str(real_root), "ro")
    assert policy.permission_request(target_file, write=False, operation="read_file") is None
    # Writes still require approval since the grant is read-only.
    assert policy.permission_request(target_file, write=True, operation="write_file") is not None

    # After revoking the grant, approval is required again.
    policy.revoke(grant)
    assert policy.permission_request(target_file, write=False, operation="read_file") is not None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows UNC/WSL path semantics")
def test_p0_4_cross_share_unc_alias_root_is_resolved_without_waking_wsl(tmp_path, monkeypatch):
    """A configured UNC alias pointing to another share must allow access without probing WSL roots."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    alias_root = Path(r"\\alias-host\alias-share\docs")
    real_root = Path(r"\\real-host\real-share\docs")
    wsl_root = Path(r"\\wsl.localhost\Ubuntu\home\tester")
    policy = FilesystemPolicy(
        workspace,
        [FilesystemRoot(alias_root, "ro"), FilesystemRoot(wsl_root, "ro")],
    )

    resolved_roots: list[str] = []

    def resolve_root(root: Path) -> Path:
        resolved_roots.append(str(root))
        if str(root).casefold() == str(alias_root).casefold():
            return real_root
        raise AssertionError(f"Unexpected resolution of unrelated root: {root}")

    def resolve_candidate(raw: str) -> Path:
        norm = os.path.normpath(raw)
        if norm.casefold().startswith(str(alias_root).casefold()):
            return Path(str(real_root) + norm[len(str(alias_root)):])
        return Path(norm)

    monkeypatch.setattr(policy, "_candidate", resolve_candidate)
    monkeypatch.setattr(policy, "_resolve_unc_root", resolve_root)

    # First read via the alias path resolves only the alias root and succeeds.
    assert policy.resolve(r"\\alias-host\alias-share\docs\hello.txt", write=False) == Path(
        r"\\real-host\real-share\docs\hello.txt"
    )
    assert resolved_roots == [str(alias_root)]

    # Direct read via the resolved target share on a fresh policy also succeeds without probing WSL.
    policy2 = FilesystemPolicy(
        workspace,
        [FilesystemRoot(alias_root, "ro"), FilesystemRoot(wsl_root, "ro")],
    )
    resolved_roots.clear()
    monkeypatch.setattr(policy2, "_candidate", resolve_candidate)
    monkeypatch.setattr(policy2, "_resolve_unc_root", resolve_root)
    assert policy2.resolve(r"\\real-host\real-share\docs\hello.txt", write=False) == Path(
        r"\\real-host\real-share\docs\hello.txt"
    )
    assert resolved_roots == [str(alias_root)]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows UNC/WSL path semantics")
def test_p0_4_overlapping_cross_share_unc_alias_is_order_independent(tmp_path, monkeypatch):
    """Overlapping parent + cross-share alias subdirectory must enforce the specific rule on first access."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    parent = Path(r"\\real-host\real-share\parent")
    alias_sub = Path(r"\\alias-host\alias-share\sub")
    real_sub = Path(r"\\real-host\real-share\parent\sub")
    wsl_root = Path(r"\\wsl.localhost\Ubuntu\home\tester")
    target = r"\\real-host\real-share\parent\sub\file.txt"

    def make_policy(parent_mode: str, sub_mode: str) -> FilesystemPolicy:
        policy = FilesystemPolicy(
            workspace,
            [
                FilesystemRoot(parent, parent_mode),
                FilesystemRoot(alias_sub, sub_mode),
                FilesystemRoot(wsl_root, "ro"),
            ],
        )

        def resolve_root(root: Path) -> Path:
            if str(root).casefold() == str(wsl_root).casefold():
                raise AssertionError("WSL root must not be resolved for SMB access")
            if str(root).casefold() == str(alias_sub).casefold():
                return real_sub
            return root

        monkeypatch.setattr(policy, "_candidate", lambda raw: Path(os.path.normpath(raw)))
        monkeypatch.setattr(policy, "_resolve_unc_root", resolve_root)
        return policy

    # Parent is ro, cross-share alias subdir is rw: first write via real path must succeed.
    ro_parent_rw_sub = make_policy("ro", "rw")
    assert ro_parent_rw_sub.resolve(target, write=True) == Path(target)

    # Parent is rw, cross-share alias subdir is ro: first write via real path must be rejected.
    rw_parent_ro_sub = make_policy("rw", "ro")
    with pytest.raises(PermissionError, match="read-only"):
        rw_parent_ro_sub.resolve(target, write=True)
