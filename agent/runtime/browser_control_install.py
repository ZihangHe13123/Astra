"""Opt-in, ownership-checked Browser Control lifecycle for source installations."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import sys
import uuid

from . import browser_control_registry as registry
from .browser_control_storage import check_directory, open_private, private_directory, read_private

HOST_NAME = registry.HOST_NAME
BROWSERS = {'edge': 'Microsoft Edge', 'chrome': 'Google/Chrome'}


def _supported():
    if sys.platform not in {'darwin', 'win32'} or (sys.platform == 'win32' and os.name != 'nt'):
        raise RuntimeError('Native host installation supports native Windows and macOS')
    if sys.platform == 'win32':
        registry.require_user_registry()


def _paths(browser, home=None):
    if browser not in BROWSERS:
        raise ValueError('Choose edge or chrome')
    home = Path(home) if home is not None else Path.home()
    if sys.platform == 'win32':
        local = home / 'AppData/Local' if home != Path.home() else Path(os.environ.get('LOCALAPPDATA', home / 'AppData/Local'))
        private = local / 'Astra/browser-control-host'
        manifest = private / f'{browser}-{HOST_NAME}.json'
        launcher = private / f'host-{browser}.cmd'
        if any(char in str(private) for char in '%\r\n'):
            raise ValueError('Native Messaging command-launcher paths cannot contain % or newlines; use a supported LOCALAPPDATA path')
    else:
        support = home / 'Library/Application Support'
        private = support / 'Astra/browser-control-host'
        manifest = support / BROWSERS[browser] / 'NativeMessagingHosts' / (HOST_NAME + '.json')
        launcher = private / f'host-{browser}.sh'
    return {'manifest': manifest, 'launcher': launcher, 'record': private / f'owned-{browser}.json'}


def _targets(repo=None, python=None):
    repo = Path(repo or Path(__file__).resolve().parents[2]).absolute()
    if any(part.lower() in {'.worktrees', 'worktrees'} for part in repo.parts):
        raise ValueError('Use a stable checkout, not a temporary worktree')
    if (repo / '.git').is_file() and '/worktrees/' in (repo / '.git').read_text(encoding='utf-8').replace('\\', '/'):
        raise ValueError('Use a stable checkout, not a temporary worktree')
    repo = repo.resolve()
    # Keep the virtualenv executable path (resolving a POSIX symlink loses it).
    python = Path(python or sys.executable).absolute()
    if not (repo / 'agent/runtime/browser_control_host.py').is_file() or not python.is_file():
        raise ValueError('A stable Astra source checkout and Python interpreter are required; run astra setup')
    if not (repo / 'browser-control-extension/manifest.json').is_file():
        raise ValueError('Browser Control needs a complete source installation with browser-control-extension')
    return repo, python


def _launcher(repo, python):
    if sys.platform != 'win32':
        return ('#!/bin/sh\nset -eu\ncd ' + shlex.quote(str(repo)) + '\nexec ' +
                shlex.quote(str(python)) + ' -m agent.runtime.browser_control_host "$@"\n').encode('utf-8')
    if any(char in str(python) for char in '\r\n"\0'):
        raise ValueError('Unsupported Python path for a Windows command launcher')
    executable = str(python).replace('%', '%%')
    # Select UTF-8 before cmd parses non-ASCII paths; disable delayed expansion.
    # No browser-supplied argument is interpolated into this shell command.
    code = ("import runpy,sys;sys.path.insert(0,bytes.fromhex('" + str(repo).encode('utf-8').hex() +
            "').decode('utf-8'));runpy.run_module('agent.runtime.browser_control_host',run_name='__main__')")
    return ('@echo off\r\nchcp 65001 >nul\r\nsetlocal DisableDelayedExpansion\r\n' +
            f'"{executable}" -I -c "{code}"\r\n').encode('utf-8')


def _json(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def _content(paths, browser, extension_id, repo, python):
    launcher = _launcher(repo, python)
    manifest = _json({'name': HOST_NAME, 'description': 'Astra opt-in browser control',
                      'path': str(paths['launcher']), 'type': 'stdio',
                      'allowed_origins': [f'chrome-extension://{extension_id}/']})
    record = _json({'schema': 1, 'browser': browser, 'extension_id': extension_id,
                    'repo': str(repo), 'python': str(python),
                    'hashes': {key: hashlib.sha256(value).hexdigest() for key, value in
                               [('launcher', launcher), ('manifest', manifest)]}})
    return {'launcher': launcher, 'manifest': manifest, 'record': record}


def _present(path):
    return path.exists() or path.is_symlink()


def _owned(paths, browser, *, allow_missing=False):
    check_directory(paths['launcher'].parent)
    record = json.loads(read_private(paths['record']))
    if not isinstance(record, dict) or record.get('schema') != 1 or record.get('browser') != browser:
        raise ValueError('Unrecognized Browser Control ownership record')
    if not re.fullmatch('[a-p]{32}', str(record.get('extension_id', ''))):
        raise ValueError('Invalid extension ID in ownership record')
    if not all(isinstance(record.get(key), str) and Path(record[key]).is_absolute() for key in ('repo', 'python')):
        raise ValueError('Invalid paths in ownership record')
    hashes = record.get('hashes')
    if not isinstance(hashes, dict) or set(hashes) != {'launcher', 'manifest'}:
        raise ValueError('Invalid ownership hashes')
    for key in ('launcher', 'manifest'):
        if allow_missing and not _present(paths[key]):
            continue
        if hashlib.sha256(read_private(paths[key])).hexdigest() != hashes[key]:
            raise PermissionError('Native host artifacts changed; refusing to overwrite or remove them')
    return record


def _legacy(paths, browser, repo):
    """Recognize only the historical macOS generator, for explicit repair."""
    if sys.platform != 'darwin' or _present(paths['record']):
        raise ValueError('No verified ownership record; refusing to replace existing files')
    check_directory(paths['launcher'].parent)
    manifest = json.loads(read_private(paths['manifest']))
    if not isinstance(manifest, dict):
        raise ValueError('Invalid legacy native-host manifest')
    lines = read_private(paths['launcher']).decode('utf-8').splitlines()
    if len(lines) != 4:
        raise ValueError('Not an installer-owned legacy launcher')
    command = shlex.split(lines[3])
    if len(command) != 5 or command[0] != 'exec' or command[2:] != ['-m', 'agent.runtime.browser_control_host', '$@']:
        raise ValueError('Not an installer-owned legacy launcher')
    origins = manifest.get('allowed_origins', [])
    if len(origins) != 1 or not re.fullmatch(r'chrome-extension://[a-p]{32}/', str(origins[0])):
        raise ValueError('Invalid legacy extension origin')
    extension_id = origins[0].split('/')[2]
    content = _content(paths, browser, extension_id, repo, Path(command[1]))
    if read_private(paths['launcher']) != content['launcher'] or json.loads(content['manifest']) != manifest:
        raise ValueError('Legacy registration does not match this checkout')
    return json.loads(content['record'])


@contextmanager
def _mutation(paths):
    private_directory(paths['launcher'].parent)
    fd = open_private(paths['launcher'].parent / 'management.lock', os.O_RDWR | os.O_CREAT)
    try:
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def _write(path, data, *, executable=False, replace=False):
    target = path.with_name('.' + path.name + '.' + uuid.uuid4().hex) if replace else path
    fd = open_private(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700 if executable else 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(target, path)
    except BaseException:
        target.unlink(missing_ok=True)
        raise


def status(*, browser='edge', home=None, repo=None):
    result = {'browser': browser, 'state': 'not_configured', 'live': 'not_probed'}
    try:
        _supported()
        paths = _paths(browser, home)
        if not any(_present(path) for path in paths.values()):
            if sys.platform == 'win32' and registry.entries(browser):
                raise ValueError('Native host is registered elsewhere')
            return result
        root = Path(repo or Path(__file__).resolve().parents[2]).resolve()
        record = _owned(paths, browser) if _present(paths['record']) else _legacy(paths, browser, root)
        if Path(record['repo']).resolve() != root:
            raise ValueError('Native host belongs to another checkout; use that installation or explicitly repair')
        _targets(root, record['python'])
        if sys.platform == 'win32' and not registry.matches(browser, paths['manifest']):
            raise ValueError('Native host registry binding is missing or conflicts')
        result.update(state='configured', extension_id=record['extension_id'], manifest=str(paths['manifest']))
    except RuntimeError as exc:
        result.update(state='unsupported', message=str(exc))
    except (OSError, ValueError, TypeError, KeyError) as exc:
        result.update(state='invalid', message=f'{exc}. Run astra browser-control repair --browser {browser}.')
    return result


def install(*, extension_id, browser='edge', home=None, repo=None, python=None):
    if not re.fullmatch('[a-p]{32}', extension_id):
        raise ValueError('An exact Chromium extension ID ([a-p]{32}) is required')
    _supported()
    repo, python = _targets(repo, python)
    paths = _paths(browser, home)
    contents = _content(paths, browser, extension_id, repo, python)
    if sys.platform == 'win32' and registry.entries(browser):
        if not registry.matches(browser, paths['manifest']) or not _present(paths['record']):
            raise FileExistsError('Native host is registered elsewhere or has no ownership record; no files were changed')
    with _mutation(paths):
        if any(_present(path) for path in paths.values()):
            record = _owned(paths, browser)
            if record != json.loads(contents['record']) or any(read_private(paths[key]) != value for key, value in contents.items()):
                raise FileExistsError('Host registration differs; use explicit repair or uninstall')
            if sys.platform == 'win32' and not registry.matches(browser, paths['manifest']):
                raise FileExistsError('Registry binding is missing; use explicit repair')
            return paths
        if sys.platform == 'darwin':
            paths['manifest'].parent.mkdir(parents=True, exist_ok=True)
        created, published = [], False
        try:
            for key, value in contents.items():
                _write(paths[key], value, executable=key == 'launcher')
                created.append(paths[key])
            if sys.platform == 'win32':
                published = registry.publish(browser, paths['manifest'])
        except BaseException:
            if sys.platform == 'win32' and (published or registry.matches(browser, paths['manifest'])):
                registry.remove(browser, paths['manifest'])
            for path in reversed(created):
                path.unlink(missing_ok=True)
            raise
    return paths


def repair(*, browser='edge', home=None, repo=None, python=None):
    _supported()
    repo, python = _targets(repo, python)
    paths = _paths(browser, home)
    if not any(_present(path) for path in paths.values()):
        raise FileNotFoundError(
            f'No owned native-host files in this user environment. Run astra browser-control install '
            f'--browser {browser} --extension-id YOUR_EXTENSION_ID first.'
        )
    if sys.platform == 'win32':
        from .browser_control_repair import repair_endpoint_permissions, repair_host_permissions
        # Ownership and exact generated contents are verified before tightening
        # any ACL. Unsafe unknown files require manual recovery.
        try:
            _owned(paths, browser, allow_missing=True)
        except PermissionError:
            repair_host_permissions(paths)
        repair_endpoint_permissions(paths['launcher'].parent.parent / 'browser-control')
    with _mutation(paths):
        record = _owned(paths, browser, allow_missing=True) if _present(paths['record']) else _legacy(paths, browser, repo)
        if sys.platform == 'win32' and registry.entries(browser) and not registry.matches(browser, paths['manifest']):
            raise FileExistsError('Native host registry belongs to another installation')
        had_registration = sys.platform == 'win32' and registry.matches(browser, paths['manifest'])
        old = {key: read_private(path) if _present(path) else None for key, path in paths.items()}
        contents = _content(paths, browser, record['extension_id'], repo, python)
        changed, published = [], False
        try:
            for key, value in contents.items():
                _write(paths[key], value, executable=key == 'launcher', replace=old[key] is not None)
                changed.append(key)
            if sys.platform == 'win32':
                published = registry.publish(browser, paths['manifest'])
        except BaseException:
            if sys.platform == 'win32' and not had_registration and (published or registry.matches(browser, paths['manifest'])):
                registry.remove(browser, paths['manifest'])
            for key in reversed(changed):
                if old[key] is None:
                    paths[key].unlink(missing_ok=True)
                else:
                    _write(paths[key], old[key], executable=key == 'launcher', replace=True)
            raise
    return paths


def uninstall(*, browser='edge', home=None, repo=None):
    _supported()
    paths = _paths(browser, home)
    if not any(_present(path) for path in paths.values()):
        if sys.platform == 'win32' and registry.entries(browser):
            raise PermissionError('Unowned registry binding was preserved')
        return paths
    with _mutation(paths):
        if _present(paths['record']):
            _owned(paths, browser, allow_missing=True)
        else:
            _legacy(paths, browser, Path(repo or Path(__file__).resolve().parents[2]).resolve())
        old = {key: read_private(path) for key, path in paths.items() if _present(path)}
        had_registration = sys.platform == 'win32' and registry.matches(browser, paths['manifest'])
        removed = []
        try:
            if sys.platform == 'win32':
                registry.remove(browser, paths['manifest'])
            # Keep the record until last, so process-interrupted removal can resume.
            for key in ('manifest', 'launcher', 'record'):
                if key in old:
                    paths[key].unlink()
                    removed.append(key)
        except BaseException:
            for key in removed:
                _write(paths[key], old[key], executable=key == 'launcher')
            if had_registration:
                registry.publish(browser, paths['manifest'])
            raise
    return paths


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['install', 'repair', 'status', 'uninstall'], nargs='?', default='install')
    parser.add_argument('--browser', choices=list(BROWSERS), default='edge')
    parser.add_argument('--extension-id')
    parser.add_argument('--repo', type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument('--python', type=Path, default=Path(sys.executable))
    parser.add_argument('--uninstall', action='store_true', help='legacy alias for uninstall')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    action = 'uninstall' if args.uninstall else args.action
    try:
        if action == 'status':
            result = status(browser=args.browser, repo=args.repo)
        else:
            kwargs = {'browser': args.browser, 'repo': args.repo}
            if action != 'uninstall':
                kwargs['python'] = args.python
            if action == 'install':
                if not args.extension_id:
                    parser.error('--extension-id is required for install')
                kwargs['extension_id'] = args.extension_id
            paths = {'install': install, 'repair': repair, 'uninstall': uninstall}[action](**kwargs)
            result = {'action': action, 'browser': args.browser, 'manifest': str(paths['manifest'])}
            if action != 'uninstall':
                result.update(live='not_probed', next='Reload the extension, start an Astra browser task, then connect and allow only the intended tab.')
        print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else '\n'.join(f'{key}: {value}' for key, value in result.items()))
        return 1 if result.get('state') in {'invalid', 'unsupported'} else 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f'Browser Control: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
