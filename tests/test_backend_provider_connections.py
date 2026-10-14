"""Real backend IPC: credentials, provider discovery, switching and responsiveness."""
import json
import threading

import pytest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from dotenv import dotenv_values

from test_backend_task_protocol import _start_question_protocol_backend, _stop_protocol_backend


def test_backend_starts_with_example_env_and_bundled_models(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    example = root / ".env.example"
    # The standalone backend pins ASTRA_ENV_FILE to its installation. Pass the
    # parsed example as process values to test first-run settings without
    # writing an .env into the checkout or inheriting the developer's keys.
    example_values = {name: value for name, value in dotenv_values(example).items() if value is not None}
    monkeypatch.setenv("ASTRA_HOME", str(tmp_path / "state"))
    proc, _, _, wait = _start_question_protocol_backend(
        tmp_path, 0, "example-env-startup", env_overrides={
            **example_values,
            "AGENT_MODELS_FILE": str(root / "config" / "models.yaml"),
        },
    )
    try:
        initial = wait(lambda event: event.get("type") == "model_info")
        hunyuan = next(model for model in initial["models"] if model["name"] == "hy4-preview")
        assert hunyuan["endpoint"] == "https://tokenhub.tencentmaas.com/v1"
        assert initial["connection_routes"]
        assert proc.stdin is not None
        proc.stdin.write(json.dumps({"type": "command", "cmd": "/yolo status"}) + "\n")
        proc.stdin.flush()
        wait(lambda event: event.get("type") == "yolo_status")
    finally:
        _stop_protocol_backend(proc)


def test_aliyun_deepseek_startup_reports_half_million_input_budget(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("ASTRA_HOME", str(tmp_path / "state"))
    proc, _, _, wait = _start_question_protocol_backend(
        tmp_path, 0, "aliyun-context", settings_overrides={
            "selected_model": "qwen38::deepseek-v4.1-flash",
        }, env_overrides={
            "AGENT_MODELS_FILE": str(root / "config" / "models.yaml"),
            "QWEN38_API_KEY": "fixture-only-key",
        },
    )
    try:
        initial = wait(lambda event: event.get("type") == "model_info")
        assert initial["model"] == "deepseek-v4.1-flash"
        assert initial["model_key"] == "qwen38::deepseek-v4.1-flash"
        assert initial["context_limit"] == 500_000
        model = next(item for item in initial["models"] if item["current"])
        assert model["context_limit"] == 1_000_000
        assert model["metadata_known"]
        assert "token-plan.cn-beijing.maas.aliyuncs.com" in model["endpoint"]
    finally:
        _stop_protocol_backend(proc)


class ModelsHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.server.slow:
            self.server.started.set()
            self.server.release.wait(5)
        self.server.requests.append((self.path, self.headers.get("Authorization", "")))
        payload = json.dumps({"data": [{"id": self.server.model_id,
                                       "context_length": self.server.context_length}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.mark.parametrize("unconfigured", [False, True])
@pytest.mark.parametrize("manual_model", [False, True])
def test_provider_flow_over_real_ipc(tmp_path, monkeypatch, unconfigured, manual_model):
    monkeypatch.setenv("ASTRA_HOME", str(tmp_path / "state"))
    server = ThreadingHTTPServer(("127.0.0.1", 0), ModelsHandler)
    server.slow = False
    server.model_id, server.context_length = "fresh-model", 65536
    server.started, server.release = threading.Event(), threading.Event()
    server.requests = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    overrides = {}
    if unconfigured:
        config = tmp_path / "unconfigured.yaml"
        config.write_text("models:\n  Qwen3.6-35B-A3B:\n    base_url: https://unconfigured.example/v1\n    api_key_env: ABSENT_TEST_PROVIDER_KEY\n    context_limit: 32768\n")
        monkeypatch.delenv("ABSENT_TEST_PROVIDER_KEY", raising=False)
        overrides["AGENT_MODELS_FILE"] = str(config)
    proc, _, seen, wait = _start_question_protocol_backend(tmp_path, server.server_port, "provider-flow", env_overrides=overrides)
    def send(payload):
        proc.stdin.write(json.dumps(payload) + "\n")
        proc.stdin.flush()
    try:
        initial = wait(lambda e: e.get("type") == "model_info")
        original_key = initial["model_key"]
        assert initial["connection_routes"] and initial["providers"]
        send({"type": "connect_provider", "request_id": "connect-1", "route_id": "custom",
              "base_url": f"http://127.0.0.1:{server.server_port}/v1", "api_key": "PRIVATE-CONNECT-KEY", "api_key_env": ""})
        connected = wait(lambda e: e.get("type") == "connection_result")
        assert not connected["error"]
        assert all(e.get("model_key", original_key) == original_key for e in seen)
        provider = connected["provider_id"]
        selected_model = "gpt-6.1-sol" if manual_model else "fresh-model"
        selector = f"{provider}::{selected_model}"
        send({"type": "command", "cmd": f"/model {selector}"})
        selected = wait(lambda e: e.get("type") == "model_info" and e.get("model_key") == selector)
        assert selected["recent_models"][0] == selector
        assert selected["context_limit"] == (24_576 if manual_model else 32_768)
        assert json.loads((tmp_path / "settings.json").read_text())["selected_model"] == selector
        # A slow GET must not block live controls or a second provider's UI.
        server.slow = True
        server.model_id, server.context_length = selected_model, 272_000
        send({"type": "refresh_models", "provider_id": provider, "force": True})
        assert server.started.wait(2)
        send({"type": "command", "cmd": "/yolo status"})
        wait(lambda e: e.get("type") == "yolo_status", timeout=2)
        server.release.set()
        refreshed = wait(lambda e: e.get("type") == "model_info" and e.get("model_key") == selector)
        assert refreshed["context_limit"] == (263_808 if manual_model else 136_000)
        current = next(item for item in refreshed["models"] if item["current"])
        assert current["context_limit"] == 272_000
        assert current["source"] == "live"
        assert refreshed["context_pct"] == round(refreshed["context_used"] / refreshed["context_limit"] * 100, 1)
        assert "PRIVATE-CONNECT-KEY" not in json.dumps(seen)
        assert any(auth == "Bearer PRIVATE-CONNECT-KEY" for _, auth in server.requests)
    finally:
        server.release.set()
        _stop_protocol_backend(proc)
        server.shutdown()
        server.server_close()
    for path in (tmp_path / "sessions").glob("*.json*"):
        assert "PRIVATE-CONNECT-KEY" not in path.read_text()


def test_desktop_model_selection_receipts_are_correlated_without_changing_tui(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRA_HOME", str(tmp_path / "state"))
    proc, _, _, wait = _start_question_protocol_backend(tmp_path, 0, "desktop-model-selection")

    def send(payload):
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(payload) + "\n")
        proc.stdin.flush()

    try:
        initial = wait(lambda e: e.get("type") == "model_info")
        original = initial["model_key"]
        send({"type": "command", "cmd": "/model definitely-missing", "request_id": "select-missing"})
        missing = wait(lambda e: e.get("type") == "model_selection_result")
        assert missing["request_id"] == "select-missing"
        assert missing["model_key"] == original
        assert missing["code"] == "unknown_model"
        assert "Unknown model: definitely-missing" in missing["error"]
        assert "Current model unchanged" in missing["error"]

        selector = "test::another-model"
        send({"type": "command", "cmd": f"/model {selector}", "request_id": "select-valid"})
        selected = wait(lambda e: e.get("type") == "model_selection_result")
        assert selected["request_id"] == "select-valid"
        assert selected["model_key"] == selector
        assert not selected["error"]
        assert json.loads((tmp_path / "settings.json").read_text())["selected_model"] == selector

        # The ordinary TUI command retains the existing output/error contract.
        send({"type": "command", "cmd": "/model definitely-missing"})
        tui = wait(lambda e: e.get("type") == "tool_result" and e.get("name") == "model")
        assert "request_id" not in tui
        assert tui["error"] == ""
        assert tui["output"].startswith("Unknown model: definitely-missing")
    finally:
        _stop_protocol_backend(proc)


def test_codex_context_override_survives_startup_refresh_and_reselection(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRA_HOME", str(tmp_path / "state"))
    overrides = tmp_path / "user-models.yaml"
    overrides.write_text("""models:
  gpt-6.1-sol:
    provider: openai-codex
    base_url: https://chatgpt.com/backend-api/codex
    api_key_env: ''
    catalog_provider: codex
    context_window: 512000
    auto_compact_token_limit: 450000
    capabilities: [tools, reasoning, streaming, vision]
""", encoding="utf-8")
    bootstrap = """
import runpy
from agent.cli import model_cache, model_catalog, provider_connections
from agent.runtime import codex_auth
codex_auth._write({'access_token': 'fixture-only-token', 'account_id': 'fixture-account'})
provider_connections.save_connection(*provider_connections.connection_record('codex'))
items = [{'id': 'gpt-6.1-sol', 'context_length': 272000,
          'supported_parameters': ['tools', 'reasoning'], 'input_modalities': ['text', 'image'],
          'reasoning_levels': ['low', 'medium', 'high', 'xhigh', 'max', 'ultra']}]
endpoint = next(e for e in model_catalog.provider_endpoints() if e.id == 'codex')
model_cache.write_cache(endpoint, items)
restored = model_catalog.configured_model_catalog().resolve('codex::gpt-6.1-sol')
assert restored.profile.reasoning_levels == tuple(items[0]['reasoning_levels'])
async def fetch_models():
    return items
codex_auth.fetch_models = fetch_models
runpy.run_module('agent.cli.backend', run_name='__main__')
"""
    proc, _, _, wait = _start_question_protocol_backend(
        tmp_path, 0, "codex-context-override",
        settings_overrides={"selected_model": "codex::gpt-6.1-sol"},
        env_overrides={"AGENT_USER_MODELS_FILE": str(overrides)}, bootstrap_code=bootstrap,
    )

    def send(payload):
        proc.stdin.write(json.dumps(payload) + "\n")
        proc.stdin.flush()

    def assert_context(event):
        assert event["model_key"] == "codex::gpt-6.1-sol"
        assert event["context_limit"] == 450_000
        assert next(item for item in event["models"] if item["current"])["context_limit"] == 512_000

    try:
        assert_context(wait(lambda e: e.get("type") == "model_info"))
        send({"type": "refresh_models", "provider_id": "codex", "force": True})
        refreshed = wait(lambda e: e.get("type") == "model_info")
        assert_context(refreshed)
        assert next(item for item in refreshed["models"] if item["current"])["source"] == "live"
        send({"type": "command", "cmd": "/model test::Qwen3.6-35B-A3B"})
        other = wait(lambda e: e.get("type") == "model_info")
        assert other["model_key"] == "test::Qwen3.6-35B-A3B"
        assert other["context_limit"] == 65_536
        send({"type": "command", "cmd": "/model codex::gpt-6.1-sol"})
        assert_context(wait(lambda e: e.get("type") == "model_info"))
    finally:
        _stop_protocol_backend(proc)
