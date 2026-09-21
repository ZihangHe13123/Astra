"""Desktop session export/deletion uses the existing stores without inference."""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from agent.cli import sessions
from agent.runtime.session_store import SessionStore
from agent.ui.session_actions import act
from agent.ui.session_ownership import claim_session


ROOT = Path(__file__).resolve().parents[1]
MODES = ("work", "minimal", "bar")


@pytest.fixture
def data_root(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    monkeypatch.setattr(sessions, "SESSION_DIR", root)
    monkeypatch.setenv("AGENT_SESSION_DIR", str(root))
    monkeypatch.setenv("ASTRA_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("AGENT_MEMORY_PATH", str(tmp_path / "state" / "memory.db"))
    monkeypatch.setenv("ASTRA_ENV_FILE", str(tmp_path / "missing.env"))
    return root


def _store(root, mode, name="样例"):
    return SessionStore((root if mode == "work" else root / mode) / f"{name}.json")


def _files(root):
    return {p.relative_to(root): (p.stat().st_mtime_ns, p.read_bytes())
            for p in root.rglob("*") if p.is_file()}


def _cli(request, env=None):
    process = subprocess.run(
        [sys.executable, "-m", "agent.ui.session_actions"], cwd=ROOT,
        input=json.dumps(request), capture_output=True, text=True, timeout=15,
        env=env or os.environ.copy(),
    )
    assert process.returncode == 0, process.stderr
    return json.loads(process.stdout)


@pytest.mark.parametrize("mode", MODES)
def test_export_is_complete_readonly_and_matches_existing_export(data_root, mode, monkeypatch):
    store = _store(data_root, mode)
    messages = [{"role": "user", "content": f"message-{i}"} for i in range(1100)]
    messages += [{"role": "assistant", "tool_calls": [{"name": "read_file"}]},
                 {"role": "tool", "content": "x" * 20000},
                 {"role": "assistant", "content": "最终回答"}]
    store.save({"messages": messages})
    store.append_subagent_event({"type": "tool_result", "process_id": "child-1",
                                "tool_name": "read_file", "output": "child output"})
    before = _files(data_root)

    def forbidden(*_args):
        raise AssertionError("Export attempted lifecycle recovery")

    monkeypatch.setattr(SessionStore, "recover_interrupted", forbidden)
    result = act({"action": "export", "name": "样例", "mode": mode})
    markdown = result["markdown"]
    assert result["name"] == "样例" and result["mode"] == mode
    assert "message-0\n" in markdown and "message-1099\n" in markdown
    assert "[tool calls]" in markdown and "x" * 20000 in markdown
    assert "最终回答" in markdown and "# Subagent transcripts" in markdown
    assert "child output" in markdown and "child-1" in markdown
    assert _files(data_root) == before
    assert not (data_root.parent / "state").exists()
    destination = data_root.parent / "export.md"
    store.export_markdown("样例", destination)
    assert destination.read_text() == markdown
    assert _files(data_root) == before


@pytest.mark.parametrize("mode", MODES)
def test_delete_removes_related_files_and_media_only_in_target_namespace(data_root, mode):
    stores = {other: _store(data_root, other) for other in MODES}
    for store in stores.values():
        store.save({"messages": [{"role": "user", "content": "preserve other modes"}]})
    target = stores[mode]
    for related in target.related_paths():
        related.parent.mkdir(parents=True, exist_ok=True)
        if not related.exists():
            related.write_text("{}\n")
    media = target.appshot_media.path
    media.mkdir(mode=0o700)
    media.chmod(0o700)
    asset = media / ("a" * 32 + ".png")
    asset.write_bytes(b"opaque session-owned file")
    asset.chmod(0o600)
    other_before = {other: store.markdown("样例") for other, store in stores.items() if other != mode}
    result = _cli({"action": "delete", "name": "样例", "mode": mode})
    assert result == {"ok": True, "result": {"name": "样例", "mode": mode, "deleted": True}}
    assert not any(path.exists() for path in target.related_paths())
    assert not media.exists()
    for other, expected in other_before.items():
        assert stores[other].markdown("样例") == expected
    assert not (data_root.parent / "state").exists()


@pytest.mark.parametrize("mode", MODES)
def test_cli_export_allows_live_reading_but_delete_refuses_writer(data_root, mode):
    store = _store(data_root, mode)
    store.save({"messages": [{"role": "user", "content": "still owned"}]})
    lease = claim_session(store.legacy_path)
    before = _files(data_root)
    exported = _cli({"action": "export", "name": "样例", "mode": mode})
    assert exported["ok"] and "still owned" in exported["result"]["markdown"]
    assert before == _files(data_root)
    rejected = _cli({"action": "delete", "name": "样例", "mode": mode})
    assert not rejected["ok"] and "in use by another" in rejected["error"]
    assert store.exists and "still owned" in store.markdown("样例")
    del lease
    assert _cli({"action": "delete", "name": "样例", "mode": mode})["ok"]


@pytest.mark.parametrize("action", ("export", "delete"))
@pytest.mark.parametrize("name", ("../victim", "sub/victim", "sub\\victim", "/victim", ".", "..", ""))
def test_path_traversal_is_rejected_before_any_write(data_root, name, action):
    store = _store(data_root, "work", "victim")
    store.save({"messages": [{"role": "user", "content": "keep"}]})
    before = _files(data_root)
    with pytest.raises(ValueError):
        act({"action": action, "name": name, "mode": "work"})
    assert _files(data_root) == before


def test_work_delete_clears_only_matching_working_memory_without_core_initialization(data_root):
    path = Path(os.environ["AGENT_MEMORY_PATH"])
    path.parent.mkdir()
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE working_memories (session_id TEXT PRIMARY KEY, data_json TEXT)")
        db.executemany("INSERT INTO working_memories VALUES (?,?)", [("样例", "{}"), ("keep", "{}")])
        db.execute("CREATE TABLE core_memories (content TEXT)")
        db.execute("INSERT INTO core_memories VALUES ('never migrate this')")
    for mode in ("minimal", "work"):
        _store(data_root, mode).save({"messages": [{"role": "user", "content": "one"}]})
        assert _cli({"action": "delete", "name": "样例", "mode": mode})["ok"]
        with sqlite3.connect(path) as db:
            names = [row[0] for row in db.execute("SELECT session_id FROM working_memories ORDER BY session_id")]
            assert names == (["keep", "样例"] if mode == "minimal" else ["keep"])
            assert db.execute("SELECT content FROM core_memories").fetchone()[0] == "never migrate this"
        assert not (path.parent / "memory").exists()


def test_working_memory_rolls_back_when_media_cleanup_refuses(data_root):
    store = _store(data_root, "work")
    store.save({"messages": [{"role": "user", "content": "keep"}]})
    store.appshot_media.path.mkdir(mode=0o700)
    (store.appshot_media.path / "unexpected.txt").write_text("unowned")
    path = Path(os.environ["AGENT_MEMORY_PATH"])
    path.parent.mkdir()
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE working_memories (session_id TEXT PRIMARY KEY, data_json TEXT)")
        db.execute("INSERT INTO working_memories VALUES ('样例', '{}')")
    assert not _cli({"action": "delete", "name": "样例", "mode": "work"})["ok"]
    assert store.exists
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT session_id FROM working_memories").fetchone()[0] == "样例"


def test_cli_loads_environment_before_resolving_namespace(tmp_path):
    root = tmp_path / "configured-sessions"
    _store(root, "bar", "configured").save({"messages": [{"role": "user", "content": "from env"}]})
    env_file = tmp_path / "selected.env"
    env_file.write_text(f"AGENT_SESSION_DIR={root}\n")
    env = {key: value for key, value in os.environ.items() if key != "AGENT_SESSION_DIR"}
    env["ASTRA_ENV_FILE"] = str(env_file)
    result = _cli({"action": "export", "name": "configured", "mode": "bar"}, env)
    assert result["ok"] and "from env" in result["result"]["markdown"]


@pytest.mark.parametrize("payload", [[], {}, {"action": "other"},
                                    {"action": "export", "name": 12},
                                    {"action": "export", "name": "missing", "mode": "elsewhere"},
                                    {"action": "delete", "name": "missing"}])
def test_invalid_and_missing_requests_return_structured_error(data_root, payload):
    result = _cli(payload)
    assert result["ok"] is False and isinstance(result["error"], str)
    assert not data_root.exists()


@pytest.mark.parametrize("mode", ("local", "writing"))
@pytest.mark.parametrize("action", ("export", "delete"))
def test_unmanaged_modes_cannot_fall_through_to_work_or_extension_files(data_root, mode, action):
    for namespace in ("work", "fixture"):
        _store(data_root, namespace).save({"messages": [{"role": "user", "content": "keep"}]})
    before = _files(data_root)
    result = _cli({"action": action, "name": "样例", "mode": mode})
    assert not result["ok"] and "mode" in result["error"].lower()
    assert _files(data_root) == before
