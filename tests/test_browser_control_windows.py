"""Native Windows acceptance for storage, registry and the generated launcher."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

import pytest

from agent.runtime import browser_control_install as installer
from agent.runtime import browser_control_registry as registry
from agent.runtime.browser_control_storage import check_directory, open_private, private_directory, read_private
from agent.runtime.browser_control_transport import BrowserControlTransport, encode_frame, read_frame

pytestmark = pytest.mark.skipif(os.name != 'nt', reason='native Windows APIs and command launcher')
REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def isolated_registry(monkeypatch):
    # Real winreg APIs, but never the user's Edge/Chrome host registration.
    names = {browser: f'Software\\AstraBrowserControlTests\\{uuid.uuid4().hex}\\{browser}'
             for browser in ('edge', 'chrome')}
    monkeypatch.setattr(registry, 'KEYS', names)
    # These are deliberate native API tests in disposable non-browser keys.
    monkeypatch.delenv('CODEX_WINDOWS_SANDBOX_PACKAGE_FAMILY', raising=False)
    yield
    import winreg
    for name in names.values():
        for path in (name, name.rsplit('\\', 1)[0]):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
            except FileNotFoundError:
                pass


def test_isolated_install_refuses_before_writing_files_or_registry(tmp_path, isolated_registry, monkeypatch):
    monkeypatch.setenv('CODEX_WINDOWS_SANDBOX_PACKAGE_FAMILY', 'test-isolated-family')
    with pytest.raises(RuntimeError, match='standalone CMD'):
        installer.install(extension_id='a' * 32, home=tmp_path, repo=REPO)
    assert not list(tmp_path.iterdir())
    assert not registry.entries('edge')
    assert installer.status(home=tmp_path, repo=REPO)['state'] == 'unsupported'


def test_repair_of_absent_install_explains_first_install(tmp_path, isolated_registry):
    with pytest.raises(FileNotFoundError, match='install .*--extension-id'):
        installer.repair(home=tmp_path, repo=REPO)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('browser', ['edge', 'chrome'])
def test_windows_install_repair_remove_are_owned_and_idempotent(tmp_path, isolated_registry, browser):
    kwargs = dict(browser=browser, home=tmp_path, repo=REPO, python=Path(sys.executable))
    assert installer.status(browser=browser, home=tmp_path, repo=REPO)['state'] == 'not_configured'
    assert not list(tmp_path.iterdir())
    paths = installer.install(extension_id='a' * 32, **kwargs)
    assert installer.install(extension_id='a' * 32, **kwargs) == paths
    assert registry.matches(browser, paths['manifest'])
    assert installer.status(browser=browser, home=tmp_path, repo=REPO)['state'] == 'configured'
    with pytest.raises(FileExistsError):
        installer.install(extension_id='b' * 32, **kwargs)
    paths['launcher'].unlink()
    assert installer.status(browser=browser, home=tmp_path, repo=REPO)['state'] == 'invalid'
    installer.repair(**kwargs)
    assert paths['launcher'].is_file()
    installer.uninstall(browser=browser, home=tmp_path)
    installer.uninstall(browser=browser, home=tmp_path)
    assert registry.entries(browser) == []
    assert not any(path.exists() for path in paths.values())


def test_foreign_registration_and_changed_files_are_preserved(tmp_path, isolated_registry):
    registry.publish('edge', tmp_path / 'foreign.json')
    with pytest.raises(FileExistsError):
        installer.install(extension_id='a' * 32, home=tmp_path, repo=REPO)
    assert not list(tmp_path.iterdir())
    registry.remove('edge', tmp_path / 'foreign.json')
    paths = installer.install(extension_id='a' * 32, home=tmp_path, repo=REPO)
    paths['launcher'].write_bytes(b'foreign content')
    with pytest.raises(PermissionError):
        installer.uninstall(home=tmp_path)
    with pytest.raises(PermissionError):
        installer.repair(home=tmp_path, repo=REPO)
    assert paths['launcher'].read_bytes() == b'foreign content'
    registry.remove('edge', paths['manifest'])


def test_failed_publish_rolls_back_files(tmp_path, isolated_registry, monkeypatch):
    def fail(*args):
        raise OSError('injected registry failure')
    monkeypatch.setattr(registry, 'publish', fail)
    with pytest.raises(OSError, match='injected'):
        installer.install(extension_id='a' * 32, home=tmp_path, repo=REPO)
    assert not any(path.exists() for path in installer._paths('edge', tmp_path).values())


def test_repair_rolls_back_partial_replacement(tmp_path, isolated_registry, monkeypatch):
    paths = installer.install(extension_id='a' * 32, home=tmp_path, repo=REPO)
    before = {key: path.read_bytes() for key, path in paths.items()}
    write = installer._write
    failed = False
    def fail_once(path, data, **kwargs):
        nonlocal failed
        if path == paths['record'] and not failed:
            failed = True
            raise OSError('injected repair failure')
        return write(path, data, **kwargs)
    monkeypatch.setattr(installer, '_write', fail_once)
    with pytest.raises(OSError, match='injected'):
        installer.repair(home=tmp_path, repo=REPO)
    assert {key: path.read_bytes() for key, path in paths.items()} == before
    assert registry.matches('edge', paths['manifest'])
    installer.uninstall(home=tmp_path)


def test_uninstall_failure_restores_files_and_registration(tmp_path, isolated_registry, monkeypatch):
    paths = installer.install(extension_id='a' * 32, home=tmp_path, repo=REPO)
    before = {key: path.read_bytes() for key, path in paths.items()}
    unlink = Path.unlink
    failed = False
    def fail_once(path, *args, **kwargs):
        nonlocal failed
        if path == paths['launcher'] and not failed:
            failed = True
            raise PermissionError('injected uninstall failure')
        return unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', fail_once)
    with pytest.raises(PermissionError, match='injected'):
        installer.uninstall(home=tmp_path)
    assert {key: path.read_bytes() for key, path in paths.items()} == before
    assert registry.matches('edge', paths['manifest'])
    installer.uninstall(home=tmp_path)


def test_foreign_registry_named_value_is_not_treated_as_absent(tmp_path, isolated_registry):
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, registry.KEYS['edge']) as key:
        winreg.SetValueEx(key, 'foreign', 0, winreg.REG_SZ, 'preserve')
    assert installer.status(home=tmp_path, repo=REPO)['state'] == 'invalid'
    with pytest.raises(ValueError, match='default manifest'):
        installer.install(extension_id='a' * 32, home=tmp_path, repo=REPO)
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, registry.KEYS['edge']) as key:
        assert winreg.QueryValueEx(key, 'foreign')[0] == 'preserve'
    assert not list(tmp_path.iterdir())


def test_missing_registry_can_be_repaired_without_rebinding_other_browser(tmp_path, isolated_registry):
    paths = installer.install(extension_id='a' * 32, home=tmp_path, repo=REPO)
    registry.remove('edge', paths['manifest'])
    assert installer.status(home=tmp_path, repo=REPO)['state'] == 'invalid'
    installer.repair(home=tmp_path, repo=REPO)
    assert registry.matches('edge', paths['manifest'])
    assert not registry.entries('chrome')
    installer.uninstall(home=tmp_path)


def test_private_acl_and_links_fail_closed(tmp_path):
    unsafe = tmp_path / 'unsafe'
    unsafe.mkdir()
    with pytest.raises(PermissionError):
        check_directory(unsafe)
    private = tmp_path / 'private'
    private_directory(private)
    check_directory(private)
    fd = open_private(private / 'secret', os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    os.write(fd, b'private')
    os.close(fd)
    assert read_private(private / 'secret') == b'private'
    os.link(private / 'secret', private / 'link')
    with pytest.raises(PermissionError):
        read_private(private / 'secret')


def test_descriptor_read_does_not_create_directories(tmp_path):
    from agent.runtime.browser_control_host import read_descriptor
    with pytest.raises(FileNotFoundError):
        read_descriptor(tmp_path / 'absent')
    assert not list(tmp_path.iterdir())


def test_junction_parent_is_rejected_without_touching_target(tmp_path):
    target, link = tmp_path / 'target', tmp_path / 'junction'
    private_directory(target)
    command = "New-Item -ItemType Junction -Path '{}' -Target '{}' | Out-Null".format(
        str(link).replace("'", "''"), str(target).replace("'", "''"))
    subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command],
                   check=True, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        with pytest.raises(PermissionError, match='reparse'):
            private_directory(link / 'endpoint')
        with pytest.raises(PermissionError, match='reparse'):
            check_directory(link)
        assert not list(target.iterdir())
    finally:
        # rmdir removes this test-owned junction, never its target contents.
        os.rmdir(link)


def test_native_generated_launcher_binary_protocol_and_disconnect(tmp_path, isolated_registry):
    # The real checkout contains Chinese characters. The installed launcher also
    # lives in a path with spaces and shell punctuation; cwd is unrelated.
    home = tmp_path / '用户 space & !'
    paths = installer.install(extension_id='a' * 32, home=home, repo=REPO)
    local = home / 'AppData/Local'
    async def scenario():
        server = BrowserControlTransport(local / 'Astra/browser-control')
        await server.start()
        env = {**os.environ, 'LOCALAPPDATA': str(local)}
        proc = await asyncio.create_subprocess_shell(
            f'"{paths["launcher"]}"',
            cwd=tmp_path, env=env, creationflags=subprocess.CREATE_NO_WINDOW,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            assert await asyncio.wait_for(read_frame(proc.stdout), 10) == {'type': 'astra_control_ready', 'version': 1}
            proc.stdin.write(encode_frame({'id': 'astra_control_ready', 'ok': True, 'result': {'version': 1}}))
            await proc.stdin.drain()
            await server.wait_connected(timeout=2)
            for size in (10, 13, 26, 0):
                text = '中文\r\n\u001a'
                base = len(encode_frame({'id': '0' * 32, 'operation': 'read', 'args': {'text': text}})) - 4
                args = {'text': text + 'x' * ((size - base) % 256)}
                pending = asyncio.create_task(server.request('read', args=args))
                request = await asyncio.wait_for(read_frame(proc.stdout), 2)
                assert (len(encode_frame(request)) - 4) % 256 == size
                response = encode_frame({'id': request['id'], 'ok': True, 'result': request['args']})
                for chunk in (response[:1], response[1:3], response[3:]):
                    proc.stdin.write(chunk)
                    await proc.stdin.drain()
                assert await pending == request['args']
            await server.close()
            assert await asyncio.wait_for(proc.wait(), 5) == 0
            assert await proc.stderr.read() == b''
        finally:
            await server.close()
            if proc.returncode is None:
                # Kill only this test-owned process tree (cmd + its Python child).
                subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'], capture_output=True)
            await proc.communicate()
    try:
        asyncio.run(scenario())
    finally:
        installer.uninstall(home=home)


def test_acl_repair_only_tightens_verified_inactive_storage(tmp_path, isolated_registry):
    paths = installer.install(extension_id='a' * 32, home=tmp_path, repo=REPO)
    endpoint = paths['launcher'].parent.parent / 'browser-control'
    endpoint.mkdir()
    (endpoint / 'owner.lock').write_bytes(b'0')
    installer.repair(home=tmp_path, repo=REPO)
    check_directory(endpoint)
    read_private(endpoint / 'owner.lock')
    check_directory(paths['launcher'].parent)
    installer.uninstall(home=tmp_path)


def test_acl_repair_refuses_active_endpoint(tmp_path, isolated_registry):
    paths = installer.install(extension_id='a' * 32, home=tmp_path, repo=REPO)
    endpoint = paths['launcher'].parent.parent / 'browser-control'
    async def scenario():
        server = BrowserControlTransport(endpoint)
        await server.start()
        before = read_private(server.descriptor_path)
        try:
            with pytest.raises(RuntimeError, match='in use'):
                installer.repair(home=tmp_path, repo=REPO)
            assert read_private(server.descriptor_path) == before
        finally:
            await server.close()
    asyncio.run(scenario())
    installer.uninstall(home=tmp_path)


def test_handover_metadata_is_private_and_legacy_permissions_can_be_repaired(tmp_path, isolated_registry):
    from agent.runtime.browser_control_transport import OWNER_INFO, RELEASE_REQUEST
    paths = installer.install(extension_id='a' * 32, home=tmp_path, repo=REPO)
    endpoint = paths['launcher'].parent.parent / 'browser-control'

    async def scenario():
        owner, requester = BrowserControlTransport(endpoint), BrowserControlTransport(endpoint)
        await owner.start()
        try:
            assert json.loads(read_private(endpoint / OWNER_INFO))['pid'] == os.getpid()
            requester.request_release()
            assert json.loads(read_private(endpoint / RELEASE_REQUEST))['pid'] == os.getpid()
            requester.withdraw_release_request()
            assert not (endpoint / RELEASE_REQUEST).exists()
        finally:
            await owner.close()
        assert not (endpoint / OWNER_INFO).exists()

    asyncio.run(scenario())
    metadata = {OWNER_INFO: {'pid': 42, 'label': 'old window'},
                RELEASE_REQUEST: {'pid': 43, 'label': 'other window', 'at': 1.0}}
    for name, value in metadata.items():
        (endpoint / name).write_text(json.dumps(value))
    installer.repair(home=tmp_path, repo=REPO)
    for name, value in metadata.items():
        assert json.loads(read_private(endpoint / name)) == value
    installer.uninstall(home=tmp_path)


@pytest.mark.parametrize('metadata', [
    {'pid': True, 'label': 'foreign', 'at': 1},
    {'pid': 42, 'label': 'foreign', 'at': float('nan')},
    {'pid': 42, 'label': 'foreign', 'at': 10**400},
    {'pid': 42, 'label': 'foreign', 'at': 1, 'extra': True},
])
def test_endpoint_repair_preserves_unrecognized_handover_files(tmp_path, metadata):
    from agent.runtime.browser_control_repair import repair_endpoint_permissions
    endpoint = tmp_path / 'endpoint'
    endpoint.mkdir()
    (endpoint / 'owner.lock').write_bytes(b'0')
    request = endpoint / 'release.request'
    request.write_text(json.dumps(metadata))
    before = request.read_bytes()
    with pytest.raises(ValueError, match='handover'):
        repair_endpoint_permissions(endpoint)
    assert request.read_bytes() == before
    with pytest.raises(PermissionError):
        check_directory(endpoint)


def test_invalid_launcher_path_and_worktree_fail_before_mutation(tmp_path, isolated_registry):
    with pytest.raises(ValueError, match='cannot contain'):
        installer.install(extension_id='a' * 32, home=tmp_path / '%PATH%', repo=REPO)
    with pytest.raises(ValueError, match='worktree'):
        installer.install(extension_id='a' * 32, home=tmp_path, repo=tmp_path / '.worktrees/tree')
    assert not list(tmp_path.iterdir())


def test_host_acl_repair_and_foreign_file_refusal(tmp_path, isolated_registry):
    paths = installer.install(extension_id='a' * 32, home=tmp_path, repo=REPO)
    directory = paths['launcher'].parent
    subprocess.run(['icacls', str(directory), '/inheritance:e'], check=True, capture_output=True)
    with pytest.raises(PermissionError):
        check_directory(directory)
    installer.repair(home=tmp_path, repo=REPO)
    check_directory(directory)
    subprocess.run(['icacls', str(directory), '/inheritance:e'], check=True, capture_output=True)
    foreign = directory / 'foreign.txt'
    foreign.write_text('preserve')
    with pytest.raises(PermissionError, match='Foreign files'):
        installer.repair(home=tmp_path, repo=REPO)
    assert foreign.read_text() == 'preserve'
    registry.remove('edge', paths['manifest'])


def test_two_processes_cannot_own_the_same_endpoint(tmp_path):
    async def scenario():
        server = BrowserControlTransport(tmp_path / 'endpoint')
        await server.start()
        code = (
            'import asyncio,sys\n'
            'from agent.runtime.browser_control_transport import BrowserControlTransport,BrowserEndpointOwnedError\n'
            'async def probe():\n'
            ' try: await BrowserControlTransport(sys.argv[1]).start()\n'
            ' except BrowserEndpointOwnedError: print("owned_elsewhere"); return\n'
            ' raise RuntimeError("incorrectly acquired endpoint")\n'
            'asyncio.run(probe())\n'
        )
        try:
            proc = await asyncio.create_subprocess_exec(sys.executable, '-c', code, str(server.directory),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out, err = await asyncio.wait_for(proc.communicate(), 10)
            assert proc.returncode == 0, err.decode()
            assert out.strip() == b'owned_elsewhere'
        finally:
            await server.close()
    asyncio.run(scenario())
