import assert from 'node:assert/strict';
import test from 'node:test';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { Approval } from '../src/renderer/controls.js';
import { approvalText } from '../src/renderer/approval-preview.js';

const base = { type: 'tool_approval_request', request_id: 'approval', risk: 'execute', kind: 'host_execution',
  reason: '这是系统事实', agent_reason: '这是模型说明', scope: 'host-execution:exact-hash',
  approval_effect: '会读取或修改宿主机文件与进程', approval_boundary: '仅此精确调用', choices: ['once', 'deny'] };
const render = (event: object) => renderToStaticMarkup(React.createElement(Approval, { event: { ...base, ...event }, send: async () => {} }));

test('normal Python and shell approval summaries never replace the operation or its authoritative boundary', () => {
  const python = render({ tool_name: 'execute_python', approval_title: '运行 Python', approval_summary: '运行 39 字符的 Python 程序',
    target: '/tmp/project', arguments: { code: 'if enabled:\n    print("EXACT_CODE")', background: 'false' } });
  assert.match(python, /代码预览.*if enabled:\n    print\(&quot;EXACT_CODE&quot;\)/s);
  for (const value of ['运行 39 字符', '模型说明：这是模型说明', '/tmp/project', '会读取或修改宿主机文件与进程', '仅此精确调用', '执行', 'host-execution:exact-hash']) assert.ok(python.includes(value), value);
  const shell = render({ tool_name: 'execute_shell', approval_summary: '检查工作区', target: 'git status --short && rg "sentinel" .',
    arguments: { command: 'git status --short && rg "sentinel" .', environment: 'posix' }, compound_command_count: 2,
    compound_commands: ['git status --short', 'rg "sentinel" .'], session_scope_label: 'git status' });
  assert.match(shell, /命令预览.*git status --short &amp;&amp; rg &quot;sentinel&quot; \./s);
  assert.match(shell, /会话授权前缀.*git status/s);
  assert.match(shell, /操作数.*2 项/s);
});

test('approval preview discloses backend and renderer truncation and makes spoofing controls inert', () => {
  const html = render({ tool_name: 'execute_python', approval_title: 'title\u202Espoof',
    arguments: { code: '[代码已截断：省略 800 字符] head … tail\u001b[31m\u202E<script>x</script>' } });
  assert.match(html, /代码已截断：省略 800 字符/);
  assert.match(html, /\\x1b\[31m\\u\{202E\}&lt;script&gt;x&lt;\/script&gt;/);
  assert.ok(!html.includes('\u001b') && !html.includes('\u202e'));
  assert.ok(!html.includes('<script>'));
  const long = approvalText('a'.repeat(25_000));
  assert.match(long, /省略 1000 字符/);
  assert.ok(long.length < 24_100);
  assert.equal(approvalText('first\n  second\tvalue'), 'first\n  second\\tvalue');
});

test('filesystem and takeover approvals retain the actual targets and all supplied arguments', () => {
  const files = render({ tool_name: 'apply_patch', kind: 'filesystem_patch', approval_summary: '修改两个文件',
    targets: ['/project/a.txt', '/project/b.txt'], outside_workspace: true, arguments: { patch: 'first\nsecond' },
    approval_boundary: '仅本批次路径' });
  assert.match(files, /\/project\/a.txt\n\/project\/b.txt/);
  assert.match(files, /工作区外宿主机/);
  assert.match(files, /仅本批次路径/);
  const takeover = render({ tool_name: 'computer', kind: 'computer_foreground_takeover', arguments: { application: 'Editor', window: 'draft', action_classes: ['text'], expected_effect: 'type text' } });
  for (const field of ['Editor', 'draft', 'text', 'type text']) assert.ok(takeover.includes(field));
});

test("a request from a chat's own conversation says which chat asks", () => {
  assert.match(render({ channel: "weixin" }), /需要你的许可：来自微信的对话/);
  assert.doesNotMatch(render({}), /来自/);
});
