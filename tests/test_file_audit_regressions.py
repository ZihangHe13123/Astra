"""Observable access and POSIX metadata contracts for host file tools."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import stat

import pytest

from agent.runtime.file_checkpoints import FileCheckpointStore
from agent.runtime.tools.files import FilesystemPolicy, FilesystemRoot, register_file_tools
from agent.runtime.tools.registry import ToolRegistry


def run(coro):
    return asyncio.run(coro)


def symlink(link, target):
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")


@pytest.mark.parametrize('configured', [False, True])
@pytest.mark.parametrize('pattern', ['private compression marker', 'compresion'])
def test_search_candidate_symlink_requires_its_own_read_grant(tmp_path, configured, pattern, monkeypatch):
    workspace = tmp_path / 'workspace'
    outside = tmp_path / 'outside'
    workspace.mkdir()
    outside.mkdir()
    secret = outside / 'notes.txt'
    secret.write_text('private compression marker\n')
    symlink(workspace / 'linked.txt', secret)
    policy = FilesystemPolicy(workspace, roots=[FilesystemRoot(outside)] if configured else [])
    registry = ToolRegistry()
    register_file_tools(registry, str(workspace), policy=policy)
    opened = []
    original_open = Path.open

    def observe_open(path, *args, **kwargs):
        if path.resolve() == secret.resolve():
            opened.append(path)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', observe_open)
    result = run(registry.execute('search_text', {'pattern': pattern}))
    assert result['error'] == ''
    assert not opened, 'unauthorized bytes must not even reach exact or approximate matching'
    assert 'linked.txt' not in result['output']
    assert 'private compression marker' not in result['output'].splitlines()[1:]

    grant = policy.grant(str(secret), 'ro')
    allowed = run(registry.execute('search_text', {'pattern': pattern}))
    assert allowed['error'] == ''
    assert opened
    assert 'linked.txt' in allowed['output'] or 'notes.txt' in allowed['output']
    policy.revoke(grant)
    opened.clear()
    run(registry.execute('search_text', {'pattern': pattern}))
    assert not opened


def test_explicit_outside_search_approval_does_not_authorize_a_further_symlink(tmp_path):
    workspace, approved, private = [tmp_path / name for name in ('workspace', 'approved', 'private')]
    for path in (workspace, approved, private):
        path.mkdir()
    (approved / 'visible.txt').write_text('needle allowed\n')
    (private / 'hidden.txt').write_text('needle forbidden\n')
    symlink(approved / 'escape.txt', private / 'hidden.txt')
    registry = ToolRegistry()
    register_file_tools(registry, str(workspace))
    registry.set_approval_handler(lambda request: asyncio.sleep(0, result='once'))
    result = run(registry.execute('search_text', {'path': str(approved), 'pattern': 'needle'}))
    assert result['error'] == ''
    assert 'needle allowed' in result['output']
    assert 'needle forbidden' not in result['output']
    assert 'escape.txt' not in result['output']


@pytest.mark.skipif(os.name == 'nt', reason='POSIX permission modes')
@pytest.mark.parametrize('mode', [0o755, 0o640, 0o600])
@pytest.mark.parametrize('operation', ['edit_file', 'apply_patch', 'write_file'])
def test_atomic_edits_and_checkpoint_restore_preserve_existing_mode(tmp_path, mode, operation):
    target = tmp_path / 'run.sh'
    before = b'#!/bin/sh\necho before\n'
    target.write_bytes(before)
    target.chmod(mode)
    registry = ToolRegistry()
    registry.yolo = True
    register_file_tools(registry, str(tmp_path))
    arguments = {
        'edit_file': {'path': 'run.sh', 'old': 'before', 'new': 'after'},
        'apply_patch': {'patch': '*** Begin Patch\n*** Update File: run.sh\n@@\n-echo before\n+echo after\n*** End Patch'},
        'write_file': {'path': 'run.sh', 'content': '#!/bin/sh\necho after\n', 'overwrite': True,
                       'expected_sha256': hashlib.sha256(before).hexdigest()},
    }[operation]
    result = run(registry.execute(operation, arguments))
    assert result['error'] == ''
    assert target.read_bytes() == b'#!/bin/sh\necho after\n'
    assert stat.S_IMODE(target.stat().st_mode) == mode
    checkpoint = json.loads(result['output'])['checkpoint_id']
    restored = run(registry.execute('checkpoint_restore', {'checkpoint_id': checkpoint}))
    assert restored['error'] == ''
    assert target.read_bytes() == before
    assert stat.S_IMODE(target.stat().st_mode) == mode


@pytest.mark.skipif(os.name == 'nt', reason='POSIX permission modes')
def test_checkpoint_restore_of_deleted_file_preserves_execute_mode(tmp_path):
    target = tmp_path / 'run.sh'
    target.write_text('#!/bin/sh\n')
    target.chmod(0o750)
    store = FileCheckpointStore(tmp_path)
    pending = store.capture([target], operation='delete')
    target.unlink()
    checkpoint = store.finalize(pending)
    store.restore(checkpoint)
    assert target.read_text() == '#!/bin/sh\n'
    assert stat.S_IMODE(target.stat().st_mode) == 0o750


@pytest.mark.skipif(os.name == 'nt', reason='POSIX permission modes')
def test_checkpoint_restore_refuses_a_later_mode_change(tmp_path):
    target = tmp_path / 'run.sh'
    target.write_text('before')
    target.chmod(0o750)
    store = FileCheckpointStore(tmp_path)
    pending = store.capture([target], operation='edit')
    target.write_text('after')
    checkpoint = store.finalize(pending)
    target.chmod(0o600)
    with pytest.raises(RuntimeError, match='restore conflict'):
        store.restore(checkpoint)
    assert target.read_text() == 'after'
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == 'nt', reason='POSIX permission modes')
@pytest.mark.parametrize('deleted', [False, True])
def test_legacy_checkpoint_without_modes_restores_without_broadening_access(tmp_path, deleted):
    target = tmp_path / 'legacy.txt'
    target.write_text('before')
    target.chmod(0o640)
    store = FileCheckpointStore(tmp_path)
    pending = store.capture([target], operation='legacy')
    if deleted:
        target.unlink()
    else:
        target.write_text('after')
    checkpoint = store.finalize(pending)
    manifest_path = store.root / checkpoint / 'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    for entry in manifest['files']:
        entry.pop('before_mode', None)
        entry.pop('after_mode', None)
    manifest['version'] = 1
    manifest_path.write_text(json.dumps(manifest))
    store.restore(checkpoint)
    assert target.read_text() == 'before'
    assert stat.S_IMODE(target.stat().st_mode) == (0o600 if deleted else 0o640)


@pytest.mark.skipif(os.name == 'nt', reason='POSIX permission modes')
def test_transactional_overwrite_keeps_executable_mode(tmp_path):
    target = tmp_path / 'run.sh'
    target.write_text('before\n')
    target.chmod(0o750)
    registry = ToolRegistry()
    registry.yolo = True
    register_file_tools(registry, str(tmp_path))
    started = run(registry.execute('begin_file_write', {'path': 'run.sh', 'overwrite': True}))
    assert started['error'] == ''
    write_id = json.loads(started['output'])['write_id']
    chunk = run(registry.execute('write_file_chunk', {'write_id': write_id, 'sequence': 0, 'content': 'after\n'}))
    assert chunk['error'] == ''
    result = run(registry.execute('commit_file_write', {'write_id': write_id}))
    assert result['error'] == ''
    assert target.read_text() == 'after\n'
    assert stat.S_IMODE(target.stat().st_mode) == 0o750


@pytest.mark.skipif(os.name == 'nt', reason='POSIX permission modes')
@pytest.mark.parametrize('operation', ['edit_file', 'write_file'])
def test_atomic_edit_refuses_permission_change_during_preparation(tmp_path, monkeypatch, operation):
    target = tmp_path / 'run.sh'
    target.write_text('before')
    target.chmod(0o750)
    registry = ToolRegistry()
    register_file_tools(registry, str(tmp_path))
    original_capture = FileCheckpointStore.capture

    def changed_during_capture(store, *args, **kwargs):
        result = original_capture(store, *args, **kwargs)
        target.chmod(0o600)
        return result

    monkeypatch.setattr(FileCheckpointStore, 'capture', changed_during_capture)
    arguments = {'path': 'run.sh', 'old': 'before', 'new': 'after'} if operation == 'edit_file' else {
        'path': 'run.sh', 'content': 'after', 'overwrite': True,
        'expected_sha256': hashlib.sha256(b'before').hexdigest(),
    }
    result = run(registry.execute(operation, arguments))
    assert result['code'] == 'file_changed'
    assert target.read_text() == 'before'
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == 'nt', reason='POSIX permission modes')
@pytest.mark.parametrize('later_user_change', [False, True])
def test_checkpoint_failed_batch_preserves_permissions_and_later_user_changes(tmp_path, monkeypatch, later_user_change):
    from agent.runtime import file_checkpoints

    first, second = tmp_path / 'first.sh', tmp_path / 'second.sh'
    for target in (first, second):
        target.write_text('before')
        target.chmod(0o750)
    store = FileCheckpointStore(tmp_path)
    pending = store.capture([first, second], operation='batch')
    for target in (first, second):
        target.write_text('after')
        target.chmod(0o640)
    checkpoint = store.finalize(pending)
    replace = file_checkpoints._replace_bytes

    def fail_second(target, content, mode):
        if target == second:
            if later_user_change:
                first.write_text('user')
                first.chmod(0o600)
            raise OSError('synthetic second-file failure')
        replace(target, content, mode)

    monkeypatch.setattr(file_checkpoints, '_replace_bytes', fail_second)
    with pytest.raises(OSError, match='synthetic'):
        store.restore(checkpoint)
    assert first.read_text() == ('user' if later_user_change else 'after')
    assert stat.S_IMODE(first.stat().st_mode) == (0o600 if later_user_change else 0o640)
    assert second.read_text() == 'after'
    assert stat.S_IMODE(second.stat().st_mode) == 0o640


@pytest.mark.skipif(os.name == 'nt', reason='POSIX permission modes')
@pytest.mark.parametrize('operation', ['write_file', 'apply_patch'])
def test_new_files_keep_private_non_executable_permissions(tmp_path, operation):
    registry = ToolRegistry()
    register_file_tools(registry, str(tmp_path))
    arguments = {'path': 'new.txt', 'content': 'new'} if operation == 'write_file' else {
        'patch': '*** Begin Patch\n*** Add File: new.txt\n+new\n*** End Patch',
    }
    result = run(registry.execute(operation, arguments))
    assert result['error'] == ''
    assert stat.S_IMODE((tmp_path / 'new.txt').stat().st_mode) == 0o600
