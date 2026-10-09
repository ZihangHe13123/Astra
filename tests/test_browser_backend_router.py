import asyncio
import pytest
from agent.runtime.browser_session import BackendCapabilities
from agent.runtime.browser_backend_router import BrowserBackendRouter
from agent.runtime.browser_control_transport import BrowserControlTransport
from agent.runtime.extension_browser_backend import ExtensionBrowserBackend


@pytest.fixture(autouse=True)
def short_handover_wait(monkeypatch):
    # A real owner here has no lifecycle to hand over, so keep the contender's wait short.
    monkeypatch.setattr(BrowserBackendRouter, 'handover_wait', 0.3)


def test_busy_owner_is_asked_to_hand_over_and_named_when_it_does_not(tmp_path):
    import json
    import os
    from agent.runtime import browser_control_transport as transport_module

    async def scenario():
        transport_module.set_owner_label_provider(lambda: '做PPT')
        first = BrowserControlTransport(tmp_path / 'endpoint')
        await first.start()
        transport_module.set_owner_label_provider(lambda: '跑测试')
        router = BrowserBackendRouter(Backend('cdp'), lambda: ExtensionBrowserBackend(endpoint_dir=tmp_path / 'endpoint'),
                                      auto_connect=True)
        request_path = tmp_path / 'endpoint' / transport_module.RELEASE_REQUEST
        filed = []

        async def watch():
            while not request_path.exists():
                await asyncio.sleep(0.02)
            filed.append(json.loads(request_path.read_text()))

        watcher = asyncio.create_task(watch())
        with pytest.raises(transport_module.BrowserEndpointOwnedError) as failure:
            await router.prepare_open()
        await asyncio.wait_for(watcher, 1)
        # The request named the window that asked; it was withdrawn when that window gave up.
        assert filed[0]['pid'] == os.getpid() and filed[0]['label'] == '跑测试'
        assert not request_path.exists()
        assert failure.value.waited and failure.value.owner_label == '做PPT'
        assert '"做PPT"' in str(failure.value) and 'hands over by itself' in str(failure.value)
        await router.close_connection()
        await first.close()
        assert not (tmp_path / 'endpoint' / transport_module.OWNER_INFO).exists()
    try:
        asyncio.run(scenario())
    finally:
        transport_module.set_owner_label_provider(None)


def test_idle_owner_releasing_on_request_lets_the_waiting_window_in(tmp_path):
    from agent.runtime.browser_control_transport import RELEASE_REQUEST

    async def scenario():
        first = BrowserControlTransport(tmp_path / 'endpoint')
        await first.start()
        ext = ExtensionBrowserBackend(endpoint_dir=tmp_path / 'endpoint')
        router = BrowserBackendRouter(Backend('cdp'), lambda: ext, auto_connect=True)
        router.handover_wait = 5

        async def idle_owner():
            while not (tmp_path / 'endpoint' / RELEASE_REQUEST).exists():
                await asyncio.sleep(0.02)
            await first.close()
            first.clear_release_request()

        async def contender():
            await router._start_or_request_handover(ext.transport)
            return ext.transport.endpoint_availability()['state']
        _, state = await asyncio.wait_for(asyncio.gather(idle_owner(), contender()), 5)
        assert state in {'owned_here', 'ready'}
        await router.close_connection()
    asyncio.run(scenario())


def test_status_reports_live_ownership_without_connecting_and_recovers_when_released(tmp_path):
    async def scenario():
        first = BrowserControlTransport(tmp_path / 'endpoint')
        await first.start()
        ext = ExtensionBrowserBackend(endpoint_dir=tmp_path / 'endpoint')
        router = BrowserBackendRouter(Backend('cdp'), lambda: ext, auto_connect=True)
        before = first.descriptor_path.read_bytes()
        ok, detail = await router.status()
        assert not ok and 'browser_endpoint_owned' in detail
        assert 'computer_apps' in detail
        assert ext.transport._server is None
        assert first.descriptor_path.read_bytes() == before
        with pytest.raises(RuntimeError, match='owned'):
            await router.list_tabs()
        await first.close()
        ok, detail = await router.status()
        assert not ok and 'auto-connect: idle' in detail
        assert ext.transport._server is None
        await router.close_connection()
    asyncio.run(scenario())


def test_registered_browser_tools_return_consistent_ownership_signals(tmp_path):
    from agent.runtime.browser_session import BrowserSessionManager
    from agent.runtime.tools.browser import register_browser_tools
    from agent.runtime.tools.registry import ToolRegistry

    async def scenario():
        first = BrowserControlTransport(tmp_path / 'endpoint')
        await first.start()
        router = BrowserBackendRouter(Backend('cdp'), lambda: ExtensionBrowserBackend(endpoint_dir=tmp_path / 'endpoint'), auto_connect=True)
        registry, manager = ToolRegistry(), BrowserSessionManager(path=tmp_path / 'browser.db')
        register_browser_tools(registry, manager=manager, backend=router)
        state = await registry.execute('browser_status', {})
        assert 'browser_endpoint_owned' in state['output'] and 'Backend: unavailable' in state['output']
        for tool, args in [('browser_tabs', {'transport': 'extension'}), ('browser_connect', {'transport': 'extension'}), ('browser_open', {'url': 'https://example.com', 'extract': False})]:
            result = await registry.execute(tool, args)
            assert result['code'] == 'browser_endpoint_owned'
            assert result['details']['dispatch_state'] == 'not_dispatched'
            assert 'computer_apps' in result['recovery_hint']
            assert 'browser_snapshot/browser_read' not in result['recovery_hint']
        # A selected CDP connection reports its own live status.
        await router.connect_existing(transport='cdp', tab_id='existing-cdp')
        assert await router.status() == (True, 'cdp')
        await router.close_connection()
        await first.close()
    asyncio.run(scenario())


class Backend:
    capabilities=BackendCapabilities(True,True,True)
    def __init__(self,name): self.name=name; self.closed=[]
    async def status(self): return True,self.name
    async def interactive_state(self,**kw): return 'https://example.com',self.name,self.name
    async def connect_existing(self,**kw): return 'Connected '+self.name
    async def close_connection(self,tab_id=''): self.closed.append(tab_id)
    async def list_tabs(self): return {'tabs':[{'id':1,'title':self.name}]}


def test_switch_does_not_retarget_existing_tabs():
    async def scenario():
        primary,ext=Backend('cdp'),Backend('extension')
        router=BrowserBackendRouter(primary,lambda:ext)
        assert (await router.interactive_state(tab_id='old'))[1]=='cdp'
        await router.connect_existing(transport='extension',tab_id='new')
        assert (await router.interactive_state(tab_id='new'))[1]=='extension'
        assert (await router.interactive_state(tab_id='old'))[1]=='cdp'
        await router.close_connection('old')
        assert primary.closed==['old'] and ext.closed==[]
        await router.close_connection()
        assert ext.closed==['']
    asyncio.run(scenario())


def test_list_does_not_change_default_transport():
    async def scenario():
        router=BrowserBackendRouter(Backend('cdp'),lambda:Backend('extension'))
        assert (await router.list_tabs(transport='extension'))['tabs'][0]['title']=='extension'
        assert (await router.interactive_state(tab_id='new'))[1]=='cdp'
        with pytest.raises(ValueError): await router.connect_existing(transport='garbage')
    asyncio.run(scenario())


def test_snapshot_text_options_follow_each_bound_transport():
    async def scenario():
        from unittest.mock import AsyncMock
        primary, ext = Backend('cdp'), Backend('extension')
        primary.interactive_snapshot = AsyncMock(return_value='cdp snapshot')
        ext.interactive_snapshot = AsyncMock(return_value='extension snapshot')
        router = BrowserBackendRouter(primary, lambda:ext)
        await router.interactive_state(tab_id='old')
        await router.connect_existing(transport='extension',tab_id='new')
        assert await router.interactive_snapshot(tab_id='new',include_text=False) == 'extension snapshot'
        assert await router.interactive_snapshot(tab_id='old',include_text=True) == 'cdp snapshot'
        ext.interactive_snapshot.assert_awaited_once_with(tab_id='new',include_text=False)
        primary.interactive_snapshot.assert_awaited_once_with(tab_id='old',include_text=True)
    asyncio.run(scenario())


def test_cdp_never_stands_in_for_a_named_extension_tab():
    async def scenario():
        primary,ext=Backend('cdp'),Backend('extension')
        connected=[]
        async def connect(**kw): connected.append(kw);return 'Connected cdp'
        primary.connect_existing=connect
        router=BrowserBackendRouter(primary,lambda:ext)
        with pytest.raises(ValueError,match='transport=extension'):
            await router.connect_existing(transport='cdp',tab_id='new',target_tab_id='7')
        # Not attached to some other page, and the logical tab is left unbound.
        assert connected==[] and 'new' not in router.bindings
        await router.connect_existing(transport='cdp',tab_id='new')
        assert connected==[{'tab_id':'new','port':0,'host':''}]
    asyncio.run(scenario())


def test_close_outcome_comes_back_from_the_bound_transport():
    async def scenario():
        primary,ext=Backend('cdp'),Backend('extension')
        async def close(tab_id=''): return 'detached'
        ext.close_connection=close
        router=BrowserBackendRouter(primary,lambda:ext)
        await router.interactive_state(tab_id='managed')
        await router.connect_existing(transport='extension',tab_id='granted')
        assert await router.close_connection('granted')=='detached'
        assert await router.close_connection('managed') is None and primary.closed==['managed']
    asyncio.run(scenario())
