"""Shared entrypoint, diagnostics and compatibility contracts."""
import asyncio
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from agent.launcher import cli
from agent.runtime import browser_autoconnect, browser_control_install as installer

ROOT = Path(__file__).resolve().parents[1]


def test_launcher_routes_without_loading_model_or_changing_cwd(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(installer, 'main', lambda args: calls.append(args) or 0)
    monkeypatch.setattr(cli, 'discover', lambda root: SimpleNamespace(root=ROOT, python=Path(sys.executable)))
    monkeypatch.chdir(tmp_path)
    assert cli.main(['browser-control', 'install', '--browser', 'edge', '--extension-id', 'a' * 32, '--json']) == 0
    assert calls == [['install', '--browser', 'edge', '--repo', str(ROOT), '--python', sys.executable,
                      '--extension-id', 'a' * 32, '--json']]
    assert not list(tmp_path.iterdir())


def test_explicit_transport_never_falls_back_and_status_is_nonmutating(tmp_path, monkeypatch):
    monkeypatch.setattr(installer, '_supported', lambda: None)
    monkeypatch.setattr(installer.registry, 'entries', lambda browser: [])
    assert installer.status(home=tmp_path)['state'] == 'not_configured'
    assert not list(tmp_path.iterdir())
    assert browser_autoconnect.auto_browser_options(home=tmp_path, environ={'ASTRA_BROWSER_TRANSPORT': 'cdp'}) == {'auto_connect': False}
    config = browser_autoconnect.auto_browser_options(home=tmp_path, environ={'ASTRA_BROWSER_TRANSPORT': 'extension'})
    assert config['auto_connect'] and config['setup_error']
    assert not list(tmp_path.iterdir())


def test_windows_discovery_uses_registration_without_launching(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, 'platform', 'win32')
    calls = []
    monkeypatch.setattr(installer, 'status', lambda **kw: {'state': 'configured' if kw['browser'] == 'edge' else 'not_configured'})
    async def launch(browser, **kwargs):
        calls.append(browser)
    monkeypatch.setattr(browser_autoconnect, 'launch_browser', launch)
    config = browser_autoconnect.auto_browser_options(home=tmp_path, environ={})
    assert config['auto_connect'] and not calls
    asyncio.run(config['launch_browser']())
    assert calls == ['edge']


@pytest.mark.skipif(os.name != 'posix', reason='native POSIX shell modes')
def test_mac_legacy_registration_is_discovered_and_adopted_on_explicit_repair(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, 'platform', 'darwin')
    paths = installer.install(extension_id='a' * 32, home=tmp_path, repo=ROOT)
    paths['record'].unlink()
    assert installer.status(home=tmp_path, repo=ROOT)['state'] == 'configured'
    assert not paths['record'].exists()
    installer.repair(home=tmp_path, repo=ROOT)
    assert json.loads(paths['record'].read_text())['extension_id'] == 'a' * 32
    installer.uninstall(home=tmp_path, repo=ROOT)


@pytest.mark.skipif(os.name != 'posix', reason='native POSIX shell modes')
def test_mac_changed_legacy_launcher_is_preserved(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, 'platform', 'darwin')
    paths = installer.install(extension_id='a' * 32, home=tmp_path, repo=ROOT)
    paths['record'].unlink()
    paths['launcher'].write_text('#!/bin/sh\nexit 0\n')
    with pytest.raises(ValueError):
        installer.repair(home=tmp_path, repo=ROOT)
    assert paths['launcher'].read_text() == '#!/bin/sh\nexit 0\n'


def test_tools_registered_is_not_reported_as_live_browser_ready():
    from agent.runtime.capabilities import CapabilityState, build_runtime_capabilities
    names = ['browser_open', 'browser_snapshot', 'browser_click', 'browser_type', 'browser_handoff', 'browser_resume']
    agent = SimpleNamespace(tools=SimpleNamespace(describe=lambda: [{'name': name} for name in names]))
    statuses = {item.name: item for item in build_runtime_capabilities(agent).statuses}
    assert statuses['browser.interactive'].state is CapabilityState.CONFIGURED
    assert 'live' in statuses['browser.interactive'].detail


def test_host_connection_is_not_extension_readiness():
    from agent.runtime.extension_browser_backend import ExtensionBrowserBackend
    transport = SimpleNamespace(connected=True, ready=False)
    backend = ExtensionBrowserBackend(transport=transport)
    available, detail = asyncio.run(backend.status())
    assert not available and 'waiting' in detail
    transport.ready = True
    assert asyncio.run(backend.status())[0]
