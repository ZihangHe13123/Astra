"""Backend stdout-order test for the turn-change ledger (M1, slice 3).

Runs the real backend as a subprocess against a scripted OpenAI-compatible
SSE server and asserts the emitted JSON lines keep the contract order:
``turn_changes`` strictly before the closing ``done``.

Harness pattern mirrors tests/test_backend_task_protocol.py
(_QuestionOpenAIHandler / _start_question_protocol_backend), including the
``workdir`` isolation: the backend process runs with cwd set to a tmp workdir
(PYTHONPATH back to the repo), so file tools and the turn-change store both
live under tmp_path.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Empty, Queue

from agent.runtime import turn_change_store as tcs
from test_backend_task_protocol import backend_environment


class _WriteThenDoneHandler(BaseHTTPRequestHandler):
    """Call 1 -> write_file tool_call; call 2 (after tool result) -> final text."""

    def log_message(self, format, *args):
        return

    def _json(self, payload: dict):
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _sse(self, chunks: list[dict]):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        for chunk in chunks:
            self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def do_GET(self):
        if self.path.endswith("/models"):
            self._json({
                "object": "list",
                "data": [{"id": "Qwen3.6-35B-A3B", "meta": {"n_ctx": 131072}}],
            })
        else:
            self.send_error(404)

    def do_POST(self):
        if not self.path.endswith("/chat/completions"):
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        self.server.requests.append(payload)
        call_number = len(self.server.requests)
        base = {
            "id": f"chatcmpl-turn-changes-{call_number}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": "Qwen3.6-35B-A3B",
        }
        has_tool_result = any(
            isinstance(message, dict) and message.get("role") == "tool"
            for message in (payload.get("messages") or [])
        )
        if not has_tool_result:
            arguments = json.dumps({
                "path": self.server.target_name,
                "content": "hello turn-changes\n",
            })
            self._sse([
                {
                    **base,
                    "choices": [{
                        "index": 0,
                        "delta": {
                            "role": "assistant",
                            "tool_calls": [{
                                "index": 0,
                                "id": "call-turn-changes-write",
                                "type": "function",
                                "function": {"name": "write_file", "arguments": arguments},
                            }],
                        },
                        "finish_reason": None,
                    }],
                },
                {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
            ])
        else:
            self._sse([
                {
                    **base,
                    "choices": [{
                        "index": 0,
                        "delta": {"role": "assistant", "content": "note written"},
                        "finish_reason": None,
                    }],
                },
                {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            ])


def _start_backend(tmp_path: Path, server_port: int, session_name: str, workdir: Path):
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps({"selected_model": "Qwen3.6-35B-A3B"}), encoding="utf-8"
    )
    models = tmp_path / "models.yaml"
    models.write_text(
        "\n".join([
            "version: 1",
            "providers:",
            "  test:",
            "    label: Test provider",
            "    provider: openai-compatible",
            f"    base_url: http://127.0.0.1:{server_port}/v1",
            "    api_key_env: ''",
            "    context_limit: 131072",
            "    capabilities: [tools, streaming]",
            "models:",
            "  Qwen3.6-35B-A3B:",
            "    provider: openai-compatible",
            f"    base_url: http://127.0.0.1:{server_port}/v1",
            "    api_key_env: ''",
            "    context_limit: 131072",
            "    capabilities: [tools, streaming]",
            "",
        ]),
        encoding="utf-8",
    )
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    repo_root = Path(__file__).resolve().parents[1]
    env = backend_environment()
    env.update({
        "AGENT_SETTINGS_PATH": str(settings),
        "AGENT_SESSION_DIR": str(sessions_dir),
        "AGENT_TASK_DB": str(tmp_path / "tasks.db"),
        "ASTRA_APPROVAL_DB": str(tmp_path / "approvals.db"),
        "ASTRA_EVENT_DB": str(tmp_path / "events.db"),
        "AGENT_MEMORY_PATH": str(tmp_path / "memory.db"),
        "AGENT_LEARNING_PATH": str(tmp_path / "learning.db"),
        "AGENT_SKILLS_PATH": str(tmp_path / "skills"),
        "AGENT_MODELS_FILE": str(models),
        "AGENT_USER_MODELS_FILE": str(tmp_path / "missing-models.yaml"),
        "LEARNING_REVIEW_AUTO": "0",
        "AGENT_SESSION": session_name,
        "SANDBOX_DOCKER": "false",
        # The parent environment may carry SANDBOX_WORKDIR (user shell); the
        # workdir must match the backend cwd so file tools and the turn-change
        # store share one workspace.
        "SANDBOX_WORKDIR": str(workdir),
        "AGENT_MCP_CONFIG": str(tmp_path / "missing-mcp.json"),
        "PYTHONUNBUFFERED": "1",
        "PYTHONPATH": os.pathsep.join(
            filter(None, [str(repo_root), env.get("PYTHONPATH", "")])
        ),
    })
    proc = subprocess.Popen(
        [sys.executable, "-m", "agent.cli.backend"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(workdir),
        env=env,
        bufsize=1,
    )
    assert proc.stdin is not None
    assert proc.stdout is not None
    events: Queue[dict] = Queue()
    seen: list[dict] = []
    stderr_tail: deque[str] = deque(maxlen=64)

    def read_stderr():
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_tail.append(line[-4096:])

    stderr_reader = threading.Thread(target=read_stderr, daemon=True)
    stderr_reader.start()

    def read_events():
        assert proc.stdout is not None
        for line in proc.stdout:
            try:
                events.put(json.loads(line))
            except json.JSONDecodeError:
                pass

    threading.Thread(target=read_events, daemon=True).start()

    def wait_for(predicate, timeout: float = 30) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                stderr_reader.join(timeout=1)
                raise AssertionError(
                    f"Backend exited with {proc.returncode}: "
                    f"{''.join(stderr_tail)}; seen={seen}"
                )
            try:
                event = events.get(timeout=0.25)
            except Empty:
                continue
            seen.append(event)
            if predicate(event):
                return event
        raise AssertionError(
            f"Timed out waiting for event; seen={seen}; stderr={''.join(stderr_tail)}"
        )

    return proc, events, seen, wait_for, stderr_tail


def _stop_backend(proc: subprocess.Popen):
    try:
        if proc.stdin is not None:
            proc.stdin.close()
    except Exception:
        pass
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass


def test_backend_stdout_order_keeps_turn_changes_before_done(tmp_path: Path):
    workdir = tmp_path / "work"
    workdir.mkdir()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _WriteThenDoneHandler)
    server.requests = []
    server.target_name = "note.txt"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    session_name = f"backend_turn_changes_{uuid.uuid4().hex}"
    proc, events, seen, wait_for, stderr_tail = _start_backend(
        tmp_path, server.server_port, session_name, workdir
    )
    try:
        wait_for(lambda event: event.get("type") == "model_info")
        assert proc.stdin is not None
        proc.stdin.write(json.dumps({"type": "message", "text": "write a note"}) + "\n")
        proc.stdin.flush()

        done_seen = False
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            try:
                event = events.get(timeout=0.5)
            except Empty:
                if proc.poll() is not None:
                    break
                continue
            seen.append(event)
            if event.get("type") == "tool_approval_request":
                proc.stdin.write(json.dumps({
                    "type": "tool_approval_response",
                    "request_id": event["request_id"],
                    "decision": "once",
                }) + "\n")
                proc.stdin.flush()
            elif event.get("type") == "done":
                done_seen = True
                break
        assert done_seen, f"no done event; seen={[e.get('type') for e in seen]}"

        types = [e.get("type") for e in seen]
        if "turn_changes" not in types:
            tool_results = [e for e in seen if e.get("type") == "tool_result"]
            tree = sorted(str(p.relative_to(workdir)) for p in workdir.rglob("*"))
            raise AssertionError(
                f"turn_changes missing; seen={types}; tool_results={tool_results}; "
                f"workdir_tree={tree}; stderr={''.join(stderr_tail)}"
            )
        tc_index = types.index("turn_changes")
        done_indices = [i for i, kind in enumerate(types) if kind == "done"]
        assert done_indices and tc_index < max(done_indices), (
            f"order violated; seen={types}"
        )

        payload = seen[tc_index]
        assert payload.get("session_id") == session_name
        assert payload.get("request_id")
        files = payload.get("files") or []
        assert any(
            change.get("path") == "note.txt" and change.get("state") == tcs.STATE_ADDED
            for change in files
        ), f"note.txt not recorded; files={files}"
        assert (workdir / "note.txt").read_text(encoding="utf-8") == "hello turn-changes\n"
    finally:
        _stop_backend(proc)
        server.shutdown()
        server.server_close()


# ------------------------------------------------------------------ /changes

CHANGES_USAGE = "Usage: /changes [@k] [n|path]"
BUSY_ERROR = "Finish or cancel the current reply before viewing changes."


class _ChangesCommandHandler(BaseHTTPRequestHandler):
    """Same script as ``_WriteThenDoneHandler`` plus an optional hang switch.

    Calls 1-2 finish one turn (write_file, then final text).  When
    ``server.hang_from_call`` is set, that call and every later one is answered
    with an open SSE stream that never completes until ``server.release`` is set
    — the reply stays active, which is what the busy guard must detect.
    """

    def log_message(self, format, *args):
        return

    def _json(self, payload: dict):
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _sse(self, chunks: list[dict]):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        for chunk in chunks:
            self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def do_GET(self):
        if self.path.endswith("/models"):
            self._json({
                "object": "list",
                "data": [{"id": "Qwen3.6-35B-A3B", "meta": {"n_ctx": 131072}}],
            })
        else:
            self.send_error(404)

    def do_POST(self):
        if not self.path.endswith("/chat/completions"):
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        self.server.requests.append(payload)
        call_number = len(self.server.requests)
        hang_from = int(getattr(self.server, "hang_from_call", 0) or 0)
        if hang_from and call_number >= hang_from:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(b": pending\n\n")
            self.wfile.flush()
            release = self.server.release
            while not release.is_set():
                time.sleep(0.05)
            return
        base = {
            "id": f"chatcmpl-changes-{call_number}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": "Qwen3.6-35B-A3B",
        }
        has_tool_result = any(
            isinstance(message, dict) and message.get("role") == "tool"
            for message in (payload.get("messages") or [])
        )
        if not has_tool_result:
            arguments = json.dumps({
                "path": self.server.target_name,
                "content": "hello turn-changes\n",
            })
            self._sse([
                {
                    **base,
                    "choices": [{
                        "index": 0,
                        "delta": {
                            "role": "assistant",
                            "tool_calls": [{
                                "index": 0,
                                "id": "call-changes-write",
                                "type": "function",
                                "function": {"name": "write_file", "arguments": arguments},
                            }],
                        },
                        "finish_reason": None,
                    }],
                },
                {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
            ])
        else:
            self._sse([
                {
                    **base,
                    "choices": [{
                        "index": 0,
                        "delta": {"role": "assistant", "content": "note written"},
                        "finish_reason": None,
                    }],
                },
                {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            ])


def _start_changes_backend(tmp_path: Path, session_name: str, *, hang_from_call: int = 0):
    workdir = tmp_path / "work"
    workdir.mkdir(exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ChangesCommandHandler)
    server.requests = []
    server.target_name = "note.txt"
    server.hang_from_call = hang_from_call
    server.release = threading.Event()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    proc, events, seen, wait_for, stderr_tail = _start_backend(
        tmp_path, server.server_port, session_name, workdir
    )
    return workdir, server, proc, events, seen, wait_for, stderr_tail


def _run_turn(proc, events, seen, wait_for, text: str):
    """Run one message turn, answering approvals, until the closing ``done``."""
    assert proc.stdin is not None
    proc.stdin.write(json.dumps({"type": "message", "text": text}) + "\n")
    proc.stdin.flush()
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            event = events.get(timeout=0.5)
        except Empty:
            if proc.poll() is not None:
                break
            continue
        seen.append(event)
        if event.get("type") == "tool_approval_request":
            proc.stdin.write(json.dumps({
                "type": "tool_approval_response",
                "request_id": event["request_id"],
                "decision": "once",
            }) + "\n")
            proc.stdin.flush()
        elif event.get("type") == "done":
            return
    raise AssertionError(f"turn did not finish; seen={[e.get('type') for e in seen]}")


def _send_command(proc, cmd: str):
    assert proc.stdin is not None
    proc.stdin.write(json.dumps({"type": "command", "cmd": cmd}) + "\n")
    proc.stdin.flush()


def _changes_result(proc, events, seen, wait_for) -> dict:
    event = wait_for(
        lambda item: item.get("type") == "tool_result" and item.get("name") == "changes"
    )
    assert event.get("code", "") == "", event
    return event


def _drain(events, seen, seconds: float) -> list[dict]:
    drained: list[dict] = []
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            event = events.get(timeout=0.1)
        except Empty:
            continue
        seen.append(event)
        drained.append(event)
    return drained


def _index_records(workdir: Path, session_name: str) -> list[dict]:
    index_path = (
        workdir / ".astra" / "turn-changes" / tcs._safe_session_name(session_name) / "turns.json"
    )
    assert index_path.exists(), f"missing {index_path}; tree={sorted(str(p) for p in workdir.rglob('*'))}"
    return json.loads(index_path.read_text(encoding="utf-8"))["records"]


def test_changes_command_reads_the_live_ledger_without_new_turns(tmp_path: Path):
    session_name = f"changes_cmd_{uuid.uuid4().hex}"
    workdir, server, proc, events, seen, wait_for, stderr_tail = _start_changes_backend(
        tmp_path, session_name
    )
    try:
        wait_for(lambda event: event.get("type") == "model_info")
        _run_turn(proc, events, seen, wait_for, "write a note")

        _send_command(proc, "/changes")
        result = _changes_result(proc, events, seen, wait_for)
        output = result.get("output") or ""
        assert result.get("error") == "", result
        assert "note.txt" in output, f"output={output!r}"
        assert re.search(r"\+\d+ \u2212\d+", output), f"counts missing: {output!r}"
        assert output.splitlines()[0].startswith("回合 @1 · request "), output

        records_before = _index_records(workdir, session_name)
        assert len(records_before) == 1, records_before
        changes_events_before = sum(1 for item in seen if item.get("type") == "turn_changes")

        _send_command(proc, "/changes @1 1")
        detail = _changes_result(proc, events, seen, wait_for)
        assert detail.get("error") == "", detail
        detail_output = detail.get("output") or ""
        assert detail_output.splitlines()[0].startswith("回合 @1 · note.txt"), detail_output
        assert "新增文件" in detail_output, detail_output

        _send_command(proc, "/changes @9")
        past_end = _changes_result(proc, events, seen, wait_for)
        assert CHANGES_USAGE in (past_end.get("error") or ""), past_end
        assert past_end.get("output") == "", past_end

        _send_command(proc, "/changes @99")
        out_of_window = _changes_result(proc, events, seen, wait_for)
        assert CHANGES_USAGE in (out_of_window.get("error") or ""), out_of_window

        _send_command(proc, "/changes")
        _changes_result(proc, events, seen, wait_for)
        _drain(events, seen, 0.5)
        assert _index_records(workdir, session_name) == records_before
        assert sum(1 for item in seen if item.get("type") == "turn_changes") == changes_events_before
        assert not (workdir / "note.txt").read_text(encoding="utf-8").startswith("回合")
    finally:
        _stop_backend(proc)
        server.shutdown()
        server.server_close()


def test_changes_command_without_a_turn_reports_no_turns(tmp_path: Path):
    """新会话（尚未跑过回合）：报“还没有完成的回合”，不是索引故障（review R5）."""
    session_name = f"changes_empty_{uuid.uuid4().hex}"
    workdir, server, proc, events, seen, wait_for, stderr_tail = _start_changes_backend(
        tmp_path, session_name
    )
    try:
        wait_for(lambda event: event.get("type") == "model_info")
        _send_command(proc, "/changes")
        result = _changes_result(proc, events, seen, wait_for)
        assert result.get("error") == "", result
        output = result.get("output") or ""
        assert "还没有完成的回合" in output, result
        assert "回合索引暂不可用" not in output, result
    finally:
        _stop_backend(proc)
        server.shutdown()
        server.server_close()


def test_changes_command_with_history_but_no_live_store_reports_unavailable(
    tmp_path: Path,
):
    """有完成回合痕迹但 store 未创建（如恢复的旧会话）：保守报不可用（review R5）."""
    session_name = f"changes_history_{uuid.uuid4().hex}"
    (tmp_path / "work").mkdir()
    (
        tmp_path / "work" / ".astra" / "turn-changes" / session_name / "turn-1"
    ).mkdir(parents=True)
    workdir, server, proc, events, seen, wait_for, stderr_tail = _start_changes_backend(
        tmp_path, session_name
    )
    try:
        wait_for(lambda event: event.get("type") == "model_info")
        _send_command(proc, "/changes")
        result = _changes_result(proc, events, seen, wait_for)
        assert result.get("error") == "", result
        assert "回合索引暂不可用" in (result.get("output") or ""), result
    finally:
        _stop_backend(proc)
        server.shutdown()
        server.server_close()


def test_changes_command_is_rejected_while_a_reply_is_active(tmp_path: Path):
    session_name = f"changes_busy_{uuid.uuid4().hex}"
    workdir, server, proc, events, seen, wait_for, stderr_tail = _start_changes_backend(
        tmp_path, session_name, hang_from_call=3
    )
    try:
        wait_for(lambda event: event.get("type") == "model_info")
        _run_turn(proc, events, seen, wait_for, "write a note")

        assert proc.stdin is not None
        proc.stdin.write(json.dumps({"type": "message", "text": "second turn"}) + "\n")
        proc.stdin.flush()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and len(server.requests) < 3:
            time.sleep(0.05)
        assert len(server.requests) >= 3, f"second model call never started: {len(server.requests)}"

        _send_command(proc, "/changes")
        busy = wait_for(
            lambda item: item.get("type") == "tool_result" and item.get("name") == "changes"
        )
        assert busy.get("error") == BUSY_ERROR, busy
        assert busy.get("output") == "", busy

        index = next(
            position
            for position, item in enumerate(seen)
            if item.get("type") == "tool_result" and item.get("name") == "changes"
        )
        trailing = _drain(events, seen, 1.0)
        assert not any(item.get("type") == "done" for item in trailing), (
            "a done event was sent while the reply was still active; "
            f"after-error={[item.get('type') for item in seen[index + 1:]]}"
        )
    finally:
        server.release.set()
        _stop_backend(proc)
        server.shutdown()
        server.server_close()
