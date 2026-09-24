import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Empty, Queue

from agent.runtime import session_recall
from agent.runtime.approval_inbox import ApprovalInbox
from agent.runtime.session_store import SessionStore
from agent.runtime.task_store import TaskStore


def _path_state(path: Path) -> tuple[int, int, int, int] | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)


def _sqlite_path_state(path: Path) -> tuple[tuple[int, int, int, int] | None, ...]:
    return tuple(_path_state(Path(f"{path}{suffix}")) for suffix in ("", "-wal", "-shm"))


def test_backend_exposes_tasks_command_without_blocking_protocol(tmp_path: Path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"selected_model": "Qwen3.6-35B-A3B"}), encoding="utf-8")
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    env = os.environ.copy()
    env.update({
        "AGENT_SETTINGS_PATH": str(settings),
        "AGENT_SESSION_DIR": str(sessions_dir),
        "AGENT_TASK_DB": str(tmp_path / "tasks.db"),
        "ASTRA_APPROVAL_DB": str(tmp_path / "approvals.db"),
        "ASTRA_EVENT_DB": str(tmp_path / "events.db"),
        "AGENT_MEMORY_PATH": str(tmp_path / "memory.db"),
        "AGENT_LEARNING_PATH": str(tmp_path / "learning.db"),
        "AGENT_SKILLS_PATH": str(tmp_path / "skills"),
        "LEARNING_REVIEW_AUTO": "0",
        "AGENT_SESSION": "backend_protocol_test",
        "QWEN_BASE_URL": "http://127.0.0.1:9/v1",
        "SANDBOX_DOCKER": "false",
        "AGENT_MCP_CONFIG": str(tmp_path / "missing-mcp.json"),
        "PYTHONUNBUFFERED": "1",
    })
    root = Path(__file__).parents[1]
    commands = "\n".join([
        json.dumps({"type": "message", "content": "malformed field"}),
        json.dumps({"type": "command"}),
        json.dumps({"type": "command", "cmd": "/memory working goal Protocol test"}),
        json.dumps({"type": "command", "cmd": "/skills"}),
        json.dumps({"type": "command", "cmd": "/learn"}),
        json.dumps({"type": "command", "cmd": "/sandbox status"}),
        json.dumps({"type": "command", "cmd": "/bar output stream"}),
        json.dumps({"type": "command", "cmd": "/bar output status"}),
        json.dumps({"type": "command", "cmd": "/bar sip"}),
        json.dumps({"type": "command", "cmd": "/mode low"}),
        json.dumps({"type": "command", "cmd": "/mode high"}),
        json.dumps({"type": "command", "cmd": "/mode max"}),
        json.dumps({"type": "command", "cmd": "/mode"}),
        json.dumps({"type": "command", "cmd": "/mode coding"}),
        json.dumps({"type": "command", "cmd": "/mode code native"}),
        json.dumps({"type": "command", "cmd": "/vision-tiles status"}),
        json.dumps({"type": "command", "cmd": "/vision-tiles off"}),
        json.dumps({"type": "command", "cmd": "/vision-tiles on"}),
        json.dumps({"type": "command", "cmd": "/vision-tiles invalid"}),
        json.dumps({"type": "command", "cmd": "/context-index status"}),
        json.dumps({"type": "command", "cmd": "/context-index session"}),
        json.dumps({"type": "command", "cmd": "/context-index invalid"}),
        json.dumps({"type": "command", "cmd": "/tasks"}),
        json.dumps({"type": "command", "cmd": "/budget 60"}),
        json.dumps({"type": "command", "cmd": "/budget"}),
        json.dumps({"type": "command", "cmd": "/budget invalid"}),
        json.dumps({"type": "command", "cmd": "/budget off"}),
        json.dumps({"type": "command", "cmd": "/diagnostics --raw"}),
        json.dumps({"type": "command", "cmd": "/diagnostics json"}),
        json.dumps({"type": "command", "cmd": "/maintenance"}),
        json.dumps({"type": "exit"}),
        "",
    ])
    completed = subprocess.run(
        [sys.executable, "-m", "agent.cli.backend"],
        input=commands,
        text=True,
        capture_output=True,
        cwd=root,
        env=env,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    events = [json.loads(line) for line in completed.stdout.splitlines() if line.startswith("{")]
    budgets = [e for e in events if e.get("type") == "tool_result" and e.get("name") == "budget"]
    assert len(budgets) == 4
    assert all("60s" in e["output"] for e in budgets[:2])
    assert "Usage: /budget" in budgets[2]["error"]
    assert "budget: off" in budgets[3]["output"]
    mode_results = [e for e in events if e.get("type") == "tool_result" and e.get("name") == "mode"]
    assert len(mode_results) == 6
    for result, effort in zip(mode_results[:3], ("low", "high", "max")):
        assert not result["error"]
        assert f"Reasoning effort: {effort}" in result["output"]
        assert "does not apply" in result["output"]  # this fixture uses Qwen
        assert "max_tokens" not in result["output"]
    assert "/mode [low|high|xhigh|max]" in mode_results[3]["output"]
    assert all("Unknown reasoning effort" in e["error"] for e in mode_results[4:])
    assert json.loads(settings.read_text())["reasoning_effort"] == "max"
    model_events = [e for e in events if e.get("type") == "model_info"]
    assert model_events and all(e.get("reasoning_effort") is None for e in model_events)
    assert all("agent_mode" not in e and e["code_mode"] == "native" for e in model_events)

    assert any(
        event.get("type") == "error"
        and "expected a string field 'text'" in event.get("message", "")
        for event in events
    )
    assert any(
        event.get("type") == "error"
        and "expected a string field 'cmd'" in event.get("message", "")
        for event in events
    )
    vision_results = [
        event
        for event in events
        if event.get("type") == "tool_result"
        and event.get("name") == "vision-tiles"
    ]
    assert len(vision_results) == 4
    assert "Vision tiles: ON" in vision_results[0]["output"]
    assert "Vision tiles: OFF" in vision_results[1]["output"]
    assert "Vision tiles: ON" in vision_results[2]["output"]
    assert "Usage: /vision-tiles [on|off]" in vision_results[3]["error"]
    context_index_results = [
        event
        for event in events
        if event.get("type") == "tool_result"
        and event.get("name") == "context-index"
    ]
    assert len(context_index_results) == 3
    assert "Active mode: OFF" in context_index_results[0]["output"]
    assert "Active mode: SESSION" in context_index_results[1]["output"]
    assert (
        context_index_results[2]["error"]
        == "Usage: /context-index [on|off|session|all|status|why] or /context-index feedback R1 useful|irrelevant|outdated"
    )
    task_result = next(event for event in events if event.get("type") == "tool_result" and event.get("name") == "tasks")
    assert task_result["error"] == ""
    assert "No recorded tasks" in task_result["output"]
    diagnostics = [
        event for event in events
        if event.get("type") == "tool_result" and event.get("name") == "diagnostics"
    ]
    assert "Astra diagnostics (local snapshot)" in diagnostics[0]["output"]
    diagnostics_json = json.loads(diagnostics[1]["output"])
    assert diagnostics_json["version"] == 1
    assert diagnostics_json["context"]["limit"] > 0
    maintenance = next(
        event for event in events
        if event.get("type") == "tool_result" and event.get("name") == "maintenance"
    )
    assert "Astra maintenance preview (no changes made)" in maintenance["output"]
    assert any(
        event.get("type") == "tool_result"
        and event.get("name") == "skills"
        and "astra-core" in event.get("output", "")
        for event in events
    )
    assert any(
        event.get("type") == "tool_result"
        and event.get("name") == "learn"
        and "Direct skill learning: review" in event.get("output", "")
        for event in events
    )
    assert any(
        event.get("type") == "tool_result"
        and event.get("name") == "sandbox"
        and "Sandbox: OFF" in event.get("output", "")
        for event in events
    )
    assert any(
        event.get("type") == "working_memory"
        and event.get("memory", {}).get("goal") == "Protocol test"
        for event in events
    )
    assert any(
        event.get("type") == "bar_state" and event.get("output_mode") == "stream"
        for event in events
    )
    assert any(
        event.get("type") == "tool_result"
        and event.get("name") == "bar"
        and "Bar output mode: stream" in event.get("output", "")
        for event in events
    )
    assert not any(
        event.get("type") == "history" for event in events
    ), "state-only /bar commands must not replay the full terminal history"
    assert json.loads(settings.read_text(encoding="utf-8"))["bar_output_mode"] == "stream"
    assert json.loads(settings.read_text(encoding="utf-8"))["vision_tiles_enabled"] is True
    assert any(event.get("type") == "done" for event in events)


def test_backend_history_preserves_message_timestamps_and_tool_activity(tmp_path: Path):
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps({"selected_model": "Qwen3.6-35B-A3B"}),
        encoding="utf-8",
    )
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    SessionStore(sessions_dir / "timestamp_history.json").save({
        "system_prompt": "sys",
        "messages": [
            {"role": "user", "content": "old question", "timestamp": 1_750_000_000.0},
            {"role": "assistant", "content": "old answer", "timestamp": 1_750_000_060.0, "tool_calls": [
                {"id": "saved-call", "type": "function", "function": {"name": "context_open", "arguments": "{}"}},
            ]},
            {"role": "tool", "tool_call_id": "saved-call", "content": "[Tool result: context_open | status: success]\nsaved context"},
        ],
    })
    task_store = TaskStore(tmp_path / "tasks.db")
    run = task_store.start_run("saved-task", "old question", session_id="timestamp_history")
    claim = task_store.claim_tool(run["id"], "context_open", {}, "read")
    task_store.finish_step(claim.step_id, output={"type": "tool_result", "id": "saved-call", "name": "context_open", "output": "saved context", "error": "", "duration_ms": 18})
    task_store.finish_run(run["id"], "completed")
    env = os.environ.copy()
    env.update({
        "AGENT_SETTINGS_PATH": str(settings),
        "AGENT_SESSION_DIR": str(sessions_dir),
        "AGENT_TASK_DB": str(tmp_path / "tasks.db"),
        "ASTRA_APPROVAL_DB": str(tmp_path / "approvals.db"),
        "ASTRA_EVENT_DB": str(tmp_path / "events.db"),
        "AGENT_MEMORY_PATH": str(tmp_path / "memory.db"),
        "AGENT_LEARNING_PATH": str(tmp_path / "learning.db"),
        "AGENT_SKILLS_PATH": str(tmp_path / "skills"),
        "LEARNING_REVIEW_AUTO": "0",
        "AGENT_SESSION": "timestamp_history",
        "QWEN_BASE_URL": "http://127.0.0.1:9/v1",
        "SANDBOX_DOCKER": "false",
        "AGENT_MCP_CONFIG": str(tmp_path / "missing-mcp.json"),
        "PYTHONUNBUFFERED": "1",
    })

    completed = subprocess.run(
        [sys.executable, "-m", "agent.cli.backend"],
        input=json.dumps({"type": "exit"}) + "\n",
        text=True,
        capture_output=True,
        cwd=Path(__file__).parents[1],
        env=env,
        timeout=60,
        check=False,
    )
    events = [
        json.loads(line)
        for line in completed.stdout.splitlines()
        if line.startswith("{")
    ]
    history = next(event for event in events if event.get("type") == "history")

    assert completed.returncode == 0, completed.stderr
    assert history["messages"] == [
        {"role": "user", "content": "old question", "timestamp": 1_750_000_000.0},
        {"role": "assistant", "content": "old answer", "timestamp": 1_750_000_060.0},
    ]
    assert history["tool_results"] == [{"name": "context_open", "output": "saved context", "error": "", "duration_ms": 18}]
    assert history["session_id"] == "timestamp_history"


def test_backend_loads_saved_vision_tiles_off_at_startup(tmp_path: Path):
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps({
            "selected_model": "Qwen3.6-35B-A3B",
            "vision_tiles_enabled": False,
        }),
        encoding="utf-8",
    )
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    env = os.environ.copy()
    env.update({
        "AGENT_SETTINGS_PATH": str(settings),
        "AGENT_SESSION_DIR": str(sessions_dir),
        "AGENT_TASK_DB": str(tmp_path / "tasks.db"),
        "ASTRA_APPROVAL_DB": str(tmp_path / "approvals.db"),
        "ASTRA_EVENT_DB": str(tmp_path / "events.db"),
        "AGENT_MEMORY_PATH": str(tmp_path / "memory.db"),
        "AGENT_LEARNING_PATH": str(tmp_path / "learning.db"),
        "AGENT_SKILLS_PATH": str(tmp_path / "skills"),
        "LEARNING_REVIEW_AUTO": "0",
        "AGENT_SESSION": "backend_vision_tiles_startup_test",
        "QWEN_BASE_URL": "http://127.0.0.1:9/v1",
        "SANDBOX_DOCKER": "false",
        "AGENT_MCP_CONFIG": str(tmp_path / "missing-mcp.json"),
        "PYTHONUNBUFFERED": "1",
    })
    commands = "\n".join([
        json.dumps({"type": "command", "cmd": "/vision-tiles"}),
        json.dumps({"type": "exit"}),
        "",
    ])

    completed = subprocess.run(
        [sys.executable, "-m", "agent.cli.backend"],
        input=commands,
        text=True,
        capture_output=True,
        cwd=Path(__file__).parents[1],
        env=env,
        timeout=60,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    events = [
        json.loads(line)
        for line in completed.stdout.splitlines()
        if line.startswith("{")
    ]
    result = next(
        event
        for event in events
        if event.get("type") == "tool_result"
        and event.get("name") == "vision-tiles"
    )
    assert "Vision tiles: OFF" in result["output"]
    assert "Active for current model: NO" in result["output"]
    assert "provider downscaling" in result["output"]


class _SlowOpenAIHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def _json(self, payload: dict):
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.endswith("/models"):
            self._json({"object": "list", "data": [{"id": "Qwen3.6-35B-A3B"}]})
        else:
            self.send_error(404)

    def do_POST(self):
        if not self.path.endswith("/chat/completions"):
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        messages = payload.get("messages") or []
        if any(
            "strict completion verifier" in str(message.get("content") or "").lower()
            for message in messages
        ):
            self._json({
                "id": "chatcmpl-goal-verifier",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": "Qwen3.6-35B-A3B",
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": '{"met": false, "evidence": "cancelled", "next_step": "retry"}',
                    },
                    "finish_reason": "stop",
                }],
            })
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        chunk = {
            "id": "chatcmpl-test",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": "Qwen3.6-35B-A3B",
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": "working"}, "finish_reason": None}],
        }
        try:
            self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
            self.wfile.flush()
            time.sleep(10)
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


class _ApprovalOpenAIHandler(BaseHTTPRequestHandler):
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
        self.rfile.read(length)
        call_number = getattr(self.server, "call_number", 0) + 1
        self.server.call_number = call_number
        base = {
            "id": f"chatcmpl-approval-{call_number}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": "Qwen3.6-35B-A3B",
        }
        if call_number == 1:
            arguments = json.dumps({
                "path": str(self.server.target_path),
                "content": self.server.target_content,
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
                                "id": "call-write-approval",
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
                        "delta": {"role": "assistant", "content": "saved"},
                        "finish_reason": None,
                    }],
                },
                {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            ])


class _QuestionOpenAIHandler(BaseHTTPRequestHandler):
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
            "id": f"chatcmpl-question-{call_number}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": "Qwen3.6-35B-A3B",
        }
        if call_number == 1:
            arguments = json.dumps({
                "questions": [{
                    "id": "storage",
                    "header": "Storage",
                    "question": "Choose storage",
                    "options": [
                        {"label": "SQLite (Recommended)", "description": "Session local"},
                        {"label": "Markdown", "description": "Human editable"},
                    ],
                    "multi_select": False,
                }],
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
                                "id": "call-user-question",
                                "type": "function",
                                "function": {
                                    "name": "ask_user_question",
                                    "arguments": arguments,
                                },
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
                        "delta": {"role": "assistant", "content": "SQLite selected."},
                        "finish_reason": None,
                    }],
                },
                {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            ])


class _QuestionThenWriteOpenAIHandler(_QuestionOpenAIHandler):
    def do_POST(self):
        if not self.path.endswith("/chat/completions"):
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        self.server.requests.append(payload)
        call_number = len(self.server.requests)
        base = {
            "id": f"chatcmpl-question-write-{call_number}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": "Qwen3.6-35B-A3B",
        }
        if call_number == 1:
            name = "ask_user_question"
            call_id = "call-question-before-write"
            arguments = json.dumps({
                "questions": [{
                    "id": "storage",
                    "header": "Storage",
                    "question": "Choose storage",
                    "options": [
                        {"label": "SQLite (Recommended)", "description": "Session local"},
                        {"label": "Markdown", "description": "Human editable"},
                    ],
                    "multi_select": False,
                }],
            })
        elif call_number == 2:
            name = "write_file"
            call_id = "call-write-after-question"
            arguments = json.dumps({
                "path": str(self.server.target_path),
                "content": self.server.target_content,
            })
        else:
            self._sse([
                {
                    **base,
                    "choices": [{
                        "index": 0,
                        "delta": {"role": "assistant", "content": "saved"},
                        "finish_reason": None,
                    }],
                },
                {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            ])
            return
        self._sse([
            {
                **base,
                "choices": [{
                    "index": 0,
                    "delta": {
                        "role": "assistant",
                        "tool_calls": [{
                            "index": 0,
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": arguments},
                        }],
                    },
                    "finish_reason": None,
                }],
            },
            {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        ])


class _QuestionThenPlanOpenAIHandler(_QuestionOpenAIHandler):
    def do_POST(self):
        if not self.path.endswith("/chat/completions"):
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        self.server.requests.append(payload)
        call_number = len(self.server.requests)
        base = {
            "id": f"chatcmpl-question-plan-{call_number}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": "Qwen3.6-35B-A3B",
        }
        if call_number == 1:
            name = "ask_user_question"
            call_id = "call-question-before-plan"
            arguments = json.dumps({
                "questions": [{
                    "id": "storage",
                    "header": "Storage",
                    "question": "Choose storage",
                    "options": [
                        {"label": "SQLite (Recommended)", "description": "Session local"},
                        {"label": "Markdown", "description": "Human editable"},
                    ],
                    "multi_select": False,
                }],
            })
        elif call_number == 2:
            name = "plan_update"
            call_id = "call-direct-plan"
            arguments = json.dumps({
                "goal": "Implement the selected storage",
                "steps": [
                    {"text": "Record the selection", "status": "in_progress"},
                    {"text": "Verify the result", "status": "pending"},
                ],
            })
        else:
            self._sse([
                {
                    **base,
                    "choices": [{
                        "index": 0,
                        "delta": {"role": "assistant", "content": "Plan recorded."},
                        "finish_reason": None,
                    }],
                },
                {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            ])
            return
        self._sse([
            {
                **base,
                "choices": [{
                    "index": 0,
                    "delta": {
                        "role": "assistant",
                        "tool_calls": [{
                            "index": 0,
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": arguments},
                        }],
                    },
                    "finish_reason": None,
                }],
            },
            {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        ])


class _GoalOpenAIHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def _json(self, payload: dict):
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.endswith("/models"):
            self._json({"object": "list", "data": [{"id": "Qwen3.6-35B-A3B"}]})
        else:
            self.send_error(404)

    def do_POST(self):
        if not self.path.endswith("/chat/completions"):
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        if payload.get("stream"):
            self.server.main_calls += 1
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            base = {
                "id": "chatcmpl-goal-main",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": "Qwen3.6-35B-A3B",
            }
            chunks = [
                {
                    **base,
                    "choices": [{
                        "index": 0,
                        "delta": {"role": "assistant", "content": "work complete"},
                        "finish_reason": None,
                    }],
                },
                {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            ]
            for chunk in chunks:
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return

        self.server.verifier_calls += 1
        self._json({
            "id": "chatcmpl-goal-verifier",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "Qwen3.6-35B-A3B",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "not valid verdict json"},
                "finish_reason": "stop",
            }],
        })


def test_goal_resume_launches_work_but_verifier_failure_does_not_spend_round(tmp_path: Path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _GoalOpenAIHandler)
    server.main_calls = 0
    server.verifier_calls = 0
    threading.Thread(target=server.serve_forever, daemon=True).start()

    task_db = tmp_path / "tasks.db"
    store = TaskStore(task_db)
    goal = store.set_goal("backend_goal_resume_test", "finish persisted work")
    store.pause_goal("backend_goal_resume_test")
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"selected_model": "Qwen3.6-35B-A3B"}), encoding="utf-8")
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    env = os.environ.copy()
    env.update({
        "AGENT_SETTINGS_PATH": str(settings),
        "AGENT_SESSION_DIR": str(sessions_dir),
        "AGENT_TASK_DB": str(task_db),
        "ASTRA_EVENT_DB": str(tmp_path / "events.db"),
        "AGENT_MEMORY_PATH": str(tmp_path / "memory.db"),
        "AGENT_LEARNING_PATH": str(tmp_path / "learning.db"),
        "AGENT_SKILLS_PATH": str(tmp_path / "skills"),
        "LEARNING_REVIEW_AUTO": "0",
        "AGENT_SESSION": "backend_goal_resume_test",
        "QWEN_BASE_URL": f"http://127.0.0.1:{server.server_port}/v1",
        "SANDBOX_DOCKER": "false",
        "AGENT_MCP_CONFIG": str(tmp_path / "missing-mcp.json"),
        "PYTHONUNBUFFERED": "1",
    })
    root = Path(__file__).parents[1]
    proc = subprocess.Popen(
        [sys.executable, "-m", "agent.cli.backend"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=root,
        env=env,
        bufsize=1,
    )
    assert proc.stdin is not None
    assert proc.stdout is not None
    events: Queue[dict] = Queue()

    def read_events():
        assert proc.stdout is not None
        for line in proc.stdout:
            try:
                events.put(json.loads(line))
            except json.JSONDecodeError:
                pass

    threading.Thread(target=read_events, daemon=True).start()

    def wait_for(predicate, timeout: float = 15) -> dict:
        deadline = time.time() + timeout
        seen = []
        while time.time() < deadline:
            if proc.poll() is not None:
                stderr = proc.stderr.read() if proc.stderr is not None else ""
                raise AssertionError(f"Backend exited with {proc.returncode}: {stderr}; seen={seen}")
            try:
                event = events.get(timeout=0.25)
            except Empty:
                continue
            seen.append(event)
            if predicate(event):
                return event
        raise AssertionError(f"Timed out waiting for event; seen={seen}")

    try:
        wait_for(lambda event: event.get("type") == "model_info")
        proc.stdin.write(json.dumps({"type": "command", "cmd": "/goal resume"}) + "\n")
        proc.stdin.flush()
        wait_for(lambda event: event.get("type") == "task_started")
        failed = wait_for(
            lambda event: event.get("type") == "goal_status"
            and event.get("stage") == "verification_failed"
        )
        assert failed["verdict"]["parse_error"] is True
        persisted = TaskStore(task_db).get_goal(goal["id"])
        assert persisted is not None
        assert persisted["status"] == "active"
        assert persisted["round"] == 0
        assert server.main_calls == 1
        assert server.verifier_calls == 1
    finally:
        if proc.poll() is None:
            proc.stdin.write(json.dumps({"type": "exit"}) + "\n")
            proc.stdin.flush()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        server.shutdown()
        server.server_close()


def _start_question_protocol_backend(
    tmp_path: Path,
    server_port: int,
    session_name: str,
    *,
    settings_overrides: dict | None = None,
    env_overrides: dict | None = None,
    bootstrap_code: str | None = None,
    workdir: Path | None = None,
):
    settings = tmp_path / "settings.json"
    settings_payload = {"selected_model": "Qwen3.6-35B-A3B"}
    settings_payload.update(settings_overrides or {})
    settings.write_text(json.dumps(settings_payload), encoding="utf-8")
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
    env = os.environ.copy()
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
        "AGENT_MCP_CONFIG": str(tmp_path / "missing-mcp.json"),
        "PYTHONUNBUFFERED": "1",
    })
    env.update(env_overrides or {})
    repo_root = Path(__file__).resolve().parents[1]
    if workdir is not None:
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(repo_root), env.get("PYTHONPATH", "")]))
    # Startup diagnostics must be drained too: a full stderr pipe can prevent
    # a child from ever publishing model_info. Keep a bounded tail for failures.
    diagnostic_bootstrap = (
        "import faulthandler, runpy\n"
        "faulthandler.dump_traceback_later(10, repeat=True)\n"
        + (bootstrap_code or "runpy.run_module('agent.cli.backend', run_name='__main__')")
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", diagnostic_bootstrap],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=workdir if workdir is not None else repo_root,
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

    def wait_for(predicate, timeout: float = 15) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                stderr_reader.join(timeout=1)
                stderr = "".join(stderr_tail)
                raise AssertionError(f"Backend exited with {proc.returncode}: {stderr}; seen={seen}")
            try:
                event = events.get(timeout=0.25)
            except Empty:
                continue
            seen.append(event)
            if predicate(event):
                return event
        raise AssertionError(f"Timed out waiting for event; seen={seen}; stderr={''.join(stderr_tail)}")

    return proc, events, seen, wait_for


def _stop_protocol_backend(proc: subprocess.Popen):
    if proc.poll() is not None:
        return
    assert proc.stdin is not None
    proc.stdin.write(json.dumps({"type": "exit"}) + "\n")
    proc.stdin.flush()
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def test_protocol_fixture_drains_stderr_during_startup(tmp_path: Path):
    bootstrap = """
import json, sys
print(json.dumps({'type': 'backend_hello', 'protocol_version': 1}), flush=True)
sys.stderr.write('startup warning\\n' * 16384)
sys.stderr.flush()
print(json.dumps({'type': 'model_info'}), flush=True)
sys.stdin.readline()
"""
    proc, _, _, wait_for = _start_question_protocol_backend(
        tmp_path, 9, "stderr-startup", bootstrap_code=bootstrap,
    )
    try:
        wait_for(lambda event: event.get("type") == "model_info", timeout=3)
    finally:
        _stop_protocol_backend(proc)


def test_protocol_fixture_keeps_startup_failure_diagnostics(tmp_path: Path):
    bootstrap = "import sys; print('fixture startup failure', file=sys.stderr); raise SystemExit(13)"
    proc, _, _, wait_for = _start_question_protocol_backend(
        tmp_path, 9, "stderr-failure", bootstrap_code=bootstrap,
    )
    try:
        try:
            wait_for(lambda event: event.get("type") == "model_info", timeout=3)
        except AssertionError as exc:
            assert "Backend exited with 13" in str(exc)
            assert "fixture startup failure" in str(exc)
        else:
            raise AssertionError("The failed backend must not be considered ready")
    finally:
        _stop_protocol_backend(proc)


def test_backend_forwards_provider_counted_generation_stats(tmp_path: Path):
    class UsageHandler(_SlowOpenAIHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            base = {"id": "stats-test", "object": "chat.completion.chunk",
                    "created": int(time.time()), "model": "Qwen3.6-35B-A3B"}
            chunks = [
                {**base, "choices": [{"index": 0, "delta": {
                    "role": "assistant", "reasoning_content": "thinking",
                }, "finish_reason": None}]},
                {**base, "choices": [{"index": 0, "delta": {
                    "content": "complete",
                }, "finish_reason": "stop"}]},
                {**base, "choices": [], "usage": {
                    "prompt_tokens": 12, "completion_tokens": 60, "total_tokens": 72,
                    "completion_tokens_details": {"reasoning_tokens": 40},
                }},
            ]
            for chunk in chunks:
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                self.wfile.flush()
                time.sleep(0.025)
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), UsageHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    proc, _, seen, wait_for = _start_question_protocol_backend(
        tmp_path, server.server_port, "backend_generation_stats_test",
    )
    try:
        wait_for(lambda event: event.get("type") == "model_info")
        proc.stdin.write(json.dumps({"type": "message", "text": "show output rate"}) + "\n")
        proc.stdin.flush()
        stats = wait_for(lambda event: event.get("type") == "generation_stats")
        assert stats["completion_tokens"] == 60  # reasoning is a subset, not an extra 40
        assert stats["elapsed_seconds"] >= 0.05
        assert abs(stats["tokens_per_second"] * stats["elapsed_seconds"] - 60) < 0.00001
        wait_for(lambda event: event.get("type") == "done")
        assert sum(event.get("type") == "generation_stats" for event in seen) == 1
        progress = [event for event in seen if event.get("type") == "generation_progress"]
        assert progress[0]["phase"] == "requesting"
        assert progress[-1]["phase"] == "finished"
        assert all(event["elapsed_seconds"] >= 0 and event["idle_seconds"] >= 0 for event in progress)
    finally:
        _stop_protocol_backend(proc)
        server.shutdown()
        server.server_close()


def test_backend_resumes_same_turn_with_structured_question_tool_result(tmp_path: Path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _QuestionOpenAIHandler)
    server.requests = []
    threading.Thread(target=server.serve_forever, daemon=True).start()

    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"selected_model": "Qwen3.6-35B-A3B"}), encoding="utf-8")
    models = tmp_path / "models.yaml"
    models.write_text(
        "\n".join([
            "version: 1",
            "providers:",
            "  test:",
            "    label: Test provider",
            "    provider: openai-compatible",
            f"    base_url: http://127.0.0.1:{server.server_port}/v1",
            "    api_key_env: ''",
            "    context_limit: 131072",
            "    capabilities: [tools, streaming]",
            "models:",
            "  Qwen3.6-35B-A3B:",
            "    provider: openai-compatible",
            f"    base_url: http://127.0.0.1:{server.server_port}/v1",
            "    api_key_env: ''",
            "    context_limit: 131072",
            "    capabilities: [tools, streaming]",
            "",
        ]),
        encoding="utf-8",
    )
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    session_key = f"backend_question_{uuid.uuid4().hex}"
    production_before = _sqlite_path_state(session_recall.DB_PATH)
    env = os.environ.copy()
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
        "AGENT_SESSION": session_key,
        "SANDBOX_DOCKER": "false",
        "AGENT_MCP_CONFIG": str(tmp_path / "missing-mcp.json"),
        "PYTHONUNBUFFERED": "1",
    })
    root = Path(__file__).parents[1]
    proc = subprocess.Popen(
        [sys.executable, "-m", "agent.cli.backend"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=root,
        env=env,
        bufsize=1,
    )
    assert proc.stdin is not None
    assert proc.stdout is not None
    events: Queue[dict] = Queue()
    seen: list[dict] = []

    def read_events():
        assert proc.stdout is not None
        for line in proc.stdout:
            try:
                events.put(json.loads(line))
            except json.JSONDecodeError:
                pass

    threading.Thread(target=read_events, daemon=True).start()

    def wait_for(predicate, timeout: float = 15) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if proc.poll() is not None:
                stderr = proc.stderr.read() if proc.stderr is not None else ""
                raise AssertionError(f"Backend exited with {proc.returncode}: {stderr}; seen={seen}")
            try:
                event = events.get(timeout=0.25)
            except Empty:
                continue
            seen.append(event)
            if predicate(event):
                return event
        raise AssertionError(f"Timed out waiting for event; seen={seen}")

    try:
        wait_for(lambda event: event.get("type") == "model_info")
        seen.clear()
        proc.stdin.write(json.dumps({"type": "message", "text": "choose storage"}) + "\n")
        proc.stdin.flush()
        request = wait_for(
            lambda event: event.get("type") in {"user_question_request", "done"}
        )
        assert request["type"] == "user_question_request", seen
        assert request["questions"][0]["id"] == "storage"
        response = json.dumps({
            "type": "user_question_response",
            "request_id": request["request_id"],
            "answers": [{"id": "storage", "selected": ["SQLite (Recommended)"]}],
        }) + "\n"
        proc.stdin.write(response)
        proc.stdin.write(response)
        proc.stdin.flush()

        rejected = wait_for(
            lambda event: event.get("type") == "user_question_response_rejected"
        )
        assert rejected["request_id"] == request["request_id"]
        assert "no longer pending" in rejected["reason"]
        assert rejected["retryable"] is False
        if not any(event.get("type") == "done" for event in seen):
            wait_for(lambda event: event.get("type") == "done")

        assert len(server.requests) == 2
        first_tools = server.requests[0].get("tools") or []
        assert any(
            tool.get("function", {}).get("name") == "ask_user_question"
            for tool in first_tools
        )
        second_messages = server.requests[1]["messages"]
        question_call_index = next(
            index
            for index, message in enumerate(second_messages)
            if message.get("role") == "assistant"
            and any(
                call.get("function", {}).get("name") == "ask_user_question"
                for call in message.get("tool_calls") or []
            )
        )
        tool_result_index = next(
            index
            for index, message in enumerate(second_messages)
            if message.get("role") == "tool"
            and "SQLite (Recommended)" in str(message.get("content") or "")
        )
        assert question_call_index < tool_result_index
        assert sum(message.get("role") == "user" for message in second_messages) == 1
        assert sum(event.get("type") == "done" for event in seen) == 1

        _stop_protocol_backend(proc)
        recall_path = tmp_path / "session-recall.db"
        with sqlite3.connect(recall_path) as connection:
            rows = connection.execute(
                "SELECT role, content FROM messages WHERE session_id = "
                "(SELECT id FROM sessions WHERE source_session_key = ?)",
                (session_key,),
            ).fetchall()
        assert {row[0] for row in rows} >= {"user", "assistant"}
        assert _sqlite_path_state(session_recall.DB_PATH) == production_before
    finally:
        _stop_protocol_backend(proc)
        server.shutdown()
        server.server_close()


def test_retired_code_mode_uses_native_tools_and_question_retry_refreshes_working_memory(tmp_path: Path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _QuestionThenPlanOpenAIHandler)
    server.requests = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    proc, _, seen, wait_for = _start_question_protocol_backend(
        tmp_path,
        server.server_port,
        f"backend_question_plan_{uuid.uuid4().hex}",
        settings_overrides={"code_mode": "code"},
    )

    try:
        model_info = wait_for(lambda event: event.get("type") == "model_info")
        assert model_info["code_mode"] == "native"
        seen.clear()
        assert proc.stdin is not None
        proc.stdin.write(json.dumps({"type": "message", "text": "choose and plan"}) + "\n")
        proc.stdin.flush()
        request = wait_for(lambda event: event.get("type") == "user_question_request")

        first_tools = server.requests[0].get("tools") or []
        first_names = [tool.get("function", {}).get("name") for tool in first_tools]
        assert "run_code" not in first_names
        assert "ask_user_question" in first_names
        assert "plan_update" in first_names
        proc.stdin.write(json.dumps({
            "type": "user_question_response",
            "request_id": request["request_id"],
            "answers": [{"id": "storage", "selected": ["Remote"]}],
        }) + "\n")
        proc.stdin.flush()
        retryable = wait_for(
            lambda event: event.get("type") == "user_question_response_rejected"
        )
        assert retryable["request_id"] == request["request_id"]
        assert retryable["retryable"] is True
        assert "was not offered" in retryable["reason"]
        assert not any(
            event.get("type") == "user_question_resolved"
            and event.get("request_id") == request["request_id"]
            for event in seen
        )

        corrected = json.dumps({
            "type": "user_question_response",
            "request_id": request["request_id"],
            "answers": [{"id": "storage", "selected": ["Markdown"]}],
        }) + "\n"
        proc.stdin.write(corrected)
        proc.stdin.write(corrected)
        proc.stdin.flush()

        stale = wait_for(
            lambda event: event.get("type") == "user_question_response_rejected"
        )
        assert stale["request_id"] == request["request_id"]
        assert stale["retryable"] is False
        assert "no longer pending" in stale["reason"]
        plan_result = wait_for(
            lambda event: event.get("type") == "tool_result"
            and event.get("name") == "plan_update"
        )
        assert plan_result["error"] == ""
        working = wait_for(
            lambda event: event.get("type") == "working_memory"
            and event.get("memory", {}).get("goal") == "Implement the selected storage"
        )
        assert working["memory"]["steps"][0]["status"] == "in_progress"
        wait_for(lambda event: event.get("type") == "done")
        assert len(server.requests) == 3
    finally:
        _stop_protocol_backend(proc)
        server.shutdown()
        server.server_close()


def test_question_answer_does_not_approve_following_write(tmp_path: Path):
    target = Path(tempfile.gettempdir()) / f"astra-question-approval-{uuid.uuid4().hex}.txt"
    content = "selected SQLite"
    server = ThreadingHTTPServer(("127.0.0.1", 0), _QuestionThenWriteOpenAIHandler)
    server.requests = []
    server.target_path = target
    server.target_content = content
    threading.Thread(target=server.serve_forever, daemon=True).start()
    proc, _, seen, wait_for = _start_question_protocol_backend(
        tmp_path,
        server.server_port,
        f"backend_question_approval_{uuid.uuid4().hex}",
    )

    try:
        wait_for(lambda event: event.get("type") == "model_info")
        seen.clear()
        assert proc.stdin is not None
        proc.stdin.write(json.dumps({"type": "message", "text": "choose storage and save it"}) + "\n")
        proc.stdin.flush()
        request = wait_for(lambda event: event.get("type") == "user_question_request")
        proc.stdin.write(json.dumps({
            "type": "user_question_response",
            "request_id": request["request_id"],
            "answers": [{"id": "storage", "selected": ["SQLite (Recommended)"]}],
        }) + "\n")
        proc.stdin.flush()

        approval = wait_for(lambda event: event.get("type") == "tool_approval_request")
        assert approval["call_id"] == "call-write-after-question"
        assert approval["tool_name"] == "write_file"
        assert not target.exists()
        assert not any(event.get("type") == "approval_resolved" for event in seen)

        proc.stdin.write(json.dumps({
            "type": "tool_approval_response",
            "request_id": approval["request_id"],
            "decision": "once",
        }) + "\n")
        proc.stdin.flush()
        result = wait_for(
            lambda event: event.get("type") == "tool_result"
            and event.get("name") == "write_file"
        )
        assert result["error"] == ""
        assert target.read_text(encoding="utf-8") == content
        wait_for(lambda event: event.get("type") == "done", timeout=20)
    finally:
        _stop_protocol_backend(proc)
        server.shutdown()
        server.server_close()
        if target.exists():
            target.unlink()


def test_cancel_while_question_pending_ends_task_and_clears_broker(tmp_path: Path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _QuestionOpenAIHandler)
    server.requests = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    proc, events, seen, wait_for = _start_question_protocol_backend(
        tmp_path,
        server.server_port,
        f"backend_question_cancel_{uuid.uuid4().hex}",
    )

    try:
        wait_for(lambda event: event.get("type") == "model_info")
        seen.clear()
        assert proc.stdin is not None
        proc.stdin.write(json.dumps({"type": "message", "text": "choose storage"}) + "\n")
        proc.stdin.flush()
        request = wait_for(lambda event: event.get("type") == "user_question_request")
        started = next(event for event in seen if event.get("type") == "task_started")
        task_id = started["task"]["id"]

        proc.stdin.write(json.dumps({"type": "command", "cmd": "/cancel"}) + "\n")
        proc.stdin.flush()
        terminal = wait_for(
            lambda event: event.get("type") == "task_status"
            and event.get("task", {}).get("status") == "cancelled"
        )
        assert terminal["task"]["id"] == task_id
        resolved_index = next(
            index for index, event in enumerate(seen)
            if event.get("type") == "user_question_resolved"
            and event.get("request_id") == request["request_id"]
            and event.get("state") == "cancelled"
        )
        terminal_index = seen.index(terminal)
        assert resolved_index < terminal_index

        proc.stdin.write(json.dumps({
            "type": "user_question_response",
            "request_id": request["request_id"],
            "answers": [{"id": "storage", "selected": ["SQLite (Recommended)"]}],
        }) + "\n")
        proc.stdin.flush()
        rejected = wait_for(
            lambda event: event.get("type") == "user_question_response_rejected"
        )
        assert "no longer pending" in rejected["reason"]

        time.sleep(0.25)
        while True:
            try:
                seen.append(events.get_nowait())
            except Empty:
                break
        terminal_states = [
            event for event in seen
            if event.get("type") == "task_status"
            and event.get("task", {}).get("id") == task_id
            and event.get("task", {}).get("status") in {"completed", "failed", "cancelled"}
        ]
        assert [event["task"]["status"] for event in terminal_states] == ["cancelled"]
        assert [
            event.get("state") for event in seen
            if event.get("type") == "user_question_resolved"
            and event.get("request_id") == request["request_id"]
        ] == ["cancelled"]
        assert len(server.requests) == 1
    finally:
        _stop_protocol_backend(proc)
        server.shutdown()
        server.server_close()


def test_backend_pauses_tool_call_for_frontend_approval_and_resumes_same_arguments(tmp_path: Path):
    target = Path(tempfile.gettempdir()) / f"agent-tool-approval-{uuid.uuid4().hex}.py"
    content = "<html>" + ("x" * 9_044) + "</html>"
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ApprovalOpenAIHandler)
    server.target_path = target
    server.target_content = content
    server.call_number = 0
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"selected_model": "Qwen3.6-35B-A3B"}), encoding="utf-8")
    models = tmp_path / "models.yaml"
    models.write_text(
        "\n".join([
            "version: 1",
            "providers:",
            "  test:",
            "    label: Test provider",
            "    provider: openai-compatible",
            f"    base_url: http://127.0.0.1:{server.server_port}/v1",
            "    api_key_env: ''",
            "    context_limit: 131072",
            "    capabilities: [tools, streaming]",
            "models:",
            "  Qwen3.6-35B-A3B:",
            "    provider: openai-compatible",
            f"    base_url: http://127.0.0.1:{server.server_port}/v1",
            "    api_key_env: ''",
            "    context_limit: 131072",
            "    capabilities: [tools, streaming]",
            "",
        ]),
        encoding="utf-8",
    )
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    env = os.environ.copy()
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
        "AGENT_SESSION": f"backend_approval_{uuid.uuid4().hex}",
        "QWEN_BASE_URL": f"http://127.0.0.1:{server.server_port}/v1",
        "SANDBOX_DOCKER": "false",
        "AGENT_MCP_CONFIG": str(tmp_path / "missing-mcp.json"),
        "PYTHONUNBUFFERED": "1",
    })
    root = Path(__file__).parents[1]
    proc = subprocess.Popen(
        [sys.executable, "-m", "agent.cli.backend"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=root,
        env=env,
        bufsize=1,
    )
    assert proc.stdin is not None
    assert proc.stdout is not None
    events: Queue[dict] = Queue()

    def read_events():
        assert proc.stdout is not None
        for line in proc.stdout:
            try:
                events.put(json.loads(line))
            except json.JSONDecodeError:
                pass

    threading.Thread(target=read_events, daemon=True).start()

    def wait_for(predicate, timeout: float = 15) -> dict:
        deadline = time.time() + timeout
        seen = []
        while time.time() < deadline:
            if proc.poll() is not None:
                stderr = proc.stderr.read() if proc.stderr is not None else ""
                raise AssertionError(f"Backend exited with {proc.returncode}: {stderr}; seen={seen}")
            try:
                event = events.get(timeout=0.25)
            except Empty:
                continue
            seen.append(event)
            if predicate(event):
                return event
        raise AssertionError(f"Timed out waiting for event; seen={seen}")

    try:
        wait_for(lambda event: event.get("type") == "model_info")
        proc.stdin.write(json.dumps({"type": "message", "text": "write the animation"}) + "\n")
        proc.stdin.flush()
        approval = wait_for(lambda event: event.get("type") == "tool_approval_request")
        assert approval["call_id"] == "call-write-approval"
        assert approval["tool_name"] == "write_file"
        assert approval["target"] == str(target.resolve())
        assert approval["arguments"]["content"] == f"<{len(content)} chars preserved>"
        assert approval["event_id"]
        assert approval["cursor"] > 0
        assert approval["replayable"] is True
        assert not target.exists()

        proc.stdin.write(json.dumps({
            "type": "tool_approval_response",
            "request_id": approval["request_id"],
            "decision": "once",
        }) + "\n")
        proc.stdin.flush()
        result = wait_for(
            lambda event: event.get("type") == "tool_result" and event.get("name") == "write_file"
        )
        assert result["error"] == ""
        persisted = ApprovalInbox(tmp_path / "approvals.db").get(approval["request_id"])
        assert persisted is not None
        assert persisted.state == "approved"
        assert persisted.decision == "once"
        assert persisted.request["arguments"]["content"] == f"<{len(content)} chars preserved>"
        wait_for(lambda event: event.get("type") == "done")
        proc.stdin.write(json.dumps({
            "type": "event_replay",
            "after_cursor": 0,
            "limit": 500,
        }) + "\n")
        proc.stdin.flush()
        replayed_approval = wait_for(
            lambda event: event.get("type") == "tool_approval_request"
            and event.get("request_id") == approval["request_id"]
            and event.get("replayed") is True
        )
        assert replayed_approval["arguments"]["content"] == f"<{len(content)} chars preserved>"
        assert "raw_arguments" not in replayed_approval
        replay_complete = wait_for(
            lambda event: event.get("type") == "event_replay_complete"
        )
        assert replay_complete["count"] > 0
        assert replay_complete["has_more"] is False
        assert replay_complete["next_cursor"] <= replay_complete["cursor"]
        assert target.read_text(encoding="utf-8") == content
        # Initial write request, then a direct completion: the terminal
        # verification gate is advisory, so a non-verifying model is not
        # forced into retry loops.
        assert server.call_number == 2
    finally:
        if proc.poll() is None:
            proc.stdin.write(json.dumps({"type": "exit"}) + "\n")
            proc.stdin.flush()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        server.shutdown()
        server.server_close()
        if target.exists():
            target.unlink()


def test_yolo_is_out_of_band_during_a_live_reply(tmp_path: Path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SlowOpenAIHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    proc, _, seen, wait_for = _start_question_protocol_backend(tmp_path, server.server_port, "yolo_stream")
    try:
        wait_for(lambda event: event.get("type") == "model_info")
        assert proc.stdin is not None
        proc.stdin.write(json.dumps({"type": "message", "text": "keep working"}) + "\n")
        proc.stdin.flush()
        wait_for(lambda event: event.get("type") == "chunk")
        start = len(seen)
        for command, enabled in [("/yolo", True), ("/yolo on", True), ("/yolo off", False), ("/yolo status", False), ("/yolo invalid", False)]:
            proc.stdin.write(json.dumps({"type": "command", "cmd": command}) + "\n")
            proc.stdin.flush()
            event = wait_for(lambda event: event.get("type") == "yolo_status", timeout=5)
            assert event["yolo"] is enabled
            assert bool(event.get("error")) is (command == "/yolo invalid")
        assert not any(event.get("type") in {"done", "tool_result"} for event in seen[start:]), "a control reply must not complete or flush the model turn"
        proc.stdin.write(json.dumps({"type": "command", "cmd": "/cancel"}) + "\n")
        proc.stdin.flush()
        wait_for(lambda event: event.get("type") == "task_status" and event.get("task", {}).get("status") == "cancelled")
    finally:
        _stop_protocol_backend(proc)
        server.shutdown()
        server.server_close()


def test_yolo_releases_an_eligible_pending_approval_without_a_session_grant(tmp_path: Path):
    target = tmp_path / "outside-workspace.txt"
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ApprovalOpenAIHandler)
    server.target_path = target
    server.target_content = "approved by the live switch"
    server.call_number = 0
    threading.Thread(target=server.serve_forever, daemon=True).start()
    proc, _, _, wait_for = _start_question_protocol_backend(tmp_path, server.server_port, "yolo_pending")

    def send(payload):
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(payload) + "\n")
        proc.stdin.flush()

    try:
        wait_for(lambda event: event.get("type") == "model_info")
        send({"type": "message", "text": "write the file"})
        approval = wait_for(lambda event: event.get("type") == "tool_approval_request")
        assert not target.exists()
        send({"type": "command", "cmd": "/yolo on"})
        resolved = wait_for(lambda event: event.get("type") == "approval_resolved", timeout=5)
        assert resolved["request_id"] == approval["request_id"]
        assert resolved["decision"] == "once"
        wait_for(lambda event: event.get("type") == "done")
        assert target.read_text() == server.target_content
        persisted = ApprovalInbox(tmp_path / "approvals.db").get(approval["request_id"])
        assert persisted is not None and persisted.state == "approved"
        send({"type": "command", "cmd": "/yolo off"})
        wait_for(lambda event: event.get("type") == "yolo_status" and not event["yolo"])
        server.call_number = 0
        server.target_content = "must still ask after switching off"
        send({"type": "message", "text": "update the same file"})
        second = wait_for(lambda event: event.get("type") == "tool_approval_request")
        assert second["request_id"] != approval["request_id"]
        send({"type": "tool_approval_response", "request_id": second["request_id"], "decision": "deny"})
        wait_for(lambda event: event.get("type") == "done")
        assert target.read_text() == "approved by the live switch"
    finally:
        _stop_protocol_backend(proc)
        server.shutdown()
        server.server_close()


def test_backend_cancel_clears_pending_approval_without_side_effects(tmp_path: Path):
    target = Path(tempfile.gettempdir()) / f"agent-tool-approval-cancel-{uuid.uuid4().hex}.py"
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ApprovalOpenAIHandler)
    server.target_path = target
    server.target_content = "must not be written"
    server.call_number = 0
    threading.Thread(target=server.serve_forever, daemon=True).start()
    proc, _, _, wait_for = _start_question_protocol_backend(
        tmp_path,
        server.server_port,
        f"backend_approval_cancel_{uuid.uuid4().hex}",
    )

    try:
        wait_for(lambda event: event.get("type") == "model_info")
        assert proc.stdin is not None
        proc.stdin.write(json.dumps({"type": "message", "text": "write the animation"}) + "\n")
        proc.stdin.flush()
        started = wait_for(lambda event: event.get("type") == "task_started")
        approval = wait_for(lambda event: event.get("type") == "tool_approval_request")
        assert approval["call_id"] == "call-write-approval"
        assert approval["tool_name"] == "write_file"
        assert not target.exists()

        proc.stdin.write(json.dumps({"type": "command", "cmd": "/cancel"}) + "\n")
        proc.stdin.flush()
        status = wait_for(
            lambda event: event.get("type") == "task_status"
            and event.get("task", {}).get("status") == "cancelled"
        )
        assert status["task"]["id"] == started["task"]["id"]
        assert status["task"]["status"] == "cancelled"
        assert not target.exists()

        persisted_task = TaskStore(tmp_path / "tasks.db").get_task(started["task"]["id"])
        assert persisted_task is not None
        assert persisted_task["status"] == "cancelled"
        persisted = ApprovalInbox(tmp_path / "approvals.db").get(approval["request_id"])
        assert persisted is not None
        assert persisted.state == "cancelled"

        proc.stdin.write(json.dumps({
            "type": "tool_approval_response",
            "request_id": approval["request_id"],
            "decision": "once",
        }) + "\n")
        proc.stdin.flush()
        rejected = wait_for(
            lambda event: event.get("type") == "approval_response_rejected"
            and event.get("request_id") == approval["request_id"]
        )
        assert "no longer attached" in rejected["reason"]
        assert not target.exists()
        persisted_after_rejection = ApprovalInbox(tmp_path / "approvals.db").get(
            approval["request_id"]
        )
        assert persisted_after_rejection is not None
        assert persisted_after_rejection.state == "cancelled"
        assert persisted_after_rejection.decision == ""
        assert server.call_number == 1
    finally:
        _stop_protocol_backend(proc)
        server.shutdown()
        server.server_close()
        if target.exists():
            target.unlink()


def test_backend_can_cancel_an_active_stream_and_persist_status(tmp_path: Path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SlowOpenAIHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"selected_model": "Qwen3.6-35B-A3B"}), encoding="utf-8")
    task_db = tmp_path / "tasks.db"
    goal = TaskStore(task_db).set_goal("backend_cancel_test", "finish the slow task")
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    env = os.environ.copy()
    env.update({
        "AGENT_SETTINGS_PATH": str(settings),
        "AGENT_SESSION_DIR": str(sessions_dir),
        "AGENT_TASK_DB": str(task_db),
        "ASTRA_EVENT_DB": str(tmp_path / "events.db"),
        "AGENT_MEMORY_PATH": str(tmp_path / "memory.db"),
        "AGENT_LEARNING_PATH": str(tmp_path / "learning.db"),
        "AGENT_SKILLS_PATH": str(tmp_path / "skills"),
        "LEARNING_REVIEW_AUTO": "0",
        "AGENT_SESSION": "backend_cancel_test",
        "QWEN_BASE_URL": f"http://127.0.0.1:{server.server_port}/v1",
        "SANDBOX_DOCKER": "false",
        "AGENT_MCP_CONFIG": str(tmp_path / "missing-mcp.json"),
        "PYTHONUNBUFFERED": "1",
    })
    root = Path(__file__).parents[1]
    proc = subprocess.Popen(
        [sys.executable, "-m", "agent.cli.backend"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=root,
        env=env,
        bufsize=1,
    )
    assert proc.stdin is not None
    assert proc.stdout is not None
    events: Queue[dict] = Queue()

    def read_events():
        assert proc.stdout is not None
        for line in proc.stdout:
            try:
                events.put(json.loads(line))
            except json.JSONDecodeError:
                pass

    threading.Thread(target=read_events, daemon=True).start()

    def wait_for(event_type: str, timeout: float = 12) -> dict:
        deadline = time.time() + timeout
        seen = []
        while time.time() < deadline:
            try:
                event = events.get(timeout=0.25)
            except Empty:
                continue
            seen.append(event)
            if event.get("type") == event_type:
                return event
        raise AssertionError(f"Timed out waiting for {event_type}; seen={seen}")

    try:
        wait_for("model_info")
        proc.stdin.write(json.dumps({"type": "message", "text": "slow task"}) + "\n")
        proc.stdin.flush()
        started = wait_for("task_started")
        task_id = started["task"]["id"]
        proc.stdin.write(json.dumps({"type": "command", "cmd": "/cancel"}) + "\n")
        proc.stdin.flush()
        status = wait_for("task_status")
        assert status["task"]["id"] == task_id
        assert status["task"]["status"] == "cancelled"
        time.sleep(0.5)
        persisted_goal = TaskStore(task_db).get_goal(goal["id"])
        assert persisted_goal is not None
        assert persisted_goal["status"] == "active"
        assert persisted_goal["round"] == 0
    finally:
        if proc.poll() is None:
            proc.stdin.write(json.dumps({"type": "exit"}) + "\n")
            proc.stdin.flush()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        server.shutdown()
        server.server_close()


def test_plain_continue_reuses_budget_interrupted_task(tmp_path):
    session = "budget_resume_protocol"
    store = TaskStore(tmp_path / "tasks.db")
    task = store.start_run("old-request", "finish original work", session_id=session)
    store.checkpoint(task["id"], {"resume_kind": "iteration_budget",
                                 "phase": "turn_stopped", "final_summary": "one step remains"})
    store.finish_run(task["id"], "interrupted")
    server = ThreadingHTTPServer(("127.0.0.1", 0), _GoalOpenAIHandler)
    server.main_calls = 0
    server.verifier_calls = 0
    threading.Thread(target=server.serve_forever, daemon=True).start()
    proc = None
    try:
        proc, _, _, wait_for = _start_question_protocol_backend(tmp_path, server.server_port, session)
        wait_for(lambda e: e.get("type") == "startup_banner")
        proc.stdin.write(json.dumps({"type": "message", "text": "继续"}) + "\n")
        proc.stdin.flush()
        started = wait_for(lambda e: e.get("type") == "task_started")
        assert started["task"]["id"] == task["id"]
        finished = wait_for(lambda e: e.get("type") == "task_status"
                            and e.get("task", {}).get("status") == "completed")
        assert finished["task"]["resume_count"] == 1
        assert len(store.list_tasks()) == 1
    finally:
        if proc is not None:
            _stop_protocol_backend(proc)
        server.shutdown()
        server.server_close()


def test_time_budget_interrupts_real_backend_and_only_explicit_continue_restarts_it(tmp_path):
    # Exercise interruption of an active stream, not a race against cold
    # backend/database startup on slower CI hosts.
    budget_seconds = 3
    release = threading.Event()
    requests = []
    class Handler(_SlowOpenAIHandler):
        def do_POST(self):
            if not self.path.endswith("/chat/completions"):
                self.send_error(404)
                return
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            requests.append(time.monotonic())
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            chunk = {"id": "budget", "object": "chat.completion.chunk", "created": 0,
                     "model": "test", "choices": [{"index": 0, "delta": {"content": "working"}, "finish_reason": None}]}
            try:
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                self.wfile.flush()
                release.wait(budget_seconds * 5)
            except (BrokenPipeError, ConnectionResetError):
                pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    proc = None
    try:
        proc, _, seen, wait_for = _start_question_protocol_backend(tmp_path, server.server_port, "time_budget_test")
        wait_for(lambda e: e.get("type") == "startup_banner")
        def send(payload):
            proc.stdin.write(json.dumps(payload) + "\n")
            proc.stdin.flush()
        send({"type": "command", "cmd": f"/budget {budget_seconds}"})
        wait_for(lambda e: e.get("type") == "done")
        task_id = ""
        for number, message in enumerate(["do work", "继续"], 1):
            send({"type": "message", "text": message})
            started = wait_for(lambda e: e.get("type") == "task_started")["task"]
            if task_id:
                assert started["id"] == task_id
            task_id = started["id"]
            wait_for(lambda e: e.get("code") == "turn_budget_exhausted")
            finished = wait_for(lambda e: e.get("type") == "task_status")["task"]
            assert finished["status"] == "interrupted"
            assert finished["checkpoint"]["resume_kind"] == "time_budget"
            assert finished["resume_count"] == number - 1
            wait_for(lambda e: e.get("type") == "model_info")
            time.sleep(0.1)
            assert len(requests) == number
        assert len([e for e in seen if e.get("type") == "done"]) == 3  # budget command plus two turns
    finally:
        release.set()
        if proc is not None:
            _stop_protocol_backend(proc)
        server.shutdown()
        server.server_close()
