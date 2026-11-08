"""A killed rollback must leave a recoverable journal or an unblocked app."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from agent.launcher import desktop_update, installation
from agent.launcher.common import LauncherError, read_json, write_json
from agent.launcher.desktop_distribution import seal_tree, sha256
from agent.launcher.locking import RuntimeLease

ROOT = Path(__file__).resolve().parents[1]


def make_application(app, version):
    backend = app / "Contents/Resources/runtime/backend"
    backend.mkdir(parents=True)
    (backend / "agent").mkdir()
    python = backend.parent / "python/bin/python3"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"fixture interpreter")
    python.chmod(0o755)
    (backend.parent / "astra-desktop-update").write_text("fixture updater")
    write_json(backend / "desktop-installation.json", {
        "schema": 1, "distribution": "astra-desktop", "version": version,
        "target": "darwin-arm64", "python": "../python/bin/python3",
    })
    (app / "version.txt").write_text(version)
    return backend


@pytest.mark.parametrize("operation", ["rollback", "recover"])
@pytest.mark.parametrize("boundary", ["desktop-update.json", "desktop-pending.json"])
def test_terminated_cleanup_leaves_app_launchable_or_recoverable(tmp_path, monkeypatch, operation, boundary):
    app = tmp_path / "installed/Astra.app"
    backend = make_application(app, "old")
    profile = tmp_path / "profile"
    monkeypatch.setattr(installation, "user_home", lambda: profile)
    install = installation.discover(backend)
    candidate = tmp_path / "candidate"
    make_application(candidate / "Astra.app", "new")
    manifest = seal_tree(candidate / "Astra.app", version="new", target="darwin-arm64")
    manifest["application"] = "Astra.app"
    write_json(candidate / "release.json", manifest)
    control = tmp_path / "updater"
    desktop_update.stage(candidate, app, control, expected_sha256=sha256(candidate / "release.json"),
                         target="darwin-arm64")
    if operation == "recover":
        desktop_update.apply(app, control, validate=lambda _: None)
        # Model interruption after validation but before clearing the fence.
        write_json(install.control / "desktop-pending.json", {"journal": str(control / "desktop-update.json")})
    code = """
import os, sys
from pathlib import Path
from agent.launcher import desktop_update, installation
from agent.launcher.common import LauncherError
app, control, profile = map(Path, sys.argv[1:4])
installation.user_home = lambda: profile
original = Path.unlink
def terminate_at_boundary(path, *args, **kwargs):
    result = original(path, *args, **kwargs)
    if path.name == sys.argv[5]:
        os._exit(77)
    return result
Path.unlink = terminate_at_boundary
if sys.argv[4] == 'recover':
    desktop_update.recover(app, control)
else:
    def failed_health(_):
        raise LauncherError('synthetic health failure')
    desktop_update.apply(app, control, validate=failed_health)
"""
    result = subprocess.run([sys.executable, "-c", code, str(app), str(control), str(profile),
                             operation, boundary], cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT)},
                            capture_output=True, timeout=30)
    assert result.returncode == 77, result.stderr
    assert (app / "version.txt").read_text() == "old"
    if (control / "desktop-update.json").exists():
        assert desktop_update.recover(app, control)["phase"] == "restored"
    assert not (install.control / "desktop-pending.json").exists()
    with RuntimeLease(install, "recovered fixture"):
        assert (app / "version.txt").read_text() == "old"
    assert not (control / "desktop-update.json").exists()


INTERRUPT = """
import os, shutil, sys
from pathlib import Path
from agent.launcher import desktop_update, installation
app, control, profile = map(Path, sys.argv[1:4])
installation.user_home = lambda: profile
if sys.argv[4] == 'finalize':
    original = shutil.rmtree
    def terminate_mid_delete(path, *args, **kwargs):
        if Path(path).name.endswith('-previous'):
            # Model a kill after rmtree removed only part of the saved version.
            next(item for item in Path(path).rglob('*') if item.is_file()).unlink()
            os._exit(77)
        return original(path, *args, **kwargs)
    shutil.rmtree = terminate_mid_delete
    desktop_update.finalize(app, control)
else:
    replace = os.replace
    def terminate_after_move_aside(source, destination):
        replace(source, destination)
        if Path(destination).name.endswith('-displaced'):
            os._exit(77)
    os.replace = terminate_after_move_aside
    desktop_update.recover(app, control)
"""


def applied_update(tmp_path, monkeypatch):
    app = tmp_path / "installed/Astra.app"
    backend = make_application(app, "old")
    profile = tmp_path / "profile"
    monkeypatch.setattr(installation, "user_home", lambda: profile)
    install = installation.discover(backend)
    candidate = tmp_path / "candidate"
    make_application(candidate / "Astra.app", "new")
    manifest = seal_tree(candidate / "Astra.app", version="new", target="darwin-arm64")
    manifest["application"] = "Astra.app"
    write_json(candidate / "release.json", manifest)
    control = tmp_path / "updater"
    desktop_update.stage(candidate, app, control, expected_sha256=sha256(candidate / "release.json"),
                         target="darwin-arm64")
    desktop_update.apply(app, control, validate=lambda _: None)
    return app, control, profile, install


def interrupt(app, control, profile, action):
    result = subprocess.run([sys.executable, "-c", INTERRUPT, str(app), str(control), str(profile), action],
                            cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT)}, capture_output=True, timeout=30)
    assert result.returncode == 77, result.stderr


@pytest.mark.parametrize("follow_up", ["recover", "finalize"])
def test_interrupted_finalize_completes_and_never_restores_partial_previous_version(tmp_path, monkeypatch, follow_up):
    app, control, profile, install = applied_update(tmp_path, monkeypatch)
    interrupt(app, control, profile, "finalize")
    assert list(app.parent.glob(".astra-update-*-previous"))
    result = getattr(desktop_update, follow_up)(app, control)
    assert result["phase"] == "finalized"
    assert (app / "version.txt").read_text() == "new"
    assert not list(app.parent.glob(".astra-update-*"))
    assert not list(control.glob("updater-*"))
    assert not (control / "desktop-update.json").exists()
    with RuntimeLease(install, "finalized fixture"):
        assert (app / "version.txt").read_text() == "new"


def test_finalize_clears_launch_fence_left_by_interrupted_apply(tmp_path, monkeypatch):
    app, control, _, install = applied_update(tmp_path, monkeypatch)
    # Apply recorded success but was killed before it cleared the fence.
    write_json(install.control / "desktop-pending.json", {"journal": str(control / "desktop-update.json")})
    assert desktop_update.finalize(app, control)["phase"] == "finalized"
    assert not (install.control / "desktop-pending.json").exists()
    with RuntimeLease(install, "finalized fixture"):
        assert (app / "version.txt").read_text() == "new"


def test_rollback_interrupted_after_moving_new_version_aside_restores_without_remnants(tmp_path, monkeypatch):
    app, control, profile, install = applied_update(tmp_path, monkeypatch)
    interrupt(app, control, profile, "recover")
    assert not app.exists()
    assert desktop_update.recover(app, control)["phase"] == "restored"
    assert (app / "version.txt").read_text() == "old"
    assert not list(app.parent.glob(".astra-update-*"))
    assert not list(control.glob("updater-*"))
    with RuntimeLease(install, "recovered fixture"):
        assert (app / "version.txt").read_text() == "old"


STAGE_KILLED = """
import os, shutil, sys
from pathlib import Path
from agent.launcher import desktop_update, installation
candidate, app, control, profile, digest = sys.argv[1:6]
installation.user_home = lambda: Path(profile)
copy = shutil.copytree
def terminate_before_updater_copy(source, destination, *args, **kwargs):
    if Path(destination).name.startswith('updater-'):
        os._exit(77)
    return copy(source, destination, *args, **kwargs)
shutil.copytree = terminate_before_updater_copy
desktop_update.stage(Path(candidate), Path(app), Path(control), expected_sha256=digest, target='darwin-arm64')
"""


def installed_update(tmp_path):
    app = tmp_path / "installed/Astra.app"
    install = installation.discover(make_application(app, "old"))
    candidate = tmp_path / "candidate"
    make_application(candidate / "Astra.app", "new")
    manifest = seal_tree(candidate / "Astra.app", version="new", target="darwin-arm64")
    manifest["application"] = "Astra.app"
    write_json(candidate / "release.json", manifest)
    stage = ["stage", "--candidate", str(candidate), "--target", "darwin-arm64",
             "--manifest-sha256", sha256(candidate / "release.json")]
    # The CLI keeps its journal beside the application.
    return app, app.parent / ".Astra.app.astra-updater", install, stage


def installed_updater(monkeypatch, capsys, app, *args):
    """Run the CLI as the installed application's own updater."""
    monkeypatch.setattr(desktop_update.sys, "executable", str(app / "Contents/Resources/runtime/python/bin/python3"))
    monkeypatch.setattr(desktop_update.sys, "argv", ["desktop_update", *args, "--application", str(app)])
    code = desktop_update.main()
    return code, capsys.readouterr()


@pytest.mark.parametrize("interrupted", [True, False], ids=["killed-while-preparing", "staged-not-applied"])
def test_installed_updater_discards_stage_that_never_touched_the_application(tmp_path, monkeypatch, capsys,
                                                                            interrupted):
    profile = tmp_path / "profile"
    monkeypatch.setattr(installation, "user_home", lambda: profile)
    app, control, install, stage = installed_update(tmp_path)
    if interrupted:
        killed = subprocess.run([sys.executable, "-c", STAGE_KILLED, stage[2], str(app), str(control), str(profile),
                                 stage[-1]], cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT)},
                                capture_output=True, timeout=30)
        assert killed.returncode == 77, killed.stderr
        assert read_json(control / "desktop-update.json")["phase"] == "preparing"
        assert installed_updater(monkeypatch, capsys, app, *stage)[0] == 1
    else:
        assert installed_updater(monkeypatch, capsys, app, *stage)[0] == 0
        # An apply killed after fencing launches but before recording 'replacing'.
        write_json(install.control / "desktop-pending.json", {"journal": str(control / "desktop-update.json")})
    code, output = installed_updater(monkeypatch, capsys, app, "recover")
    assert code == 0, output.err
    assert json.loads(output.out)["phase"] == "discarded"
    assert (app / "version.txt").read_text() == "old"
    assert not list(app.parent.glob(".astra-update-*"))
    assert not list(control.glob("updater-*"))
    assert not (control / "desktop-update.json").exists()
    with RuntimeLease(install, "untouched fixture"):
        assert (app / "version.txt").read_text() == "old"
    assert installed_updater(monkeypatch, capsys, app, *stage)[0] == 0


def test_installed_updater_cannot_skip_recovery_once_apply_moved_the_application(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(installation, "user_home", lambda: tmp_path / "profile")
    app, control, _, stage = installed_update(tmp_path)
    assert installed_updater(monkeypatch, capsys, app, *stage)[0] == 0
    desktop_update.apply(app, control, validate=lambda _: None)
    code, output = installed_updater(monkeypatch, capsys, app, "recover")
    assert code == 1 and "updater printed by stage" in output.err
    with pytest.raises(LauncherError, match="already started"):
        desktop_update.recover(app, control, untouched_only=True)
    # A journal claiming an untouched phase must not discard a moved-aside original.
    state = read_json(control / "desktop-update.json")
    write_json(control / "desktop-update.json", {**state, "phase": "staged"})
    code, output = installed_updater(monkeypatch, capsys, app, "recover")
    assert code == 1 and "does not match" in output.err
    assert (app / "version.txt").read_text() == "new"
    assert (Path(state["backup"]) / "version.txt").read_text() == "old"
    write_json(control / "desktop-update.json", state)
    assert desktop_update.recover(app, control)["phase"] == "restored"
    assert (app / "version.txt").read_text() == "old"
