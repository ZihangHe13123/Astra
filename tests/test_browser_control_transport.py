import asyncio
import json
import os
import struct
import sys
import pytest
import functools

def run_async(fn):
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))
    return wrapped

from agent.runtime.browser_control_transport import BrowserControlTransport, BrowserEndpointOwnedError, BrowserUnsupportedOperation, encode_frame, read_frame


async def connect_peer(server):
    await server.start()
    descriptor=json.loads(server.descriptor_path.read_text())
    reader,writer=await asyncio.open_connection('127.0.0.1',descriptor['port'])
    writer.write(encode_frame({'token':descriptor['token']}));await writer.drain()
    assert (await read_frame(reader))['ok']
    return reader,writer


@pytest.mark.parametrize('peer_state', ['authenticating', 'authenticated', 'ready'])
@run_async
async def test_close_disconnects_peers_before_waiting_for_server(tmp_path, monkeypatch, peer_state):
    server = BrowserControlTransport(tmp_path / 'endpoint')
    accepted = asyncio.Event()
    original_accept = server._accept

    async def accept(reader, writer):
        accepted.set()
        await original_accept(reader, writer)

    monkeypatch.setattr(server, '_accept', accept)
    await server.start()
    descriptor = json.loads(server.descriptor_path.read_text())
    reader, writer = await asyncio.open_connection('127.0.0.1', descriptor['port'])
    try:
        await asyncio.wait_for(accepted.wait(), 2)
        if peer_state != 'authenticating':
            writer.write(encode_frame({'token': descriptor['token']}))
            await writer.drain()
            assert (await read_frame(reader))['ok']
        if peer_state == 'ready':
            await acknowledge_ready(server, writer)

        # The remote peer must not have to disconnect before close can finish.
        await asyncio.wait_for(asyncio.gather(server.close(), server.close()), 2)
        assert await asyncio.wait_for(reader.read(), 2) == b''
        assert not server.connected and not server.ready
        assert not server._tasks and server._server is None and server._lock_fd is None
        assert not server.descriptor_path.exists()
        replacement = BrowserControlTransport(server.directory)
        try:
            await replacement.start()
        finally:
            await replacement.close()
    finally:
        writer.close()
        await writer.wait_closed()
        await asyncio.wait_for(server.close(), 2)


@run_async
async def test_accept_callback_after_close_does_not_wait_for_authentication(tmp_path, monkeypatch):
    server = BrowserControlTransport(tmp_path / 'endpoint')
    entered, release = asyncio.Event(), asyncio.Event()
    original_accept = server._accept

    async def delayed_accept(reader, writer):
        entered.set()
        await release.wait()
        await original_accept(reader, writer)

    monkeypatch.setattr(server, '_accept', delayed_accept)
    await server.start()
    descriptor = json.loads(server.descriptor_path.read_text())
    reader, writer = await asyncio.open_connection('127.0.0.1', descriptor['port'])
    try:
        await asyncio.wait_for(entered.wait(), 2)
        closing = asyncio.create_task(server.close())
        await asyncio.sleep(0)
        assert server._closed
        release.set()
        await asyncio.wait_for(closing, 2)
        assert await asyncio.wait_for(reader.read(), 2) == b''
    finally:
        release.set()
        writer.close()
        await writer.wait_closed()
        await asyncio.wait_for(server.close(), 2)


@run_async
async def test_close_fails_pending_write_without_replaying_it(tmp_path):
    server = BrowserControlTransport(tmp_path / 'endpoint')
    reader, writer = await connect_peer(server)
    await acknowledge_ready(server, writer)
    pending = asyncio.create_task(server.request('click', tab_id='7', args={'selector': 'button'}))
    try:
        assert (await asyncio.wait_for(read_frame(reader), 2))['operation'] == 'click'
        await asyncio.wait_for(server.close(), 2)
        with pytest.raises(ConnectionError, match='unknown_outcome.*will not be replayed'):
            await asyncio.wait_for(pending, 2)
        assert not server._pending
        assert await asyncio.wait_for(reader.read(), 2) == b''
    finally:
        writer.close()
        await writer.wait_closed()
        await asyncio.wait_for(server.close(), 2)
        await asyncio.gather(pending, return_exceptions=True)


@run_async
async def test_capability_announcement_precedes_legacy_ready_and_resets_on_reconnect(tmp_path):
    server=BrowserControlTransport(tmp_path/'endpoint')
    _,writer=await connect_peer(server)
    writer.write(encode_frame({'id':'astra_control_capabilities','ok':True,'result':{
        'version':1,'controllerVersion':2,'extensionVersion':'0.3.3','operations':['snapshot','click']}}))
    await acknowledge_ready(server,writer)
    assert server.operation_support('check') is False
    assert server.operation_support('click') is True
    assert server.controller_capabilities['extensionVersion']=='0.3.3'
    with pytest.raises(BrowserUnsupportedOperation):await server.request('check')
    writer.close();await writer.wait_closed()
    for _ in range(100):
        if not server.connected:break
        await asyncio.sleep(.001)
    assert not server.controller_capabilities
    _,writer=await connect_peer(server)
    await acknowledge_ready(server,writer)
    assert server.operation_support('check') is None
    assert server.operation_support('click') is True
    await server.close();writer.close();await writer.wait_closed()


@run_async
async def test_legacy_unsupported_is_not_dispatched_and_is_not_sent_again(tmp_path):
    server=BrowserControlTransport(tmp_path/'endpoint')
    reader,writer=await connect_peer(server);await acknowledge_ready(server,writer)
    request=asyncio.create_task(server.request('check',tab_id=7,args={'selector':'#a','checked':True}))
    sent=await read_frame(reader)
    writer.write(encode_frame({'id':sent['id'],'ok':False,'error':'Unsupported operation or arguments'}));await writer.drain()
    with pytest.raises(BrowserUnsupportedOperation) as captured:await request
    assert captured.value.result()['dispatch_state']=='not_dispatched'
    assert captured.value.result()['repeat_input'] is False
    with pytest.raises(BrowserUnsupportedOperation):await server.request('check',tab_id=7,args={'ref':'new-ref','checked':True})
    with pytest.raises(TimeoutError):await asyncio.wait_for(reader.read(1),.02)
    with pytest.raises(ValueError,match='object'):await server.request('click',args=[])
    await server.close();writer.close();await writer.wait_closed()


@pytest.mark.parametrize('value',[
    {'version':True,'operations':['click']}, {'version':2,'operations':['click']},
    {'version':1,'operations':'click'}, {'version':1,'operations':[{}]},
    {'version':1,'operations':['click'],'controllerVersion':True},
])
def test_malformed_capabilities_are_rejected(value):
    with pytest.raises(ValueError):BrowserControlTransport._validate_capabilities(value)


@run_async
async def test_availability_uses_lock_not_pid_and_does_not_claim_or_rewrite_endpoint(tmp_path):
    directory = tmp_path / 'runtime'
    first, second = BrowserControlTransport(directory), BrowserControlTransport(directory)
    assert second.endpoint_availability() == {'state': 'available'}
    assert not directory.exists()
    await first.start()
    before = first.descriptor_path.read_bytes()
    assert first.endpoint_availability()['state'] == 'owned_here'
    assert second.endpoint_availability() == {'state': 'owned_elsewhere', 'owner_pid': os.getpid(), 'owner_label': ''}
    assert first.descriptor_path.read_bytes() == before
    assert second._server is None and second._lock_fd is None
    with pytest.raises(BrowserEndpointOwnedError):
        await second.start()
    await first.close()
    # Even a live PID in stale metadata is not a held lock.
    first.descriptor_path.write_bytes(before)
    assert second.endpoint_availability() == {'state': 'available'}
    assert first.descriptor_path.read_bytes() == before
    await second.start()
    await second.close()

async def acknowledge_ready(server, writer):
    writer.write(encode_frame({'id':'astra_control_ready','ok':True,'result':{'version':1}}))
    await writer.drain()
    await server.wait_connected(timeout=.5)

@run_async
async def test_auth_request_timeout_and_reconnect(tmp_path):
    server = BrowserControlTransport(tmp_path / 'endpoint', timeout=.05)
    await server.start()
    descriptor = json.loads(server.descriptor_path.read_text())
    if os.name == "posix":
        assert server.descriptor_path.stat().st_mode & 0o777 == 0o600
    bad_r, bad_w = await asyncio.open_connection('127.0.0.1', descriptor['port'])
    bad_w.write(encode_frame({'token':'wrong'})); await bad_w.drain()
    assert await bad_r.read() == b''
    bad_w.close(); await bad_w.wait_closed()
    r,w = await asyncio.open_connection('127.0.0.1', descriptor['port'])
    w.write(encode_frame({'token':descriptor['token']})); await w.drain()
    assert (await read_frame(r))['ok']
    await acknowledge_ready(server,w)
    task = asyncio.create_task(server.request('click', tab_id='7', args={'selector':'button'}))
    req = await read_frame(r)
    assert req['operation'] == 'click' and req['tabId'] == '7'
    with pytest.raises(TimeoutError, match='unknown_outcome'):
        await task
    w.write(encode_frame({'id':req['id'], 'ok':True, 'result':{}})); await w.drain()
    next_task = asyncio.create_task(server.request('tabs'))
    next_req = await read_frame(r)
    assert next_req['id'] != req['id']
    w.write(encode_frame({'id':next_req['id'],'ok':True,'result':{'tabs':[]}})); await w.drain()
    assert await next_task == {'tabs':[]}
    pending = asyncio.create_task(server.request('type',tab_id='7'))
    await read_frame(r)
    w.close(); await w.wait_closed()
    with pytest.raises(ConnectionError, match='unknown_outcome'):
        await pending
    r2,w2 = await asyncio.open_connection('127.0.0.1', descriptor['port'])
    w2.write(encode_frame({'token':descriptor['token']})); await w2.drain()
    assert (await read_frame(r2))['ok']
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(r2.read(1), .03)
    await server.close()
    w2.close(); await w2.wait_closed()
    assert not server.descriptor_path.exists()

@run_async
async def test_owner_and_frame_bounds(tmp_path):
    first=BrowserControlTransport(tmp_path/'runtime')
    second=BrowserControlTransport(tmp_path/'runtime')
    await first.start()
    before=first.descriptor_path.read_bytes()
    with pytest.raises(RuntimeError, match='owned') as captured:
        await second.start()
    assert f'PID {os.getpid()}' in str(captured.value)
    assert 'Disconnect' in str(captured.value)
    assert json.loads(before)['token'] not in str(captured.value)
    assert first.descriptor_path.read_bytes()==before
    with pytest.raises(ValueError): encode_frame({'x':'x'*(1024*1024)})
    reader=asyncio.StreamReader();reader.feed_data(struct.pack('<I',1024*1024+1))
    with pytest.raises(ValueError): await read_frame(reader)
    await second.close()
    assert first.descriptor_path.exists()
    await first.close()


@pytest.mark.parametrize('use_launcher', [False, True])
@run_async
async def test_other_process_owner_can_exit_and_waiting_runtime_can_retry(tmp_path, use_launcher):
    code = '''
import asyncio, json, os, sys
from agent.runtime.browser_control_transport import BrowserControlTransport
async def main():
    transport = BrowserControlTransport(sys.argv[1])
    await transport.start()
    print(json.dumps({"ready": True, "pid": os.getpid()}), flush=True)
    try:
        await asyncio.to_thread(sys.stdin.readline)
    finally:
        await transport.close()
asyncio.run(main())
'''
    directory = tmp_path / 'runtime'
    command = [sys.executable, '-c', code, str(directory)]
    if use_launcher:
        command = [sys.executable, '-c',
                   'import subprocess, sys; sys.exit(subprocess.call(sys.argv[1:]))', *command]
    owner = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
    )
    second = BrowserControlTransport(directory)
    try:
        # Windows venv launchers may have a different PID from the interpreter.
        ready = json.loads(await asyncio.wait_for(owner.stdout.readline(), 5))
        assert ready['ready'] is True
        child_pid = ready['pid']
        assert type(child_pid) is int and child_pid > 0
        assert json.loads(second.descriptor_path.read_text())['pid'] == child_pid
        with pytest.raises(RuntimeError, match=f'PID {child_pid}'):
            await second.start()
        owner.stdin.write(b'close\n')
        await owner.stdin.drain()
        assert await asyncio.wait_for(owner.wait(), 5) == 0
        await second.start()
        assert json.loads(second.descriptor_path.read_text())['pid'] == os.getpid()
    finally:
        # Close stdin first so a launcher child also exits on assertion failure.
        owner.stdin.close()
        if owner.returncode is None:
            try:
                await asyncio.wait_for(owner.wait(), 5)
            except TimeoutError:
                owner.kill()
                await owner.wait()
        await second.close()


@run_async
async def test_busy_error_does_not_expose_malformed_owner_metadata(tmp_path):
    first = BrowserControlTransport(tmp_path / 'runtime')
    second = BrowserControlTransport(tmp_path / 'runtime')
    await first.start()
    original = first.descriptor_path.read_bytes()
    try:
        first.descriptor_path.write_text('{"pid":"PRIVATE-OWNER-TEXT","token":"PRIVATE-TOKEN"}')
        with pytest.raises(RuntimeError, match='owned') as captured:
            await second.start()
        assert 'PRIVATE' not in str(captured.value)
    finally:
        first.descriptor_path.write_bytes(original)
        await second.close()
        await first.close()

@run_async
async def test_native_host_stdio_bridge(tmp_path):
    import sys
    from agent.runtime.browser_control_host import read_descriptor
    server=BrowserControlTransport(tmp_path/'runtime')
    await server.start()
    descriptor=read_descriptor(server.directory)
    assert descriptor['port'] > 0
    proc=await asyncio.create_subprocess_exec(sys.executable, '-m','agent.runtime.browser_control_host','--endpoint-dir',str(server.directory),stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    for _ in range(100):
        if server.connected: break
        await asyncio.sleep(.01)
    ready=await asyncio.wait_for(read_frame(proc.stdout),2)
    assert ready == {'type':'astra_control_ready','version':1}
    await acknowledge_ready(server,proc.stdin)
    task=asyncio.create_task(server.request('tabs'))
    request=await asyncio.wait_for(read_frame(proc.stdout),2)
    assert 'token' not in request
    proc.stdin.write(encode_frame({'id':request['id'],'ok':True,'result':{'tabs':[]}}));await proc.stdin.drain()
    assert await task == {'tabs':[]}
    await server.close()
    assert await asyncio.wait_for(proc.wait(),2) == 0
    assert b'token' not in await proc.stderr.read()

@run_async
async def test_second_authenticated_client_cannot_steal_requests(tmp_path):
    server=BrowserControlTransport(tmp_path/'runtime')
    await server.start()
    descriptor=json.loads(server.descriptor_path.read_text())
    r,w=await asyncio.open_connection('127.0.0.1',descriptor['port'])
    w.write(encode_frame({'token':descriptor['token']}));await w.drain()
    await read_frame(r)
    r2,w2=await asyncio.open_connection('127.0.0.1',descriptor['port'])
    w2.write(encode_frame({'token':descriptor['token']}));await w2.drain()
    assert await r2.read()==b''
    assert server.connected
    w2.close();await w2.wait_closed()
    await server.close()
    w.close();await w.wait_closed()

def test_native_frame_and_descriptor_fail_closed(tmp_path):
    import io
    from agent.runtime.browser_control_host import read_sync_frame, read_descriptor
    with pytest.raises(ValueError): read_sync_frame(io.BytesIO(struct.pack('<I',1024*1024+1)))
    private=tmp_path/'runtime';private.mkdir(mode=0o700)
    descriptor=private/'endpoint.json';descriptor.write_text('{}');descriptor.chmod(0o644)
    if os.name != 'nt':
        with pytest.raises(PermissionError): read_descriptor(private)

@run_async
async def test_structured_unknown_outcome_remains_result(tmp_path):
    server=BrowserControlTransport(tmp_path/'runtime')
    await server.start();descriptor=json.loads(server.descriptor_path.read_text())
    r,w=await asyncio.open_connection('127.0.0.1',descriptor['port'])
    w.write(encode_frame({'token':descriptor['token']}));await w.drain();await read_frame(r)
    await acknowledge_ready(server,w)
    task=asyncio.create_task(server.request('click',tab_id=7))
    request=await read_frame(r)
    outcome={'status':'unknown_outcome','message':'Dispatch lost its document; do not replay'}
    w.write(encode_frame({'id':request['id'],'ok':False,'error':outcome}));await w.drain()
    assert await task == outcome
    await server.close();w.close();await w.wait_closed()

@run_async
async def test_wait_connected_can_precede_native_host_and_never_queues_actions(tmp_path):
    server=BrowserControlTransport(tmp_path/'runtime')
    waiting=asyncio.create_task(server.wait_connected(timeout=.5))
    for _ in range(100):
        if server.descriptor_path.exists(): break
        await asyncio.sleep(.001)
    descriptor=json.loads(server.descriptor_path.read_text())
    r,w=await asyncio.open_connection('127.0.0.1',descriptor['port'])
    w.write(encode_frame({'token':descriptor['token']}));await w.drain()
    assert (await read_frame(r))['ok']
    await acknowledge_ready(server,w)
    await waiting
    assert server.connected
    with pytest.raises(asyncio.TimeoutError): await asyncio.wait_for(r.read(1),.02)
    await server.close();w.close();await w.wait_closed()

@run_async
async def test_wait_connected_timeout_cancel_and_close(tmp_path):
    server=BrowserControlTransport(tmp_path/'runtime')
    with pytest.raises(TimeoutError,match='auto-connect'):
        await server.wait_connected(timeout=.01)
    pending=asyncio.create_task(server.wait_connected(timeout=2))
    await asyncio.sleep(.01);pending.cancel()
    with pytest.raises(asyncio.CancelledError): await pending
    pending=asyncio.create_task(server.wait_connected(timeout=2))
    await asyncio.sleep(.01);await server.close()
    with pytest.raises(ConnectionError,match='closed'): await pending

@run_async
async def test_authenticated_host_does_not_dispatch_before_extension_ready(tmp_path):
    server=BrowserControlTransport(tmp_path/'runtime');await server.start()
    descriptor=json.loads(server.descriptor_path.read_text())
    r,w=await asyncio.open_connection('127.0.0.1',descriptor['port'])
    w.write(encode_frame({'token':descriptor['token']}));await w.drain();await read_frame(r)
    try:
        assert server.connected and not server.ready
        with pytest.raises(ConnectionError,match='restoring'):
            await server.request('open',args={'url':'https://example.com'})
        waiting=asyncio.create_task(server.wait_connected(timeout=.3))
        await asyncio.sleep(.01);assert not waiting.done()
        w.write(encode_frame({'id':'astra_control_ready','ok':True,'result':{'version':1}}));await w.drain()
        await waiting;assert server.ready
        with pytest.raises(asyncio.TimeoutError): await asyncio.wait_for(r.read(1),.01)
    finally:
        await server.close();w.close();await w.wait_closed()

@run_async
async def test_close_serializes_with_listener_startup(tmp_path,monkeypatch):
    server=BrowserControlTransport(tmp_path/'runtime')
    entered=asyncio.Event();release=asyncio.Event();real=asyncio.start_server
    async def delayed(*args,**kwargs):
        entered.set();await release.wait();return await real(*args,**kwargs)
    monkeypatch.setattr(asyncio,'start_server',delayed)
    starting=asyncio.create_task(server.start());await entered.wait()
    closing=asyncio.create_task(server.close());await asyncio.sleep(.01)
    closed_early=closing.done()
    release.set();await asyncio.gather(starting,closing,return_exceptions=True)
    try:
        assert not closed_early, 'close returned while start could still publish a listener'
        assert server._server is None and server._lock_fd is None
        assert not server.descriptor_path.exists()
    finally:
        await server.close()


@run_async
async def test_release_requests_are_seen_only_by_the_owner_and_only_when_fresh(tmp_path):
    import time
    from agent.runtime.browser_control_transport import RELEASE_REQUEST, RELEASE_REQUEST_MAX_AGE
    directory = tmp_path / 'runtime'
    owner, other = BrowserControlTransport(directory), BrowserControlTransport(directory)
    path = directory / RELEASE_REQUEST

    def write(value):
        path.write_text(json.dumps(value))
        path.chmod(0o600)

    await owner.start()
    try:
        now = time.time()
        write({'pid': os.getpid() + 1, 'label': '跑测试\n"x"', 'at': now})
        assert owner.release_request(now=now) == {'pid': os.getpid() + 1, 'label': '跑测试 x'}
        assert other.release_request(now=now) is None  # not the lock holder
        assert owner.release_request(now=now + RELEASE_REQUEST_MAX_AGE + 1) is None  # stale
        write({'pid': os.getpid(), 'label': 'self', 'at': now})
        assert owner.release_request(now=now) is None  # never hands over to itself
        for malformed in ({'pid': 'x', 'at': now}, {'pid': True, 'at': now}, {'pid': 7, 'at': 'now'}, [1]):
            write(malformed)
            assert owner.release_request(now=now) is None
        # A runtime withdraws only its own request.
        write({'pid': os.getpid() + 1, 'at': now})
        other.withdraw_release_request()
        assert path.exists()
        other.request_release()
        assert json.loads(path.read_text())['pid'] == os.getpid()
        other.withdraw_release_request()
        assert not path.exists()
    finally:
        await owner.close()
