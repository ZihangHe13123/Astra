"""CDP isolation and uncertain-write regressions without launching a browser."""
import asyncio
import json
from unittest.mock import AsyncMock
import pytest
from agent.runtime.cdp_backend import CdpConnection, CdpBrowserBackend


def test_page_helper_runs_in_named_isolated_world():
    async def check():
        conn = CdpConnection('', page_ws_url='ws://test')
        conn.send = AsyncMock(side_effect=[
            {'frameTree': {'frame': {'id': 'main'}}},
            {'executionContextId': 17},
            {'result': {'value': None}},
            {'result': {'value': {'url':'https://example.org','title':'Test','text':'Hello','elements':[]}}},
        ])
        assert hasattr(conn, 'page_operation'), 'CDP shared helper adapter must exist'
        result = await conn.page_operation('snapshot', {})
        assert result['text'] == 'Hello'
        calls = conn.send.call_args_list
        assert calls[1].args[0] == 'Page.createIsolatedWorld'
        assert calls[1].args[1]['worldName'] == 'astra-browser-control'
        for call in calls[2:]:
            assert call.args[1]['contextId'] == 17
        assert '__astraBrowserPage' in calls[3].args[1]['expression']
    asyncio.run(check())


def test_uncertain_cdp_write_is_not_replayed():
    async def check():
        conn = CdpConnection('', page_ws_url='ws://test')
        conn.send = AsyncMock(side_effect=[{'frameTree':{'frame':{'id':'main'}}}, {'executionContextId':17}, {'result':{}}, RuntimeError('connection closed')])
        result = json.loads(await conn.click('ref:s1:1'))
        assert result['status'] == 'unknown_outcome'
        assert conn.send.await_count == 4
    asyncio.run(check())


def test_live_snapshot_keeps_structured_refs_and_does_not_navigate():
    async def check():
        backend = CdpBrowserBackend(chrome_path='/fake')
        conn = AsyncMock()
        conn.page_operation.return_value = {'url':'https://new.org','title':'New','text':'Text','elements':[{'ref':'id'}]}
        conn.is_connected = True
        backend._connections['t'] = conn
        backend.ensure_connection = AsyncMock(return_value=conn)
        url,title,text = await backend.interactive_state(tab_id='t', url='https://old.org')
        assert (url,title)==('https://new.org','New')
        assert json.loads(text)['elements'][0]['ref']=='id'
        backend.ensure_connection.assert_not_called()
        conn.navigate.assert_not_called()
    asyncio.run(check())


def test_origin_assertion_rejects_missing_connection_and_changed_origin():
    async def check():
        backend = CdpBrowserBackend(chrome_path='/fake')
        assert hasattr(backend,'assert_origin'), 'origin guard must exist'
        with pytest.raises(RuntimeError):
            await backend.assert_origin(tab_id='t',expected_url='https://example.org')
        conn = AsyncMock(); conn.is_connected = True; conn.get_url.return_value='https://other.org/'
        backend._connections['t']=conn
        with pytest.raises(RuntimeError):
            await backend.assert_origin(tab_id='t',expected_url='https://example.org')
        conn.get_url.return_value='https://example.org/path'
        await backend.assert_origin(tab_id='t',expected_url='https://example.org/')
        assert '_approved_origin' not in conn.__dict__
    asyncio.run(check())


def test_live_snapshot_does_not_recreate_lost_connection():
    async def check():
        backend = CdpBrowserBackend(chrome_path='/fake')
        backend.ensure_connection = AsyncMock()
        with pytest.raises(RuntimeError, match='connection lost'):
            await backend.interactive_state(tab_id='missing', url='https://example.org')
        backend.ensure_connection.assert_not_called()
    asyncio.run(check())


def test_cdp_compact_snapshot_keeps_target_state_and_bound_origin():
    async def check():
        backend = CdpBrowserBackend(chrome_path='/fake')
        conn = AsyncMock(); conn.is_connected = True
        snapshot = {'url':'https://example.org','text':'','textIncluded':False,
            'elements':[{'ref':'s1:0','checked':False,'indeterminate':True}]}
        conn.page_operation.return_value = snapshot
        backend._connections['t'] = conn
        backend.ensure_connection = AsyncMock()
        result = await backend.interactive_snapshot(tab_id='t',url='https://example.org/form',
            role_filter='checkbox',include_text=False)
        assert json.loads(result) == snapshot
        conn.page_operation.assert_awaited_once_with('snapshot', {
            'role_filter':'checkbox','include_text':False,'expectedOrigin':'https://example.org'})
        backend.ensure_connection.assert_not_called()
    asyncio.run(check())


def test_cdp_advertises_structured_snapshots():
    assert getattr(CdpBrowserBackend, 'structured_snapshots', False) is True


def test_concurrent_writes_bind_each_request_origin_before_awaits():
    async def check():
        backend = CdpBrowserBackend(chrome_path='/fake')
        conn = CdpConnection('', page_ws_url='ws://test')
        conn._started = True
        conn._ws = object()
        backend._connections['t'] = conn
        expressions = []
        async def send(method, params=None):
            await asyncio.sleep(0)
            # Simulate a competing approval at every transport boundary.
            conn._approved_origin = 'https://wrong.org'
            if method == 'Page.getFrameTree': return {'frameTree': {'frame': {'id': 'main'}}}
            if method == 'Page.createIsolatedWorld': return {'executionContextId': 17}
            if params['expression'].startswith('globalThis.__astraBrowserPage('):
                expressions.append(params['expression'])
                return {'result': {'value': {'status': 'observed'}}}
            return {'result': {}}
        conn.send = send
        await asyncio.gather(
            backend.interactive_click('#a',tab_id='t',url='https://a.org/path'),
            backend.interactive_type('#b','text',tab_id='t',url='https://b.org/path'),
            backend.interactive_select('#c','v',tab_id='t',url='https://c.org/path'),
        )
        for selector, origin in [('#a','https://a.org'),('#b','https://b.org'),('#c','https://c.org')]:
            expression = next(x for x in expressions if selector in x)
            assert f'"expectedOrigin": "{origin}"' in expression
        expressions.clear()
        await conn.click('#unscoped')
        assert 'wrong.org' not in expressions[0]
    asyncio.run(check())


@pytest.mark.parametrize('operation', ['wait', 'screenshot'])
def test_read_operations_use_bound_connection_without_navigation(operation):
    async def check():
        backend = CdpBrowserBackend(chrome_path='/fake')
        conn = AsyncMock(); conn.is_connected = True
        backend._connections['t'] = conn
        backend.ensure_connection = AsyncMock(return_value=conn)
        if operation == 'wait':
            await backend.interactive_wait(tab_id='t',url='https://old.org',selector='ref:s1:1')
        else:
            await backend.interactive_screenshot(tab_id='t',url='https://old.org')
        backend.ensure_connection.assert_not_called()
        backend._connections.clear()
        with pytest.raises(RuntimeError, match='connection lost'):
            if operation == 'wait': await backend.interactive_wait(tab_id='t',selector='#x')
            else: await backend.interactive_screenshot(tab_id='t')
    asyncio.run(check())


def test_cdp_wait_uses_ref_preserving_probe():
    async def check():
        conn = CdpConnection('', page_ws_url='ws://test')
        conn.page_operation = AsyncMock(side_effect=[{'matched':False},{'matched':True}])
        conn.evaluate = AsyncMock(return_value=False)
        result = await conn.wait_for(selector='ref:s1:1',text='Ready',url_contains='/done',timeout_ms=300)
        assert result == 'Wait condition satisfied'
        conn.evaluate.assert_not_called()
        assert conn.page_operation.await_count == 2
        conn.page_operation.assert_awaited_with('probe', {'selector':'ref:s1:1','text':'Ready','urlContains':'/done'})
        conn.page_operation = AsyncMock(return_value={'status':'stale_snapshot','message':'expired'})
        assert 'stale_snapshot' in await conn.wait_for(selector='ref:old',timeout_ms=300)
        conn.page_operation.assert_awaited_once()
    asyncio.run(check())


def test_cdp_read_passes_the_offset_with_the_bound_origin():
    async def check():
        backend = CdpBrowserBackend(chrome_path='/fake')
        conn = AsyncMock(); conn.is_connected = True
        conn.page_operation.return_value = {'status':'observed','value':'rest','valueTruncated':False,'valueOffset':12000}
        backend._connections['t'] = conn
        result = await backend.interactive_read('#content',tab_id='t',url='https://example.org/long',offset=12000)
        assert json.loads(result)['valueOffset'] == 12000
        conn.page_operation.assert_awaited_once_with('read', {
            'selector':'#content','offset':12000,'expectedOrigin':'https://example.org'})
        await backend.interactive_read('#content',tab_id='t',url='https://example.org/long')
        conn.page_operation.assert_awaited_with('read', {'selector':'#content','expectedOrigin':'https://example.org'})
    asyncio.run(check())


def test_cdp_wait_timeout_reports_the_time_actually_waited_and_no_observation():
    async def check():
        conn = CdpConnection('', page_ws_url='ws://test')
        conn.page_operation = AsyncMock(return_value={'matched':False})
        # Requests are held to 100..30000 ms; the receipt names the time used, not the time asked for.
        result = json.loads(await conn.wait_for(text='never',timeout_ms=5))
        assert result['status'] == 'timeout' and 'after 100 ms' in result['message']
        assert 'after' not in result
    asyncio.run(check())
