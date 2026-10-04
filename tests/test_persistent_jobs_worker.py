"""Actual job workers against a loopback provider, including process budgets."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from agent.runtime.jobs import scheduler
from agent.runtime.jobs.store import JobStore
from agent.runtime.session_store import SessionStore

ROOT = Path(__file__).resolve().parents[1]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if "messages" not in body:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"tokens": []}')
            return
        self.server.requests.append({"body": body, "authorization": self.headers.get("Authorization")})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        if len(self.server.requests) == 1 and self.server.try_write:
            delta = {"tool_calls": [{"index": 0, "id": "unauthorized-write", "type": "function", "function": {
                "name": "write_file", "arguments": json.dumps({"path": str(self.server.target), "content": "MODIFIED"})}}]}
            reason = "tool_calls"
        else:
            delta = {"content": "fixture-check-result"}
            reason = "stop"
        for chunk in ({"choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                      {"choices": [{"index": 0, "delta": {}, "finish_reason": reason}]}):
            self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


@pytest.fixture
def provider(tmp_path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.requests = []
    server.try_write = False
    server.target = tmp_path / "protected.txt"
    server.target.write_text("ORIGINAL", encoding="utf-8")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def environment(home, provider):
    home.mkdir(exist_ok=True)
    models = home / "models.yaml"
    models.write_text(f"""version: 1
models:
  fixture:
    provider: openai-compatible
    model_id: jobs-fixture
    base_url: http://127.0.0.1:{provider.server_port}/v1
    context_limit: 8192
    capabilities: [tools]
    api_key_env: JOBS_FIXTURE_API_KEY
    generation:
      max_tokens: 512
""", encoding="utf-8")
    (home / ".env").write_text(f"JOBS_FIXTURE_API_KEY=token-{home.name}\n", encoding="utf-8")
    return {**os.environ, "ASTRA_HOME": str(home), "ASTRA_ENV_FILE": str(home / ".env"),
            "AGENT_MODELS_FILE": str(models), "AGENT_USER_MODELS_FILE": str(home / "user-models.yaml"),
            "AGENT_SETTINGS_PATH": str(home / "settings.json"), "ASTRA_PROJECT_TRUST": "trusted",
            "AGENT_SESSION_DIR": str(home / "live-sessions"), "AGENT_TOOL_POLICY": "permissive",
            "JOBS_FIXTURE_API_KEY": "stale-parent-credential",
            "LLM_STREAM_TIMEOUT": "10", "LLM_MAX_RETRIES": "0"}


def command(env, *args):
    process = subprocess.run([sys.executable, "-m", "agent.cli.jobs_commands", *args], cwd=ROOT,
                             env=env, text=True, capture_output=True, timeout=40)
    assert process.returncode == 0, process.stderr
    return json.loads(process.stdout)


def test_real_worker_fresh_context_read_only_tools_result_and_receipt(tmp_path, provider):
    work = tmp_path / "work"
    (work / ".git").mkdir(parents=True)
    (work / "AGENTS.md").write_text("JOBS_GUIDANCE_VERSION_ONE", encoding="utf-8")
    home = tmp_path / "A"
    env = environment(home, provider)
    live = home / "live-sessions" / "default.json"
    live.parent.mkdir()
    live.write_text('{"sentinel":"LIVE_SESSION"}', encoding="utf-8")
    original = live.read_bytes()
    provider.try_write = True
    job = command(env, "add", "check", "--after", "60", "--model", "fixture", "--workdir", str(work),
                  "--prompt", "Read the project status", "--request", "Please check my project periodically")
    result = command(env, "run", job["id"])
    run = result["executed"][0]
    assert run["state"] == "completed", command(env, "history")
    assert live.read_bytes() == original
    assert provider.target.read_text(encoding="utf-8") == "ORIGINAL"
    request = provider.requests[0]
    assert request["authorization"] == "Bearer token-A"
    assert "JOBS_GUIDANCE_VERSION_ONE" in request["body"]["messages"][0]["content"]
    assert "LIVE_SESSION" not in json.dumps(request["body"])
    names = {tool["function"]["name"] for tool in request["body"]["tools"]}
    assert "read_file" in names and not names.intersection({"write_file", "execute_shell", "delegate", "send_message"})
    assert not names.intersection({"search_web", "fetch_url"})
    inbox = command(env, "inbox")
    assert inbox[0]["output"] == "fixture-check-result" and inbox[0]["delivery_state"] == "delivered"
    saved = SessionStore(home / "jobs" / "runs" / run["id"] / "session.json").load(readonly=True)
    assert saved["messages"][-1]["content"] == "fixture-check-result"
    (work / "AGENTS.md").write_text("JOBS_GUIDANCE_VERSION_TWO", encoding="utf-8")
    before = len(provider.requests)
    command(env, "run", job["id"])
    assert "JOBS_GUIDANCE_VERSION_TWO" in provider.requests[before]["body"]["messages"][0]["content"]
    assert len(command(env, "inbox")) == 2


def test_real_worker_secrets_and_results_follow_a_to_b_to_a_scope(tmp_path, provider):
    for name in ("A", "B", "A"):
        env = environment(tmp_path / name, provider)
        job = command(env, "add", f"check-{name}", "--after", "60", "--model", "fixture", "--workdir", str(tmp_path),
                      "--prompt", f"Check {name}", "--request", f"User authorized {name}", "--web")
        before = len(provider.requests)
        result = command(env, "run", job["id"])
        assert result["executed"][0]["state"] == "completed", command(env, "history")
        request = provider.requests[before]
        assert request["authorization"] == f"Bearer token-{name}"
        names = {tool["function"]["name"] for tool in request["body"]["tools"]}
        assert {"search_web", "fetch_url"} <= names
        assert all(row["spec"]["name"].endswith(name) for row in command(env, "inbox"))
        saved = SessionStore(tmp_path / name / "jobs" / "runs" / result["executed"][0]["id"] / "session.json").load(readonly=True)
        assert f"token-{name}" not in json.dumps(saved)


@pytest.mark.parametrize("active,expected", [(False, "inactivity limit"), (True, "hard run limit")])
def test_worker_supervision_separates_idle_from_absolute_time(tmp_path, monkeypatch, active, expected):
    store = JobStore(tmp_path / "home")
    store.add(name="check", prompt="fixture", original_request="fixture", model="fixture", workdir=tmp_path,
              next_at=1000, idle_seconds=1, timeout_seconds=3)
    run = store.claim_due(now=1000)
    progress = store.run_dir(run["id"]) / "progress"
    original_popen = subprocess.Popen
    script = """
import sys,time
from pathlib import Path
from agent.runtime.jobs.store import JobStore
store=JobStore(Path(sys.argv[1])); lock=store.run_lock(sys.argv[2]); lock.acquire()
store.start(sys.argv[2]); progress=Path(sys.argv[3]); progress.touch()
while True:
    time.sleep(.15)
    if sys.argv[4]=='active': progress.touch()
"""
    children = []
    def spawn(_args, **kwargs):
        child = original_popen([sys.executable, "-c", script, str(store.home), run["id"], str(progress),
                                "active" if active else "idle"], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(scheduler.subprocess, "Popen", spawn)
    started = time.monotonic()
    scheduler.execute(store, run)
    elapsed = time.monotonic() - started
    assert children[0].poll() is not None
    assert store.run(run["id"])["state"] == "unknown"
    assert expected in store.run(run["id"])["detail"]
    assert elapsed >= (2.9 if active else 0.9)
    assert scheduler.recover_abandoned(store) == 0
    assert store.claim_due(now=1000) is None
