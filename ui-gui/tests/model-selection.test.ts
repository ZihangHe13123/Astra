import { test } from 'node:test';
import assert from 'node:assert/strict';
import { initialSession, type UIEvent } from '@astra/ui-core/session-state';
import { Runtime } from '../src/main/runtime.js';

// Exercise the host's real send/accept methods without starting a Python child.
// Backend receipt generation is covered separately over real Python IPC.
function host() {
  const writes: UIEvent[] = [];
  const runtime = Object.assign(Object.create(Runtime.prototype), {
    state: { ...initialSession('test'), status: 'ready' },
    waiters: new Set(), emit() {}, write(command: UIEvent) { writes.push(command); },
  }) as Runtime;
  return { runtime, writes };
}

test('model selection settles only its own receipt, not ordinary model output', async () => {
  const { runtime, writes } = host();
  let settled = false;
  const pending = runtime.send({ type: 'select_model', model_key: 'provider::B', request_id: 'B' });
  void pending.then(() => { settled = true; });
  assert.deepEqual(writes[0], { type: 'command', cmd: '/model provider::B', request_id: 'B' });
  runtime.accept({ type: 'tool_result', name: 'model', output: 'Ordinary /model response' });
  runtime.accept({ type: 'model_selection_result', request_id: 'A', model_key: 'provider::A', error: '' });
  await Promise.resolve();
  assert.equal(settled, false);
  assert.equal(runtime.state.tools.length, 1);
  runtime.accept({ type: 'model_selection_result', request_id: 'B', model_key: 'provider::B', error: '' });
  await pending;
  assert.equal(runtime.state.info.gui_model_result.request_id, 'B');
  assert.equal(runtime.state.messages.length, 1); // only the ordinary command output
});

test('a timed-out receipt cannot settle a newer selection', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const { runtime } = host();
  const first = runtime.send({ type: 'select_model', model_key: 'provider::A', request_id: 'A' });
  const timeout = assert.rejects(first, /模型切换尚未确认/);
  t.mock.timers.tick(15_000);
  await timeout;
  let settled = false;
  const second = runtime.send({ type: 'select_model', model_key: 'provider::B', request_id: 'B' });
  void second.then(() => { settled = true; });
  runtime.accept({ type: 'model_selection_result', request_id: 'A', model_key: 'unchanged', error: 'late failure' });
  await Promise.resolve();
  assert.equal(settled, false);
  runtime.accept({ type: 'model_selection_result', request_id: 'B', model_key: 'provider::B', error: '' });
  await second;
});

test('unknown manual models reject the request without adding a chat error', async () => {
  const { runtime } = host();
  const pending = runtime.send({ type: 'select_model', model_key: 'missing', request_id: 'missing' });
  const rejected = assert.rejects(pending, /Unknown model: missing/);
  runtime.accept({ type: 'model_selection_result', request_id: 'missing', model_key: 'unchanged',
    error: 'Unknown model: missing. Current model unchanged.', code: 'unknown_model' });
  await rejected;
  assert.equal(runtime.state.messages.length, 0);
  assert.equal(runtime.state.info.gui_model_result.code, 'unknown_model');
});

test('connection progress survives panel closure without copying credentials into state', async () => {
  const { runtime, writes } = host();
  await runtime.send({ type: 'connect_provider', request_id: 'login', route_id: 'codex', api_key: 'sensitive', api_key_env: '', base_url: '' });
  assert.equal(runtime.state.info.connection_pending.request_id, 'login');
  assert.equal(runtime.state.info.connection_pending.route_id, 'codex');
  assert.equal(JSON.stringify(runtime.state).includes('sensitive'), false);
  assert.equal(writes[0].api_key, 'sensitive');
});

test('connection dispatch failure remains visible after the panel reopens', async () => {
  const { runtime } = host();
  runtime.state.status = 'disconnected';
  await assert.rejects(runtime.send({ type: 'connect_provider', request_id: 'login', route_id: 'codex' }), /disconnected/);
  assert.equal(runtime.state.info.connection_result.request_id, 'login');
  assert.match(runtime.state.info.connection_result.error, /disconnected/);
  assert.equal(runtime.state.messages.length, 0);
});

test('a second connection attempt cannot hide the first active authorization', async () => {
  const { runtime, writes } = host();
  await runtime.send({ type: 'connect_provider', request_id: 'first', route_id: 'codex' });
  await assert.rejects(runtime.send({ type: 'connect_provider', request_id: 'second', route_id: 'other' }), /正在进行/);
  assert.equal(runtime.state.info.connection_pending.request_id, 'first');
  assert.equal(writes.length, 1);
  runtime.accept({ type: 'connection_result', request_id: 'first', error: 'cancelled' });
  await runtime.send({ type: 'connect_provider', request_id: 'second', route_id: 'other' });
  assert.equal(writes.length, 2);
});

test('closing while awaiting the handshake does not admit a new message afterward', async () => {
  const { runtime, writes } = host();
  runtime.state.status = 'connecting';
  const send = runtime.send({ type: 'message', text: 'must not run', submission_id: 'message' });
  const rejected = assert.rejects(send, /not sent/);
  await runtime.close(); runtime.accept({ type: 'gui_ready' });
  await rejected;
  assert.equal(writes.length, 0);
  assert.equal(runtime.state.messages.length, 0);
});
