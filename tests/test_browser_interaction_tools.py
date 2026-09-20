import asyncio
import json

import pytest

from agent.runtime.browser_session import BackendCapabilities, BrowserSessionManager
from agent.runtime.tools.browser import register_browser_tools
from agent.runtime.tools.registry import ToolRegistry


class Backend:
    name = 'test'
    capabilities = BackendCapabilities(True, True, True)

    def __init__(self):
        self.navigations = 0
        self.reads = 0
        self.writes = 0
        self.origin_changed = False

    async def extract(self, url, **kw):
        self.navigations += 1
        return 'static ' * 100

    async def interactive_navigate(self, url, **kw):
        self.navigations += 1

    async def interactive_state(self, **kw):
        self.reads += 1
        return 'https://example.com/form', 'Form', json.dumps({'snapshotId':'read','text':'live form','elements':[]})

    async def assert_origin(self, **kw):
        if self.origin_changed:
            raise RuntimeError('live_origin_changed: refresh before writing')

    async def interactive_click(self, *args, **kw):
        self.writes += 1
        return json.dumps({'status':'observed','message':'control changed','after':{
            'url':'https://example.com/form','title':'Form','snapshotId':'after',
            'text':'saved','elements':[{'ref':'after:e1','name':'Next','role':'button'}]}})


def setup(tmp_path):
    backend = Backend()
    manager = BrowserSessionManager(path=tmp_path / 'browser.db')
    reg = ToolRegistry()
    register_browser_tools(reg, manager=manager, backend=backend)
    return reg, backend, manager


def test_refresh_observes_current_page_without_navigating(tmp_path):
    async def scenario():
        reg, b, _ = setup(tmp_path)
        await reg.execute('browser_open', {'url':'https://example.com/form','extract':False})
        result = await reg.execute('browser_snapshot', {'refresh':True})
        assert 'live form' in result['output']
        assert b.navigations == 0
        assert b.reads == 1
    asyncio.run(scenario())


def test_write_checks_live_origin_before_dispatch(tmp_path):
    async def scenario():
        reg, b, _ = setup(tmp_path)
        await reg.execute('browser_open', {'url':'https://example.com/form','extract':False})
        b.origin_changed = True
        result = await reg.execute('browser_click', {'selector':'#save'})
        assert b.writes == 0
        assert 'live_origin_changed' in result['error']
    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["click", "type", "fill", "check"])
@pytest.mark.parametrize("stage", ["origin", "guard", "backend", "observation"])
def test_mutation_exception_receipt_tracks_dispatch_without_replay(tmp_path, operation, stage):
    async def scenario():
        reg, backend, _ = setup(tmp_path)
        await reg.execute("browser_open", {"url": "https://example.com/form", "extract": False})

        async def action(*args, **kwargs):
            backend.writes += 1
            if stage == "backend":
                raise RuntimeError("backend outcome unavailable")
            return "legacy action result"

        async def observe(**kwargs):
            backend.reads += 1
            raise RuntimeError("observation unavailable")

        setattr(backend, "interactive_" + operation, action)
        backend.interactive_state = observe
        if stage == "origin":
            backend.origin_changed = True
        elif stage == "guard":
            backend.capabilities = BackendCapabilities(True, False, True)
        args = {"selector": "#target"}
        if operation in {"type", "fill"}:
            args["text"] = "replacement"
        result = await reg.execute("browser_" + operation, args)

        dispatched = stage in {"backend", "observation"}
        assert result["error"]
        assert result["details"]["dispatch_state"] == ("unknown" if dispatched else "not_dispatched")
        assert result["details"]["operation"] == operation
        assert result["partial"] is dispatched
        assert result["retryable"] is False
        assert "browser_snapshot/browser_read" in result["recovery_hint"]
        assert "Do not replay" in result["recovery_hint"]
        assert backend.writes == int(dispatched)
        assert backend.reads == int(stage == "observation")
    asyncio.run(scenario())


@pytest.mark.parametrize("stage", ["backend", "observation"])
def test_read_exception_is_not_a_partial_mutation(tmp_path, stage):
    async def scenario():
        reg, backend, _ = setup(tmp_path)
        await reg.execute("browser_open", {"url": "https://example.com/form", "extract": False})
        calls = []

        async def read(*args, **kwargs):
            calls.append("read")
            if stage == "backend":
                raise RuntimeError("read unavailable")
            return "legacy read result"

        async def observe(**kwargs):
            backend.reads += 1
            raise RuntimeError("observation unavailable")

        backend.interactive_read = read
        backend.interactive_state = observe
        result = await reg.execute("browser_read", {"selector": "#target"})
        assert result["error"]
        assert result["partial"] is False
        assert backend.writes == 0
        assert calls == ["read"]
        assert backend.reads == int(stage == "observation")
    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["click", "type"])
def test_nonawaitable_mutation_hook_has_clear_failure_without_replay(tmp_path, operation):
    async def scenario():
        reg, backend, _ = setup(tmp_path)
        await reg.execute("browser_open", {"url": "https://example.com/form", "extract": False})

        def synchronous_hook(*args, **kwargs):
            backend.writes += 1
            return "untrusted synchronous receipt"

        setattr(backend, "interactive_" + operation, synchronous_hook)
        args = {"selector": "#target"}
        if operation == "type":
            args["text"] = "replacement"
        result = await reg.execute("browser_" + operation, args)

        assert "browser backend operation must be awaitable" in result["error"]
        assert result["details"] == {"operation": operation, "dispatch_state": "unknown"}
        assert result["partial"] is True and result["retryable"] is False
        assert "Do not replay" in result["recovery_hint"]
        assert backend.writes == 1 and backend.reads == 0
    asyncio.run(scenario())


def test_browser_type_legacy_execution_and_permission_policy_survive(tmp_path):
    async def scenario():
        reg, backend, _ = setup(tmp_path)
        backend.interactive_type = backend.interactive_click
        await reg.execute("browser_open", {"url": "https://example.com/form", "extract": False})
        args = {"selector": "#target", "text": "replacement"}
        result = await reg.execute("browser_type", args)
        assert not result["error"] and "after:e1" in result["output"]
        assert backend.writes == 1

        reg.policy.add_rule_shortcut("browser_type", "deny")
        denied = await reg.execute("browser_type", args)
        assert denied["error"]
        assert backend.writes == 1
    asyncio.run(scenario())


def test_browser_negative_status_never_suggests_completion_even_with_verified_flag(tmp_path):
    async def scenario():
        reg, backend, _ = setup(tmp_path)
        await reg.execute('browser_open', {'url': 'https://example.com/form', 'extract': False})

        async def uncertain(*args, **kwargs):
            backend.writes += 1
            return json.dumps({'status': 'unknown_outcome', 'verified': True,
                               'dispatch_state': 'unknown', 'after': {'url': 'https://example.com/form', 'elements': []}})

        backend.interactive_click = uncertain
        result = await reg.execute('browser_click', {'selector': '#save'})
        assert result['code'] == 'browser_unknown_outcome'
        assert 'inspect_after_then_observe' in result['error']
        assert 'continue_from_after' not in result['error']
        assert backend.writes == 1
    asyncio.run(scenario())


def test_write_returns_same_after_refs_without_resnapshot(tmp_path):
    async def scenario():
        reg, b, manager = setup(tmp_path)
        await reg.execute('browser_open', {'url':'https://example.com/form','extract':False})
        result = await reg.execute('browser_click', {'selector':'#save'})
        assert 'after:e1' in result['output']
        assert b.reads == 0
        assert 'after:e1' in manager.list_sessions()[0].tabs[manager.list_sessions()[0].current_tab_id].last_snapshot
    asyncio.run(scenario())


def test_unsupported_operation_has_deterministic_recovery_and_no_extra_observation(tmp_path):
    async def scenario():
        reg,b,manager=setup(tmp_path)
        await reg.execute('browser_open',{'url':'https://example.com/form','extract':False})
        async def unsupported(*args, **kwargs):
            return json.dumps({'status':'unsupported_operation','operation':'check','dispatch_state':'not_dispatched',
                'recovery_hint':'Use an available route; do not change CSS/ref syntax or repeat check.'})
        b.interactive_check=unsupported
        result=await reg.execute('browser_check',{'selector':'#a'})
        assert result['code']=='browser_unsupported_operation'
        assert result['details']['dispatch_state']=='not_dispatched'
        assert not result['partial']
        assert 'use_available_channel' in result['error'] and 'CSS/ref' in result['recovery_hint']
        assert b.reads==0
    asyncio.run(scenario())


def test_structured_open_creates_real_tab_even_without_extraction(tmp_path):
    async def scenario():
        reg,b,manager=setup(tmp_path)
        b.structured_snapshots=True
        result=await reg.execute('browser_open',{'url':'https://example.com/form','extract':False})
        assert b.navigations==1 and b.reads==1
        assert 'Opened tab' in result['output']
    asyncio.run(scenario())


def test_tabs_and_connect_can_explicitly_select_extension(tmp_path):
    async def scenario():
        reg,b,manager=setup(tmp_path)
        async def tabs(**kw): return [{'id':7,'url':'https://example.com/form','title':'Form'}]
        async def connect(**kw):
            assert kw['transport']=='extension' and kw['target_tab_id']=='7'
            return 'Connected extension'
        b.list_tabs=tabs
        b.connect_existing=connect
        listing=await reg.execute('browser_tabs',{})
        assert 'Form' in listing['output']
        result=await reg.execute('browser_connect',{'transport':'extension','target_tab_id':'7'})
        assert 'Connected extension' in result['output']
    asyncio.run(scenario())


def test_structured_snapshot_sanitization_preserves_json_and_refs():
    from agent.runtime.browser_session import sanitize_snapshot
    source={'snapshotId':'s1','text':'Cookie: session=secret-value','elements':[{'ref':'s1:e1','name':'Next'}]}
    clean=json.loads(sanitize_snapshot(json.dumps(source)))
    assert clean['elements'][0]['ref']=='s1:e1'
    assert 'secret-value' not in clean['text']


def test_noninteractive_structured_backend_keeps_static_open(tmp_path):
    async def scenario():
        reg,b,_=setup(tmp_path)
        b.structured_snapshots=True
        b.capabilities=BackendCapabilities(True,False,False)
        result=await reg.execute('browser_open',{'url':'https://example.com/form'})
        assert b.reads==0
        assert 'static' in result['output']
    asyncio.run(scenario())


def test_wait_preserves_refs_from_its_own_final_observation(tmp_path):
    async def scenario():
        reg,b,manager=setup(tmp_path)
        async def wait(**kwargs): return await b.interactive_click()
        b.interactive_wait=wait
        await reg.execute('browser_open',{'url':'https://example.com/form','extract':False})
        result=await reg.execute('browser_wait',{'text':'Saved'})
        assert 'after:e1' in result['output'] and b.reads==0
    asyncio.run(scenario())


def test_react_snapshot_refresh_does_not_reuse_old_refs(tmp_path):
    from agent.runtime.react import ReActAgent

    async def scenario():
        reg, b, _ = setup(tmp_path)
        await reg.execute('browser_open', {'url':'https://example.com/form','extract':False})
        async def state(**kw):
            b.reads += 1
            return 'https://example.com/form', 'Form', json.dumps({
                'snapshotId':f's{b.reads}', 'elements':[{'ref':f's{b.reads}:0','role':'button','name':'Save'}]})
        b.interactive_state = state
        agent = ReActAgent('test', object(), reg)
        cache = {}
        results = []
        for i in range(2):
            events = await agent._execute_tool_calls([{'id':str(i),'name':'browser_snapshot',
                'arguments':'{"refresh":true}'}], turn_tool_cache=cache)
            results.append(events[0]['output'])
        assert b.reads == 2
        assert 's1:0' in results[0] and 's2:0' in results[1]
    asyncio.run(scenario())


def test_react_browser_observation_can_repeat_but_is_bounded(tmp_path):
    from agent.core.msg import ContentBlock, Msg
    from agent.runtime.react import ReActAgent

    class LLM:
        def __init__(self): self.calls = 0
        async def chat_stream(self, messages, tools):
            self.calls += 1
            yield {'type':'tool_calls','calls':[{'id':f'read-{self.calls}',
                'name':'browser_snapshot','arguments':'{"refresh":true}'}],
                'content':'','reasoning_content':'','usage':None}
            yield {'type':'done','content':'','usage':None}

    async def scenario():
        reg, b, _ = setup(tmp_path)
        await reg.execute('browser_open', {'url':'https://example.com/form','extract':False})
        agent = ReActAgent('test', LLM(), reg, max_iterations=25)
        events = [e async for e in agent.reply_stream(Msg(content=[ContentBlock.text('Observe the page')]))]
        assert b.reads == 20
        assert any('per-turn tool budget exhausted' in e.get('message','') for e in events)
        assert not any('repeated tool call' in e.get('message','') for e in events)
        # Changing observation policy must not permit automatic replay of writes.
        for name in ('browser_click','browser_type','browser_select','browser_open'):
            assert reg.get(name).repeat_guard is True
            assert reg.get(name).idempotent is False
    asyncio.run(scenario())


def test_react_browser_wait_and_connection_status_are_fresh(tmp_path):
    from agent.runtime.react import ReActAgent

    async def scenario():
        reg, b, _ = setup(tmp_path)
        await reg.execute('browser_open', {'url':'https://example.com/form','extract':False})
        counts = {}
        async def wait(**kw):
            counts['browser_wait'] = counts.get('browser_wait',0)+1
            return json.dumps({'status':'observed','message':f"wait-{counts['browser_wait']}"})
        async def tabs(**kw):
            counts['browser_tabs'] = counts.get('browser_tabs',0)+1
            return [{'id':counts['browser_tabs'],'url':'https://example.com/form'}]
        async def status(**kw):
            counts['browser_status'] = counts.get('browser_status',0)+1
            return True, f"status-{counts['browser_status']}"
        async def connect(**kw):
            counts['browser_connect'] = counts.get('browser_connect',0)+1
            return f"Connected-{counts['browser_connect']}"
        b.interactive_wait=wait; b.list_tabs=tabs; b.status=status; b.connect_existing=connect
        agent=ReActAgent('test',object(),reg)
        for name in ('browser_wait','browser_tabs','browser_status','browser_connect'):
            cache={}; outputs=[]
            for i in range(2):
                events=await agent._execute_tool_calls([{'id':f'{name}-{i}','name':name,'arguments':'{}'}],turn_tool_cache=cache)
                outputs.append(events[0]['output'])
            assert counts.get(name)==2, name
            assert outputs[0]!=outputs[1], name
    asyncio.run(scenario())


def test_frame_fill_read_and_scoped_snapshot_preserve_target_evidence(tmp_path):
    async def scenario():
        reg, b, manager = setup(tmp_path)
        await reg.execute('browser_open', {'url':'https://example.com/form','extract':False})
        async def snapshot(**kwargs):
            assert kwargs['scope']=='editable' and kwargs['frame_ref']=='f4'
            assert kwargs['role_filter']=='textbox' and kwargs['offset']==2
            return json.dumps({'url':'https://example.com/form','frames':[{'frameRef':'f4'}],
                'elements':[{'ref':'s4:1','frameRef':'f4','value':'old'}]})
        async def fill(selector, text, **kwargs):
            assert selector=='ref:s4:1' and text=='fourth'
            return json.dumps({'status':'verified','verified':True,'target':{'frameRef':'f4'},
                'beforeValue':'old','value':'fourth','after':{'url':'https://example.com/form',
                    'elements':[{'ref':'s5:1','frameRef':'f4','value':'fourth'}]}})
        async def read(selector, **kwargs):
            assert selector=='ref:s5:1'
            return json.dumps({'status':'observed','target':{'frameRef':'f4'},'value':'fourth'})
        b.interactive_snapshot=snapshot;b.interactive_fill=fill;b.interactive_read=read
        result=await reg.execute('browser_snapshot',{'scope':'editable','frame_ref':'f4','role_filter':'textbox','offset':2})
        assert not result['error'] and 's4:1' in result['output']
        result=await reg.execute('browser_fill',{'selector':'ref:s4:1','text':'fourth'})
        assert not result['error'] and 's5:1' in result['output']
        result=await reg.execute('browser_read',{'selector':'ref:s5:1'})
        assert not result['error'] and 'fourth' in result['output']
        assert b.reads==0
        tab=manager.list_sessions()[0].tabs[manager.list_sessions()[0].current_tab_id]
        assert 's5:1' in tab.last_snapshot
    asyncio.run(scenario())


def test_browser_failures_are_errors_and_never_reobserve_or_replay(tmp_path):
    async def scenario():
        reg,b,_=setup(tmp_path)
        await reg.execute('browser_open',{'url':'https://example.com/form','extract':False})
        for index, state in enumerate(('error','ambiguous_target','stale_snapshot','verification_failed','unknown_outcome','timeout','target_not_found')):
            async def fill(*args,state=state,**kw):
                b.writes+=1
                return json.dumps({'status':state,'message':'target rejected'})
            b.interactive_fill=fill
            result=await reg.execute('browser_fill',{'selector':f'ref:s:{index}','text':'wanted'})
            assert bool(result['error']), result
            assert result['code']=='browser_'+state, result
            assert b.reads==0 and b.writes==index+1
        assert reg.get('browser_fill').idempotent is False
        assert reg.get('browser_fill').repeat_guard is True
    asyncio.run(scenario())


def test_idle_is_not_reported_as_unavailable(tmp_path):
    async def scenario():
        reg,b,_=setup(tmp_path)
        async def status(): return False,'Extension auto-connect: idle; first task connects'
        b.status=status
        result=await reg.execute('browser_status',{})
        assert 'Backend: idle' in result['output'] and 'unavailable' not in result['output']
    asyncio.run(scenario())


def test_compact_snapshot_option_and_checked_evidence_survive_tool_and_session(tmp_path):
    async def scenario():
        reg, b, manager = setup(tmp_path)
        await reg.execute('browser_open', {'url':'https://example.com/form','extract':False})
        calls = []

        async def snapshot(**kwargs):
            calls.append(kwargs)
            included = kwargs['include_text']
            return json.dumps({'url':'https://example.com/form','snapshotId':f's{len(calls)}',
                'text':'Saved' if included else '', 'textIncluded':included,
                'elements':[{'ref':f's{len(calls)}:0','role':'checkbox','checked':False}]})

        async def click(*args, **kwargs):
            return json.dumps({'status':'observed','after':{'url':'https://example.com/form',
                'snapshotId':'after','text':'','textIncluded':False,
                'elements':[{'ref':'after:0','role':'checkbox','checked':True}]}})

        b.interactive_snapshot = snapshot
        b.interactive_click = click
        result = await reg.execute('browser_snapshot', {'include_text':False})
        assert not result['error'] and '"textIncluded": false' in result['output']
        assert len(calls) == 1 and calls[0]['include_text'] is False
        result = await reg.execute('browser_click', {'selector':'ref:s1:0'})
        assert not result['error'] and '"checked": true' in result['output']
        tab = manager.list_sessions()[0].tabs[manager.list_sessions()[0].current_tab_id]
        saved = json.loads(tab.last_snapshot)
        assert saved['textIncluded'] is False and saved['elements'][0]['checked'] is True
        # A request for text must not return the prior compact cached snapshot.
        result = await reg.execute('browser_snapshot', {'include_text':True})
        assert not result['error'] and 'Saved' in result['output']
        assert len(calls) == 2 and calls[1]['include_text'] is True
        assert b.reads == 0 and b.navigations == 0

    asyncio.run(scenario())
