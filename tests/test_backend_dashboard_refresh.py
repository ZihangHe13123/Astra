import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from queue import Empty, Queue

from agent.runtime.learning import LearningStore
from agent.runtime.skills import SkillStore
from tests.learning_fixtures import stage_legacy_proposal


def test_reset_resends_dashboard_snapshot_after_skill_state_changes(tmp_path: Path):
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps({"selected_model": "Qwen3.6-35B-A3B"}),
        encoding="utf-8",
    )
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    learning_path = tmp_path / "learning.db"
    store = LearningStore(learning_path)
    env = os.environ.copy()
    env.update({
        "AGENT_SETTINGS_PATH": str(settings),
        "AGENT_SESSION_DIR": str(sessions_dir),
        "AGENT_TASK_DB": str(tmp_path / "tasks.db"),
        "ASTRA_APPROVAL_DB": str(tmp_path / "approvals.db"),
        "ASTRA_EVENT_DB": str(tmp_path / "events.db"),
        "AGENT_MEMORY_PATH": str(tmp_path / "memory.db"),
        "AGENT_LEARNING_PATH": str(learning_path),
        "AGENT_SKILLS_PATH": str(tmp_path / "skills"),
        "LEARNING_REVIEW_AUTO": "0",
        "AGENT_SESSION": "dashboard_refresh",
        "QWEN_BASE_URL": "http://127.0.0.1:9/v1",
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

    def read_events() -> None:
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
                raise AssertionError(
                    f"Backend exited with {proc.returncode}: {stderr}; seen={seen}"
                )
            try:
                event = events.get(timeout=0.25)
            except Empty:
                continue
            seen.append(event)
            if predicate(event):
                return event
        raise AssertionError(f"Timed out waiting for event; seen={seen}")

    try:
        initial = wait_for(lambda event: event.get("type") == "startup_banner")
        assert initial["skills"] == 1  # Packaged astra-core is always available.

        SkillStore(tmp_path / "skills").create("manual-example", "---\nname: manual-example\ndescription: User-provided example\n---\n\nKeep user content.\n", "user")
        proc.stdin.write(json.dumps({"type": "command", "cmd": "/reset"}) + "\n")
        proc.stdin.flush()
        wait_for(lambda event: event.get("type") == "session_info")
        refreshed = wait_for(
            lambda event: event.get("type") == "startup_banner",
            timeout=5,
        )

        assert refreshed["learning"]["pending"] == 0
        assert refreshed["learning"]["auto"] is False
        assert refreshed["learning"]["learned"] == 0
        assert refreshed["skills"] == 2

        stage_legacy_proposal(store, "migration-fixture", "skill_create", {
            "name": "auto-example", "content": "---\nname: auto-example\ndescription: Historical automatic summary\n---\n\nCheck the result.\n",
        }, "Automatic task summary")
        proc.stdin.write(json.dumps({"type": "command", "cmd": "/learn migrate"}) + "\n")
        proc.stdin.flush()
        migrated = wait_for(lambda event: event.get("type") == "startup_banner" and event.get("learning", {}).get("learned") == 1)
        assert migrated["skills"] == 3
        imported = wait_for(lambda event: event.get("type") == "tool_result" and event.get("name") == "learn")
        assert not imported["error"] and "1 automatic skill(s)" in imported["output"]
        proc.stdin.write(json.dumps({"type": "command", "cmd": "/skills"}) + "\n")
        proc.stdin.flush()
        origins = wait_for(lambda event: event.get("type") == "tool_result" and event.get("name") == "skills")
        assert "[auto] [learned] auto-example" in origins["output"]
        assert "[user] [user] manual-example" in origins["output"]

        # Broken optional maintenance data must not crash the backend or
        # pretend there are zero learned skills. Preserve it and report it.
        state_path = tmp_path / "skills-learning" / "index.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text("{broken", encoding="utf-8")
        proc.stdin.write(json.dumps({"type": "command", "cmd": "/reset"}) + "\n")
        proc.stdin.flush()
        degraded = wait_for(lambda event: event.get("type") == "startup_banner")
        assert degraded["learning"]["learned"] is None
        assert degraded["learning"]["error"]
        proc.stdin.write(json.dumps({"type": "command", "cmd": "/learn"}) + "\n")
        proc.stdin.flush()
        result = wait_for(lambda event: event.get("type") == "tool_result" and event.get("name") == "learn")
        assert result["error"]
        assert state_path.read_text(encoding="utf-8") == "{broken"
    finally:
        if proc.poll() is None:
            proc.stdin.write(json.dumps({"type": "exit"}) + "\n")
            proc.stdin.flush()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
