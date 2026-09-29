"""Explicit offline desktop updates with verified staging and retained rollback.

Run from a copied updater outside the app on Windows. The running application
is never mutated, and source checkout update transactions remain independent.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path

from agent.runtime.instance_lock import InstanceAlreadyRunning, InstanceLock

from .common import LauncherError, read_json, write_json
from .desktop_distribution import resources_for, sha256, verify_tree
from .installation import discover
from .locking import exclusive, require_idle


def _journal(control: Path) -> Path:
    return control / 'desktop-update.json'


def _state(application: Path, control: Path) -> dict:
    state = read_json(_journal(control))
    if not state or state.get('application') != str(application.resolve()):
        raise LauncherError('No matching staged desktop update. Stage a release first.')
    for key in ('staging', 'backup'):
        path = Path(state[key])
        if path.parent != application.resolve().parent or not path.name.startswith('.astra-update-'):
            raise LauncherError('Invalid desktop update recovery path.')
    return state


def ensure_idle(application: Path, control: Path) -> None:
    backend = resources_for(application) / 'runtime/backend'
    if (backend / 'desktop-installation.json').is_file():
        install = discover(backend)
        require_idle(install)


def _pending_install(application: Path, state: dict):
    root = resources_for(application) / 'runtime/backend'
    if (root / 'desktop-installation.json').is_file():
        return discover(root)
    backup_root = resources_for(Path(state['backup'])) / 'runtime/backend'
    # A macOS staging/backup directory deliberately keeps a non-.app basename;
    # its content layout still comes from the application's target identity.
    if application.suffix == '.app':
        backup_root = Path(state['backup']) / 'Contents/Resources/runtime/backend'
    if (backup_root / 'desktop-installation.json').is_file() and state.get('installation_control'):
        prior = discover(backup_root)
        return replace(prior, root=root, control=Path(state['installation_control']))
    return None


def stage(candidate: Path, application: Path, control: Path, *, expected_sha256: str, target: str) -> dict:
    application = application.resolve()
    manifest_path = candidate / 'release.json'
    if sha256(manifest_path) != expected_sha256:
        raise LauncherError('Desktop release manifest digest mismatch.')
    manifest = read_json(manifest_path)
    name = manifest.get('application')
    if not isinstance(name, str) or Path(name).name != name or name != application.name:
        raise LauncherError('Desktop application identity does not match the update.')
    verify_tree(candidate / name, manifest, target=target)
    control.mkdir(parents=True, exist_ok=True)
    with InstanceLock(control / 'desktop-update.lock'):
        if _journal(control).exists():
            raise LauncherError('A desktop update already exists; recover or finalize it first.')
        token = uuid.uuid4().hex
        staging = application.parent / f'.astra-update-{token}-staged'
        backup = application.parent / f'.astra-update-{token}-previous'
        state = {'schema': 1, 'application': str(application), 'staging': str(staging), 'backup': str(backup),
                 'phase': 'preparing', 'manifest': manifest, 'manifest_sha256': expected_sha256}
        write_json(_journal(control), state)
        try:
            shutil.copytree(candidate / name, staging, symlinks=True)
            verify_tree(staging, manifest, target=target)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            _journal(control).unlink(missing_ok=True)
            raise
        state['phase'] = 'staged'
        write_json(_journal(control), state)
        return {'phase': 'staged', 'version': manifest['version']}


def _health(application: Path) -> None:
    root = resources_for(application) / 'runtime/backend'
    install = discover(root)
    python = install.python
    code = 'import ssl,sqlite3,agent.ui.queries,agent.cli.backend; print("desktop runtime healthy")'
    with tempfile.TemporaryDirectory(prefix='astra-update-health-') as temporary:
        # Import checks cannot read or mutate the active user's credentials,
        # settings or sessions. Only OS launch essentials are inherited.
        env = {key: value for key, value in os.environ.items()
               if key.upper() in {'PATH', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'TEMP', 'TMP', 'TMPDIR'}}
        env.update(PYTHONPATH=str(root), PYTHONNOUSERSITE='1', PYTHONDONTWRITEBYTECODE='1',
                   HOME=temporary, USERPROFILE=temporary, LOCALAPPDATA=temporary, APPDATA=temporary,
                   ASTRA_HOME=temporary, AGENT_SESSION_DIR=str(Path(temporary) / 'sessions'),
                   ASTRA_ENV_FILE=str(Path(temporary) / '.env'), AGENT_LOG_DIR=str(Path(temporary) / 'logs'))
        completed = subprocess.run([str(python), '-c', code], cwd=root, env=env, capture_output=True, timeout=60)
    if completed.returncode:
        raise LauncherError('Updated desktop runtime failed its health check.')


def apply(application: Path, control: Path, *, validate=None) -> dict:
    application = application.resolve()
    with InstanceLock(control / 'desktop-update.lock'):
        state = _state(application, control)
        if state['phase'] != 'staged':
            raise LauncherError('Desktop update is interrupted or already applied; recover it first.')
        staging, backup = Path(state['staging']), Path(state['backup'])
        verify_tree(staging, state['manifest'])
        backend = resources_for(application) / 'runtime/backend'
        install = discover(backend) if (backend / 'desktop-installation.json').is_file() else None
        # The standard install lock serializes a new backend/desktop lease with
        # this final idle check. A dedicated transaction lock also serializes
        # stage/recover commands without changing the source updater journal.
        with exclusive(install) if install else nullcontext():
            ensure_idle(application, control)
            state['phase'] = 'replacing'
            if install:
                state['installation_control'] = str(install.control)
                write_json(install.control / 'desktop-pending.json', {'journal': str(_journal(control))})
            write_json(_journal(control), state)
            try:
                os.replace(application, backup)
                os.replace(staging, application)
                state['phase'] = 'validating'
                write_json(_journal(control), state)
                (validate or _health)(application)
            except BaseException as exc:
                # If replacement was denied (e.g. locked Windows files), keep
                # both original app and staged candidate. Roll back only after
                # the original directory was successfully moved aside.
                if backup.exists():
                    if application.exists():
                        os.replace(application, staging)
                    os.replace(backup, application)
                shutil.rmtree(staging, ignore_errors=True)
                _journal(control).unlink(missing_ok=True)
                if install:
                    (install.control / 'desktop-pending.json').unlink(missing_ok=True)
                raise LauncherError(f'Desktop update failed; previous application restored: {exc}') from exc
            state['phase'] = 'applied'
            write_json(_journal(control), state)
            if install:
                (install.control / 'desktop-pending.json').unlink(missing_ok=True)
            return {'phase': 'applied', 'version': state['manifest']['version'], 'rollback_available': True}


def recover(application: Path, control: Path) -> dict:
    application = application.resolve()
    with InstanceLock(control / 'desktop-update.lock'):
        state = _state(application, control)
        install = _pending_install(application, state)
        with exclusive(install, allow_pending=True) if install else nullcontext():
            if install:
                require_idle(install)
            return _recover(application, control, state, install)


def _recover(application: Path, control: Path, state: dict, install) -> dict:
    staging, backup = Path(state['staging']), Path(state['backup'])
    if backup.exists():
        if application.exists():
            # Retain the failed/new tree until the original is back in place.
            displaced = staging.with_name(staging.name + '-displaced')
            os.replace(application, displaced)
        else:
            displaced = None
        try:
            os.replace(backup, application)
        except BaseException:
            if displaced is not None:
                os.replace(displaced, application)
            raise
        if displaced is not None:
            shutil.rmtree(displaced)
    elif not application.exists():
        raise LauncherError('Original desktop application is missing; recovery left all files intact.')
    shutil.rmtree(staging, ignore_errors=True)
    _journal(control).unlink()
    if install:
        (install.control / 'desktop-pending.json').unlink(missing_ok=True)
    return {'phase': 'restored'}

def finalize(application: Path, control: Path) -> dict:
    """Explicitly discard a validated version's rollback before the next update."""
    application = application.resolve()
    with InstanceLock(control / 'desktop-update.lock'):
        state = _state(application, control)
        if state['phase'] != 'applied':
            raise LauncherError('Only an applied desktop update can be finalized.')
        verify_tree(application, state['manifest'])
        shutil.rmtree(Path(state['backup']))
        _journal(control).unlink()
        return {'phase': 'finalized'}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['stage', 'apply', 'recover', 'finalize'])
    parser.add_argument('--application', type=Path, required=True)
    parser.add_argument('--candidate', type=Path)
    parser.add_argument('--manifest-sha256')
    parser.add_argument('--target', choices=['darwin-arm64', 'win32-x64'])
    options = parser.parse_args()
    application = options.application.resolve()
    # Stable sibling location survives a rename interrupted between app versions.
    control = application.parent / ('.' + application.name + '.astra-updater')
    try:
        if options.action in {'apply', 'recover'} and Path(sys.executable).resolve().is_relative_to(application):
            raise LauncherError('Run the updater from the external candidate release; the installed application must be closed.')
        if options.action == 'stage':
            if not options.candidate or not options.manifest_sha256 or not options.target:
                parser.error('stage requires --candidate, --manifest-sha256 and --target')
            result = stage(options.candidate, application, control,
                           expected_sha256=options.manifest_sha256, target=options.target)
        elif options.action == 'apply':
            result = apply(application, control)
        elif options.action == 'recover':
            result = recover(application, control)
        else:
            result = finalize(application, control)
        print(json.dumps(result))
        return 0
    except (LauncherError, OSError, InstanceAlreadyRunning) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
