import { test } from 'node:test';
import assert from 'node:assert/strict';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import type { Message } from '@astra/ui-core/session-state';
import { expandRestoredReasoning } from '../src/renderer/restored-reasoning.js';
import { messageOffsets, windowRange } from '../src/renderer/message-window.js';
import { MessageRow } from '../src/renderer/messages.js';

const source = Object.freeze({ index: 7, digest: 'a'.repeat(64) });
const answer: Message = Object.freeze({ id: 'work:session:7', role: 'assistant', content: 'answer',
  reasoning_content: '  saved thought\n\nwith exact whitespace  ', timestamp: 123, source_ref: source });

test('derived reasoning preserves source identity and original messages without mutation', () => {
  const messages = [Object.freeze({ id: 'user', role: 'user', content: 'question' }), answer];
  Object.freeze(messages);
  const rows = expandRestoredReasoning(messages, true);
  assert.equal(messages.length, 2); assert.equal(rows.length, 3);
  assert.equal(rows[0], messages[0]); assert.equal(rows[2], answer);
  assert.deepEqual(rows[1], { id: 'reasoning:work:session:7', role: 'reasoning', content: answer.reasoning_content,
    timestamp: 123, source_ref: source });
  assert.equal(rows[1].source_ref, source);
  assert.equal(expandRestoredReasoning(messages, false), messages);
});

test('live and legacy reasoning rows are retained verbatim without creating duplicate rows', () => {
  const messages: Message[] = [
    { id: 'live', role: 'reasoning', content: 'streamed thought', stream_id: 'step' },
    { id: 'legacy', role: 'reasoning', content: 'legacy thought', source_ref: source },
    { id: 'body', role: 'assistant', content: 'reply' },
    { id: 'empty', role: 'assistant', content: 'reply', reasoning_content: ' \n ' },
    { id: 'user', role: 'user', content: 'question', reasoning_content: 'not an assistant' },
  ];
  assert.equal(expandRestoredReasoning(messages, true), messages);
  assert.equal(expandRestoredReasoning(messages, false), messages);
});

test('prepended pages keep body navigation ids and measured reasoning heights stable', () => {
  const page = [answer];
  const first = expandRestoredReasoning(page, true);
  const next = expandRestoredReasoning([{ id: 'earlier', role: 'assistant', content: '', reasoning_content: 'earlier thought' }, ...page], true);
  assert.deepEqual(next.slice(-2), first);
  const sizes = new Map([[first[0].id, 44], [answer.id, 190]]);
  const offsets = messageOffsets(next, sizes);
  const target = next.findIndex(m => m.id === answer.id);
  assert.equal(offsets[target + 1] - offsets[target], 190);
  assert.equal(offsets[target] - offsets[target - 1], 44);
  const visible = windowRange(offsets, offsets[target], 300);
  assert.ok(visible.start <= target && visible.end > target);
  assert.equal(page.length, 1); assert.equal(page[0].source_ref?.index, 7);
});

test('a restored reasoning row uses the existing collapsed renderer and never gets regeneration controls', () => {
  const row = expandRestoredReasoning([answer], true)[0];
  const html = renderToStaticMarkup(React.createElement(MessageRow, { message: row, timeline: true, reasoning: true,
    expanded: new Map(), fail() {}, openLog() {}, responses: { versions: { revision: 1, active_branch: 'main', branch_id: 'main', choices: [], groups: [], targets: [{ source_ref: source }] },
      busy: false, regenerate() {}, select() {} } }));
  assert.match(html, /<details class="reasoning">/);
  assert.match(html, /推理 \/ 摘要/);
  assert.match(html, /saved thought/);
  assert.match(html, /查看消息记录/);
  assert.doesNotMatch(html, /重新生成回复|data-response-group-id| open=""/);
});
