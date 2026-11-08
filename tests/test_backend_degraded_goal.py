"""The backend command protocol remains usable after TaskStore startup fails."""
import json
import os
from pathlib import Path
import subprocess
import sys


def test_goal_without_task_storage_reports_error_and_accepts_next_command(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"selected_model": "Qwen3.6-35B-A3B"}))
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    unavailable_db = tmp_path / "database-is-a-directory"
    unavailable_db.mkdir()
    env = {
        **os.environ,
        "AGENT_SETTINGS_PATH": str(settings),
        "AGENT_SESSION_DIR": str(sessions),
        "AGENT_TASK_DB": str(unavailable_db),
        "ASTRA_APPROVAL_DB": str(tmp_path / "approvals.db"),
        "ASTRA_EVENT_DB": str(tmp_path / "events.db"),
        "AGENT_MEMORY_PATH": str(tmp_path / "memory.db"),
        "AGENT_LEARNING_PATH": str(tmp_path / "learning.db"),
        "AGENT_SKILLS_PATH": str(tmp_path / "skills"),
        "AGENT_MCP_CONFIG": str(tmp_path / "missing-mcp.json"),
        "ASTRA_CHANNEL_CONFIG": str(tmp_path / "missing-channels.json"),
        "ASTRA_CONTEXT_INDEX_EMBEDDING": "off",
        "LEARNING_REVIEW_AUTO": "0",
        "QWEN_BASE_URL": "http://127.0.0.1:9/v1",
        "SANDBOX_DOCKER": "false",
    }
    commands = [
        {"type": "command", "cmd": "/goal"},
        {"type": "command", "cmd": "/tasks"},
        {"type": "exit"},
    ]
    result = subprocess.run(
        [sys.executable, "-m", "agent.cli.backend"],
        input="\n".join(json.dumps(command) for command in commands) + "\n",
        capture_output=True,
        text=True,
        timeout=30,
        env={**env, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
    )
    assert result.returncode == 0, result.stderr
    events = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
    for name in ("goal", "tasks"):
        event = next(
            event for event in events
            if event.get("type") == "tool_result" and event.get("name") == name
        )
        assert "Task persistence is disabled" in event["error"]
    assert not any(event.get("type") == "error" for event in events)
    assert "UnboundLocalError" not in result.stderr
