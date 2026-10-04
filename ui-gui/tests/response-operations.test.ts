import { test } from 'node:test';
import assert from 'node:assert/strict';
import { ResponseOperations } from '../src/main/response-operations.js';
const command = { type: 'response_regenerate', request_id: 'request-1', source_ref: { index: 1, digest: 'a'.repeat(64) } };
test('response operation waits for its own receipt and does not resend', async () => {
  const events: any[] = [], writes: any[] = [];
  const operations = new ResponseOperations(e => events.push(e));
  let settled = false;
  const result = operations.run(command, c => writes.push(c)).then(() => { settled = true; });
  await Promise.resolve(); assert.equal(settled, false);
  assert.equal(operations.finish({ type: 'response_operation_result', request_id: 'other' }), false);
  await assert.rejects(operations.run(command, c => writes.push(c)), /正在进行/);
  operations.finish({ type: 'response_operation_result', request_id: 'request-1' });
  await result; assert.equal(settled, true); assert.equal(writes.length, 1);
  assert.deepEqual(events.map(e => e.type), ['response_operation_pending', 'response_operation_result']);
});
test('failure, disconnection and a bounded timeout reject without retrying', async () => {
  for (const cause of ['error', 'disconnect', 'timeout', 'write']) {
    const events: any[] = []; let writes = 0;
    const operations = new ResponseOperations(e => events.push(e), { regenerate: 5, select: 5 });
    const result = operations.run(command, () => { writes++; if (cause === 'write') throw new Error('closed stdin'); });
    const rejected = assert.rejects(result);
    if (cause === 'error') operations.finish({ type: 'response_operation_result', request_id: 'request-1', error: 'failure' });
    if (cause === 'disconnect') operations.disconnect();
    await rejected; assert.equal(writes, 1); assert.equal(operations.active, false);
    assert.ok(events.at(-1).error);
  }
});

test('runtime blocks normal text and image admission during regeneration without creating a user bubble', async () => {
  const { Runtime } = await import('../src/main/runtime.js');
  const { initialSession } = await import('@astra/ui-core/session-state');
  const writes: any[] = [];
  const runtime = Object.assign(Object.create(Runtime.prototype), {
    state: { ...initialSession('test'), status: 'ready' }, waiters: new Set(), emit() {},
    write(value: any) { writes.push(value); },
  });
  const operations = new ResponseOperations(e => runtime.accept(e));
  runtime.responseOperations = operations;
  const result = runtime.send(command);
  for (const type of ['message', 'image']) await assert.rejects(runtime.send({ type, text: 'keep draft', submission_id: 'new' }), /草稿已保留/);
  assert.equal(runtime.state.messages.length, 0); assert.equal(writes.length, 1);
  operations.finish({ type: 'response_operation_result', request_id: 'request-1' });
  await result;
});
