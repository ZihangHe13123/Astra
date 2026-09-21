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
        payload = json.dumps({"data": [{"id": "fresh-model", "context_length": 65536}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.mark.parametrize("unconfigured", [False, True])
def test_provider_flow_over_real_ipc(tmp_path, monkeypatch, unconfigured):
    monkeypatch.setenv("ASTRA_HOME", str(tmp_path / "state"))
    server = ThreadingHTTPServer(("127.0.0.1", 0), ModelsHandler)
    server.slow = False
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
        selector = f"{provider}::fresh-model"
        send({"type": "command", "cmd": f"/model {selector}"})
        selected = wait(lambda e: e.get("type") == "model_info" and e.get("model_key") == selector)
        assert selected["recent_models"][0] == selector
        assert json.loads((tmp_path / "settings.json").read_text())["selected_model"] == selector
        # A slow GET must not block live controls or a second provider's UI.
        server.slow = True
        send({"type": "refresh_models", "provider_id": provider, "force": True})
        assert server.started.wait(2)
        send({"type": "command", "cmd": "/yolo status"})
        wait(lambda e: e.get("type") == "yolo_status", timeout=2)
        server.release.set()
        wait(lambda e: e.get("type") == "model_info" and e.get("model_key") == selector)
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
