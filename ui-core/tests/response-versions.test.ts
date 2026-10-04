import { test } from 'node:test';
import assert from 'node:assert/strict';
import { initialSession, projectEvent } from '../src/session-state.js';
import { responseControlsBusy } from '../src/response-versions.js';
const source = { index: 1, digest: 'a'.repeat(64) };
const conversation = () => projectEvent(projectEvent(initialSession('runtime'), { type: 'gui_ready' }), {
  type: 'history', session_id: 'session', branch_id: 'branch-a', messages: [
    { id: 'user', role: 'user', content: 'question' }, { id: 'answer', role: 'assistant', content: 'answer A', source_ref: source }],
});
const pending = { type: 'response_operation_pending', operation: 'response_regenerate', request_id: 'retry-1', source_ref: source };
test('regeneration streams independently while canonical user and answer remain intact', () => {
  let state = conversation(); const original = state.messages;
  state = projectEvent(state, pending);
  state = projectEvent(state, { type: 'response_regeneration', request_id: 'retry-1', source_ref: source, status: 'running', delta: 'answer ' });
  state = projectEvent(state, { type: 'response_regeneration', request_id: 'retry-1', source_ref: source, status: 'running', delta: 'B' });
  assert.equal(state.messages, original); assert.equal(state.regeneration?.content, 'answer B');
  assert.equal(state.session, 'session'); assert.equal(state.id, 'runtime'); assert.equal(state.isDraft, false);
  assert.equal(responseControlsBusy(state), true);
  state = projectEvent(state, { type: 'response_operation_result', request_id: 'unrelated' });
  assert.ok(state.responseOperation);
  state = projectEvent(state, { type: 'response_regeneration', request_id: 'old', status: 'running', delta: 'stale' });
  assert.equal(state.regeneration?.content, 'answer B');
});
test('failed, cancelled and disconnected regeneration retains original answer and clears overlay', () => {
  for (const status of ['failed', 'cancelled', 'disconnected']) {
    let state = projectEvent(conversation(), pending); const original = state.messages;
    state = projectEvent(state, status === 'disconnected' ? { type: 'gui_disconnected' } : { type: 'response_regeneration', request_id: 'retry-1', status });
    assert.equal(state.regeneration, undefined); assert.deepEqual(state.messages, original);
    state = projectEvent(state, { type: 'response_operation_result', request_id: 'retry-1', error: status });
    assert.equal(state.responseOperation, undefined);
  }
});
test('successful history replacement preserves runtime and cannot be revived by a late completed stream event', () => {
  let state = projectEvent(conversation(), pending);
  state = projectEvent(state, { type: 'history', session_id: 'session', branch_id: 'branch-b', messages: [
    { id: 'user', role: 'user', content: 'question' }, { id: 'answer-b', role: 'assistant', content: 'answer B' }] });
  assert.equal(state.branchId, 'branch-b'); assert.equal(state.regeneration, undefined); assert.ok(state.responseOperation);
  state = projectEvent(state, { type: 'response_regeneration', request_id: 'retry-1', source_ref: source, status: 'completed', content: 'answer B' });
  assert.equal(state.regeneration, undefined);
  state = projectEvent(state, { type: 'response_versions', branch_id: 'branch-b', active_branch: 'branch-b', revision: 2, choices: [], groups: [], targets: [] });
  state = projectEvent(state, { type: 'response_operation_result', request_id: 'retry-1' });
  assert.equal(state.id, 'runtime'); assert.deepEqual(state.messages.map(m => m.role), ['user', 'assistant']);
  assert.equal(responseControlsBusy(state), false);
});
test('active background work and uncertain submissions block response path changes', () => {
  const state = conversation(); assert.equal(responseControlsBusy(state), false);
  for (const patch of [
    { busy: true }, { approvals: [{ type: 'approval' }] }, { questions: [{ type: 'question' }] },
    { tools: [{ type: 'tool', status: 'running' }] },
    { delegates: { child: { process_id: 'child', goal: 'x', status: 'idle' as const } } },
    { info: { wakeup_status: { type: 'wakeup_status', plan: { state: 'scheduled' } } } },
    { messages: [{ id: 'unknown', role: 'user', content: 'x', submissionState: 'unknown' as const }] },
  ]) assert.equal(responseControlsBusy({ ...state, ...patch }), true);
});
