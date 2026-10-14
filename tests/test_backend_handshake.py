import asyncio

import pytest

from agent.cli import backend


def test_ipc_handshake_precedes_optional_initialization(monkeypatch):
    events = []
    monkeypatch.setattr(backend, "_write_event", events.append)

    def cold_initialization(*args):
        assert len(events) == 1
        assert events[0]["type"] == "backend_hello"
        assert events[0]["protocol_version"] == 1
        assert "ui_queries" in events[0]["capabilities"]
        raise RuntimeError("simulated initialization failure")

    monkeypatch.setattr(backend, "load_project_env", cold_initialization)
    with pytest.raises(RuntimeError, match="simulated initialization failure"):
        asyncio.run(backend.main())


@pytest.fixture
def cold_backend_source(tmp_path):
    import shutil
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    source = tmp_path / "source"
    shutil.copytree(root / "agent", source / "agent", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copyfile(root / "pyproject.toml", source / "pyproject.toml")
    return source


def _cold_environment(source, tmp_path):
    import os

    env = os.environ.copy()
    env.update({
        "PYTHONPATH": str(source), "PYTHONUNBUFFERED": "1",
        "ASTRA_HOME": str(tmp_path / "home"), "AGENT_SESSION_DIR": str(tmp_path / "sessions"),
        "AGENT_SESSION": "cold-startup", "AGENT_MCP_CONFIG": str(tmp_path / "no-mcp.json"),
        "SANDBOX_DOCKER": "false", "ASTRA_UI_SURFACE": "gui", "ASTRA_GUI_DISABLE_APPSHOT": "1",
        "LEARNING_REVIEW_AUTO": "0", "ASTRA_PROFILE_STARTUP": "1",
    })
    return env


@pytest.mark.parametrize("release_action", ["continue", "fail"])
def test_script_handshake_precedes_blocked_optional_import(cold_backend_source, tmp_path, release_action):
    import json
    import os
    import subprocess
    import sys
    import time

    source = cold_backend_source
    entered, release = tmp_path / "import-entered", tmp_path / "import-release"
    hook = tmp_path / "hook"
    hook.mkdir()
    (hook / "sitecustomize.py").write_text(
        "import sys,time\nfrom pathlib import Path\n"
        f"root=Path({str(tmp_path)!r})\n"
        "class Gate:\n"
        " def find_spec(self, fullname, path=None, target=None):\n"
        "  if fullname != 'openai': return None\n"
        "  (root/'import-entered').write_text('blocked')\n"
        "  deadline=time.monotonic()+45\n"
        "  while not (root/'import-release').exists():\n"
        "   if time.monotonic()>deadline: raise ImportError('optional import gate timed out')\n"
        "   time.sleep(.02)\n"
        + ("  raise ImportError('optional import fixture released with failure')\n" if release_action == "fail" else "  return None\n")
        + "sys.meta_path.insert(0,Gate())\n", encoding="utf-8",
    )
    env = _cold_environment(source, tmp_path)
    env["PYTHONPATH"] = os.pathsep.join([str(hook), str(source)])
    models = tmp_path / "models.yaml"
    models.write_text("version: 1\nmodels:\n  fixture:\n    provider: openai-compatible\n"
                      "    base_url: http://127.0.0.1:9/v1\n    api_key_env: ''\n"
                      "    context_limit: 32768\n    capabilities: [tools, streaming]\n", encoding="utf-8")
    env.update(AGENT_MODELS_FILE=str(models), AGENT_USER_MODELS_FILE=str(tmp_path / "no-models.yaml"),
               LLM_MODEL="fixture", ASTRA_ENV_FILE=str(tmp_path / "no-env"))
    output, diagnostic = tmp_path / "stdout.jsonl", tmp_path / "stderr.log"
    def events():
        return [json.loads(line) for line in output.read_bytes().split(b"\n")[:-1] if line.startswith(b"{")]
    with output.open("wb") as stdout, diagnostic.open("wb") as stderr:
        process = subprocess.Popen([sys.executable, "-m", "agent.cli.backend"], cwd=source, env=env,
                                   stdin=subprocess.PIPE, stdout=stdout, stderr=stderr)
        try:
            deadline = time.monotonic() + 20
            while not entered.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(.02)
            assert entered.exists(), diagnostic.read_text(encoding="utf-8")
            seen = events()
            assert [event["type"] for event in seen] == ["backend_hello"], "protocol must connect while optional imports are still blocked"
            assert seen[0] == {"type": "backend_hello", "protocol_version": 1,
                               "capabilities": ["ui_queries", "session_ownership", "gui_ready", "response_versions"]}
            release.touch()
            if release_action == "continue":
                deadline = time.monotonic() + 20
                while not any(event.get("type") == "gui_ready" for event in events()) and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(.02)
                assert any(event.get("type") == "gui_ready" for event in events()), diagnostic.read_text(encoding="utf-8")
        finally:
            release.touch()
            try:
                process.communicate(b'{"type":"exit"}\n', timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
                raise
    seen = events()
    assert len([event for event in seen if event.get("type") == "backend_hello"]) == 1
    if release_action == "fail":
        assert process.returncode != 0
        assert "optional import fixture released with failure" in diagnostic.read_text(encoding="utf-8")
        assert not any(event.get("type") == "gui_ready" for event in seen)
    else:
        assert process.returncode == 0, diagnostic.read_text(encoding="utf-8")


@pytest.mark.parametrize("marker", ["pending.json", "services.json", "desktop-pending.json"])
def test_script_rejects_pending_update_before_handshake(cold_backend_source, tmp_path, marker):
    import subprocess
    import sys

    control = cold_backend_source / ".astra" / "launcher"
    control.mkdir(parents=True)
    (control / marker).write_text("{}", encoding="utf-8")
    completed = subprocess.run([sys.executable, "-m", "agent.cli.backend"], cwd=cold_backend_source,
                               env=_cold_environment(cold_backend_source, tmp_path),
                               capture_output=True, text=True, timeout=15, check=False)
    assert completed.returncode != 0
    assert completed.stdout == ""
    assert "interrupted" in completed.stderr
