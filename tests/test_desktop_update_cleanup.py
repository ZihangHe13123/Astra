"""A killed rollback must leave a recoverable journal or an unblocked app."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

from agent.launcher import desktop_update, installation
from agent.launcher.common import write_json
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
    with RuntimeLease(install, "recovered fixture"):
        assert (app / "version.txt").read_text() == "old"
