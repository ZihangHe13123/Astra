"""Cross-process ownership and read-only desktop queries, without model calls."""
import gc
import re
import subprocess
import sys
from pathlib import Path

import pytest

from agent.runtime.context import AgentContext
from agent.runtime.session_store import SessionStore
from agent.ui import queries
from agent.ui.session_ownership import claim_session
from agent.ui.submissions import Submissions

ROOT = Path(__file__).resolve().parents[1]


def _try_claim(path):
    return subprocess.run([sys.executable, "-c", "from agent.ui.session_ownership import claim_session;"
                           "import sys;lease=claim_session(sys.argv[1])", str(path)],
                          cwd=ROOT, capture_output=True, text=True, timeout=15)


def test_lease_blocks_another_process_and_releases_after_last_context(tmp_path):
    path = tmp_path / "会话.json"
    context = AgentContext(enforce_session_ownership=True)
    context.set_session(str(path))
    assert "in use by another" in _try_claim(path).stderr
    shared = claim_session(path)
    context.set_session(str(tmp_path / "next.json"))
    assert _try_claim(path).returncode != 0
    del shared
    gc.collect()
    assert _try_claim(path).returncode == 0


def test_failed_candidate_does_not_release_current_lease(tmp_path, monkeypatch):
    context = AgentContext(enforce_session_ownership=True)
    original = tmp_path / "original.json"
    context.set_session(str(original))
    def fail(*_args, **_kwargs):
        raise ValueError("broken history")
    (tmp_path / "broken.json").write_text("{}")
    monkeypatch.setattr(AgentContext, "load", fail)
    with pytest.raises(ValueError):
        context.stage_session(str(tmp_path / "broken.json"))
    assert _try_claim(original).returncode != 0
    assert context.session_path == str(original)


def test_history_query_does_not_recover_or_touch_live_store(tmp_path, monkeypatch):
    monkeypatch.setattr(queries.sessions, "SESSION_DIR", tmp_path)
    monkeypatch.setenv("ASTRA_GUI_HISTORY_CACHE", str(tmp_path.parent / (tmp_path.name + "-history-cache")))
    path = tmp_path / "demo.json"
    store = SessionStore(path)
    store.save({"messages": [{"role": "user", "content": "一"}, {"role": "assistant", "content": "二"}]})
    before = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in tmp_path.rglob("*") if p.is_file()}
    def forbidden(*_args):
        raise AssertionError("read query attempted lifecycle recovery")
    monkeypatch.setattr(SessionStore, "recover_interrupted", forbidden)
    history = queries.history("demo", limit=1)
    assert [m["content"] for m in history["messages"]] == ["二"]
    assert history["has_more"]
    assert queries.history("demo", before=history["before"], limit=1)["messages"][0]["content"] == "一"
    assert before == {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in tmp_path.rglob("*") if p.is_file()}
    assert queries.list_sessions()[0]["name"] == "demo"
    with pytest.raises(ValueError):
        queries.history("../demo")


def test_receipt_same_id_is_never_admitted_twice():
    ledger = Submissions()
    command = {"type": "message", "text": "write once", "submission_id": "message-1"}
    assert ledger.begin(command) is None
    assert ledger.begin(command)["status"] == "pending"
    ledger.finish("message-1", True)
    assert ledger.begin(command)["type"] == "message_accepted"
    assert ledger.begin({**command, "text": "different"})["code"] == "submission_payload_conflict"
    assert Submissions().status("message-1")["status"] == "unknown"
    assert ledger.begin({**command, "submission_id": "../invalid"})["code"] == "invalid_submission"


def test_command_catalog_covers_the_tui_roots():
    source = (ROOT / "ui-tui/src/command-menu.ts").read_text()
    block = source.split("const SLASH_COMMANDS:", 1)[1].split("];", 1)[0]
    roots = re.findall(r'command: "(/[^" ]+)"', block)
    catalog = queries.command_catalog()
    assert len(roots) == 51
    assert sorted(roots) == sorted(c["command"] for c in catalog)
    assert len(set(c["id"] for c in catalog)) == len(roots)


def test_recovery_preserves_other_live_sessions(tmp_path):
    from agent.runtime.task_store import TaskStore
    from agent.runtime.approval_inbox import ApprovalInbox
    tasks = TaskStore(tmp_path / "tasks.db")
    first = tasks.start_run("one", "running here", session_id="one")
    second = tasks.start_run("two", "running elsewhere", session_id="two")
    assert tasks.recover_interrupted(session_id="one") == 1
    assert tasks.get_task(first["id"])["status"] == "interrupted"
    assert tasks.get_task(second["id"])["status"] == "running"
    approvals = ApprovalInbox(tmp_path / "approvals.db")
    approvals.create({"tool_name": "write_file"}, request_id="one", session_id="one")
    approvals.create({"tool_name": "write_file"}, request_id="two", session_id="two")
    assert len(approvals.recover_orphaned(session_id="one")) == 1
    assert approvals.get("two").state == "pending"


def test_shared_preferences_parallel_writers_keep_all_keys(tmp_path):
    path = tmp_path / "settings.json"
    code = """from pathlib import Path
import sys,time
from agent.runtime.json_preferences import update_preferences
def change(data):
    time.sleep(.1)
    data[sys.argv[2]] = True
update_preferences(Path(sys.argv[1]), change)
"""
    processes = [subprocess.Popen([sys.executable, "-c", code, str(path), f"field-{i}"], cwd=ROOT) for i in range(4)]
    assert all(p.wait(timeout=15) == 0 for p in processes)
    import json
    assert json.loads(path.read_text()) == {f"field-{i}": True for i in range(4)}


def test_snapshot_preview_keeps_unknown_and_binary_distinct():
    from agent.runtime.turn_change_store import LoadedSides
    from agent.ui.queries import preview_sides
    result = preview_sides(LoadedSides(b'apple\nbanana\n', b'apple\norange', 'captured', 'captured'), 'full')
    assert '-banana' in result['unified'] and '+orange' in result['unified']
    assert '末尾换行' in result['unified']
    binary = preview_sides(LoadedSides(b'\x00binary', b'\x00new', 'captured', 'captured'), 'none')
    assert binary['binary'] and binary['unified'] is None and binary['before'] is None
    missing = preview_sides(LoadedSides(None, None, 'uncaptured', 'absent'), 'none')
    assert missing['before_state'] == 'uncaptured' and missing['after_state'] == 'absent'
    large = preview_sides(LoadedSides(b'a'*200000, b'b'*200000, 'captured', 'captured'), 'coarse')
    assert large['truncated'] and len(large['before']) == 128 * 1024 and large['unified'] is None


def test_backend_reports_session_conflict_before_initializing_a_model(tmp_path, monkeypatch):
    import json
    import os
    path = tmp_path / 'sessions' / 'owned.json'
    lease = claim_session(path)
    monkeypatch.setenv('AGENT_SESSION', 'owned')
    monkeypatch.setenv('AGENT_SESSION_DIR', str(path.parent))
    monkeypatch.setenv('ASTRA_HOME', str(tmp_path / 'state'))
    monkeypatch.setenv('ASTRA_EVENT_DB', str(tmp_path / 'events.db'))
    monkeypatch.setenv('ASTRA_ENV_FILE', str(tmp_path / 'missing.env'))
    monkeypatch.setenv('ASTRA_UI_SURFACE', 'gui')
    result = subprocess.run([sys.executable, '-m', 'agent.cli.backend'], cwd=ROOT,
                            env=os.environ.copy(), input='', capture_output=True, text=True, timeout=20)
    events = [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]
    assert result.returncode == 1, result.stderr
    assert any(e['type'] == 'error' and 'in use by another' in e.get('message', '') for e in events)
    assert not any(e['type'] in ('gui_ready', 'model_info') for e in events)
    del lease
