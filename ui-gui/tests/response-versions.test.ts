import { test } from 'node:test';
import assert from 'node:assert/strict';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { ResponseVersionNav, EarlierResponseVersions, responseForMessage, type ResponseControls } from '../src/renderer/response-versions.js';
import { MessageRow } from '../src/renderer/messages.js';
import type { ResponseGroup } from '@astra/ui-core/response-versions';
const source = { index: 1, digest: 'a'.repeat(64) };
const message = { id: 'answer', role: 'assistant', content: 'answer A', source_ref: source };
const group: ResponseGroup = { id: 'g1', selected_version: 'a', user_text: 'question', response_text: 'answer A', source_ref: source,
  versions: [{ id: 'a', branch_id: 'a', number: 1, status: 'completed' }, { id: 'b', branch_id: 'b', number: 2, status: 'completed' }, { id: 'failed', branch_id: 'failed', number: 3, status: 'failed' }] };
const controls: ResponseControls = { versions: { revision: 1, branch_id: 'a', active_branch: 'a', choices: [], groups: [group], targets: [{ source_ref: source }] }, busy: false, regenerate() {}, select() {} };
test('navigation selects only completed versions and has bounded arrow states', () => {
  let html = renderToStaticMarkup(React.createElement(ResponseVersionNav, { group, busy: false, select() {} }));
  assert.match(html, /1 \/ 2/); assert.match(html, /aria-label="上一个回复版本" disabled/); assert.doesNotMatch(html, /aria-label="下一个回复版本" disabled/);
  html = renderToStaticMarkup(React.createElement(ResponseVersionNav, { group: { ...group, selected_version: 'b' }, busy: true, select() {} }));
  assert.match(html, /2 \/ 2/); assert.match(html, /aria-label="上一个回复版本" disabled/); assert.match(html, /aria-label="下一个回复版本" disabled/);
});
test('pending body overlays only its exact source, hides old log link, and restores on failure', () => {
  const pending: ResponseControls = { ...controls, busy: true, regeneration: { request_id: 'request', source_ref: source, status: 'running', content: 'answer B' } };
  assert.equal(responseForMessage(message, pending).content, 'answer B');
  assert.equal(responseForMessage({ ...message, source_ref: { ...source, digest: 'b'.repeat(64) } }, pending).content, 'answer A');
  const row = (responses: ResponseControls) => renderToStaticMarkup(React.createElement(MessageRow, { message, responses, timeline: false, reasoning: false, expanded: new Map(), fail() {}, openLog() {} }));
  const html = row(pending); assert.match(html, /data-response-pending="true"/); assert.match(html, /answer B/); assert.doesNotMatch(html, /answer A|查看消息记录/);
  assert.match(html, /aria-label="重新生成回复"[^>]*disabled/);
  assert.match(row({ ...pending, regeneration: { ...pending.regeneration!, content: '' } }), /正在重新生成回复/);
  const restored = row(controls); assert.match(restored, /answer A/); assert.match(restored, /查看消息记录/); assert.doesNotMatch(restored, /data-response-pending/);
});
test('compacted groups retain an explicit version entry while visible groups are not duplicated', () => {
  const render = (messages: typeof message[]) => renderToStaticMarkup(React.createElement(EarlierResponseVersions, { messages, controls }));
  assert.equal(render([message]), '');
  const html = render([]); assert.match(html, /较早回复版本/); assert.match(html, /data-response-group="g1"/); assert.match(html, /question/);
  assert.doesNotMatch(renderToStaticMarkup(React.createElement(MessageRow, { message, timeline: false, reasoning: false, expanded: new Map(), fail() {} })), /重新生成回复/);
});
