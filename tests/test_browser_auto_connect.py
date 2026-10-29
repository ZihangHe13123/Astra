import asyncio
import json
import os
import sys

import pytest

from agent.runtime.browser_backend_router import BrowserBackendRouter
from agent.runtime.browser_session import BackendCapabilities


class Transport:
    def __init__(self):
        self.connected=False; self.starts=0; self.waits=0; self.failure=None
    @property
    def ready(self): return self.connected
    async def start(self):
        self.starts+=1
        if self.failure: raise self.failure
    async def wait_connected(self,timeout=45):
        self.waits+=1
        await asyncio.sleep(.01)
        self.connected=True


class Backend:
    structured_snapshots=True
    capabilities=BackendCapabilities(True,True,True)
    def __init__(self,name): self.name=name; self.transport=Transport(); self.writes=0
    async def interactive_state(self,**kw): return 'https://example.com',self.name,'{}'
    async def interactive_navigate(self,url,**kw): self.writes+=1; return '{}'
    async def close_connection(self,tab_id=''): pass
    async def status(self): return True,self.name


def test_auto_open_awaits_readiness_without_attach_and_keeps_old_binding():
    async def scenario():
        primary,ext=Backend('cdp'),Backend('extension');launches=[]
        async def launch(): launches.append('edge')
        router=BrowserBackendRouter(primary,lambda:ext,auto_connect=True,launch_browser=launch)
        await router.interactive_state(tab_id='old')
        await router.startup()
        assert ext.transport.starts==0 and launches==[]
        await asyncio.gather(router.prepare_open(),router.prepare_open())
        assert launches==['edge'] and ext.transport.waits==1
        assert (await router.interactive_state(tab_id='old'))[1]=='cdp'
        assert (await router.interactive_state(tab_id='new'))[1]=='extension'
        assert primary.writes==ext.writes==0
    asyncio.run(scenario())


def test_idle_startup_does_not_claim_browser_and_task_never_falls_back():
    async def scenario():
        primary,ext=Backend('cdp'),Backend('extension')
        ext.transport.failure=RuntimeError('endpoint already owned')
        router=BrowserBackendRouter(primary,lambda:ext,auto_connect=True)
        await router.startup()
        available,message=await router.status()
        assert not available and ext.transport.starts == 0
        with pytest.raises(RuntimeError,match='owned'): await router.prepare_open()
        available,message=await router.status()
        assert not available and 'owned' in message
        assert primary.writes==ext.writes==0
    asyncio.run(scenario())


def test_two_idle_backends_leave_endpoint_available_for_browser_task(tmp_path):
    from agent.runtime.extension_browser_backend import ExtensionBrowserBackend

    async def scenario():
        endpoint = tmp_path / 'endpoint'
        first = BrowserBackendRouter(Backend('cdp'), lambda: ExtensionBrowserBackend(endpoint_dir=endpoint), auto_connect=True)
        second = BrowserBackendRouter(Backend('cdp'), lambda: ExtensionBrowserBackend(endpoint_dir=endpoint), auto_connect=True)
        await first.startup()
        await first.status()
        await second.startup()
        await second.status()
        assert not (endpoint / 'endpoint.json').exists()
        try:
            await second._extension().transport.start()
            assert (endpoint / 'endpoint.json').exists()
        finally:
            await first.close_connection()
            await second.close_connection()
    asyncio.run(scenario())


def test_auto_wait_cancel_does_not_navigate_or_change_default():
    async def scenario():
        primary,ext=Backend('cdp'),Backend('extension')
        entered=asyncio.Event()
        async def wait(**kw): entered.set();await asyncio.Event().wait()
        ext.transport.wait_connected=wait
        router=BrowserBackendRouter(primary,lambda:ext,auto_connect=True)
        job=asyncio.create_task(router.prepare_open());await entered.wait();job.cancel()
        with pytest.raises(asyncio.CancelledError): await job
        assert router.default is primary and not router.bindings
        assert primary.writes==ext.writes==0
    asyncio.run(scenario())


@pytest.mark.skipif(os.name != 'posix', reason='macOS native host installation requires POSIX ownership')
def test_automatic_config_requires_matching_installed_host(tmp_path,monkeypatch, stable_browser_checkout):
    from scripts.install_browser_control_host import install
    from agent.runtime.browser_autoconnect import auto_browser_options
    monkeypatch.setattr(sys,'platform','darwin')
    repo=stable_browser_checkout
    assert auto_browser_options(home=tmp_path,repo=repo,environ={})['auto_connect'] is False
    paths=install(extension_id='a'*32,browser='edge',home=tmp_path,repo=repo)
    config=auto_browser_options(home=tmp_path,repo=repo,environ={})
    assert config['auto_connect'] and callable(config['launch_browser'])
    assert auto_browser_options(home=tmp_path,repo=repo,environ={'ASTRA_BROWSER_TRANSPORT':'cdp'})['auto_connect'] is False
    assert auto_browser_options(home=tmp_path,repo=tmp_path,environ={})['auto_connect'] is False
    # A replaced/wildcard registration must not silently activate automatic mode.
    data=json.loads(paths['manifest'].read_text());data['allowed_origins']=['*']
    paths['manifest'].write_text(json.dumps(data))
    config=auto_browser_options(home=tmp_path,repo=repo,environ={'ASTRA_BROWSER_TRANSPORT':'auto'})
    assert config['auto_connect'] and config['setup_error']


def test_explicit_auto_missing_install_reports_setup(monkeypatch,tmp_path):
    from agent.runtime.browser_autoconnect import auto_browser_options
    monkeypatch.setattr(sys,'platform','darwin')
    options=auto_browser_options(home=tmp_path,environ={'ASTRA_BROWSER_TRANSPORT':'auto'})
    assert options['auto_connect'] and 'install' in options['setup_error'].lower()


def test_browser_open_prepares_auto_transport_before_creating_tab(tmp_path):
    from agent.runtime.browser_session import BrowserSessionManager
    from agent.runtime.tools.browser import register_browser_tools
    from agent.runtime.tools.registry import ToolRegistry
    async def scenario():
        primary,ext=Backend('cdp'),Backend('extension')
        router=BrowserBackendRouter(primary,lambda:ext,auto_connect=True)
        registry=ToolRegistry();manager=BrowserSessionManager(path=tmp_path/'browser.db')
        register_browser_tools(registry,manager=manager,backend=router)
        result=await registry.execute('browser_open',{'url':'https://example.com','extract':False})
        assert 'live browser' in result['output']
        assert ext.writes==1 and primary.writes==0
        assert ext.transport.waits==1
    asyncio.run(scenario())


def test_failed_auto_open_leaves_no_phantom_tab(tmp_path):
    from agent.runtime.browser_session import BrowserSessionManager
    from agent.runtime.tools.browser import register_browser_tools
    from agent.runtime.tools.registry import ToolRegistry
    async def scenario():
        primary,ext=Backend('cdp'),Backend('extension')
        router=BrowserBackendRouter(primary,lambda:ext,auto_connect=True,setup_error='Install extension first')
        registry=ToolRegistry();manager=BrowserSessionManager(path=tmp_path/'browser.db')
        register_browser_tools(registry,manager=manager,backend=router)
        result=await registry.execute('browser_open',{'url':'https://example.com','extract':False})
        assert 'Install extension first' in result['output']
        assert manager.list_sessions()==[]
        assert ext.writes==primary.writes==0
    asyncio.run(scenario())


def test_close_during_launch_does_not_restart_listener():
    async def scenario():
        primary,ext=Backend('cdp'),Backend('extension')
        entered=asyncio.Event();finish=asyncio.Event()
        async def launch(): entered.set();await finish.wait()
        router=BrowserBackendRouter(primary,lambda:ext,auto_connect=True,launch_browser=launch)
        job=asyncio.create_task(router.prepare_open());await entered.wait()
        await router.close_connection();finish.set()
        with pytest.raises(ConnectionError,match='closed'): await job
        assert ext.transport.waits==0
    asyncio.run(scenario())
