"""Explicit offline desktop updates with verified staging and retained rollback.

The installed application's updater stages a candidate: trusted code checks it
before any of its code runs. apply, recover and finalize then run from the
verified updater copy that staging leaves outside both application trees. The
running application is never mutated, and source checkout update transactions
remain independent.
"""
from __future__ import annotations

import argparse
import hashlib
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
from .desktop_distribution import resources_for, verify_tree
from .installation import discover
from .locking import exclusive, require_idle

# Each application bundles its updater entry point in <resources>/runtime.
UPDATER = {'darwin-arm64': 'astra-desktop-update', 'win32-x64': 'astra-desktop-update.cmd'}
# Journal phases in which the installed application has not been touched.
UNTOUCHED = {'preparing', 'staged'}


def _journal(control: Path) -> Path:
    return control / 'desktop-update.json'


def _runtime_part(application: Path) -> str:
    # Staging and backup names drop the .app suffix; their layout follows the application's.
    return (resources_for(application) / 'runtime').relative_to(application).as_posix()


def _discard_updater(state: dict) -> None:
    # Windows cannot delete the interpreter running this process; a copy kept
    # for that reason is removed by the next stage.
    path = Path(state['updater']) if state.get('updater') else None
    if path is not None and not Path(sys.executable).resolve().is_relative_to(path.resolve()):
        shutil.rmtree(path, ignore_errors=True)


def _finish_recovery(control: Path, install) -> None:
    # Keep the durable recovery instructions until the restored app can start.
    # Interruption at either unlink must leave a runnable app or a journal that
    # can finish clearing its fence on the next recover invocation.
    if install:
        (install.control / 'desktop-pending.json').unlink(missing_ok=True)
    _journal(control).unlink(missing_ok=True)


def _state(application: Path, control: Path) -> dict:
    state = read_json(_journal(control))
    if not state or state.get('application') != str(application.resolve()):
        raise LauncherError('No matching staged desktop update. Stage a release first.')
    for key in ('staging', 'backup'):
        path = Path(state[key])
        if path.parent != application.resolve().parent or not path.name.startswith('.astra-update-'):
            raise LauncherError('Invalid desktop update recovery path.')
    updater = Path(state['updater']) if state.get('updater') else None
    if updater is not None and (updater.parent != control.resolve() or not updater.name.startswith('updater-')):
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
    """Verify a candidate with this already installed code before any candidate code runs."""
    application = application.resolve()
    # Hash and parse the same bytes so the checked manifest is the one used.
    data = (candidate / 'release.json').read_bytes()
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        raise LauncherError('Desktop release manifest digest mismatch.')
    try:
        manifest = json.loads(data)
    except ValueError as exc:
        raise LauncherError(f'Invalid desktop release manifest: {exc}') from exc
    name = manifest.get('application') if isinstance(manifest, dict) else None
    if not isinstance(name, str) or Path(name).name != name or name != application.name:
        raise LauncherError('Desktop application identity does not match the update.')
    verify_tree(candidate / name, manifest, target=target)
    runtime, entry = _runtime_part(application), UPDATER.get(target)
    if entry is None or f'{runtime}/{entry}' not in manifest['files']:
        raise LauncherError('Desktop release has no bundled updater for this target.')
    # apply and recover rename the installed and staged applications, so the
    # interpreter performing them must live in neither: Windows cannot rename
    # a directory whose executable is running.
    updater_manifest = {**manifest, 'files': {key[len(runtime) + 1:]: value for key, value in manifest['files'].items()
                                              if key.startswith(runtime + '/')}}
    control.mkdir(parents=True, exist_ok=True)
    with InstanceLock(control / 'desktop-update.lock'):
        if _journal(control).exists():
            raise LauncherError('A desktop update already exists; recover or finalize it first.')
        for previous in control.glob('updater-*'):
            _discard_updater({'updater': str(previous)})
        token = uuid.uuid4().hex
        staging = application.parent / f'.astra-update-{token}-staged'
        backup = application.parent / f'.astra-update-{token}-previous'
        updater = control.resolve() / f'updater-{token}'
        state = {'schema': 1, 'application': str(application), 'staging': str(staging), 'backup': str(backup),
                 'updater': str(updater), 'phase': 'preparing', 'manifest': manifest,
                 'manifest_sha256': expected_sha256}
        write_json(_journal(control), state)
        try:
            shutil.copytree(candidate / name, staging, symlinks=True)
            verify_tree(staging, manifest, target=target)
            shutil.copytree(staging / runtime, updater, symlinks=True)
            verify_tree(updater, updater_manifest, target=target)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            shutil.rmtree(updater, ignore_errors=True)
            _journal(control).unlink(missing_ok=True)
            raise
        state['phase'] = 'staged'
        write_json(_journal(control), state)
        return {'phase': 'staged', 'version': manifest['version'], 'updater': str(updater / entry)}


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
                _discard_updater(state)
                _finish_recovery(control, install)
                raise LauncherError(f'Desktop update failed; previous application restored: {exc}') from exc
            state['phase'] = 'applied'
            write_json(_journal(control), state)
            if install:
                (install.control / 'desktop-pending.json').unlink(missing_ok=True)
            return {'phase': 'applied', 'version': state['manifest']['version'], 'rollback_available': True}


def recover(application: Path, control: Path, *, untouched_only: bool = False) -> dict:
    application = application.resolve()
    with InstanceLock(control / 'desktop-update.lock'):
        state = _state(application, control)
        install = _pending_install(application, state)
        if state['phase'] in UNTOUCHED:
            return _discard_staged(application, control, state, install)
        if untouched_only:
            # The installed updater may only drop copies; once apply has begun,
            # recovery renames the installation and must run from outside it.
            raise LauncherError('The desktop update has already started replacing the application; run recover '
                                'with the updater printed by stage.')
        if state['phase'] == 'finalizing':
            # Finalization had begun deleting the previous application, so it
            # is no longer a rollback target. Finish discarding it instead.
            return {**_finish_finalize(control, state, install), 'rollback_available': False}
        with exclusive(install, allow_pending=True) if install else nullcontext():
            if install:
                require_idle(install)
            return _recover(application, control, state, install)


def _recover(application: Path, control: Path, state: dict, install) -> dict:
    staging, backup = Path(state['staging']), Path(state['backup'])
    # Retain the failed/new tree here until the original is back in place.
    displaced = staging.with_name(staging.name + '-displaced')
    if backup.exists():
        # Only apply moves the previous application aside, always atomically.
        if state['phase'] not in {'replacing', 'validating', 'applied'}:
            raise LauncherError('Desktop update journal does not match its files; recovery left all files intact.')
        moved = application.exists()
        if moved:
            os.replace(application, displaced)
        try:
            os.replace(backup, application)
        except BaseException:
            if moved:
                os.replace(displaced, application)
            raise
    elif not application.exists():
        raise LauncherError('Original desktop application is missing; recovery left all files intact.')
    # Also clears a displaced tree left by a recovery interrupted after its restore.
    shutil.rmtree(displaced, ignore_errors=True)
    shutil.rmtree(staging, ignore_errors=True)
    _discard_updater(state)
    _finish_recovery(control, install)
    return {'phase': 'restored'}


def _discard_staged(application: Path, control: Path, state: dict, install) -> dict:
    # apply records 'replacing' before its first rename, so in these phases the
    # installed application is untouched and only the update's copies exist.
    if Path(state['backup']).exists() or not application.exists():
        raise LauncherError('Desktop update journal does not match its files; recovery left all files intact.')
    shutil.rmtree(Path(state['staging']), ignore_errors=True)
    _discard_updater(state)
    # Also clears a fence left by an apply killed before it recorded 'replacing'.
    _finish_recovery(control, install)
    return {'phase': 'discarded', 'version': state['manifest'].get('version')}


def _finish_finalize(control: Path, state: dict, install) -> dict:
    backup = Path(state['backup'])
    if backup.exists():
        shutil.rmtree(backup)
    _discard_updater(state)
    # An apply interrupted after validation can still hold the launch fence.
    _finish_recovery(control, install)
    return {'phase': 'finalized'}


def finalize(application: Path, control: Path) -> dict:
    """Explicitly discard a validated version's rollback before the next update."""
    application = application.resolve()
    with InstanceLock(control / 'desktop-update.lock'):
        state = _state(application, control)
        if state['phase'] not in {'applied', 'finalizing'}:
            raise LauncherError('Only an applied desktop update can be finalized.')
        if state['phase'] == 'applied':
            verify_tree(application, state['manifest'])
            # Record the decision before deleting anything: recovery must never
            # move a partly deleted previous application over the new one.
            state['phase'] = 'finalizing'
            write_json(_journal(control), state)
        return _finish_finalize(control, state, _pending_install(application, state))


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
        running = Path(sys.executable).resolve()
        journal = read_json(_journal(control))
        # apply/recover rename the application and staged copy; finalize deletes the backup.
        busy = [Path(journal[key]) for key in ('staging', 'backup') if isinstance(journal.get(key), str)]
        # The installed updater may still discard an update that never touched
        # the installation, such as an interrupted stage; recover re-checks the
        # phase under the transaction lock.
        if options.action == 'apply' or (options.action == 'recover' and journal.get('phase') not in UNTOUCHED):
            busy.append(application)
        if options.action != 'stage' and any(running.is_relative_to(path) for path in busy):
            raise LauncherError('Run this action with the updater printed by stage, outside the installed and '
                                'staged applications; the installed application must be closed.')
        if options.action == 'stage':
            if not options.candidate or not options.manifest_sha256 or not options.target:
                parser.error('stage requires --candidate, --manifest-sha256 and --target')
            if running.is_relative_to(options.candidate.resolve()):
                raise LauncherError("Stage with the installed application's updater; a candidate's own code "
                                    'runs only after it has been staged and verified.')
            result = stage(options.candidate, application, control,
                           expected_sha256=options.manifest_sha256, target=options.target)
        elif options.action == 'apply':
            result = apply(application, control)
        elif options.action == 'recover':
            result = recover(application, control, untouched_only=running.is_relative_to(application))
        else:
            result = finalize(application, control)
        print(json.dumps(result))
        return 0
    except (LauncherError, OSError, InstanceAlreadyRunning) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
