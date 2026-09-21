"""The local-mode host owns writer leases even for older local extensions."""

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.cli import sessions
from agent.runtime.context import AgentContext
from agent.runtime.local_mode import load_local_mode
from agent.runtime.session_store import SessionStore

ROOT = Path(__file__).resolve().parents[1]
PROBE = """
import sys
from agent.ui.session_ownership import claim_session
try:
    lease = claim_session(sys.argv[1])
except OSError:
    sys.exit(3)
print('owned', flush=True)
if len(sys.argv) > 2:
    sys.stdin.readline()
"""


@pytest.fixture
def local(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRA_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("ASTRA_ENV_FILE", str(tmp_path / "missing.env"))
    monkeypatch.setenv("ASTRA_LOCAL_MODE_FILE", str(ROOT / "tests/fixtures/local_mode.py"))
    monkeypatch.setattr(sessions, "SESSION_DIR", tmp_path / "sessions")
    work = AgentContext("Work fixture.")
    work.enforce_session_ownership = True
    work.set_session(str(sessions.session_path("work")))
    agent = SimpleNamespace(
        context=work, local_mode=False, tools_enabled=True, tool_allowlist=None,
        memory_store=None, external_memory_provider=None, skill_store=None, task_store=None,
        runtime_context_provider=None, runtime_turn_context_provider=None,
        generation_overrides_provider=None, finalize_after_tools_provider=None, forced_tool_name=None,
    )
    return agent, work, load_local_mode(agent)


def _can_claim(path):
    probe = subprocess.run([sys.executable, "-c", PROBE, str(path)], cwd=ROOT,
                           env=os.environ.copy(), capture_output=True, text=True, timeout=10)
    assert probe.returncode in (0, 3), probe.stderr
    return probe.returncode == 0


def test_legacy_extension_enter_and_leave_hold_shared_writer_lease(local):
    agent, work, mode = local
    target = mode.session_path("shared")
    assert mode.enter(target)
    # The older fixture deliberately does not inherit the context flag.
    assert not agent.context.enforce_session_ownership
    assert not _can_claim(target)
    assert not _can_claim(target.with_suffix(".jsonl"))
    assert not _can_claim(Path(work.session_path))
    assert mode.leave()
    assert agent.context is work
    assert _can_claim(target)
    assert not _can_claim(Path(work.session_path))


@pytest.mark.parametrize("raises", [False, True])
def test_enter_refusal_or_exception_releases_candidate_and_preserves_work(local, monkeypatch, raises):
    agent, work, mode = local
    target = mode.session_path("candidate")

    def refuse(_path):
        if raises:
            raise OSError("fixture enter refused")
        return False

    monkeypatch.setattr(mode.controller, "enter", refuse)
    if raises:
        with pytest.raises(OSError, match="fixture enter refused"):
            mode.enter(target)
    else:
        assert not mode.enter(target)
    assert agent.context is work
    assert _can_claim(target)
    assert not _can_claim(Path(work.session_path))


def test_locked_switch_refuses_before_controller_writes_and_preserves_old_lease(local, monkeypatch):
    agent, _work, mode = local
    first, second = mode.session_path("first"), mode.session_path("second")
    assert mode.enter(first)
    context = agent.context
    owner = subprocess.Popen([sys.executable, "-c", PROBE, str(second), "hold"], cwd=ROOT,
                             env=os.environ.copy(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
    try:
        assert owner.stdout is not None and owner.stdout.readline().strip() == "owned"
        monkeypatch.setattr(mode.controller, "switch", lambda _: pytest.fail("controller must not run"))
        with pytest.raises(OSError, match="in use"):
            mode.switch(second)
        assert agent.context is context
        assert not _can_claim(first)
    finally:
        owner.communicate("release\n", timeout=10)
    assert _can_claim(second)


def test_failed_switch_preserves_old_lease_and_success_releases_only_old(local, monkeypatch):
    agent, work, mode = local
    first, second = mode.session_path("first"), mode.session_path("second")
    assert mode.enter(first)
    switch = mode.controller.switch

    def refuse(_path):
        raise OSError("fixture switch refused")

    monkeypatch.setattr(mode.controller, "switch", refuse)
    with pytest.raises(OSError, match="fixture switch refused"):
        mode.switch(second)
    assert not _can_claim(first)
    assert _can_claim(second)
    monkeypatch.setattr(mode.controller, "switch", switch)
    mode.switch(second)
    assert Path(agent.context.session_path) == second
    assert _can_claim(first)
    assert not _can_claim(second)
    assert not _can_claim(Path(work.session_path))


@pytest.mark.parametrize("raises", [False, True])
def test_failed_leave_keeps_active_local_lease(local, monkeypatch, raises):
    _agent, _work, mode = local
    target = mode.session_path("active")
    assert mode.enter(target)

    def refuse():
        if raises:
            raise OSError("fixture leave refused")
        return False

    monkeypatch.setattr(mode.controller, "leave", refuse)
    if raises:
        with pytest.raises(OSError, match="fixture leave refused"):
            mode.leave()
    else:
        assert not mode.leave()
    assert not _can_claim(target)


def test_local_session_menu_never_recovers_or_writes_history(local, monkeypatch):
    _agent, _work, mode = local
    target = mode.session_path("menu")
    SessionStore(target).save({"messages": [{"role": "user", "content": "retained"}]})
    original = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in SessionStore(target).related_paths() if p.exists()}
    monkeypatch.setattr(SessionStore, "recover_interrupted", lambda _: pytest.fail("menu must be read-only"))
    assert mode.menu_event()["sessions"] == [{"name": "menu", "messages": 1, "current": False}]
    assert original == {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in original}
