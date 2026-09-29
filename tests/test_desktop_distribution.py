"""Standalone artifacts are relocatable, private-state-free and recoverable."""
import hashlib
import json
from pathlib import Path

import pytest

from agent.launcher import installation
from agent.launcher.common import LauncherError


def runtime(tmp_path):
    root = tmp_path / 'runtime'
    backend = root / 'backend'
    backend.mkdir(parents=True)
    (backend / 'agent').mkdir()
    (root / 'python/bin').mkdir(parents=True)
    (root / 'python/bin/python3').write_bytes(b'python')
    (root / 'python/bin/python3').chmod(0o755)
    (backend / 'desktop-installation.json').write_text(json.dumps({
        'schema': 1, 'version': '0.2.0', 'python': '../python/bin/python3',
        'target': 'darwin-arm64', 'distribution': 'astra-desktop',
    }))
    return root


def test_desktop_installation_uses_bundled_python_and_external_user_state(tmp_path, monkeypatch):
    root = runtime(tmp_path)
    monkeypatch.delenv('ASTRA_HOME', raising=False)
    monkeypatch.delenv('AGENT_SESSION_DIR', raising=False)
    monkeypatch.setattr(installation, 'user_home', lambda: tmp_path / 'user-data')
    inst = installation.discover(root / 'backend')
    assert inst.kind == 'desktop'
    assert inst.python == root / 'python/bin/python3'
    assert inst.data == tmp_path / 'user-data'
    assert inst.sessions == inst.data / 'sessions'
    assert not inst.control.is_relative_to(root)
    env = installation.runtime_environment(inst, tmp_path)
    assert env['ASTRA_ENV_FILE'] == str(inst.data / '.env')
    assert env['PYTHONNOUSERSITE'] == '1'
    assert env['PYTHONDONTWRITEBYTECODE'] == '1'


def test_manifest_verifies_modes_hashes_target_and_rejects_extra_files(tmp_path):
    from agent.launcher.desktop_distribution import seal_tree, verify_tree
    root = runtime(tmp_path)
    descriptor = seal_tree(root, version='0.2.0', target='darwin-arm64')
    verify_tree(root, descriptor, target='darwin-arm64')
    with pytest.raises(LauncherError, match='target'):
        verify_tree(root, descriptor, target='win32-x64')
    executable = root / 'python/bin/python3'
    executable.chmod(0o444)
    with pytest.raises(LauncherError, match='mode'):
        verify_tree(root, descriptor)
    executable.chmod(0o755)
    executable.write_bytes(b'corrupt')
    with pytest.raises(LauncherError, match='hash'):
        verify_tree(root, descriptor)
    executable.write_bytes(b'python')
    (root / '.env').write_text('secret')
    with pytest.raises(LauncherError, match='Unexpected'):
        verify_tree(root, descriptor)


def test_manifest_rejects_external_symlink_and_path_traversal(tmp_path):
    from agent.launcher.desktop_distribution import seal_tree, verify_tree
    root = runtime(tmp_path)
    (root / 'secret').symlink_to(tmp_path / 'private')
    with pytest.raises(LauncherError, match='symlink'):
        seal_tree(root, version='0.2.0', target='darwin-arm64')
    (root / 'secret').unlink()
    descriptor = seal_tree(root, version='0.2.0', target='darwin-arm64')
    descriptor['files']['../private'] = {'sha256': '0' * 64, 'mode': 0o644, 'size': 0}
    with pytest.raises(LauncherError, match='path'):
        verify_tree(root, descriptor)


def release(path, version):
    from agent.launcher.desktop_distribution import seal_tree
    app = path / 'Astra.app'
    root = runtime(app)
    (root / 'backend/version.txt').write_text(version)
    descriptor = seal_tree(app, version=version, target='darwin-arm64')
    descriptor['application'] = 'Astra.app'
    manifest = path / 'release.json'
    manifest.write_text(json.dumps(descriptor))
    return hashlib.sha256(manifest.read_bytes()).hexdigest()


def test_update_checks_expected_manifest_and_recovers_failed_validation(tmp_path, monkeypatch):
    from agent.launcher import desktop_update
    installed = tmp_path / 'installed/Astra.app'
    installed.mkdir(parents=True)
    (installed / 'old.txt').write_text('previous app')
    candidate = tmp_path / 'candidate'
    candidate.mkdir()
    digest = release(candidate, '0.3.0')
    control = tmp_path / 'control'
    with pytest.raises(LauncherError, match='digest'):
        desktop_update.stage(candidate, installed, control, expected_sha256='0' * 64, target='darwin-arm64')
    result = desktop_update.stage(candidate, installed, control, expected_sha256=digest, target='darwin-arm64')
    assert result['phase'] == 'staged'
    with pytest.raises(LauncherError, match='health'):
        desktop_update.apply(installed, control, validate=lambda _: (_ for _ in ()).throw(LauncherError('health failed')))
    assert (installed / 'old.txt').read_text() == 'previous app'
    assert not (control / 'desktop-update.json').exists()


def test_update_refuses_running_installation_and_retains_recovery(tmp_path, monkeypatch):
    from agent.launcher import desktop_update
    installed = tmp_path / 'installed/Astra.app'
    installed.mkdir(parents=True)
    (installed / 'old.txt').write_text('previous app')
    candidate = tmp_path / 'candidate'
    candidate.mkdir()
    digest = release(candidate, '0.3.0')
    control = tmp_path / 'control'
    desktop_update.stage(candidate, installed, control, expected_sha256=digest, target='darwin-arm64')
    monkeypatch.setattr(desktop_update, 'ensure_idle', lambda *args: (_ for _ in ()).throw(LauncherError('running')))
    with pytest.raises(LauncherError, match='running'):
        desktop_update.apply(installed, control, validate=lambda _: None)
    assert (installed / 'old.txt').is_file()
    monkeypatch.setattr(desktop_update, 'ensure_idle', lambda *args: None)
    assert desktop_update.apply(installed, control, validate=lambda _: None)['phase'] == 'applied'
    assert (installed / 'runtime/backend/version.txt').read_text() == '0.3.0'
    assert desktop_update.recover(installed, control)['phase'] == 'restored'
    assert (installed / 'old.txt').is_file()


def test_packager_allowlist_excludes_checkout_state_and_untracked_secrets(tmp_path):
    import importlib.util
    script = Path(__file__).resolve().parents[1] / 'scripts/package_desktop.py'
    spec = importlib.util.spec_from_file_location('package_desktop', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = tmp_path / 'source'
    for relative in ['agent/__init__.py', 'agent/runtime/session_recall.py', 'agent/runtime/astra.md', 'agent/ui/commands.json',
                     'agent/nu', 'agent/api_key.json', 'agent/secret.env', '.env', '.astra/settings.json',
                     '.sessions/private.json', 'config/models.yaml', 'config/macos_computer_compatibility.json',
                     'session_recall.py', 'LICENSE']:
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('placeholder')
    target = tmp_path / 'backend'
    module.copy_backend(source, target)
    paths = {p.relative_to(target).as_posix() for p in target.rglob('*') if p.is_file()}
    assert paths == {'agent/__init__.py', 'agent/runtime/session_recall.py', 'agent/runtime/astra.md', 'agent/ui/commands.json',
                     'config/models.yaml', 'config/macos_computer_compatibility.json', 'LICENSE'}


def test_update_recovery_holds_installation_lock_and_blocks_new_lease(tmp_path, monkeypatch):
    from agent.launcher.locking import RuntimeLease
    from agent.launcher.common import write_json
    app = tmp_path / 'Astra.app'
    app.mkdir()
    root = runtime(app / 'Contents/Resources')
    monkeypatch.setattr(installation, 'user_home', lambda: tmp_path / 'profile')
    install = installation.discover(root / 'backend')
    write_json(install.control / 'desktop-pending.json', {'phase': 'replacing'})
    with pytest.raises(LauncherError, match='desktop'):
        RuntimeLease(install, 'backend')


def test_interrupted_update_restores_original_after_first_rename(tmp_path):
    from agent.launcher import desktop_update
    from agent.launcher.common import read_json, write_json
    installed = tmp_path / 'installed/Astra.app'
    installed.mkdir(parents=True)
    (installed / 'old.txt').write_text('previous app')
    candidate = tmp_path / 'candidate'
    candidate.mkdir()
    digest = release(candidate, '0.3.0')
    control = tmp_path / 'control'
    desktop_update.stage(candidate, installed, control, expected_sha256=digest, target='darwin-arm64')
    state = read_json(control / 'desktop-update.json')
    state['phase'] = 'replacing'
    write_json(control / 'desktop-update.json', state)
    installed.rename(state['backup'])
    desktop_update.recover(installed, control)
    assert (installed / 'old.txt').read_text() == 'previous app'


def test_successful_desktop_upgrade_preserves_data_and_blocks_while_gui_lease_active(tmp_path, monkeypatch):
    from agent.launcher import desktop_update
    from agent.launcher.locking import RuntimeLease
    installed = tmp_path / 'installed/Astra.app'
    root = runtime(installed / 'Contents/Resources')
    monkeypatch.setattr(installation, 'user_home', lambda: tmp_path / 'profile')
    inst = installation.discover(root / 'backend')
    inst.data.mkdir(parents=True)
    (inst.data / 'preferences.json').write_text('private settings')
    candidate = tmp_path / 'candidate'
    candidate.mkdir()
    digest = release(candidate, '0.3.0')
    control = tmp_path / 'control'
    desktop_update.stage(candidate, installed, control, expected_sha256=digest, target='darwin-arm64')
    with RuntimeLease(inst, 'desktop'):
        with pytest.raises(LauncherError, match='Close these Astra instances'):
            desktop_update.apply(installed, control, validate=lambda _:None)
    desktop_update.apply(installed, control, validate=lambda _:None)
    assert (inst.data / 'preferences.json').read_text() == 'private settings'


def test_finalize_removes_only_saved_rollback_after_success(tmp_path):
    from agent.launcher import desktop_update
    installed = tmp_path / 'installed/Astra.app'
    installed.mkdir(parents=True)
    (installed / 'old.txt').write_text('previous app')
    candidate = tmp_path / 'candidate'
    candidate.mkdir()
    digest = release(candidate, '0.3.0')
    control = tmp_path / 'control'
    desktop_update.stage(candidate, installed, control, expected_sha256=digest, target='darwin-arm64')
    with pytest.raises(LauncherError, match='applied'):
        desktop_update.finalize(installed, control)
    desktop_update.apply(installed, control, validate=lambda _:None)
    assert desktop_update.finalize(installed, control)['phase'] == 'finalized'
    assert (installed / 'runtime/backend/version.txt').read_text() == '0.3.0'
    assert not list(installed.parent.glob('.astra-update-*'))


def test_workspace_dependencies_uses_manifest_and_reports_unavailable_source(tmp_path):
    from agent.runtime.tools.workspace_dependencies import workspace_dependencies
    assert workspace_dependencies(tmp_path)['status'] == 'unavailable'
    root = runtime(tmp_path)
    lib = root / 'python/lib/python3.12/site-packages'
    lib.mkdir(parents=True)
    (root / 'runtime.json').write_text(json.dumps({'schema': 1, 'distribution': 'astra-desktop',
       'version': '0.2.0', 'target': 'darwin-arm64', 'python': 'python/bin/python3', 'python_version':'3.12.14',
       'python_library': 'python/lib/python3.12/site-packages',
       'python_distributions': {'python-docx':'1.2.0', 'openpyxl':'3.1.5'},
       'office': {'available': False}}))
    result = workspace_dependencies(root / 'backend')
    assert result['status'] == 'available'
    assert result['python'] == str(root / 'python/bin/python3')
    assert result['python_distributions'] == {'python-docx':'1.2.0','openpyxl':'3.1.5'}
    assert result['office']['available'] is False
    assert not list(tmp_path.rglob('__pycache__'))


def test_desktop_supervisor_runs_packaged_executable_without_node(tmp_path, monkeypatch):
    from agent.launcher import gui, dependencies
    from types import SimpleNamespace
    monkeypatch.setattr(dependencies, "sys", SimpleNamespace(platform="darwin"))
    from contextlib import nullcontext
    app = tmp_path / 'Astra.app'
    root = runtime(app / 'Contents/Resources')
    executable = app / 'Contents/MacOS/Astra'
    executable.parent.mkdir(parents=True)
    executable.write_text('application')
    monkeypatch.setattr(installation, 'user_home', lambda: tmp_path / 'profile')
    monkeypatch.setattr(gui, 'RuntimeLease', lambda *args:nullcontext())
    monkeypatch.setattr(gui.signal, 'signal', lambda *args:None)
    calls=[]
    class Child:
        def wait(self):return 0
    monkeypatch.setattr(gui.subprocess, 'Popen', lambda args,**kwargs:calls.append(args) or Child())
    assert gui.supervise(root / 'backend', tmp_path / 'ready.json') == 0
    assert calls == [[str(executable)]]


def test_packager_removes_nonrelocatable_console_scripts(tmp_path):
    import importlib.util
    script = Path(__file__).resolve().parents[1] / 'scripts/package_desktop.py'
    spec = importlib.util.spec_from_file_location('package_desktop', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    python = tmp_path / 'python'
    (python / 'bin').mkdir(parents=True)
    for name in ['python3.12','pip','dotenv','python3.12-config']:
        (python / 'bin' / name).write_text('#!/build/machine/python\n')
    (python / 'bin/python3').symlink_to('python3.12')
    (python / 'Scripts').mkdir()
    (python / 'Scripts/pip.exe').write_bytes(b'embedded build interpreter path')
    module.prune_runtime_launchers(python)
    assert {p.name for p in (python / 'bin').iterdir()} == {'python3.12','python3'}
    assert not (python / 'Scripts').exists()


def test_recovery_refuses_to_rename_its_own_running_interpreter(tmp_path, monkeypatch, capsys):
    from agent.launcher import desktop_update
    application = tmp_path / 'Astra.app'
    monkeypatch.setattr(desktop_update.sys, 'executable', str(application / 'Contents/python'))
    monkeypatch.setattr(desktop_update.sys, 'argv', ['desktop_update','recover','--application',str(application)])
    assert desktop_update.main() == 1
    assert 'external candidate release' in capsys.readouterr().err


def test_desktop_cli_rejects_profile_inside_bundle_and_ignores_source_session_override(tmp_path, monkeypatch):
    root = runtime(tmp_path)
    monkeypatch.setenv('ASTRA_HOME', str(root / 'private-data'))
    with pytest.raises(LauncherError, match='outside'):
        installation.discover(root / 'backend')
    monkeypatch.setenv('ASTRA_HOME', str(tmp_path / 'profile'))
    monkeypatch.setenv('AGENT_SESSION_DIR', str(tmp_path / 'source/.sessions'))
    monkeypatch.setenv('AGENT_LOG_DIR', str(root / '.logs'))
    inst = installation.discover(root / 'backend')
    assert inst.sessions == tmp_path / 'profile/sessions'
    env = installation.runtime_environment(inst, tmp_path)
    assert env['AGENT_LOG_DIR'] == str(tmp_path / 'profile/logs')


def test_runtime_provenance_records_revision_and_uncommitted_source(monkeypatch):
    import importlib.util
    script = Path(__file__).resolve().parents[1] / 'scripts/package_desktop.py'
    spec = importlib.util.spec_from_file_location('package_desktop', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'git', lambda root,*args:'abcdef123456' if args[0]=='rev-parse' else ' M agent/launcher/installation.py',raising=False)
    assert module.source_provenance(Path('/source')) == {'source_revision':'abcdef123456','source_dirty':True}


@pytest.mark.parametrize('retired', ['agent/runtime/tools/comfyui.py', 'agent/runtime/writing_mode.py'])
def test_public_desktop_builder_rejects_retired_private_generator(tmp_path, retired):
    import importlib.util
    script = Path(__file__).resolve().parents[1] / 'scripts/package_desktop.py'
    spec = importlib.util.spec_from_file_location('package_desktop', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = tmp_path / 'source'
    private = source / retired
    private.parent.mkdir(parents=True)
    private.write_text('private implementation')
    target = tmp_path / 'backend'
    with pytest.raises(LauncherError, match='private'):
        module.copy_backend(source, target)
    assert not target.exists()
