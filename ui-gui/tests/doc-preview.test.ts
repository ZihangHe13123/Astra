import {test} from 'node:test';
import assert from 'node:assert/strict';
import {dirname, docPathFromTool, isMarkdownPath, latestDoc, openComments, resolveAgainst} from '../src/renderer/doc-preview.js';

const result = (name: string, output: unknown, extra: Record<string, unknown> = {}) => ({name, output: JSON.stringify(output), ...extra});

test('doc tool results point at the Markdown document, including the export source', () => {
 assert.equal(docPathFromTool(result('doc_write_section', {path: '/w/notes/plan.md'})), '/w/notes/plan.md');
 assert.equal(docPathFromTool(result('doc_export', {path: '/w/plan.docx', source: '/w/plan.md'})), '/w/plan.md');
 assert.equal(docPathFromTool(result('doc_write_section', {path: '/w/plan.md'}, {error: 'section_changed'})), undefined);
 assert.equal(docPathFromTool(result('write_file', {path: '/w/plan.md'})), undefined);
 assert.equal(docPathFromTool(result('doc_outline', {path: '/w/plan.txt'})), undefined);
 assert.equal(docPathFromTool({name: 'doc_outline', output: 'not json'}), undefined);
 assert.ok(isMarkdownPath('A.MARKDOWN') && !isMarkdownPath('a.mdx'));
});

test('the newest live document wins and restored history is ignored', () => {
 const tools = [
  result('doc_create', {path: '/w/a.md'}, {result_index: 1}),
  result('doc_write_section', {path: '/w/b.md'}, {result_index: 3}),
  result('doc_write_section', {path: '/w/old.md'}, {result_index: 9, historical: true}),
  result('read_file', {path: '/w/c.md'}, {result_index: 4}),
 ];
 assert.deepEqual(latestDoc(tools), {path: '/w/b.md', index: 3});
 assert.equal(latestDoc([tools[2]]), undefined);
});

test('relative targets resolve against the document folder on both platforms', () => {
 assert.equal(dirname('/w/notes/plan.md'), '/w/notes');
 assert.equal(dirname('C:\\w\\plan.md'), 'C:\\w');
 assert.equal(dirname('plan.md'), '');
 assert.equal(resolveAgainst('/w/notes', 'figures/chart.png'), '/w/notes/figures/chart.png');
 assert.equal(resolveAgainst('/w/notes', '../shared/a%20b.png'), '/w/notes/../shared/a b.png');
 assert.equal(resolveAgainst('C:\\w', 'fig.png'), 'C:\\w\\fig.png');
 for (const target of ['/abs.png', 'C:/x.png', 'https://e.com/x.png', '#section', 'mailto:a@b.c'])
  assert.equal(resolveAgainst('/w/notes', target), target);
 assert.equal(resolveAgainst(undefined, 'fig.png'), 'fig.png');
});

test('open review comments are listed outside fenced code only', () => {
 const text = [
  '# T', 'Text <!-- @astra: tighten this --> more.', '<!-- astra:section id="a" -->', '## A',
  '```md', '<!-- @astra: not a comment -->', '```', '<!-- @astra:', '  spans lines', '-->',
 ].join('\n');
 assert.deepEqual(openComments(text), ['tighten this', 'spans lines']);
});
