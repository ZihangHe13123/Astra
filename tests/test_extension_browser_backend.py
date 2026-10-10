import asyncio
import json
import pytest
from agent.runtime.extension_browser_backend import ExtensionBrowserBackend
from agent.runtime.browser_control_transport import BrowserUnsupportedOperation

class Transport:
    connected=True
    generation=1
    def __init__(self): self.calls=[]; self.url='https://example.com/a'
    async def start(self): pass
    async def close(self): self.connected=False
    async def request(self,operation,*,tab_id=None,args=None):
        self.calls.append((operation,tab_id,args))
        if operation=='tabs': return {'tabs':[{'id':7,'url':self.url,'title':'Example','owned':False}]}
        if operation in ('attach','open'): return {'tabId':7,'url':self.url,'title':'Example'}
        if operation=='snapshot': return {'url':self.url,'title':'Example','snapshotId':'s1','text':'Hello','elements':[],'limitations':[]}
        return {'status':'no_observed_change','after':{'url':self.url,'text':'Hello'}}

def test_explicit_attach_snapshot_and_origin_binding():
    async def run():
        transport=Transport();backend=ExtensionBrowserBackend(transport=transport)
        with pytest.raises(RuntimeError,match='attached'): await backend.interactive_click('button')
        assert 'Connected' in await backend.connect_existing(target_tab_id='7')
        state=await backend.interactive_state()
        assert state[:2]==(transport.url,'Example') and json.loads(state[2])['snapshotId']=='s1'
        assert not any(c[0]=='open' for c in transport.calls)
        await backend.assert_origin(tab_id='default',expected_url=transport.url)
        assert transport.calls[-1] == ('snapshot',7,{'metadataOnly':True})
        await backend.interactive_click('ref:s1:e1',url=transport.url)
        assert transport.calls[-1] == ('click',7,{'ref':'s1:e1','expectedOrigin':'https://example.com'})
        transport.url='https://different.example/'
        with pytest.raises(PermissionError): await backend.interactive_type('input','secret',url='https://example.com/a')
        transport.generation+=1
        with pytest.raises(ConnectionError): await backend.interactive_state()
    asyncio.run(run())

def test_attach_never_selects_arbitrary_tab_and_close():
    async def run():
        transport=Transport();backend=ExtensionBrowserBackend(transport=transport)
        with pytest.raises(ValueError): await backend.connect_existing(target_tab_id='99')
        await backend.connect_existing(target_tab_id='7')
        await backend.close_connection('default')
        assert transport.calls[-1][0]=='close'
        with pytest.raises(RuntimeError): await backend.interactive_state()
        await backend.close_connection()
        assert not transport.connected
    asyncio.run(run())

def test_actions_carry_bound_approved_origin_even_without_url():
    async def run():
        transport=Transport();backend=ExtensionBrowserBackend(transport=transport)
        transport.url='https://example.com:443/path'
        await backend.connect_existing(target_tab_id='7')
        await backend.interactive_type('input','hello')
        assert transport.calls[-1][2]['expectedOrigin']=='https://example.com'
        await backend.interactive_handoff(tab_id='default',url='https://example.com:443/path')
        assert transport.calls[-1][2]['expectedOrigin']=='https://example.com'
    asyncio.run(run())

@pytest.mark.parametrize('result',[
    {'tabId':True,'url':'https://example.com/a'},
    {'tabId':'7','url':'https://example.com/a'},
    {'tabId':-1,'url':'https://example.com/a'},
    {'tabId':8,'url':'https://example.com/a'},
    {'tabId':7,'url':'https://different.example/'},
    {'tabId':7,'url':'javascript:alert(1)'},
])
def test_attach_validates_exact_target_and_origin(result):
    class ChangedTransport(Transport):
        async def request(self,operation,**kwargs):
            return result if operation=='attach' else await super().request(operation,**kwargs)
    async def run():
        backend=ExtensionBrowserBackend(transport=ChangedTransport())
        with pytest.raises((ValueError,PermissionError)):
            await backend.connect_existing(target_tab_id='7')
        with pytest.raises(RuntimeError):await backend.interactive_state()
    asyncio.run(run())

@pytest.mark.parametrize('result',[
    {'tabId':'7','url':'https://example.com/'},
    {'tabId':7,'url':'https://other.example/'},
    {'tabId':7,'url':''},
])
def test_open_validates_response(result):
    class ChangedTransport(Transport):
        async def request(self,operation,**kwargs):return result
    async def run():
        backend=ExtensionBrowserBackend(transport=ChangedTransport())
        with pytest.raises((ValueError,PermissionError)):
            await backend.interactive_navigate('https://example.com/')
        with pytest.raises(RuntimeError):await backend.interactive_state()
    asyncio.run(run())


def test_frame_operations_keep_ref_and_origin_and_do_not_reconnect():
    async def run():
        t=Transport();b=ExtensionBrowserBackend(transport=t)
        await b.connect_existing(target_tab_id='7')
        await b.interactive_snapshot(scope='editable',frame_ref='frame4',role_filter='textbox',offset=1,limit=4,include_text=False)
        assert t.calls[-1]==('snapshot',7,{'scope':'editable','frame_ref':'frame4','role_filter':'textbox','offset':1,'limit':4,'include_text':False,'expectedOrigin':'https://example.com'})
        await b.interactive_fill('ref:frame4:editor','answer')
        assert t.calls[-1]==('fill',7,{'ref':'frame4:editor','text':'answer','expectedOrigin':'https://example.com'})
        await b.interactive_read('ref:frame4:editor')
        assert t.calls[-1]==('read',7,{'ref':'frame4:editor','expectedOrigin':'https://example.com'})
        t.generation+=1
        with pytest.raises(ConnectionError): await b.interactive_fill('ref:old','wrong')
    asyncio.run(run())


def test_check_batch_reaches_transport_once_and_keeps_origin_binding():
    async def run():
        transport=Transport(); backend=ExtensionBrowserBackend(transport=transport)
        await backend.connect_existing(target_tab_id='7')
        checks=[{'selector':'ref:s1:e1','checked':True},{'selector':'ref:s1:e2','checked':False}]
        await backend.interactive_check(checks=checks)
        writes=[c for c in transport.calls if c[0]=='check']
        assert writes==[('check',7,{'checks':checks,'expectedOrigin':'https://example.com'})]
        transport.generation+=1
        with pytest.raises(ConnectionError): await backend.interactive_check(checks=checks)
        assert [c for c in transport.calls if c[0]=='check']==writes
    asyncio.run(run())


class ChoiceTransport(Transport):
    def __init__(self, support=None, page_compat=True, failure=None):
        super().__init__()
        self.support = support
        self.page_compat = page_compat
        self.failure = failure
        self.controller_capabilities = {} if support is None else {'extensionVersion':'0.3.3'}

    def operation_support(self, operation):
        return self.support if operation == 'check' else True

    async def request(self, operation, *, tab_id=None, args=None):
        result = await super().request(operation, tab_id=tab_id, args=args)
        if operation == 'snapshot' and not (args or {}).get('metadataOnly'):
            result['capabilities'] = {'check':True, 'checkBatchLimit':20, 'checkViaClick':self.page_compat}
        if operation == 'check' and self.failure:
            if self.failure == 'unsupported':
                self.support = False
                raise BrowserUnsupportedOperation(operation)
            if isinstance(self.failure, Exception):
                raise self.failure
            return {'status':self.failure, 'dispatch_state':'unknown'}
        return result


@pytest.mark.parametrize('support,route', [(None,'click'),(False,'click'),(True,'check')])
def test_negotiated_and_legacy_choice_routes_are_truthful(support, route):
    async def run():
        t=ChoiceTransport(support);b=ExtensionBrowserBackend(transport=t)
        await b.connect_existing(target_tab_id='7')
        s=json.loads((await b.interactive_state())[2])
        assert s['capabilities']['check'] is True
        assert s['capabilities']['checkRoute']==route
        goals=[{'selector':'#a','checked':True},{'selector':'#b','checked':False}]
        await b.interactive_check(checks=goals)
        writes=[c for c in t.calls if c[0] in {'check','click'}]
        assert len(writes)==1 and writes[0][0]==route
        assert writes[0][2]['expectedOrigin']=='https://example.com'
        assert writes[0][2]['checks' if route=='check' else 'choiceGoals']==goals
    asyncio.run(run())


def test_definite_rejection_selects_compatibility_once_and_unknown_never_does():
    async def run():
        for failure in ['unsupported','unknown_outcome','verification_failed',TimeoutError('unknown_outcome')]:
            t=ChoiceTransport(True, failure=failure);b=ExtensionBrowserBackend(transport=t)
            await b.connect_existing(target_tab_id='7');await b.interactive_state()
            if isinstance(failure, Exception):
                with pytest.raises(TimeoutError):await b.interactive_check('#a')
            else:
                result=json.loads(await b.interactive_check('#a'))
                if failure!='unsupported':assert result['status']==failure
            assert [c[0] for c in t.calls if c[0] in {'check','click'}] == (
                ['check','click'] if failure=='unsupported' else ['check'])
    asyncio.run(run())


def test_missing_routes_return_not_dispatched_and_reconnect_invalidates_binding():
    async def run():
        t=ChoiceTransport(False, page_compat=False);b=ExtensionBrowserBackend(transport=t)
        await b.connect_existing(target_tab_id='7')
        s=json.loads((await b.interactive_state())[2])
        assert s['capabilities']['check'] is False
        result=json.loads(await b.interactive_check('#a'))
        assert result['status']=='unsupported_operation' and result['dispatch_state']=='not_dispatched'
        assert not any(c[0] in {'check','click'} for c in t.calls)
        t.generation+=1
        with pytest.raises(ConnectionError):await b.interactive_check('#a')
    asyncio.run(run())


def test_wait_receipt_reports_actual_limit_and_changed_url_without_claiming_match():
    class FinishedTransport(Transport):
        async def request(self, operation, **kwargs):
            if operation=='wait':
                self.calls.append((operation,kwargs.get('tab_id'),kwargs.get('args')))
                return {'status':'timeout','after':{'url':'https://example.com/results','text':'10 of 10'}}
            return await super().request(operation, **kwargs)
    async def run():
        t=FinishedTransport();b=ExtensionBrowserBackend(transport=t)
        await b.connect_existing(target_tab_id='7')
        result=json.loads(await b.interactive_wait(url=t.url,text='submitted',timeout_ms=15000))
        assert result['status']=='timeout' and result['wait']['matched'] is False
        assert result['wait']['timeoutMs']==10000 and result['wait']['requestedTimeoutMs']==15000
        assert result['wait']['urlChanged'] is True and result['after']['text']=='10 of 10'
    asyncio.run(run())


def test_read_offset_reaches_the_page_script_only_when_used():
    async def run():
        t=Transport();b=ExtensionBrowserBackend(transport=t)
        await b.connect_existing(target_tab_id='7')
        await b.interactive_read('ref:s1:e1',offset=12000)
        assert t.calls[-1]==('read',7,{'ref':'s1:e1','offset':12000,'expectedOrigin':'https://example.com'})
        # A first read is sent exactly as before, so an extension not yet reloaded sees nothing new.
        await b.interactive_read('ref:s1:e1')
        assert t.calls[-1]==('read',7,{'ref':'s1:e1','expectedOrigin':'https://example.com'})
    asyncio.run(run())


def test_close_message_tells_an_opened_tab_from_a_users_tab(tmp_path):
    from agent.runtime.browser_session import BrowserSessionManager
    from agent.runtime.tools.browser import register_browser_tools
    from agent.runtime.tools.registry import ToolRegistry

    class ControllerTransport(Transport):
        """Answers close the way the controller does: its own tab is closed, a granted one detached."""
        def __init__(self): super().__init__(); self.owned=set()
        async def request(self,operation,*,tab_id=None,args=None):
            if operation=='close':
                self.calls.append((operation,tab_id,args))
                return {'status':'closed' if tab_id in self.owned else 'detached','tabId':tab_id}
            result=await super().request(operation,tab_id=tab_id,args=args)
            if operation=='open': self.owned.add(result['tabId'])
            return result

    async def run():
        for opened,expected in ((True,'Closed browser tab'),(False,'Released browser tab')):
            transport=ControllerTransport()
            registry=ToolRegistry()
            register_browser_tools(registry,manager=BrowserSessionManager(path=tmp_path/f'{opened}.db'),
                backend=ExtensionBrowserBackend(transport=transport))
            if opened:
                result=await registry.execute('browser_open',{'url':'https://example.com/a','extract':False})
            else:
                result=await registry.execute('browser_connect',{'target_tab_id':'7'})
            assert not result['error'] and '[Browser Error]' not in result['output'],result
            closed=await registry.execute('browser_close',{})
            assert expected in closed['output'],closed
            assert transport.calls[-1][0]=='close'
            if not opened: assert 'stays open' in closed['output']
    asyncio.run(run())
