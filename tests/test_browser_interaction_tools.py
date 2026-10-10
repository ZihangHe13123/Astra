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
            arguments='{"text":"ready"}' if name=='browser_wait' else '{}'
            for i in range(2):
                events=await agent._execute_tool_calls([{'id':f'{name}-{i}','name':name,'arguments':arguments}],turn_tool_cache=cache)
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


# ── what the model is told about limits, moved pages and refused calls ──

def setup_isolated(tmp_path):
    """Like setup(), with oversized results kept under tmp_path instead of the working directory."""
    backend = Backend()
    manager = BrowserSessionManager(path=tmp_path / 'browser.db')
    reg = ToolRegistry(artifact_dir=tmp_path / 'tool-results')
    register_browser_tools(reg, manager=manager, backend=backend)
    return reg, backend, manager


def shown(result):
    """The text the next model request receives: the complete result when the stored one is a preview."""
    return result.get('fresh_output') or result['output']


def observation(length, flag):
    return {'url':'https://example.com/form','title':'Form','snapshotId':'s1','text':'x'*length,'textIncluded':True,
            **({} if flag is None else {'textTruncated':flag}),'elements':[]}


@pytest.mark.parametrize('flag,length,note', [
    (True, 12000, 'page text is cut after 12000 characters (limit 12000)'),
    (True, 6000, 'page text is cut after 6000 characters (limit 12000)'),
    (False, 12000, ''),
    # A page script from before the flag: a full window is the only sign.
    (None, 12000, 'page text fills the 12000-character limit'),
    (None, 300, ''),
])
def test_cut_page_text_is_announced_on_open_snapshot_and_action_results(tmp_path, flag, length, note):
    async def scenario():
        reg, b, _ = setup_isolated(tmp_path)
        b.structured_snapshots = True

        async def state(**kw):
            return 'https://example.com/form', 'Form', json.dumps(observation(length, flag))

        async def click(*args, **kw):
            return json.dumps({'status':'observed','after':observation(length, flag)})

        b.interactive_state = state
        b.interactive_click = click
        opened = await reg.execute('browser_open', {'url':'https://example.com/form'})
        refreshed = await reg.execute('browser_snapshot', {'refresh':True})
        for result in (opened, refreshed):
            assert not result['error']
            assert ('Note: page text' in shown(result)) is bool(note)
            assert bool(result.get('partial')) is bool(note)
            if note:
                # Ahead of the long JSON, so a bounded preview keeps it.
                assert note in result['output'] and 'browser_read' in result['output']
        clicked = await reg.execute('browser_click', {'selector':'#next'})
        assert not clicked['error']
        assert ('after_text_note' in shown(clicked)) is bool(note) and note in shown(clicked)
        # The click itself is complete; only its observation is bounded.
        assert not clicked.get('partial')
    asyncio.run(scenario())


def test_read_reports_a_cut_value_and_continues_from_the_offset(tmp_path):
    body = ''.join(str(i % 10) for i in range(30000))

    async def scenario():
        reg, b, _ = setup_isolated(tmp_path)
        await reg.execute('browser_open', {'url':'https://example.com/form','extract':False})
        offsets = []

        async def read(selector, *, tab_id, url, offset=0):
            offsets.append(offset)
            return json.dumps({'status':'observed','target':{'role':'main'},'value':body[offset:offset+12000],
                'valueTruncated':len(body) > offset+12000,'valueOffset':offset,'valueLength':len(body)})

        b.interactive_read = read
        first = await reg.execute('browser_read', {'selector':'#content'})
        assert not first['error'] and first['partial'] is True
        assert 'characters 0 to 12000 of 30000' in shown(first) and 'offset=12000' in shown(first)
        last = await reg.execute('browser_read', {'selector':'#content','offset':24000})
        assert offsets == [0, 24000] and body[24000:] in shown(last)
        assert not last.get('partial') and 'value_note' not in shown(last)
        refused = await reg.execute('browser_read', {'selector':'#content','offset':-1})
        assert refused['error'] and offsets == [0, 24000]
    asyncio.run(scenario())


def test_read_offset_fails_clearly_when_the_page_script_ignores_it(tmp_path):
    async def scenario():
        reg, b, _ = setup_isolated(tmp_path)
        await reg.execute('browser_open', {'url':'https://example.com/form','extract':False})

        async def read(selector, **kw):
            # An extension not yet reloaded: always the start of the value, and no valueOffset.
            return json.dumps({'status':'observed','target':{'role':'main'},'value':'a'*12000,'valueTruncated':True})

        b.interactive_read = read
        first = await reg.execute('browser_read', {'selector':'#content'})
        assert first['partial'] is True and 'cannot continue from an offset' in shown(first)
        assert 'offset=' not in shown(first)
        again = await reg.execute('browser_read', {'selector':'#content','offset':12000})
        assert again['code'] == 'browser_unsupported_operation'
        # The repeated start of the value is never presented as the next part.
        assert 'aaaa' not in again['error'] and not again['output']
        assert 'reload' in again['recovery_hint']
    asyncio.run(scenario())


async def approve_once(_request):
    return 'once'


def test_open_redirect_names_the_destination_and_leaves_no_tab(tmp_path):
    async def scenario():
        reg, b, manager = setup(tmp_path)
        b.structured_snapshots = True
        reg.set_approval_handler(approve_once)
        closed = []

        async def close(tab_id=''):
            closed.append(tab_id)

        async def state(**kw):
            return 'https://www.example.com/form?step=2', 'Form', json.dumps({'text':'OTHER-ORIGIN-CONTENT','elements':[]})

        b.close_connection = close
        b.interactive_state = state
        result = await reg.execute('browser_open', {'url':'http://example.com/form?step=2'})
        text = result['output'] + result['error']
        assert 'Cross-origin redirect blocked' in text
        assert 'https://www.example.com/form?step=2' in text and 'browser_open' in text
        # The rule is unchanged: nothing from the other origin is shown, and no tab is left to "attach".
        assert 'OTHER-ORIGIN-CONTENT' not in text and 'attach' not in text
        assert len(closed) == 1
        after = await reg.execute('browser_snapshot', {})
        assert 'No active tab' in after['output'] + after['error']
    asyncio.run(scenario())


@pytest.mark.parametrize('arguments', [{'refresh':True}, {'include_text':False}])
def test_page_that_left_its_origin_is_named_when_observed(tmp_path, arguments):
    async def scenario():
        reg, b, manager = setup(tmp_path)
        reg.set_approval_handler(approve_once)
        await reg.execute('browser_open', {'url':'https://example.com/form','extract':False})
        moved = {'url':'https://elsewhere.example/landing','title':'Else','text':'OTHER-ORIGIN-CONTENT','elements':[]}

        async def state(**kw):
            return moved['url'], moved['title'], json.dumps(moved)

        async def snapshot(**kw):
            return json.dumps(moved)

        b.interactive_state = state
        b.interactive_snapshot = snapshot
        result = await reg.execute('browser_snapshot', arguments)
        assert result['error'] and 'https://elsewhere.example/landing' in result['error']
        assert 'OTHER-ORIGIN-CONTENT' not in result['error'] + result['output']
        assert 'browser_open' in result['recovery_hint']
        assert 'browser_snapshot/browser_read' not in result['recovery_hint']
        # The tab stays bound to the origin it was opened on.
        session = manager.list_sessions()[0]
        assert session.tabs[session.current_tab_id].url == 'https://example.com/form'
    asyncio.run(scenario())


def test_connect_with_a_tab_id_uses_the_extension_and_refuses_cdp(tmp_path):
    from agent.runtime.browser_backend_router import BrowserBackendRouter

    calls = []

    class Side:
        capabilities = BackendCapabilities(True, True, True)

        def __init__(self, name):
            self.name = name

        async def status(self):
            return True, self.name

        async def connect_existing(self, **kw):
            calls.append((self.name, kw))
            return 'Connected ' + self.name

        async def interactive_state(self, **kw):
            return 'https://example.com/form', self.name, '{}'

        async def close_connection(self, tab_id=''):
            return None

    async def scenario():
        manager = BrowserSessionManager(path=tmp_path / 'browser.db')
        reg = ToolRegistry()
        register_browser_tools(reg, manager=manager, backend=BrowserBackendRouter(Side('cdp'), lambda: Side('extension')))
        # The id copied from browser_tabs, with no transport named.
        result = await reg.execute('browser_connect', {'target_tab_id':'7'})
        assert 'Connected extension' in result['output']
        assert [(name, kw.get('target_tab_id')) for name, kw in calls] == [('extension', '7')]
        tabs = len(manager.list_sessions()[0].tabs)

        refused = await reg.execute('browser_connect', {'transport':'cdp','target_tab_id':'7'})
        assert refused['code'] == 'invalid_arguments' and 'transport=extension' in refused['recovery_hint']
        # Nothing was attached in its place, and no placeholder tab is left behind.
        assert len(calls) == 1 and len(manager.list_sessions()[0].tabs) == tabs

        plain = await reg.execute('browser_connect', {})
        assert 'Connected cdp' in plain['output']
        assert calls[-1][0] == 'cdp' and 'target_tab_id' not in calls[-1][1]
    asyncio.run(scenario())


class ControlTransport:
    """Stands in for the extension controller behind ExtensionBrowserBackend."""
    connected = True
    generation = 1

    def __init__(self):
        self.calls = []

    async def start(self):
        pass

    async def close(self):
        self.connected = False

    async def request(self, operation, *, tab_id=None, args=None):
        self.calls.append(operation)
        page = {'url':'https://example.com/a','title':'A','snapshotId':'s1','text':'Hello','elements':[]}
        if operation == 'open':
            return {'tabId':7,'url':page['url'],'title':page['title']}
        if operation == 'snapshot':
            return page
        if operation == 'wait':
            # With no condition the real controller sleeps, then answers like this.
            return {'status':'observed','after':page}
        raise AssertionError(operation)


def test_wait_without_a_condition_fails_the_same_way_on_both_backends(tmp_path):
    from unittest import mock
    from agent.runtime.cdp_backend import CdpBrowserBackend
    from agent.runtime.extension_browser_backend import ExtensionBrowserBackend

    async def scenario():
        transport = ControlTransport()
        extension = ExtensionBrowserBackend(transport=transport)

        # The CDP backend with its page connection replaced; no browser is started.
        cdp = CdpBrowserBackend('/fake/chrome', timeout=1)
        page = {'url':'https://example.com/a','title':'A','snapshotId':'s1','text':'Hello','elements':[]}
        connection = mock.Mock(is_connected=True)
        connection.navigate = mock.AsyncMock()
        connection.evaluate = mock.AsyncMock(return_value='A')
        connection.get_url = mock.AsyncMock(return_value=page['url'])
        connection.page_operation = mock.AsyncMock(return_value=page)
        connection.wait_for = mock.AsyncMock(return_value='Wait condition satisfied')

        async def connection_for(tab_id, **kwargs):
            cdp._connections[tab_id] = connection
            return connection

        cdp.ensure_connection = mock.AsyncMock(side_effect=connection_for)

        dispatched = {'extension': lambda: transport.calls.count('wait'), 'cdp': lambda: connection.wait_for.await_count}
        failures = []
        for name, backend in (('extension', extension), ('cdp', cdp)):
            reg = ToolRegistry()
            register_browser_tools(reg, manager=BrowserSessionManager(path=tmp_path / f'{name}.db'), backend=backend)
            opened = await reg.execute('browser_open', {'url':'https://example.com/a','extract':False})
            assert 'live browser' in opened['output'], opened
            for arguments in ({}, {'timeout_ms':500}, {'selector':'','text':'','url_contains':''}):
                result = await reg.execute('browser_wait', arguments)
                assert result['code'] == 'invalid_arguments' and not result['output']
                failures.append((result['error'], result['recovery_hint']))
            # Nothing reached this backend.
            assert dispatched[name]() == 0
            # A real condition still does.
            waited = await reg.execute('browser_wait', {'text':'Hello'})
            assert not waited['error'], waited
            assert dispatched[name]() == 1
        assert len(set(failures)) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize('with_after', [False, True])
def test_wait_timeout_hint_matches_what_the_result_contains(tmp_path, with_after):
    async def scenario():
        reg, b, _ = setup(tmp_path)
        await reg.execute('browser_open', {'url':'https://example.com/form','extract':False})

        async def wait(**kw):
            result = {'status':'timeout','message':'Wait condition timed out after 100 ms'}
            if with_after:
                result['after'] = {'url':'https://example.com/form','text':'still loading','elements':[]}
            return json.dumps(result)

        b.interactive_wait = wait
        result = await reg.execute('browser_wait', {'text':'Saved','timeout_ms':100})
        assert result['code'] == 'browser_timeout'
        assert ('after URL/text' in result['recovery_hint']) is with_after
        assert ('browser_snapshot' in result['recovery_hint']) is not with_after
        assert 'do not repeat the preceding input' in result['recovery_hint']
    asyncio.run(scenario())


def test_select_miss_returns_the_choices_and_how_to_use_them(tmp_path):
    async def scenario():
        reg, b, _ = setup(tmp_path)
        await reg.execute('browser_open', {'url':'https://example.com/form','extract':False})

        async def select(selector, value, **kw):
            # The page script's answer when neither a value nor a label matches.
            return json.dumps({'status':'error',
                'message':'Selectable option not found: no option has this value or exact label. Choose from options.',
                'options':[{'value':'sg','label':'Singapore'},{'value':'my','label':'Malaysia'}]})

        b.interactive_select = select
        result = await reg.execute('browser_select', {'selector':'ref:s1:3','value':'Japan'})
        assert result['code'] == 'browser_error'
        assert 'Singapore' in result['error'] and '"value": "my"' in result['error']
        assert 'browser_select again' in result['recovery_hint']
        # There is no after in this result to inspect.
        assert 'Inspect after' not in result['recovery_hint'] and 'inspect_after' not in result['error']
        assert b.reads == 0 and result['partial'] is False
    asyncio.run(scenario())


PNG = bytes.fromhex(
    '89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489'
    '0000000d49444154789c6360000002000001e221bc330000000049454e44ae426082')


@pytest.mark.parametrize(('status', 'clicks_run', 'stopped'), [
    ('observed', 5, False),            # each click changed the page: "load more" five times
    ('no_observed_change', 2, True),   # nothing happened: the third identical click is a stuck repeat
])
def test_the_same_click_may_repeat_only_while_it_changes_the_page(tmp_path, status, clicks_run, stopped):
    from agent.core.msg import ContentBlock, Msg
    from agent.runtime.react import ReActAgent

    class LLM:
        def __init__(self):
            self.calls = 0

        async def chat_stream(self, messages, tools):
            self.calls += 1
            if self.calls <= 5:
                yield {'type':'tool_calls','calls':[{'id':f'more-{self.calls}','name':'browser_click',
                    'arguments':'{"selector":"button.load-more"}'}],
                    'content':'','reasoning_content':'','usage':None}
                yield {'type':'done','content':'','usage':None}
            else:
                yield {'type':'done','content':'All items are loaded.','usage':None}

    async def scenario():
        reg, b, _ = setup(tmp_path)
        clicks = []

        async def click(selector, **kw):
            clicks.append(selector)
            return json.dumps({'status':status,'message':'clicked','after':{
                'url':'https://example.com/form','title':'Form','snapshotId':f'after-{len(clicks)}',
                'text':f'{len(clicks) * 10} items','elements':[]}})

        b.interactive_click = click
        await reg.execute('browser_open', {'url':'https://example.com/form','extract':False})
        agent = ReActAgent('test', LLM(), reg, max_iterations=10)
        events = [e async for e in agent.reply_stream(Msg(content=[ContentBlock.text('Load every item')]))]

        assert len(clicks) == clicks_run
        assert any('repeated tool call' in e.get('message', '') for e in events) is stopped

    asyncio.run(scenario())


def test_screenshot_says_the_image_is_attached_and_can_be_repeated_in_a_turn(tmp_path):
    from agent.core.msg import ContentBlock, Msg
    from agent.runtime.react import ReActAgent

    class LLM:
        def __init__(self):
            self.calls = 0

        async def chat_stream(self, messages, tools):
            self.calls += 1
            if self.calls <= 3:
                yield {'type':'tool_calls','calls':[{'id':f'shot-{self.calls}',
                    'name':'browser_screenshot','arguments':'{}'}],
                    'content':'','reasoning_content':'','usage':None}
                yield {'type':'done','content':'','usage':None}
            else:
                yield {'type':'done','content':'The page finished loading.','usage':None}

    async def scenario():
        reg, b, _ = setup(tmp_path)
        shots = []

        async def screenshot(*, tab_id, url, output_path=''):
            path = tmp_path / f'shot-{len(shots)}.png'
            path.write_bytes(PNG)
            shots.append(path)
            return str(path)

        b.interactive_screenshot = screenshot
        await reg.execute('browser_open', {'url':'https://example.com/form','extract':False})
        direct = await reg.execute('browser_screenshot', {})
        payload = json.loads(direct['output'])
        assert payload['image_paths'] == [str(shots[0])]
        assert 'attached in the following message' in payload['message']

        # Watching a page change: the same argument-less call three times in one turn.
        shots.clear()
        llm = LLM()
        agent = ReActAgent('test', llm, reg, max_iterations=10)
        events = [e async for e in agent.reply_stream(Msg(content=[ContentBlock.text('Watch the page load')]))]
        assert len(shots) == 3 and llm.calls == 4
        assert not any('repeated tool call' in e.get('message','') for e in events)
    asyncio.run(scenario())


@pytest.mark.parametrize('outcome,expected', [
    ('closed', 'Closed browser tab'),
    ('detached', 'Released browser tab'),
    (None, 'closed or released'),
])
def test_close_says_whether_the_tab_was_closed_or_only_released(tmp_path, outcome, expected):
    async def scenario():
        reg, b, _ = setup(tmp_path)

        async def close(tab_id=''):
            return outcome

        b.close_connection = close
        await reg.execute('browser_open', {'url':'https://example.com/form','extract':False})
        result = await reg.execute('browser_close', {})
        assert not result['error'] and expected in result['output']
        if outcome == 'detached':
            assert 'stays open' in result['output'] and 'Closed' not in result['output']
        gone = await reg.execute('browser_snapshot', {})
        assert 'No active tab' in gone['output'] + gone['error']
    asyncio.run(scenario())
