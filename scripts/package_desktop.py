#!/usr/bin/env python3
"""Build a relocatable Electron + CPython application on its target OS.

Requires locked npm dependencies/build output and uv only on the build host.
The resulting application has no checkout, Python, Node or npm dependency.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import plistlib
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent.launcher.common import LauncherError, git, read_json, write_json  # noqa: E402
from agent.launcher.desktop_distribution import resources_for, seal_tree, sha256, verify_tree  # noqa: E402

BACKEND_RESOURCES = (
    'agent/runtime/astra.md', 'agent/ui/commands.json',
    'agent/evals/context_index_benchmark/data/quality-v1.json',
    'config/models.yaml', 'config/macos_computer_compatibility.json', 'LICENSE',
)


def copy_backend(source: Path, target: Path) -> None:
    """Positive allowlist: code plus named data files, never checkout/state trees."""
    retired = ('agent/runtime/tools/comfyui.py', 'agent/runtime/writing_mode.py')
    if any((source / name).exists() for name in retired):
        raise LauncherError('Refusing a public desktop build containing retired private runtime modules.')
    paths = set(p.relative_to(source).as_posix() for p in (source / 'agent').rglob('*.py')
                if '__pycache__' not in p.parts and not p.is_symlink())
    paths.update(name for name in BACKEND_RESOURCES if (source / name).is_file())
    for name in sorted(paths):
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / name, destination)


def source_provenance(root: Path) -> dict:
    try:
        return {'source_revision': git(root, 'rev-parse', 'HEAD'),
                'source_dirty': bool(git(root, 'status', '--porcelain', '--untracked-files=normal'))}
    except LauncherError:
        return {'source_revision': 'unknown', 'source_dirty': True}


def command(args, *, cwd=ROOT, env=None):
    subprocess.run(list(map(str, args)), cwd=cwd, env=env, check=True)


def clean_environment() -> dict:
    env = dict(os.environ, PYTHONNOUSERSITE='1', PYTHONDONTWRITEBYTECODE='1')
    for key in ('PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV', 'AGENT_PROJECT_ROOT', 'AGENT_PYTHON'):
        env.pop(key, None)
    return env


def prune_runtime_launchers(python: Path) -> None:
    """Third-party console scripts capture build paths; expose Python -m only."""
    for path in (python / 'bin').glob('*'):
        if not re.fullmatch(r'python(?:3(?:\.\d+)?)?', path.name):
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
    shutil.rmtree(python / 'Scripts', ignore_errors=True)


def copy_node_package(name: str, target: Path, copied: set[str], platform_name: str, arch: str) -> None:
    if name in copied:
        return
    source = ROOT / 'ui-gui/node_modules' / name
    manifest_path = source / 'package.json'
    if not manifest_path.is_file():
        raise LauncherError(f'Required packaged Node dependency missing: {name}')
    manifest = read_json(manifest_path)
    if manifest.get('os') and platform_name not in manifest['os']:
        return
    if manifest.get('cpu') and arch not in manifest['cpu']:
        return
    copied.add(name)
    shutil.copytree(source, target / name, symlinks=False,
                    ignore=shutil.ignore_patterns('.DS_Store', '.cache'))
    for dependency in manifest.get('dependencies', {}):
        copy_node_package(dependency, target, copied, platform_name, arch)
    for dependency in manifest.get('optionalDependencies', {}):
        if (ROOT / 'ui-gui/node_modules' / dependency / 'package.json').is_file():
            copy_node_package(dependency, target, copied, platform_name, arch)


def download_python(lock: dict, target: str, cache: Path, runtime: Path):
    entry = lock['targets'][target]
    archive = cache / f"python-{entry['sha256']}.tar.gz"
    cache.mkdir(parents=True, exist_ok=True)
    if not archive.is_file() or sha256(archive) != entry['sha256']:
        partial = archive.with_suffix('.partial')
        with urllib.request.urlopen(entry['url'], timeout=60) as response, partial.open('wb') as output:
            shutil.copyfileobj(response, output)
        if sha256(partial) != entry['sha256']:
            partial.unlink()
            raise LauncherError('Standalone Python download hash mismatch.')
        os.replace(partial, archive)
    with tarfile.open(archive) as source:
        source.extractall(runtime, filter='data')
    return runtime / entry['python']


def bundle(options) -> Path:
    lock = read_json(ROOT / 'packaging/desktop-runtime.lock.json')
    host = f"{sys.platform}-{'arm64' if platform.machine().lower() in ('arm64', 'aarch64') else 'x64'}"
    if options.target != host:
        raise LauncherError(f'Build and validate on the target OS/architecture ({host}, requested {options.target}).')
    uv_version = subprocess.check_output(['uv', '--version'], text=True).split()[1]
    if uv_version != lock['uv_version']:
        raise LauncherError(f"Desktop build requires uv {lock['uv_version']}.")
    version = tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']['version']
    provenance = source_provenance(ROOT)
    gui_manifest = read_json(ROOT / 'ui-gui/package.json')
    electron_version = gui_manifest['devDependencies']['electron']
    electron = ROOT / 'ui-gui/node_modules/electron/dist'
    if (electron / 'version').read_text().strip() != electron_version:
        raise LauncherError('Electron distribution differs from the locked GUI version.')
    if not (ROOT / 'ui-gui/dist/main.cjs').is_file():
        raise LauncherError('Build ui-gui before packaging.')
    output = options.output.resolve()
    if output.exists():
        raise LauncherError('Use an empty output path; existing releases are never overwritten.')
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.astra-package-', dir=output.parent))
    try:
        application = staging / ('Astra.app' if sys.platform == 'darwin' else 'Astra')
        if sys.platform == 'darwin':
            shutil.copytree(electron / 'Electron.app', application, symlinks=True)
            plist_path = application / 'Contents/Info.plist'
            info = plistlib.loads(plist_path.read_bytes())
            info.update(CFBundleName='Astra', CFBundleDisplayName='Astra', CFBundleIdentifier='dev.astra.desktop',
                        CFBundleShortVersionString=version, CFBundleVersion=version, CFBundleExecutable='Astra')
            plist_path.write_bytes(plistlib.dumps(info))
            (application / 'Contents/MacOS/Electron').rename(application / 'Contents/MacOS/Astra')
        else:
            shutil.copytree(electron, application)
            (application / 'electron.exe').rename(application / 'Astra.exe')
        resources = resources_for(application)
        notices = resources / 'licenses'
        notices.mkdir(exist_ok=True)
        for name in ('LICENSE', 'LICENSES.chromium.html'):
            if (electron / name).is_file():
                shutil.copy2(electron / name, notices / name)
        (resources / 'default_app.asar').unlink(missing_ok=True)
        app = resources / 'app'
        app.mkdir()
        shutil.copytree(ROOT / 'ui-gui/dist', app / 'dist')
        # Only LO remains external to the compiled main bundle. React, UI core,
        # PDF.js and every other renderer dependency are already built assets.
        if '@deepseek-ai/libreoffice-kit' in gui_manifest.get('dependencies', {}):
            copy_node_package('@deepseek-ai/libreoffice-kit', app / 'node_modules', set(), sys.platform, host.split('-')[1])
        write_json(app / 'package.json', {'name': 'astra-desktop', 'version': version, 'main': 'dist/main.cjs'})
        runtime = resources / 'runtime'
        runtime.mkdir()
        python = download_python(lock, options.target, options.cache.resolve(), runtime)
        backend = runtime / 'backend'
        copy_backend(ROOT, backend)
        requirements = runtime / 'requirements.lock.txt'
        export = ['uv', 'export', '--quiet', '--no-header', '--locked', '--no-emit-project', '--format', 'requirements-txt', '--output-file', str(requirements)]
        for extra in lock['extras']:
            export += ['--extra', extra]
        subprocess.run(export, cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
        command(['uv', 'pip', 'install', '--python', python, '--require-hashes', '--no-deps', '--only-binary', ':all:',
                 '--requirements', requirements], env=clean_environment())
        installed = json.loads(subprocess.check_output([str(python), '-I', '-c',
            'import importlib.metadata,json,sysconfig; print(json.dumps({"library":sysconfig.get_path("purelib"),'
            '"distributions":{d.metadata["Name"].lower().replace("_","-"):d.version for d in importlib.metadata.distributions()}}))'],
            cwd=runtime, env=clean_environment(), text=True))
        prune_runtime_launchers(runtime / 'python')
        marker = {'schema': 1, 'distribution': 'astra-desktop', 'version': version, 'target': options.target,
                  'python': '../' + lock['targets'][options.target]['python']}
        write_json(backend / 'desktop-installation.json', marker)
        helper = None
        if options.native_helper:
            source = options.native_helper.resolve(strict=True)
            expected = 'AstraMacComputerHelper.app' if sys.platform == 'darwin' else 'AstraWindowsComputerHelper'
            if source.name != expected:
                raise LauncherError('Unexpected native helper bundle identity.')
            native = runtime / 'native' / source.name
            shutil.copytree(source, native, symlinks=True)
            helper = 'native/' + source.name + ('/Contents/MacOS/AstraMacComputerHelper' if sys.platform == 'darwin' else '/AstraWindowsComputerHelper.exe')
            if sys.platform == 'darwin':
                command(['/usr/bin/codesign', '--verify', '--deep', '--strict', native])
                build_info = read_json(native / 'Contents/Resources/build-info.json')
                revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
                if build_info.get('helper_git_revision') != revision:
                    raise LauncherError('Native helper was not built from this revision.')
        descriptor = {'schema': 1, 'distribution': 'astra-desktop', 'version': version, 'target': options.target,
                      **provenance,
                      'python': lock['targets'][options.target]['python'], 'python_version': lock['python_version'],
                      'python_library': Path(installed['library']).relative_to(runtime).as_posix(),
                      'python_distributions': installed['distributions'],
                      'office': {'available': (app / 'node_modules/@deepseek-ai/libreoffice-kit').is_dir(),
                                 'engine': '@deepseek-ai/libreoffice-kit', 'version': '0.1.1',
                                 'surface': 'desktop-preview', 'cli_available': False},
                      'helper': helper, 'helper_policy': 'bundled' if helper else 'unavailable',
                      'locks': {name: sha256(ROOT / name) for name in
                                ['uv.lock', 'ui-gui/package-lock.json', 'ui-core/package-lock.json', 'packaging/desktop-runtime.lock.json']}}
        write_json(runtime / 'runtime.json', descriptor)
        # Runtime never writes __pycache__ in the immutable app. Clean the
        # upstream archive caches so no build-machine paths enter new bytecode.
        for cache in runtime.rglob('__pycache__'):
            shutil.rmtree(cache)
        if sys.platform == 'darwin':
            command(['/usr/bin/codesign', '--force', '--deep', '--sign', '-', application])
            command(['/usr/bin/codesign', '--verify', '--deep', '--strict', application])
        manifest = seal_tree(application, version=version, target=options.target, python=lock['python_version'],
                             electron=electron_version, helper=descriptor['helper_policy'], signing='ad-hoc' if sys.platform == 'darwin' else 'unsigned')
        manifest['application'] = application.name
        manifest.update(provenance)
        write_json(staging / 'release.json', manifest)
        verify_tree(application, manifest, target=options.target)
        (staging / 'release.sha256').write_text(sha256(staging / 'release.json') + '  release.json\n')
        # Updates run from the downloaded candidate's independent interpreter,
        # so replacing the installed directory never locks the updater itself.
        if sys.platform == 'darwin':
            updater = staging / 'astra-desktop-update'
            updater.write_text('#!/bin/sh\nset -eu\n'
                               'release_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"\n'
                               'runtime="$release_dir/Astra.app/Contents/Resources/runtime"\n'
                               'unset PYTHONHOME VIRTUAL_ENV\n'
                               'export PYTHONPATH="$runtime/backend" PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1\n'
                               'exec "$runtime/python/bin/python3" -P -m agent.launcher.desktop_update "$@"\n')
            updater.chmod(0o755)
        else:
            (staging / 'astra-desktop-update.cmd').write_text(
                '@echo off\nsetlocal\nset "PYTHONHOME="\nset "VIRTUAL_ENV="\n'
                'set "PYTHONPATH=%~dp0Astra\\resources\\runtime\\backend"\n'
                'set "PYTHONNOUSERSITE=1"\nset "PYTHONDONTWRITEBYTECODE=1"\n'
                '"%~dp0Astra\\resources\\runtime\\python\\python.exe" -P -m agent.launcher.desktop_update %*\n')
        os.replace(staging, output)
        return output / application.name
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', choices=['darwin-arm64', 'win32-x64'], required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--cache', type=Path, default=Path.home() / '.cache/astra-desktop-build')
    parser.add_argument('--native-helper', type=Path)
    args = parser.parse_args()
    try:
        print(bundle(args))
        return 0
    except (LauncherError, OSError, subprocess.CalledProcessError) as exc:
        print(f'Desktop packaging failed: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
