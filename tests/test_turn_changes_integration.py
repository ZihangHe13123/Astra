"""Integration tests for the turn-change ledger wiring (M1).

Slice 1: file-tool capture side-channel (files.py -> store).
Later slices: turn lifecycle + event order (react.py), backend stdout order.

Harness notes mirror tests/test_core_skill.py (CaptureLLM + ReActAgent) and
tests/test_search_responsiveness.py (make_agent).

Source of truth: docs/superpowers/plans/2026-09-18-turn-change-ledger-m1.md
section 4 (TC) and section 3.2 (store contract).
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.core.msg import ContentBlock, Msg
from agent.cli.turn_changes_command import execute_changes_command
from agent.runtime import turn_change_store as tcs
from agent.runtime import turn_changes_view as changes_view
from agent.runtime.llm import LLMConfig, LLMResponseError
from agent.runtime.react import ReActAgent
from agent.runtime.token_estimator import estimate_messages_tokens
from agent.runtime.tools import delegate as delegate_tools
from agent.runtime.tools.delegate import register_delegate_tools
from agent.runtime.tools.files import register_file_tools
from agent.runtime.tools.processes import ProcessManager
from agent.runtime.tools.registry import ToolDef, ToolRegistry


def run(coro):
    return asyncio.run(coro)


def make_store(tmp_path, session="sess-a"):
    return tcs.TurnChangeStore(tmp_path, session, root=tmp_path / "tc-root")


def make_file_registry(tmp_path):
    registry = ToolRegistry()
    register_file_tools(registry, workdir=str(tmp_path))
    registry.yolo = True
    return registry


# ---------------------------------------------------------------------------
# Slice 1: file-tool capture side-channel
# ---------------------------------------------------------------------------

def test_file_tool_capture_feeds_turn_store(tmp_path):
    """An in-scope edit over an existing file lands in the turn manifest."""
    store = make_store(tmp_path)
    store.begin_turn("req-1")
    registry = make_file_registry(tmp_path)
    target = tmp_path / "note.txt"
    target.write_text("before\n", encoding="utf-8")

    with tcs.turn_store_scope(store):
        # write_file refuses to overwrite existing files by design; a focused
        # edit is the supported mutation path for existing content.
        result = run(registry.execute("edit_file", {"path": "note.txt", "old": "before", "new": "after"}))
    assert not result.get("error"), result

    manifest = store.seal()
    assert manifest is not None
    by_path = {c.path: c for c in manifest.files}
    assert "note.txt" in by_path
    change = by_path["note.txt"]
    assert change.state == tcs.STATE_MODIFIED
    assert change.added == 1
    assert change.removed == 1


def test_file_tool_create_records_absent_before(tmp_path):
    """A new file records before=absent and lands as added."""
    store = make_store(tmp_path)
    store.begin_turn("req-1")
    registry = make_file_registry(tmp_path)

    with tcs.turn_store_scope(store):
        result = run(registry.execute("write_file", {"path": "new.txt", "content": "hello\n"}))
    assert not result.get("error"), result

    manifest = store.seal()
    assert manifest is not None
    by_path = {c.path: c for c in manifest.files}
    assert "new.txt" in by_path
    change = by_path["new.txt"]
    assert change.state == tcs.STATE_ADDED
    assert change.added == 1
    assert change.removed == 0


def test_capture_reads_the_file_tool_target_when_roots_differ(tmp_path):
    """R4：file tool 工作区与 store 工作区不同时，before/after 仍是同一份文件."""
    workspace = tmp_path / "files"
    sandbox = tmp_path / "sandbox"
    workspace.mkdir()
    sandbox.mkdir()
    (workspace / "note.txt").write_text("before\n", encoding="utf-8", newline="\n")
    (sandbox / "note.txt").write_text("UNRELATED-SANDBOX-CONTENT\n", encoding="utf-8")
    registry = ToolRegistry()
    register_file_tools(registry, workdir=str(workspace))
    registry.yolo = True
    store = tcs.TurnChangeStore(sandbox, "review", root=tmp_path / "ledger")
    store.begin_turn("edit-in-files")

    with tcs.turn_store_scope(store):
        result = run(registry.execute("edit_file", {"path": "note.txt", "old": "before", "new": "after"}))
    assert not result.get("error"), result

    manifest = store.seal()
    assert manifest is not None
    change = manifest.files[0]
    assert (change.path, change.state) == ("note.txt", tcs.STATE_MODIFIED)

    sides = store.load_sides(0, "note.txt")
    assert sides.after == b"after\n"
    assert (workspace / "note.txt").read_text(encoding="utf-8") == "after\n"
    assert (sandbox / "note.txt").read_text(encoding="utf-8") == "UNRELATED-SANDBOX-CONTENT\n"


# ---------------------------------------------------------------------------
# Slice 2: turn lifecycle + event order (react.py wiring)
# ---------------------------------------------------------------------------

DONE = {"type": "done", "content": "完成。", "usage": None}


class ScriptedLLM:
    """Minimal scripted LLM (pattern from tests/test_core_skill.CaptureLLM)."""

    def __init__(self, replies=(), *, delays=None):
        self.config = LLMConfig(model="deepseek-flash")
        self.requests = []
        self.replies = iter(replies)
        self.delays = dict(delays or {})
        self.calls = 0

    estimate_tokens = staticmethod(estimate_messages_tokens)

    async def chat_stream(self, messages, tools, **kwargs):
        self.requests.append(copy.deepcopy({"messages": messages, "tools": tools}))
        self.calls += 1
        delay = self.delays.get(self.calls)
        if delay:
            await asyncio.sleep(delay)
        item = next(self.replies, DONE)
        if isinstance(item, Exception):
            raise item
        yield item


def call(name, args=None, *, call_id="call-1"):
    return {"type": "tool_calls", "content": "", "usage": None, "calls": [
        {"id": call_id, "name": name, "arguments": json.dumps(args or {})},
    ]}


async def collect_stream(agent, msg):
    events = []
    async for event in agent.reply_stream(msg):
        events.append(event)
    return events


def make_turn_agent(tmp_path, replies, monkeypatch, **kwargs):
    registry = ToolRegistry()
    register_file_tools(registry, workdir=str(tmp_path))
    registry.yolo = True
    agent = ReActAgent(
        "tc-agent", ScriptedLLM(replies, delays=kwargs.pop("llm_delays", None)), registry,
        timing_log_enabled=False, query_profile_enabled=False,
        max_iterations=kwargs.pop("max_iterations", 5), **kwargs,
    )
    stores = []

    def fake_make_store(self, session_key):
        store = tcs.TurnChangeStore(tmp_path, session_key, root=tmp_path / "tc-root")
        stores.append(store)
        return store

    monkeypatch.setattr(ReActAgent, "_make_turn_change_store", fake_make_store)
    return agent, stores


def test_turn_lifecycle_publishes_net_changes_before_done(tmp_path, monkeypatch):
    """A completed turn publishes turn_changes strictly before done, and the
    sealed manifest survives in the session store."""
    agent, stores = make_turn_agent(
        tmp_path,
        [call("write_file", {"path": "note.txt", "content": "hello\n"})],
        monkeypatch,
    )
    msg = Msg(content=[ContentBlock.text("make a note")], id="msg-1")
    events = run(collect_stream(agent, msg))

    types = [e["type"] for e in events]
    assert "turn_changes" in types, events
    assert types.index("turn_changes") < types.index("done")

    payload = events[types.index("turn_changes")]
    done = events[types.index("done")]
    assert payload["request_id"] == done.get("request_id")
    assert payload["session_id"]
    assert any(
        f["path"] == "note.txt" and f["state"] == "added"
        for f in payload["files"]
    )

    assert stores, "the turn should have created a session store"
    manifest = stores[0].manifest(0)
    assert manifest is not None
    assert any(c.path == "note.txt" for c in manifest.files)


def _scrub_runtime_tokens(value):
    """Replace per-run system tokens (checkpoint ids) in nested structures.

    Tool messages carry a JSON blob as a string; parsing it first avoids the
    quoting/escaping mismatch that a flat regex would hit.
    """
    if isinstance(value, dict):
        return {
            key: ("[cid]" if key == "checkpoint_id" else _scrub_runtime_tokens(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_scrub_runtime_tokens(item) for item in value]
    if isinstance(value, str) and '"checkpoint_id"' in value:
        try:
            parsed = json.loads(value)
        except ValueError:
            # Strings that contain a JSON blob plus trailing context text still
            # need the same scrub; tolerate one or two levels of escaping.
            return re.sub(
                r'\\?"checkpoint_id\\?"\s*:\s*\\?"[0-9a-f]+\\?"',
                '"checkpoint_id": "[cid]"',
                value,
            )
        return json.dumps(_scrub_runtime_tokens(parsed), sort_keys=True, ensure_ascii=False)
    return value


def _norm_request(value) -> str:
    """Normalize request payloads for parity comparison.

    Drops wall-clock text and per-run system tokens (checkpoint ids) that are
    produced by file checkpointing itself, not by the turn-change ledger.
    """
    text = json.dumps(_scrub_runtime_tokens(value), sort_keys=True, ensure_ascii=False)
    return re.sub(r"<message_time>[^<]*</message_time>", "[time]", text)


def test_ledger_adds_no_model_request_differences(tmp_path, monkeypatch):
    """v3 hard constraint: the ledger never enters model requests."""
    replies = [call("write_file", {"path": "a.txt", "content": "x\n"}), DONE]
    agent_on, _ = make_turn_agent(tmp_path, list(replies), monkeypatch)
    run(collect_stream(agent_on, Msg(content=[ContentBlock.text("do it")], id="m-parity-on")))

    agent_off, _ = make_turn_agent(tmp_path, list(replies), monkeypatch)
    monkeypatch.setattr(ReActAgent, "_make_turn_change_store", lambda self, key: None)
    (tmp_path / "a.txt").unlink(missing_ok=True)  # parity run B starts clean
    run(collect_stream(agent_off, Msg(content=[ContentBlock.text("do it")], id="m-parity-off")))

    requests_on = agent_on.llm.requests
    requests_off = agent_off.llm.requests
    assert len(requests_on) == len(requests_off) >= 2
    for on, off in zip(requests_on, requests_off):
        assert _norm_request(on.get("tools")) == _norm_request(off.get("tools"))
        assert _norm_request(on.get("messages")) == _norm_request(off.get("messages"))


def test_edits_reverted_to_original_are_not_listed(tmp_path, monkeypatch):
    """A net-zero change (edited then reverted) produces no ledger event."""
    (tmp_path / "note.txt").write_text("v1\n", encoding="utf-8")
    agent, stores = make_turn_agent(tmp_path, [
        call("edit_file", {"path": "note.txt", "old": "v1", "new": "v2"}, call_id="c1"),
        call("edit_file", {"path": "note.txt", "old": "v2", "new": "v1"}, call_id="c2"),
        DONE,
    ], monkeypatch)
    events = run(collect_stream(agent, Msg(content=[ContentBlock.text("tweak it")], id="m-revert")))
    types = [e["type"] for e in events]
    assert "done" in types
    assert "turn_changes" not in types, f"reverted edit must not be listed; seen={types}"
    assert stores
    assert stores[0].manifest(0) is None


def test_tracked_real_command_sequence_reverting_to_original_is_not_listed(tmp_path, monkeypatch):
    """R5: a real command sequence (git snapshots + real subprocess commands)
    that returns the file to its turn-start state must not produce an event."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "sample.py").write_text("value = 0\n", encoding="utf-8")
    (repo / "codegen_step.py").write_text(
        "import pathlib\nimport sys\n"
        "pathlib.Path('sample.py').write_text(f'value = {sys.argv[1]}\\n')\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.test"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "test"], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "init"], check=True)
    (repo / "sample.py").write_text("value = 1\n", encoding="utf-8")  # 回合开始时已脏

    def run_command(command: str) -> str:
        completed = subprocess.run(
            command, shell=True, cwd=str(repo), capture_output=True, text=True, timeout=30
        )
        return f"exit={completed.returncode}\n{completed.stdout}{completed.stderr}".strip()

    registry = ToolRegistry()
    register_file_tools(registry, workdir=str(repo))
    registry.register(ToolDef(
        "execute_shell",
        "Run a shell command",
        {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
        run_command,
        risk="execute",
        group="code",
    ))
    registry.yolo = True
    script = sys.executable.replace("\\", "/")
    agent = ReActAgent(
        "tracked-agent",
        ScriptedLLM([
            call("execute_shell", {"command": f"{script} codegen_step.py 2"}, call_id="c1"),
            call("execute_shell", {"command": f"{script} codegen_step.py 1"}, call_id="c2"),
            DONE,
        ]),
        registry,
        timing_log_enabled=False,
        query_profile_enabled=False,
        max_iterations=8,
    )
    agent._sandbox = SimpleNamespace(workdir=str(repo))
    stores = []

    def fake_make_store(self, session_key):
        store = tcs.TurnChangeStore(repo, session_key, root=tmp_path / "tc-root")
        stores.append(store)
        return store

    monkeypatch.setattr(ReActAgent, "_make_turn_change_store", fake_make_store)

    events = run(collect_stream(agent, Msg(content=[ContentBlock.text("regenerate")], id="m-tracked-revert")))
    types = [e["type"] for e in events]
    assert "done" in types
    assert "turn_changes" not in types, f"reverted command sequence must not be listed; seen={types}"
    assert (repo / "sample.py").read_text(encoding="utf-8") == "value = 1\n"
    assert stores, "the turn should have created a session store"
    assert stores[0].manifest(0) is None


def test_sessions_are_isolated_in_the_ledger(tmp_path, monkeypatch):
    """Turns from a different session never leak into another session's ledger."""
    (tmp_path / "sessions").mkdir(exist_ok=True)
    agent, stores = make_turn_agent(tmp_path, [
        call("write_file", {"path": "s1.txt", "content": "one\n"}, call_id="k1"),
        DONE,
        call("write_file", {"path": "s2.txt", "content": "two\n"}, call_id="k2"),
        DONE,
    ], monkeypatch)
    run(collect_stream(agent, Msg(content=[ContentBlock.text("first")], id="m-s1")))
    agent.context.set_session(str(tmp_path / "sessions" / "other.json"))
    run(collect_stream(agent, Msg(content=[ContentBlock.text("second")], id="m-s2")))

    assert len(stores) == 2
    assert stores[0].session_id != stores[1].session_id
    manifest = stores[1].manifest(0)
    assert manifest is not None
    paths = [c.path for c in manifest.files]
    assert "s2.txt" in paths
    assert "s1.txt" not in paths


def test_aborted_stream_still_seals_the_turn(tmp_path, monkeypatch):
    """Closing the stream early (aclose) must still persist the sealed turn."""
    agent, stores = make_turn_agent(tmp_path, [
        call("write_file", {"path": "draft.txt", "content": "kept\n"}),
        DONE,
    ], monkeypatch)
    msg = Msg(content=[ContentBlock.text("write a draft")], id="m-aclose")

    async def scenario():
        events = []
        stream = agent.reply_stream(msg)
        async for event in stream:
            events.append(event)
            if event["type"] == "tool_result":
                break
        await stream.aclose()
        return events

    events = run(scenario())
    types = [e["type"] for e in events]
    assert "tool_result" in types
    assert "turn_changes" not in types
    assert "done" not in types
    assert stores, "store should exist"
    manifest = stores[0].manifest(0)
    assert manifest is not None, "the aclose fallback must persist the sealed turn"
    # 中止的回合：写入未被读取确认 → 进未知区并保留停止原因（review F5）
    assert manifest.files == []
    assert [change.path for change in manifest.unknown] == ["draft.txt"]
    assert manifest.unknown[0].reason == "cancelled"
    assert manifest.unknown[0].after_state == tcs.SIDE_UNCAPTURED


def test_model_error_path_still_publishes_once(tmp_path, monkeypatch):
    """A provider error after a completed write still publishes changes once."""
    agent, stores = make_turn_agent(tmp_path, [
        call("write_file", {"path": "saved.txt", "content": "data\n"}),
        LLMResponseError("stream_closed", "connection dropped"),
    ], monkeypatch)
    events = run(collect_stream(agent, Msg(content=[ContentBlock.text("save it")], id="m-err")))
    types = [e["type"] for e in events]
    assert types.count("turn_changes") == 1, f"seen={types}"
    assert "error" in types, f"seen={types}"
    assert types.index("turn_changes") < types.index("error")
    payload = events[types.index("turn_changes")]
    assert any(
        f["path"] == "saved.txt" and f["state"] == "added" for f in payload["files"]
    )
    manifest = stores[0].manifest(0)
    assert manifest is not None


def test_many_edits_net_out_correctly(tmp_path, monkeypatch):
    """51+ edits in a single turn still produce an exact net diff."""
    (tmp_path / "doc.txt").write_text(
        "".join(f"line {i}\n" for i in range(60)), encoding="utf-8"
    )
    replies = [
        call("edit_file", {"path": "doc.txt", "old": f"line {i}\n", "new": f"LINE {i}\n"},
             call_id=f"e{i}")
        for i in range(51)
    ]
    replies.append(DONE)
    agent, stores = make_turn_agent(tmp_path, replies, monkeypatch, max_iterations=60)
    events = run(collect_stream(agent, Msg(content=[ContentBlock.text("bulk edit")], id="m-many")))
    types = [e["type"] for e in events]
    assert "turn_changes" in types, f"seen={types}"
    payload = events[types.index("turn_changes")]
    entry = next(f for f in payload["files"] if f["path"] == "doc.txt")
    assert entry["state"] == "modified"
    assert entry["added"] == 51
    assert entry["removed"] == 51


def test_turn_store_scope_is_context_local(tmp_path):
    """Scope binding is context-local: nesting restores, fresh contexts reset.

    A bare asyncio task copies the caller's context, so it inherits whatever
    binding is active; the delegate execution entry therefore detaches the
    ledger explicitly instead of relying on context isolation (review R3, see
    the delegate-chain tests below).
    """
    store = tcs.TurnChangeStore(tmp_path, "sess-scope", root=tmp_path / "tc-root")
    store.begin_turn("req-scope")
    try:
        assert tcs.current_turn_change_store() is None

        async def scenario():
            assert tcs.current_turn_change_store() is None

            async def child():
                return tcs.current_turn_change_store()

            with tcs.turn_store_scope(store):
                assert tcs.current_turn_change_store() is store
                # A bare child task copies the active context; this inherited
                # binding is exactly what the delegate entry must detach, so
                # the ledger tests below run the real chain instead (R3).
                assert await asyncio.create_task(child()) is store

            assert await asyncio.create_task(child()) is None
            return True

        assert run(scenario()) is True
        assert tcs.current_turn_change_store() is None
    finally:
        store.seal()


def test_turn_budget_exhaustion_still_publishes_once(tmp_path, monkeypatch):
    """Budget exhaustion mid-turn publishes the changes made so far, once."""
    agent, stores = make_turn_agent(
        tmp_path,
        [
            call("write_file", {"path": "budget.txt", "content": "b\n"}),
            DONE,
        ],
        monkeypatch,
        turn_timeout_seconds=0.3,
        llm_delays={2: 1.0},
    )
    events = run(collect_stream(
        agent, Msg(content=[ContentBlock.text("slow turn")], id="m-budget")
    ))
    types = [e["type"] for e in events]
    assert types.count("turn_changes") == 1, f"seen={types}"
    assert "error" in types, f"seen={types}"
    assert types.index("turn_changes") < types.index("error")
    error = events[types.index("error")]
    assert error.get("code") == "turn_budget_exhausted", error
    payload = events[types.index("turn_changes")]
    assert any(
        f["path"] == "budget.txt" and f["state"] == "added" for f in payload["files"]
    )


def test_real_task_cancellation_keeps_the_write_and_degrades_counts(tmp_path, monkeypatch):
    """R6：真实 asyncio 取消——保留已完成写入，取消后不再读取 after、计数降级."""
    agent, stores = make_turn_agent(
        tmp_path,
        [
            call("write_file", {"path": "cancel.txt", "content": "kept\n"}),
            DONE,
        ],
        monkeypatch,
        llm_delays={2: 30},
    )

    async def scenario():
        events = []
        written = asyncio.Event()

        async def consume():
            async for event in agent.reply_stream(Msg(content=[ContentBlock.text("write")], id="m-cancel")):
                events.append(event)
                if event["type"] == "tool_result":
                    written.set()

        task = asyncio.create_task(consume())
        await asyncio.wait_for(written.wait(), 3)
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.wait_for(task, 3)
        return events

    events = run(scenario())
    types = [event["type"] for event in events]
    assert types.count("turn_changes") == 1, f"seen={types}"
    assert "done" in types
    assert any(event.get("cancelled") for event in events if event["type"] == "error")

    manifest = stores[0].manifest()
    assert manifest is not None
    # 取消后不再读取 after：条目进未知区，保留 before 与取消原因（review F5）
    assert manifest.files == []
    change = manifest.unknown[0]
    assert change.path == "cancel.txt"
    assert change.state == tcs.STATE_UNKNOWN
    assert change.before_state == tcs.SIDE_ABSENT
    assert change.after_state == tcs.SIDE_UNCAPTURED  # 取消后不再读取 after
    assert (change.compare, change.added, change.removed) == (tcs.COMPARE_NONE, None, None)
    assert change.reason == "cancelled"
    assert (tmp_path / "cancel.txt").read_text(encoding="utf-8") == "kept\n"


def test_pending_task_cancel_during_seal_stops_new_source_reads(tmp_path, monkeypatch):
    """R6：正常收尾（同步执行）期间挂起的 asyncio 取消会停止新的 after 读取."""
    agent, stores = make_turn_agent(
        tmp_path,
        [
            call("write_file", {"path": "a.txt", "content": "a\n"}, call_id="c1"),
            call("write_file", {"path": "b.txt", "content": "b\n"}, call_id="c2"),
            call("write_file", {"path": "c.txt", "content": "c\n"}, call_id="c3"),
            DONE,
        ],
        monkeypatch,
    )

    reads = []
    original = tcs._read_bytes_bounded

    def cancel_on_first_read(path, limit):
        reads.append(Path(path).name)
        if len(reads) == 1:
            task = asyncio.current_task()
            assert task is not None
            task.cancel()  # 模拟收尾同步执行期间挂起的取消请求
        return original(path, limit)

    monkeypatch.setattr(tcs, "_read_bytes_bounded", cancel_on_first_read)

    async def scenario():
        events = []
        try:
            async for event in agent.reply_stream(
                Msg(content=[ContentBlock.text("three files")], id="m-sync-cancel")
            ):
                events.append(event)
        except asyncio.CancelledError:
            pass
        return events

    try:
        run(scenario())
    except asyncio.CancelledError:
        # 挂起的取消可能在事件循环收尾时才被投递；seal 已同步完成
        pass

    assert reads == ["a.txt"], f"pending cancel must stop new reads; reads={reads}"
    manifest = stores[0].manifest()
    assert manifest is not None
    # a.txt 已读取：两侧已确定 → 主清单降级保留；b/c 未读 → 未知区（review F5）
    by_path = {change.path: change for change in manifest.files}
    assert set(by_path) == {"a.txt"}
    assert by_path["a.txt"].state == tcs.STATE_ADDED
    assert by_path["a.txt"].reason == "cancelled"
    assert (
        by_path["a.txt"].compare, by_path["a.txt"].added, by_path["a.txt"].removed
    ) == (tcs.COMPARE_NONE, None, None)
    unread = {change.path: change for change in manifest.unknown}
    assert set(unread) == {"b.txt", "c.txt"}
    assert all(change.reason == "cancelled" for change in unread.values())
    assert all(change.state == tcs.STATE_UNKNOWN for change in unread.values())
    assert all(change.after_state == tcs.SIDE_UNCAPTURED for change in unread.values())


def test_real_cancel_during_the_final_seal_reports_cancelled(tmp_path, monkeypatch):
    """G2：收尾期间的排期取消走取消终态（台账一次 + cancelled error + done）."""
    agent, stores = make_turn_agent(tmp_path, [
        call("write_file", {"path": "a.txt", "content": "a\n"}, call_id="a"),
        call("write_file", {"path": "b.txt", "content": "b\n"}, call_id="b"),
        DONE,
    ], monkeypatch)
    original = tcs._read_bytes_bounded
    state = {"cancel_requested": False}

    def read_and_schedule_cancel(path, limit):
        if not state["cancel_requested"]:
            task = asyncio.current_task()

            def request_cancel() -> None:
                state["cancel_requested"] = True
                task.cancel()

            asyncio.get_running_loop().call_soon(request_cancel)
        return original(path, limit)

    monkeypatch.setattr(tcs, "_read_bytes_bounded", read_and_schedule_cancel)

    async def scenario():
        events = []
        async for event in agent.reply_stream(
            Msg(content=[ContentBlock.text("write two files")], id="m-seal-cancel")
        ):
            events.append(event)
        return events

    events = run(scenario())
    types = [event["type"] for event in events]

    assert state["cancel_requested"] is True
    assert types.count("turn_changes") == 1  # publish-once
    assert any(event.get("cancelled") for event in events if event["type"] == "error"), events
    assert types[-1] == "done"
    payload = next(event for event in events if event["type"] == "turn_changes")
    assert payload["unknown_count"] >= 1  # 未读条目仍交付为未知区


def test_cancel_during_reply_seal_propagates_to_the_caller(tmp_path, monkeypatch):
    """G2：非流式 reply() 收尾期间的取消对调用者可见（不被吞掉）."""
    agent, _stores = make_turn_agent(tmp_path, [
        call("write_file", {"path": "note.txt", "content": "kept\n"}),
        DONE,
    ], monkeypatch)
    original = tcs._read_bytes_bounded
    state = {"scheduled": False}

    def read_and_schedule_cancel(path, limit):
        if not state["scheduled"]:
            state["scheduled"] = True
            task = asyncio.current_task()
            asyncio.get_running_loop().call_soon(task.cancel)
        return original(path, limit)

    monkeypatch.setattr(tcs, "_read_bytes_bounded", read_and_schedule_cancel)

    async def scenario():
        try:
            await agent.reply(Msg(content=[ContentBlock.text("write a note")], id="m-reply-cancel"))
        except asyncio.CancelledError:
            return "cancelled"
        return "completed"

    assert run(scenario()) == "cancelled"


def test_non_streaming_reply_records_without_events(tmp_path, monkeypatch):
    """reply() runs the same bookkeeping with no event channel to publish on."""
    agent, stores = make_turn_agent(tmp_path, [
        call("write_file", {"path": "quiet.txt", "content": "q\n"}),
        DONE,
    ], monkeypatch)
    result = run(agent.reply(Msg(content=[ContentBlock.text("quiet")], id="m-quiet")))
    assert result is not None
    manifest = stores[0].manifest(0)
    assert manifest is not None
    assert any(c.path == "quiet.txt" for c in manifest.files)


def test_previous_turn_snapshots_are_immutable(tmp_path, monkeypatch):
    """Later edits must not rewrite a previous turn's ledger snapshots."""
    (tmp_path / "hist.txt").write_text("a\n", encoding="utf-8", newline="\n")
    agent, stores = make_turn_agent(tmp_path, [
        call("edit_file", {"path": "hist.txt", "old": "a", "new": "b"}, call_id="h1"),
        DONE,
        call("edit_file", {"path": "hist.txt", "old": "b", "new": "c"}, call_id="h2"),
        DONE,
    ], monkeypatch)
    run(collect_stream(agent, Msg(content=[ContentBlock.text("turn one")], id="m-hist-1")))

    session_dir = tmp_path / "tc-root" / "default"
    turn1_manifest = (session_dir / "turn-1" / "manifest.json").read_bytes()
    assert (session_dir / "turn-1" / "before.0.bin").read_bytes() == b"a\n"
    assert (session_dir / "turn-1" / "after.0.bin").read_bytes() == b"b\n"

    run(collect_stream(agent, Msg(content=[ContentBlock.text("turn two")], id="m-hist-2")))

    # The first turn's artifacts stay byte-identical after the second turn.
    assert (session_dir / "turn-1" / "manifest.json").read_bytes() == turn1_manifest
    assert (session_dir / "turn-1" / "before.0.bin").read_bytes() == b"a\n"
    assert (session_dir / "turn-1" / "after.0.bin").read_bytes() == b"b\n"
    # The second turn has its own, newer snapshot.
    assert (session_dir / "turn-2" / "before.0.bin").read_bytes() == b"b\n"
    assert (session_dir / "turn-2" / "after.0.bin").read_bytes() == b"c\n"

    assert stores[0].manifest(0) is not None
    assert stores[0].manifest(1) is not None


# ---------------------------------------------------------------------------
# Slice 4 (review R3): the real delegate chain never feeds the parent ledger
# ---------------------------------------------------------------------------


class DelegateScriptedLLM:
    """Scripted chat LLM for a real delegate run (shape: tests/test_delegate)."""

    def __init__(self, responses, *, gate=None):
        self.responses = list(responses)
        self.gate = gate

    async def chat(self, **kwargs):
        del kwargs
        if self.gate is not None:
            await self.gate.wait()
        return self.responses.pop(0)


def worker_edit_call(call_id, path, old, new):
    """A scripted worker tool call that edits one real workspace file."""
    return {
        "content": "",
        "tool_calls": [{
            "id": call_id,
            "name": "edit_file",
            "arguments": json.dumps({"path": path, "old": old, "new": new}),
        }],
    }


def make_delegate_registry(tmp_path, monkeypatch, responses, *, gate=None, sandbox=None):
    """Register the real delegate tools behind a test-scoped ProcessManager."""
    manager = ProcessManager(artifact_dir=tmp_path / "tc-processes")
    monkeypatch.setattr(delegate_tools, "_sub_processes", manager)
    registry = ToolRegistry()
    register_file_tools(registry, workdir=str(tmp_path))
    registry.yolo = True
    llm = DelegateScriptedLLM(responses, gate=gate)
    register_delegate_tools(registry, llm_getter=lambda: llm, sandbox=sandbox)
    return registry, manager, llm


def test_delegate_child_edits_stay_out_of_parent_ledger(tmp_path, monkeypatch):
    """R3: a real shared-workspace delegate run must not feed the parent ledger."""
    target = tmp_path / "sample.py"
    target.write_text("old\n", encoding="utf-8")
    registry, _manager, _llm = make_delegate_registry(
        tmp_path,
        monkeypatch,
        [
            worker_edit_call("child-edit", "sample.py", "old", "new"),
            {"content": "child done", "tool_calls": []},
        ],
    )
    store = make_store(tmp_path, "sess-delegate")

    async def scenario():
        store.begin_turn("req-delegate")
        with tcs.turn_store_scope(store):
            result = await registry.execute(
                "delegate_task",
                {
                    "goal": "update sample value",
                    "mode": "worker",
                    "max_turns": 2,
                    "timeout": 10,
                },
                task_id="r3-shared",
            )
            # Ownership stays pinned to the parent turn during the child run.
            assert tcs.current_turn_change_store() is store
        return json.loads(result["output"])

    payload = run(scenario())
    assert payload["worker_status"] == "completed"
    assert target.read_text(encoding="utf-8") == "new\n"
    # The child really edited the file, yet the parent turn records nothing.
    assert store.seal() is None


def test_worktree_delegate_child_edits_stay_out_of_parent_ledger(tmp_path, monkeypatch):
    """R3: worktree isolation must not re-attach the child to the parent ledger."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    note = worktree / "note.txt"
    note.write_text("old\n", encoding="utf-8")
    monkeypatch.setattr(delegate_tools, "_create_detached_worktree", lambda _sandbox: worktree)
    monkeypatch.setattr(
        delegate_tools, "_finalize_detached_worktree", lambda _root, _worktree: False
    )
    registry, _manager, _llm = make_delegate_registry(
        tmp_path,
        monkeypatch,
        [
            # The child edits the file that lives inside the isolated worktree.
            worker_edit_call("wt-edit", str(note), "old", "new"),
            {"content": "worktree done", "tool_calls": []},
        ],
        sandbox=SimpleNamespace(workdir=str(tmp_path)),
    )
    store = make_store(tmp_path, "sess-delegate-wt")

    async def scenario():
        store.begin_turn("req-delegate-wt")
        with tcs.turn_store_scope(store):
            result = await registry.execute(
                "delegate_task",
                {
                    "goal": "edit note in worktree",
                    "mode": "worker",
                    "isolation": "worktree",
                    "max_turns": 2,
                    "timeout": 10,
                },
                task_id="r3-worktree",
            )
        return json.loads(result["output"])

    payload = run(scenario())
    assert payload["worker_status"] == "completed"
    assert note.read_text(encoding="utf-8") == "new\n"
    assert store.seal() is None


def test_late_background_delegate_does_not_pollute_next_turn(tmp_path, monkeypatch):
    """R3: a child finishing after the next turn begins must not write into it."""
    target = tmp_path / "late.txt"
    target.write_text("old\n", encoding="utf-8")
    store = make_store(tmp_path, "sess-delegate-late")

    async def scenario():
        gate = asyncio.Event()
        registry, manager, _llm = make_delegate_registry(
            tmp_path,
            monkeypatch,
            [
                worker_edit_call("late-edit", "late.txt", "old", "new"),
                {"content": "late done", "tool_calls": []},
            ],
            gate=gate,
        )

        store.begin_turn("req-1")
        with tcs.turn_store_scope(store):
            raw = await registry.execute(
                "delegate_task",
                {
                    "goal": "late edit",
                    "mode": "worker",
                    "max_turns": 2,
                    "timeout": 10,
                    "background": True,
                },
                task_id="r3-late",
            )
            assert tcs.current_turn_change_store() is store
        payload = json.loads(raw["output"])
        process = manager.get(payload["process_id"])
        # Turn 1 seals while the gated child has not touched any file yet.
        assert store.seal() is None

        store.begin_turn("req-2")
        gate.set()
        assert await manager.wait(process, 10_000)
        # The child finished during turn 2; its edit must not join this turn.
        return store.seal()

    manifest = run(scenario())
    assert target.read_text(encoding="utf-8") == "new\n"
    assert manifest is None


# ---------------------------------------------------------------------------
# Cross-root identity (M2 review P2): the file-tool root and the ledger
# sandbox root may legitimately differ (installation root vs user workspace);
# candidate notes must still reuse the capture-chain identity.
# ---------------------------------------------------------------------------

def make_cross_root_agent(tmp_path, replies):
    """File tools root = tmp/files; ledger sandbox root = tmp/sandbox."""
    files = tmp_path / "files"
    sandbox = tmp_path / "sandbox"
    files.mkdir()
    sandbox.mkdir()
    registry = ToolRegistry()
    register_file_tools(registry, workdir=str(files))
    registry.yolo = True
    agent = ReActAgent(
        "tc-cross-root", ScriptedLLM(replies), registry,
        timing_log_enabled=False, query_profile_enabled=False, max_iterations=5,
    )
    agent._sandbox = SimpleNamespace(workdir=sandbox)
    return agent, files, sandbox


def test_cross_root_create_reuses_the_capture_identity(tmp_path, monkeypatch):
    """P2：根不同时新建文件，不得同时出现 confirmed 与 phantom unknown."""
    agent, files, _sandbox = make_cross_root_agent(
        tmp_path, [call("write_file", {"path": "a.txt", "content": "hello\n"})]
    )
    monkeypatch.chdir(files)
    events = run(collect_stream(agent, Msg(content=[ContentBlock.text("create a.txt")], id="p2-create")))

    payloads = [event for event in events if event["type"] == "turn_changes"]
    assert len(payloads) == 1, events
    assert [item["path"] for item in payloads[0]["files"]] == ["a.txt"]
    assert payloads[0]["unknown_count"] == 0, payloads[0]
    assert (files / "a.txt").read_text(encoding="utf-8") == "hello\n"


def test_cross_root_revert_publishes_no_event(tmp_path, monkeypatch):
    """P2：根不同时改回原样，必须维持无净改动→不发布事件的语义."""
    agent, files, _sandbox = make_cross_root_agent(
        tmp_path,
        [
            call("edit_file", {"path": "a.txt", "old": "original", "new": "changed"}, call_id="p2-e1"),
            call("edit_file", {"path": "a.txt", "old": "changed", "new": "original"}, call_id="p2-e2"),
        ],
    )
    (files / "a.txt").write_text("original\n", encoding="utf-8")
    monkeypatch.chdir(files)
    events = run(collect_stream(agent, Msg(content=[ContentBlock.text("edit then revert")], id="p2-revert")))

    assert [event for event in events if event["type"] == "turn_changes"] == [], events
    assert (files / "a.txt").read_text(encoding="utf-8") == "original\n"


def test_same_root_create_has_no_unknown_paths(tmp_path, monkeypatch):
    """同根对照：新建仍是单条 confirmed、无 unknown（修复不得回归）."""
    agent, stores = make_turn_agent(
        tmp_path, [call("write_file", {"path": "a.txt", "content": "hello\n"})], monkeypatch
    )
    monkeypatch.chdir(tmp_path)
    events = run(collect_stream(agent, Msg(content=[ContentBlock.text("create a.txt")], id="same-root")))

    payloads = [event for event in events if event["type"] == "turn_changes"]
    assert len(payloads) == 1, events
    assert payloads[0]["unknown_count"] == 0
    assert [item["path"] for item in payloads[0]["files"]] == ["a.txt"]
    assert stores and stores[0].manifest(0) is not None


# ---------------------------------------------------------------------------
# Raw-path identity shapes (M2 P2 follow-up): the ledger must resolve the raw
# path (case, absolute root and hidden prefix preserved) through the file
# policy, while workflow classification keeps the normalized variant.
# ---------------------------------------------------------------------------

def _raw_path_case(files, form):
    """The three raw path shapes models emit: absolute, hidden, mixed case."""
    if form == "absolute":
        return str(files / "abs.txt"), "abs.txt"
    if form == "hidden":
        return ".memo.txt", ".memo.txt"
    if form == "mixed":
        return "MixedCase.txt", "MixedCase.txt"
    raise AssertionError(form)


@pytest.mark.parametrize("path_form", ["absolute", "hidden", "mixed"])
def test_cross_root_raw_path_forms_create_no_unknown(tmp_path, monkeypatch, path_form):
    """P2：绝对/隐藏/大小写混排路径的新建，不得产生 phantom unknown."""
    arg, name = _raw_path_case(tmp_path / "files", path_form)
    agent, files, _sandbox = make_cross_root_agent(
        tmp_path, [call("write_file", {"path": arg, "content": "hello\n"})]
    )
    monkeypatch.chdir(files)
    events = run(collect_stream(agent, Msg(content=[ContentBlock.text("create raw path")], id=f"p2-raw-create-{path_form}")))

    payloads = [event for event in events if event["type"] == "turn_changes"]
    assert len(payloads) == 1, events
    assert [item["path"] for item in payloads[0]["files"]] == [name]
    assert payloads[0]["unknown_count"] == 0, payloads[0]
    assert (files / name).read_text(encoding="utf-8") == "hello\n"


@pytest.mark.parametrize("path_form", ["absolute", "hidden", "mixed"])
def test_cross_root_raw_path_forms_revert_no_event(tmp_path, monkeypatch, path_form):
    """P2：绝对/隐藏/大小写混排路径改回原样，不得发布事件."""
    arg, name = _raw_path_case(tmp_path / "files", path_form)
    agent, files, _sandbox = make_cross_root_agent(
        tmp_path,
        [
            call("edit_file", {"path": arg, "old": "original", "new": "changed"}, call_id=f"p2-raw-{path_form}-1"),
            call("edit_file", {"path": arg, "old": "changed", "new": "original"}, call_id=f"p2-raw-{path_form}-2"),
        ],
    )
    (files / name).write_text("original\n", encoding="utf-8")
    monkeypatch.chdir(files)
    events = run(collect_stream(agent, Msg(content=[ContentBlock.text("edit raw path then revert")], id=f"p2-raw-revert-{path_form}")))

    assert [event for event in events if event["type"] == "turn_changes"] == [], events
    assert (files / name).read_text(encoding="utf-8") == "original\n"


def test_restored_session_after_normal_shutdown_reports_unavailable(tmp_path):
    """真实完成 → 正常退出（快照目录被清理）→ 恢复会话：仍保守报“暂不可用”（R5 adjacent）."""
    session_path = str(tmp_path / "sessions" / "restore.json")
    agent = ReActAgent(
        "tc-agent",
        ScriptedLLM([DONE]),
        ToolRegistry(),
        timing_log_enabled=False,
        query_profile_enabled=False,
        max_iterations=2,
    )
    agent._sandbox = SimpleNamespace(workdir=tmp_path)
    agent.context.set_session(session_path)

    event_types = [
        event["type"]
        for event in run(
            collect_stream(agent, Msg(content=[ContentBlock.text("hello")], id="msg-1"))
        )
    ]
    assert "done" in event_types

    store = agent.turn_change_store()
    assert store is not None
    index = store.completed_turns()
    assert index.ok and len(index.records) == 1

    agent.end_session("shutdown")
    assert not store.session_dir.exists()  # 正常退出的清理策略保持不变

    restored = ReActAgent(
        "tc-agent",
        ScriptedLLM([]),
        ToolRegistry(),
        timing_log_enabled=False,
        query_profile_enabled=False,
        max_iterations=2,
    )
    restored._sandbox = SimpleNamespace(workdir=tmp_path)
    restored.context.set_session(session_path)
    assert restored.context.load() is True
    assert [msg.get("role") for msg in restored.context.messages] == ["user", "assistant"]

    output, error = execute_changes_command(restored, "")

    assert error == ""
    assert output == changes_view.INDEX_UNAVAILABLE
