"""Target-specific prerequisites must fail before installation mutations."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.launcher import dependencies, services, setup, update
from agent.launcher.common import LauncherError, git, write_json
from agent.launcher.transaction import Transaction
from test_launcher import advance, assert_old_environments, assert_private_data, source as source_fixture
from test_launcher_services import companion as companion_fixture

source = source_fixture
companion = companion_fixture
CHECK_NODE = dependencies.check_node
SYNCHRONIZE = dependencies.synchronize
NPM_COMMAND = dependencies.npm_command


@pytest.mark.parametrize("gui,version,accepted", [
    (False, "17.9.1", False), (False, "18.0.0", True), (False, "20.17.0", True),
    (True, "18.20.0", False), (True, "20.19.0", False), (True, "22.11.9", False),
    (True, "22.12.0", True), (True, "22.12.1", True), (True, "24.21.0", True),
])
def test_node_version_boundaries(tmp_path, monkeypatch, gui, version, accepted):
    monkeypatch.setattr(dependencies, "node_command", lambda: "node")
    monkeypatch.setattr(dependencies, "run", lambda *args, **kwargs: version)
    if accepted:
        assert CHECK_NODE(tmp_path, gui=gui) == version
    else:
        with pytest.raises(LauncherError) as error:
            CHECK_NODE(tmp_path, gui=gui)
        message = str(error.value)
        assert version in message
        assert ("22.12.0" if gui else "18.0.0") in message
        assert ("GUI" if gui else "TUI") in message
        assert "astra setup" in message
        assert "new terminal" in message


@pytest.mark.parametrize("version", ["", "not-node", "22.12", "22.12.0-rc.1"])
def test_invalid_node_probe_is_actionable(tmp_path, monkeypatch, version):
    monkeypatch.setattr(dependencies, "node_command", lambda: "node")
    monkeypatch.setattr(dependencies, "run", lambda *args, **kwargs: version)
    with pytest.raises(LauncherError, match=r"GUI requires Node.js 22.12.0"):
        CHECK_NODE(tmp_path, gui=True)


@pytest.mark.parametrize("gui", [False, True])
def test_missing_node_reports_selected_target(tmp_path, monkeypatch, gui):
    monkeypatch.setattr(dependencies.shutil, "which", lambda _: None)
    with pytest.raises(LauncherError) as error:
        CHECK_NODE(tmp_path, gui=gui)
    assert ("GUI requires Node.js 22.12.0" if gui else "TUI requires Node.js 18.0.0") in str(error.value)


def test_failed_probe_keeps_cause_and_recovery_hint(tmp_path, monkeypatch):
    monkeypatch.setattr(dependencies, "node_command", lambda: "node")
    def fail(*args, **kwargs):
        raise LauncherError("Could not run node: timed out")
    monkeypatch.setattr(dependencies, "run", fail)
    with pytest.raises(LauncherError, match="timed out") as error:
        CHECK_NODE(tmp_path, gui=True)
    assert "astra setup --gui" in str(error.value)


def test_gui_minimum_matches_locked_manifest():
    root = Path(__file__).resolve().parents[1]
    expected = ">=" + ".".join(map(str, dependencies.GUI_NODE_MIN_VERSION))
    manifest = json.loads((root / "ui-gui/package.json").read_text(encoding="utf-8"))
    lock = json.loads((root / "ui-gui/package-lock.json").read_text(encoding="utf-8"))
    assert manifest["engines"]["node"] == expected
    assert lock["packages"][""]["engines"]["node"] == expected


@pytest.mark.parametrize("mode", ["setup", "setup-repair", "update", "update-repair"])
def test_old_node_rejected_before_mutation_or_service_stop(source, companion, monkeypatch, mode):
    inst, seed = source
    # Repair/update must honor a previously enabled desktop without --gui.
    if mode != "setup":
        write_json(inst.control / "installation.json", {"schema": 1, "root": str(inst.root), "gui": True})
    if mode == "update":
        advance(seed)
    before = git(inst.root, "rev-parse", "HEAD")
    metadata = inst.metadata.copy()
    monkeypatch.setattr(dependencies, "check_node", CHECK_NODE)
    monkeypatch.setattr(dependencies, "node_command", lambda: "node")
    monkeypatch.setattr(dependencies, "run", lambda *args, **kwargs: "20.17.0")
    def must_not_mutate(*args, **kwargs):
        pytest.fail("prerequisites must be checked before bootstrapping or synchronization")
    monkeypatch.setattr(dependencies, "ensure_uv", must_not_mutate)
    monkeypatch.setattr(dependencies, "synchronize", must_not_mutate)
    with pytest.raises(LauncherError, match=r"GUI requires Node.js 22.12.0"):
        if mode.startswith("setup"):
            setup.setup_source(inst, [], gui=mode == "setup", repair=mode == "setup-repair")
        else:
            update.update_source(inst, repair=mode == "update-repair")
    assert git(inst.root, "rev-parse", "HEAD") == before
    assert inst.metadata == metadata
    assert companion.calls == []
    assert not services.pending_services(inst)
    assert not (inst.control / "pending.json").exists()
    assert_old_environments(inst)
    assert_private_data(inst)


def test_update_check_does_not_require_node(source, companion, monkeypatch):
    inst, seed = source
    write_json(inst.control / "installation.json", {"schema": 1, "root": str(inst.root), "gui": True})
    target = advance(seed)
    def unavailable(*args, **kwargs):
        pytest.fail("update --check must not require Node")
    monkeypatch.setattr(dependencies, "check_node", unavailable)
    assert update.update_source(inst, check=True)["target"] == target
    assert companion.calls == []


def test_recovery_does_not_require_node(source, monkeypatch):
    inst, _ = source
    before = git(inst.root, "rev-parse", "HEAD")
    transaction = Transaction(inst, before, before)
    transaction.prepare()
    def unavailable(*args, **kwargs):
        pytest.fail("update --recover must work without Node")
    monkeypatch.setattr(dependencies, "check_node", unavailable)
    assert update.update_source(inst, recover=True)["outcome"] == "recovered"
    assert_old_environments(inst)
    assert_private_data(inst)


def test_tui_setup_still_accepts_node_18(source, monkeypatch):
    inst, _ = source
    monkeypatch.setattr(dependencies, "check_node", CHECK_NODE)
    monkeypatch.setattr(dependencies, "node_command", lambda: "node")
    monkeypatch.setattr(dependencies, "run", lambda *args, **kwargs: "18.0.0")
    monkeypatch.setattr(dependencies, "ensure_uv", lambda _: None)
    assert setup.setup_source(inst, [])["outcome"] == "ready"
    assert not dependencies.gui_enabled(inst)
    assert_private_data(inst)


def test_doctor_reports_old_node_even_with_incomplete_gui(source, monkeypatch):
    inst, _ = source
    write_json(inst.control / "installation.json", {"schema": 1, "root": str(inst.root), "gui": True})
    monkeypatch.setattr(dependencies, "check_node", CHECK_NODE)
    monkeypatch.setattr(dependencies, "node_command", lambda: "node")
    monkeypatch.setattr(dependencies, "run", lambda *args, **kwargs: "20.17.0")
    report = dependencies.diagnostics(inst)
    problem = next(item for item in report["problems"] if item["component"] == "gui")
    assert "GUI requires Node.js 22.12.0" in problem["message"]
    assert "20.17.0" in problem["message"]
    assert not report["gui"]["healthy"]


def test_synchronize_defensively_checks_gui_before_installing(source, monkeypatch):
    inst, _ = source
    write_json(inst.control / "installation.json", {"schema": 1, "root": str(inst.root), "gui": True})
    calls = []
    monkeypatch.setattr(dependencies, "check_node", CHECK_NODE)
    monkeypatch.setattr(dependencies, "node_command", lambda: "node")
    def probe(command, **kwargs):
        calls.append(command)
        assert command[0] == "node", "must not run uv/npm with an unsupported Node"
        return "20.17.0"
    monkeypatch.setattr(dependencies, "run", probe)
    with pytest.raises(LauncherError, match=r"GUI requires Node.js 22.12.0"):
        SYNCHRONIZE(inst, [])
    assert len(calls) == 1


def test_gui_health_uses_shared_requirement_before_electron(source, monkeypatch):
    inst, _ = source
    monkeypatch.setattr(dependencies, "check_node", CHECK_NODE)
    monkeypatch.setattr(dependencies, "node_command", lambda: "node")
    monkeypatch.setattr(dependencies, "run", lambda *args, **kwargs: "20.17.0")
    with pytest.raises(LauncherError, match=r"GUI requires Node.js 22.12.0"):
        dependencies.gui_health(inst)
    assert not (inst.root / "ui-gui").exists()


@pytest.mark.parametrize("mode", ["setup", "update"])
def test_missing_npm_rejected_before_mutation(source, companion, monkeypatch, mode):
    inst, seed = source
    if mode == "update":
        advance(seed)
    monkeypatch.setattr(dependencies.shutil, "which", lambda _: None)
    monkeypatch.setattr(dependencies, "npm_command", NPM_COMMAND)
    def must_not_mutate(*args, **kwargs):
        pytest.fail("missing npm must be detected before bootstrapping or synchronization")
    monkeypatch.setattr(dependencies, "ensure_uv", must_not_mutate)
    monkeypatch.setattr(dependencies, "synchronize", must_not_mutate)
    with pytest.raises(LauncherError, match="npm is missing"):
        if mode == "setup":
            setup.setup_source(inst, [])
        else:
            update.update_source(inst)
    assert companion.calls == []
    assert_old_environments(inst)
    assert_private_data(inst)


@pytest.mark.parametrize("found", [None, "/node with spaces/npm"])
def test_npm_command_resolves_platform_entrypoint(monkeypatch, found):
    calls = []
    def which(name):
        calls.append(name)
        return found
    monkeypatch.setattr(dependencies.shutil, "which", which)
    if found:
        assert NPM_COMMAND() == found
    else:
        with pytest.raises(LauncherError, match="npm is missing"):
            NPM_COMMAND()
    assert calls == ["npm.cmd" if dependencies.os.name == "nt" else "npm"]
