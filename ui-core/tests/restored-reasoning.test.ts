import { test } from 'node:test';
import assert from 'node:assert/strict';
import { initialSession, projectEvent } from '../src/session-state.js';

test('history retains exact assistant reasoning as display metadata without inserting messages', () => {
  const source = Object.freeze({ index: 4, digest: 'a'.repeat(64) });
  const text = '  First line\n\nSecond line  \n';
  const history = Object.freeze([
    Object.freeze({ id: 'user', role: 'user', content: 'question', reasoning_content: 'not assistant reasoning' }),
    Object.freeze({ id: 'answer', role: 'assistant', content: 'answer', reasoning_content: text, source_ref: source }),
  ]);
  const state = projectEvent(initialSession('runtime'), { type: 'history', session_id: 'session', messages: history });
  assert.equal(state.messages.length, 2);
  assert.deepEqual(state.messages.map(m => m.id), ['user', 'answer']);
  assert.equal(state.messages[0].reasoning_content, undefined);
  assert.equal(state.messages[1].reasoning_content, text);
  assert.equal(state.messages[1].source_ref, source);
  assert.equal(state.messages[1].content, 'answer');
  assert.equal(history[1].reasoning_content, text);
});

test('non-text and blank reasoning are omitted while explicit legacy reasoning stays a row', () => {
  const messages = ['', ' \n ', null, [], { text: 'no coercion' }, 42].map((reasoning_content, index) => ({
    id: `answer-${index}`, role: 'assistant', content: 'answer', reasoning_content,
  }));
  const state = projectEvent(initialSession('runtime'), { type: 'history', messages: [
    ...messages, { id: 'legacy', role: 'reasoning', content: 'legacy stored thought' },
  ] });
  assert.equal(state.messages.length, messages.length + 1);
  assert.ok(state.messages.every(m => !Object.hasOwn(m, 'reasoning_content')));
  assert.equal(state.messages.at(-1)?.role, 'reasoning');
  assert.equal(state.messages.at(-1)?.content, 'legacy stored thought');
});

test('reloading history replaces streamed reasoning rather than retaining both forms', () => {
  let state = projectEvent(initialSession('runtime'), { type: 'reasoning', stream_id: 'step', content: 'thought' });
  state = projectEvent(state, { type: 'chunk', stream_id: 'step', content: 'answer' });
  state = projectEvent(state, { type: 'history', messages: [{ id: 'saved', role: 'assistant', content: 'answer', reasoning_content: 'thought' }] });
  assert.deepEqual(state.messages.map(m => m.role), ['assistant']);
  assert.equal(state.messages[0].reasoning_content, 'thought');
});
