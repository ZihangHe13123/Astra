#!/usr/bin/env python3
"""Run a built desktop release's real updaters through an update round trip.

Everything happens under a fresh work directory: the release's application is
copied there as the "installed" application, and every updater runs with its
home, profile and temp directories redirected inside it. The release itself is
only read. Steps:

1. A copy of the candidate with one altered file is refused by the installed
   updater before any of its code runs.
2. A stage killed partway is discarded by the installed updater's recover.
3. The release is staged with the installed updater and its manifest digest,
   applied and finalized with the updater copy that stage printed.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent.launcher.common import LauncherError  # noqa: E402
from agent.launcher.desktop_distribution import resources_for, sha256, verify_tree  # noqa: E402
from agent.launcher.desktop_update import UPDATER  # noqa: E402

TIMEOUT = 900


def phase(name: str) -> None:
    print(f'UPDATE_ROUNDTRIP {name}', flush=True)


def isolated_environment(work: Path) -> dict[str, str]:
    """Only OS essentials survive; user, profile and temp locations stay inside the work tree."""
    home, temporary = work / 'home', work / 'tmp'
    home.mkdir()
    temporary.mkdir()
    env = {key: value for key, value in os.environ.items()
           if key.upper() in {'PATH', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT', 'LANG', 'LC_ALL'}}
    env.update(HOME=str(home), USERPROFILE=str(home), LOCALAPPDATA=str(home / 'AppData/Local'),
               APPDATA=str(home / 'AppData/Roaming'), XDG_DATA_HOME=str(home / '.local/share'),
               TMPDIR=str(temporary), TEMP=str(temporary), TMP=str(temporary))
    return env


class Updater:
    def __init__(self, work: Path, application: Path, env: dict[str, str]):
        self.work, self.application, self.env = work, application, env

    def command(self, entry: Path, *args: str) -> list[str]:
        if not entry.resolve().is_relative_to(self.work):
            raise AssertionError(f'Updater outside the work tree: {entry}')
        return [str(entry), *args, '--application', str(self.application)]

    def run(self, entry: Path, *args: str) -> subprocess.CompletedProcess:
        # The work root as cwd keeps every application directory free to be renamed on Windows.
        return subprocess.run(self.command(entry, *args), cwd=self.work, env=self.env, text=True,
                              capture_output=True, timeout=TIMEOUT)

    def ok(self, entry: Path, *args: str) -> dict:
        result = self.run(entry, *args)
        if result.returncode:
            raise AssertionError(f'{args[0]} failed ({result.returncode}): {result.stderr.strip()}')
        return json.loads(result.stdout.strip().splitlines()[-1])


def link_or_copy(source: str, destination: str) -> None:
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)


def leftovers(application: Path) -> list[str]:
    return sorted(path.name for path in application.parent.glob('.astra-update-*'))


def installed_interpreter(application: Path, runtime: Path, env: dict[str, str]) -> tuple[list[str], dict[str, str]]:
    """The command the bundled wrapper execs, so a kill reaches the updater process itself.

    Killing cmd.exe would leave its python.exe child holding files in the
    staging tree on Windows; the wrappers themselves run in every other step.
    """
    backend = application / runtime / 'backend'
    marker = json.loads((backend / 'desktop-installation.json').read_text(encoding='utf-8'))
    python = backend / marker['python']
    return ([str(python), '-P', '-m', 'agent.launcher.desktop_update'],
            {**env, 'PYTHONPATH': str(backend), 'PYTHONNOUSERSITE': '1', 'PYTHONDONTWRITEBYTECODE': '1'})


def round_trip(release: Path, work: Path) -> dict:
    manifest = json.loads((release / 'release.json').read_text(encoding='utf-8'))
    name, target = manifest['application'], manifest['target']
    digest = sha256(release / 'release.json')
    published = (release / 'release.sha256').read_text(encoding='utf-8').split()[0]
    if published != digest:
        raise AssertionError('release.sha256 does not match release.json')
    verify_tree(release / name, manifest, target=target)
    runtime = (resources_for(Path(name)) / 'runtime').relative_to(Path(name))
    phase('install')
    application = work / 'install' / name
    application.parent.mkdir()
    shutil.copytree(release / name, application, symlinks=True)
    verify_tree(application, manifest, target=target)
    control = application.parent / f'.{name}.astra-updater'
    installed = application / runtime / UPDATER[target]
    updater = Updater(work, application, isolated_environment(work))
    stage = ['stage', '--target', target, '--manifest-sha256', digest]

    phase('tampered-candidate-refused')
    tampered = work / 'tampered'
    # Hard links keep this copy cheap; the altered file is replaced, never written through.
    shutil.copytree(release, tampered, symlinks=True, copy_function=link_or_copy)
    marker = work / 'tampered-candidate-ran.txt'
    wrapper = tampered / name / runtime / UPDATER[target]
    lines, mode = wrapper.read_text(encoding='utf-8').splitlines(), wrapper.stat().st_mode
    record = f'echo tampered>>"{marker}"' if os.name == 'nt' else f"echo tampered >> '{marker}'"
    wrapper.unlink()
    wrapper.write_text('\n'.join([lines[0], record, *lines[1:]]) + '\n', encoding='utf-8')
    wrapper.chmod(mode)
    refused = updater.run(installed, *stage, '--candidate', str(tampered))
    if refused.returncode != 1 or 'hash mismatch' not in refused.stderr:
        raise AssertionError(f'Tampered candidate was not refused: {refused.returncode} {refused.stderr.strip()}')
    if marker.exists() or leftovers(application) or (control / 'desktop-update.json').exists():
        raise AssertionError('A refused candidate ran or left update state behind.')
    shutil.rmtree(tampered)

    phase('interrupted-stage-discarded')
    interpreter, env = installed_interpreter(application, runtime, updater.env)
    process = subprocess.Popen([*interpreter, *updater.command(installed, *stage, '--candidate', str(release))[1:]],
                               cwd=work, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + TIMEOUT
    while process.poll() is None and not any(item.endswith('-staged') for item in leftovers(application)):
        if time.monotonic() > deadline:
            process.kill()
            raise AssertionError('stage did not start copying')
        time.sleep(0.01)
    process.kill()  # A no-op if stage already finished; then the journal says "staged".
    process.wait(timeout=60)
    interrupted = json.loads((control / 'desktop-update.json').read_text(encoding='utf-8'))['phase']
    discarded = updater.ok(installed, 'recover')
    if discarded['phase'] != 'discarded' or leftovers(application) or (control / 'desktop-update.json').exists():
        raise AssertionError(f'Interrupted stage was not discarded: {discarded}')
    verify_tree(application, manifest, target=target)

    phase('stage')
    staged = updater.ok(installed, *stage, '--candidate', str(release))
    copy = Path(staged['updater'])
    if copy.resolve().is_relative_to(application) or copy.resolve().is_relative_to(release.resolve()):
        raise AssertionError(f'Printed updater is not outside both applications: {copy}')
    phase('apply')
    applied = updater.ok(copy, 'apply')
    verify_tree(application, manifest, target=target)
    if not applied.get('rollback_available') or not any(item.endswith('-previous') for item in leftovers(application)):
        raise AssertionError(f'Apply kept no previous application: {applied}')
    phase('finalize')
    finalized = updater.ok(copy, 'finalize')
    if finalized['phase'] != 'finalized' or leftovers(application) or (control / 'desktop-update.json').exists():
        raise AssertionError(f'Finalize left update state behind: {finalized}')
    verify_tree(application, manifest, target=target)
    verify_tree(release / name, manifest, target=target)
    phase('passed')
    return {'ok': True, 'target': target, 'version': manifest['version'], 'interrupted_phase': interrupted,
            'applied': applied['phase'], 'finalized': finalized['phase']}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('release', type=Path, help='package_desktop.py output directory (only read)')
    parser.add_argument('--work', type=Path, help='new directory for the round trip (default: a temp directory)')
    parser.add_argument('--keep', action='store_true', help='keep the work directory for diagnosis')
    options = parser.parse_args()
    release = options.release.resolve(strict=True)
    if options.work:
        work = options.work.resolve()
        work.mkdir(parents=True)
    else:
        work = Path(tempfile.mkdtemp(prefix='astra-update-roundtrip-')).resolve()
    if work.is_relative_to(release) or release.is_relative_to(work):
        parser.error('The work directory must be separate from the release.')
    try:
        print(json.dumps(round_trip(release, work), indent=2))
        return 0
    except (AssertionError, LauncherError, OSError, subprocess.SubprocessError, ValueError, KeyError) as exc:
        print(f'Update round trip failed: {exc}', file=sys.stderr)
        return 1
    finally:
        if not options.keep:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == '__main__':
    raise SystemExit(main())
