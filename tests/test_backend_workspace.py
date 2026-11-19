"""A backend started the way the terminal interface starts it works in the person's workspace.

``astra`` runs the terminal interface in the Astra installation and names the folder it was started
from in ASTRA_WORKSPACE (an explicit SANDBOX_WORKDIR in the .env wins); the interface then starts its
backend with AGENT_PROJECT_ROOT, the installation, as the current directory. The desktop starts each
backend in its workspace instead. Either way the file tools, the shell and the project scope of saved
memories have to agree on the workspace.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Empty

from agent.runtime.context_index.workspace import resolve_workspace
from agent.runtime.memory import MemoryStore
from test_backend_task_protocol import _start_question_protocol_backend, _stop_protocol_backend, backend_environment

CHECKOUT = Path(__file__).resolve().parents[1]
NOTE = "workspace note 7f3a\n"


class _ScriptedToolsHandler(BaseHTTPRequestHandler):
    """Answers with ``server.script[n]`` as a tool call after n tool results, then with plain text."""

    def log_message(self, format, *args):
        return

    def _send_json(self, payload: dict):
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_sse(self, chunks: list[dict]):
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
            self._send_json({"object": "list", "data": [{"id": "Qwen3.6-35B-A3B", "meta": {"n_ctx": 131072}}]})
        else:
            self.send_error(404)

    def do_POST(self):
        if not self.path.endswith("/chat/completions"):
            self.send_error(404)
            return
        payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
        results = sum(1 for message in payload.get("messages") or []
                      if isinstance(message, dict) and message.get("role") == "tool")
        base = {"id": f"chatcmpl-workspace-{results}", "object": "chat.completion.chunk",
                "created": int(time.time()), "model": "Qwen3.6-35B-A3B"}
        if results < len(self.server.script):
            name, arguments = self.server.script[results]
            delta = {"role": "assistant", "tool_calls": [{
                "index": 0, "id": f"call-workspace-{results}", "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }]}
            finish = "tool_calls"
        else:
            delta, finish = {"role": "assistant", "content": "done"}, "stop"
        self._send_sse([
            {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
            {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]},
        ])


REMEMBER = ("memory", {"action": "remember", "scope": "project", "kind": "observation",
                       "summary": "This project keeps its notes in notes/"})


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    (workspace / "notes").mkdir(parents=True)
    (workspace / "notes" / "x.md").write_text(NOTE, encoding="utf-8")
    assert not (CHECKOUT / "notes" / "x.md").exists()
    return workspace


def _model(script: list[tuple[str, dict]]) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ScriptedToolsHandler)
    server.script = script
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _project_memory_keys(memory_db: Path) -> list[str]:
    return [record.metadata.get("workspace_key") for record in MemoryStore(memory_db).record_store.list_records()
            if record.metadata.get("scope") == "project"]


def test_terminal_interface_backend_works_in_the_workspace_not_the_installation(tmp_path: Path):
    workspace = _workspace(tmp_path)
    server = _model([REMEMBER, ("read_file", {"path": "notes/x.md"}),
                     ("read_file", {"path": str(workspace / "notes" / "x.md")})])
    proc, events, seen, wait_for = _start_question_protocol_backend(
        tmp_path, server.server_port, "terminal-workspace",
        # The interface's backend: started in the installation, told the workspace by the launcher.
        workdir=CHECKOUT,
        env_overrides={"AGENT_PROJECT_ROOT": str(CHECKOUT), "ASTRA_WORKSPACE": str(workspace),
                       "SANDBOX_WORKDIR": str(workspace)},
    )
    approvals: list[dict] = []
    try:
        wait_for(lambda event: event.get("type") == "model_info", timeout=30)
        assert proc.stdin is not None
        proc.stdin.write(json.dumps({"type": "message", "text": "Note where my notes are, then read them."}) + "\n")
        proc.stdin.flush()
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                event = events.get(timeout=0.25)
            except Empty:
                assert proc.poll() is None, f"Backend exited; seen={seen}"
                continue
            seen.append(event)
            if event.get("type") == "tool_approval_request":
                approvals.append(event)
                proc.stdin.write(json.dumps({"type": "tool_approval_response",
                                             "request_id": event["request_id"], "decision": "deny"}) + "\n")
                proc.stdin.flush()
            elif event.get("type") == "done":
                break
        else:
            raise AssertionError(f"The turn did not finish; seen={[event.get('type') for event in seen]}")
    finally:
        _stop_protocol_backend(proc)
        server.shutdown()
        server.server_close()

    results = {event["call_id"]: event for event in seen if event.get("type") == "tool_result"}
    outcomes = {call: result["error"] or result["output"][:300] for call, result in results.items()}
    remembered, relative, absolute = (results.get(f"call-workspace-{index}") for index in range(3))
    asked = [(event.get("call_id"), event.get("tool_name")) for event in approvals]
    assert asked == [], f"a file in the workspace was treated as outside it; results={outcomes}"
    assert relative is not None and not relative["error"] and NOTE.strip() in relative["output"], outcomes
    assert absolute is not None and not absolute["error"] and NOTE.strip() in absolute["output"], outcomes
    # A project memory belongs to the workspace too, not to the installation the backend started in.
    assert remembered is not None and not remembered["error"], outcomes
    assert _project_memory_keys(tmp_path / "memory.db") == [resolve_workspace(workspace).key]


def test_text_cli_started_in_the_installation_works_in_the_workspace(tmp_path: Path):
    """``astra --cli`` is started in the installation as well; its file tools always took SANDBOX_WORKDIR."""
    workspace = _workspace(tmp_path)
    server = _model([REMEMBER, ("read_file", {"path": "notes/x.md"})])
    (tmp_path / "settings.json").write_text(json.dumps({"selected_model": "Qwen3.6-35B-A3B"}), encoding="utf-8")
    (tmp_path / "models.yaml").write_text("\n".join([
        "version: 1",
        "models:",
        "  Qwen3.6-35B-A3B:",
        "    provider: openai-compatible",
        f"    base_url: http://127.0.0.1:{server.server_port}/v1",
        "    api_key_env: ''",
        "    context_limit: 131072",
        "    capabilities: [tools, streaming]",
        "",
    ]), encoding="utf-8")
    env = backend_environment(tmp_path)
    env.update({
        "AGENT_SETTINGS_PATH": str(tmp_path / "settings.json"),
        "AGENT_MODELS_FILE": str(tmp_path / "models.yaml"),
        "AGENT_USER_MODELS_FILE": str(tmp_path / "missing-models.yaml"),
        "AGENT_SESSION_DIR": str(tmp_path / "sessions"),
        "AGENT_TASK_DB": str(tmp_path / "tasks.db"),
        "ASTRA_APPROVAL_DB": str(tmp_path / "approvals.db"),
        "ASTRA_EVENT_DB": str(tmp_path / "events.db"),
        "AGENT_MEMORY_PATH": str(tmp_path / "memory.db"),
        "AGENT_SKILLS_PATH": str(tmp_path / "skills"),
        "AGENT_MCP_CONFIG": str(tmp_path / "missing-mcp.json"),
        "LEARNING_REVIEW_AUTO": "0",
        "SANDBOX_DOCKER": "false",
        "AGENT_PROJECT_ROOT": str(CHECKOUT), "ASTRA_WORKSPACE": str(workspace), "SANDBOX_WORKDIR": str(workspace),
        "PYTHONUNBUFFERED": "1",
    })
    try:
        completed = subprocess.run([sys.executable, "-m", "agent.cli.main"], cwd=CHECKOUT, env=env,
                                   input="Note where my notes are, then read them.\n",
                                   capture_output=True, text=True, encoding="utf-8", timeout=120)
    finally:
        server.shutdown()
        server.server_close()

    output = f"stdout={completed.stdout[-3000:]}\nstderr={completed.stderr[-3000:]}"
    assert completed.returncode == 0, output
    transcripts = "".join(path.read_text(encoding="utf-8") for path in tmp_path.rglob("*.jsonl")
                          if workspace not in path.parents)
    assert NOTE.strip() in transcripts, output
    assert _project_memory_keys(tmp_path / "memory.db") == [resolve_workspace(workspace).key], output
