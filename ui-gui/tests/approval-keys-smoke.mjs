/** Keyboard answers to tool approvals in the desktop: real Electron/Python, a loopback model, real file writes. */
import { _electron as electron } from 'playwright';
import { expect } from '@playwright/test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { existsSync, mkdtempSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(fileURLToPath(new URL('../..', import.meta.url)));
const folder = mkdtempSync(join(tmpdir(), 'astra-approval-keys-'));
const workspace = join(folder, 'workspace'); mkdirSync(workspace);
const output = join(root, 'output/playwright/approval-keys'); mkdirSync(output, { recursive: true });
const written = word => { const path = join(workspace, `${word}.txt`); return existsSync(path) ? readFileSync(path, 'utf8') : null; };

// Every "写入 <word>" asks to write that word to <word>.txt; the tool's result ends the turn.
const requests = [];
const model = createServer(async (req, res) => {
  if (req.method === 'GET') { res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify({ data: [{ id: 'approval-test' }] })); return; }
  if (!req.url.endsWith('/chat/completions')) { res.writeHead(404); res.end(); return; }
  let body = ''; for await (const part of req) body += part;
  const payload = JSON.parse(body); requests.push(payload);
  const lastUser = payload.messages.findLastIndex(message => message.role === 'user');
  const text = JSON.stringify(payload.messages[lastUser]?.content ?? '');
  const answered = payload.messages.slice(lastUser + 1).some(message => message.role === 'tool');
  const wanted = /写入 ([a-z]+)/.exec(text);
  res.setHeader('Content-Type', 'text/event-stream');
  const chunk = (delta, finish_reason = null) => res.write('data: ' + JSON.stringify({ id: 'approval-' + requests.length, object: 'chat.completion.chunk', created: Math.floor(Date.now() / 1000), model: 'approval-test', choices: [{ index: 0, delta, finish_reason }] }) + '\n\n');
  if (wanted && !answered) {
    chunk({ role: 'assistant', tool_calls: [{ index: 0, id: 'write-' + requests.length, type: 'function', function: { name: 'write_file', arguments: JSON.stringify({ path: join(workspace, `${wanted[1]}.txt`), content: wanted[1] }) } }] });
    chunk({}, 'tool_calls');
  } else { chunk({ role: 'assistant', content: `TURN_DONE_${wanted?.[1] ?? 'plain'}` }); chunk({}, 'stop'); }
  res.end('data: [DONE]\n\n');
});
const port = await new Promise(done => model.listen(0, '127.0.0.1', () => done(model.address().port)));
writeFileSync(join(folder, '.env'), '');
writeFileSync(join(folder, 'settings.json'), JSON.stringify({ selected_model: 'approval-test' }));
writeFileSync(join(folder, 'models.yaml'), `version: 1\nproviders:\n  test:\n    label: Local fixture\n    provider: openai-compatible\n    base_url: http://127.0.0.1:${port}/v1\n    api_key_env: ''\n    context_limit: 131072\n    capabilities: [tools, streaming]\nmodels:\n  approval-test:\n    provider: openai-compatible\n    base_url: http://127.0.0.1:${port}/v1\n    api_key_env: ''\n    context_limit: 131072\n    capabilities: [tools, streaming]\n`);
const cleanEnv = Object.fromEntries(Object.entries(process.env).filter(([key]) => !/API_KEY|TOKEN|SECRET|^AGENT_|^ASTRA_|^LLM_|^SANDBOX_|^ELECTRON_/.test(key)));
const env = { ...cleanEnv, ASTRA_GUI_DISABLE_APPSHOT: '1', AGENT_PROJECT_ROOT: root, AGENT_PYTHON: process.env.AGENT_PYTHON || join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python'), ASTRA_ENV_FILE: join(folder, '.env'), ASTRA_HOME: folder, AGENT_SETTINGS_PATH: join(folder, 'settings.json'), AGENT_SESSION_DIR: join(folder, 'sessions'), AGENT_TASK_DB: join(folder, 'tasks.db'), ASTRA_APPROVAL_DB: join(folder, 'approvals.db'), ASTRA_EVENT_DB: join(folder, 'events.db'), AGENT_MEMORY_PATH: join(folder, 'memory.db'), AGENT_LEARNING_PATH: join(folder, 'learning.db'), AGENT_SKILLS_PATH: join(folder, 'skills'), AGENT_MODELS_FILE: join(folder, 'models.yaml'), AGENT_USER_MODELS_FILE: join(folder, 'missing-models.yaml'), AGENT_LOG_DIR: join(folder, 'logs'), AGENT_MCP_CONFIG: join(folder, 'missing-mcp.json'), AGENT_TOOL_POLICY: 'locked', SANDBOX_DOCKER: 'false', SANDBOX_WORKDIR: workspace, ASTRA_WORKSPACE: workspace, LEARNING_REVIEW_AUTO: '0' };
const runFile = promisify(execFile);
async function bounded(promise, label, timeout) {
  let timer;
  try { return await Promise.race([promise, new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(`${label} timed out after ${timeout} ms`)), timeout); })]); }
  finally { clearTimeout(timer); }
}
async function terminateFixture(app) {
  const child = app.process();
  if (child.exitCode !== null || child.signalCode !== null) return;
  const pid = child.pid;
  assert.ok(Number.isSafeInteger(pid) && pid > 0, 'only terminate this fixture Electron PID');
  if (process.platform === 'win32') { await runFile('taskkill.exe', ['/PID', String(pid), '/T', '/F'], { timeout: 5000, windowsHide: true }); return; }
  const { stdout } = await runFile('ps', ['-axo', 'pid=,ppid='], { timeout: 3000 });
  const rows = stdout.trim().split('\n').map(line => line.trim().split(/\s+/).map(Number));
  const owned = new Set([pid]);
  for (let changed = true; changed;) { changed = false; for (const [id, parent] of rows) if (owned.has(parent) && !owned.has(id)) { owned.add(id); changed = true; } }
  for (const id of [...owned].reverse()) { try { process.kill(id, 'SIGKILL'); } catch (error) { if (error.code !== 'ESRCH') throw error; } }
}

let app, failure;
const errors = [], externalRequests = [], checks = [];
const passed = name => { checks.push(name); console.log('PASS', name); };
try {
  app = await electron.launch({ args: [join(root, 'ui-gui')], env, timeout: 30000 });
  const page = await app.firstWindow(); page.setDefaultTimeout(30000);
  page.on('pageerror', error => errors.push(error.message));
  page.on('request', request => { if (/^https?:/i.test(request.url())) externalRequests.push(request.url()); });
  await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].setContentSize(1320, 900));
  await page.waitForFunction(() => innerWidth === 1320);
  console.log('START', folder);
  // What the backend says it was told, in order. The files show what that allowed.
  await page.evaluate(() => { window.__decisions = []; window.astra.onEvents(events => { for (const item of events) if (item.event?.type === 'approval_resolved') window.__decisions.push(item.event.decision); }); });
  const decisions = () => page.evaluate(() => window.__decisions);
  const command = process.platform === 'darwin' ? 'Meta' : 'Control';
  const composer = page.getByRole('textbox', { name: '消息', exact: true });
  const card = page.locator('.approval');
  const state = async () => page.evaluate(async () => { const boot = await window.astra.bootstrap(); return boot.sessions.find(item => item.id === boot.active); });
  const idle = async () => expect.poll(async () => { const current = await state(); return current?.status === 'ready' && !current.busy && !current.approvals.length; }, { timeout: 30000 }).toBe(true);
  const ask = async text => {
    await composer.fill(text); await page.getByRole('button', { name: '发送', exact: true }).click();
    await expect(card).toHaveCount(1); await expect(card.getByRole('button', { name: '允许一次', exact: true })).toBeEnabled();
    await expect(composer).toHaveValue('');
  };
  const done = async word => { await expect(page.getByText(`TURN_DONE_${word}`, { exact: true })).toBeVisible(); await idle(); };
  const waiting = async () => { await page.waitForTimeout(400); await expect(card).toHaveCount(1); assert.equal((await state()).approvals.length, 1); };

  // Nothing typed is taken for an answer, wherever the text goes, and the keys keep their own meaning in a field with text.
  await ask('写入 one');
  await expect(card.locator('.approval-keys')).toHaveText(`${command === 'Meta' ? '⌘' : 'Ctrl'} Enter 允许一次 · ${command === 'Meta' ? '⌘' : 'Ctrl'} Shift Enter 本会话允许 · ${command === 'Meta' ? '⌘ ⌫' : 'Ctrl Backspace'} 拒绝 · 点选卡片后可按 Y / A / N`);
  assert.equal(await card.evaluate(node => node.contains(document.activeElement)), false, 'the card does not take the focus when it appears');
  await composer.click(); await page.keyboard.type('yan YAN');
  await expect(composer).toHaveValue('yan YAN');
  await page.keyboard.press(`${command}+Backspace`);
  await waiting();
  const search = page.getByRole('textbox', { name: '搜索会话', exact: true });
  await search.fill('草稿'); await page.keyboard.press(`${command}+Enter`); await page.keyboard.press(`${command}+Shift+Enter`);
  await waiting();
  await search.fill(''); await composer.fill('');
  // A dialog covers the card: its keys are off until the dialog closes.
  await page.getByRole('button', { name: /Astra 本地工作区/ }).click();
  await expect(page.getByRole('dialog', { name: '设置', exact: true })).toBeVisible();
  await page.keyboard.press(`${command}+Enter`); await page.keyboard.press(`${command}+Backspace`);
  await waiting();
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog')).toHaveCount(0);
  assert.deepEqual([await decisions(), written('one')], [[], null]);
  await page.screenshot({ path: join(output, 'pending.png') });
  // The card and its line of keys fit the smallest window the app allows.
  const resize = async (width, height) => { await app.evaluate(({ BrowserWindow }, size) => BrowserWindow.getAllWindows()[0].setContentSize(...size), [width, height]); await page.waitForFunction(width => innerWidth === width, width); };
  await resize(850, 600);
  assert.deepEqual(await page.evaluate(() => { const card = document.querySelector('.approval'), keys = card.querySelector('.approval-keys');
    return [card.scrollWidth <= card.clientWidth, keys.scrollWidth <= keys.clientWidth, document.documentElement.scrollWidth <= innerWidth]; }), [true, true, true]);
  await card.scrollIntoViewIfNeeded(); await page.screenshot({ path: join(output, 'pending-narrow.png') });
  await resize(1320, 900);
  passed('typed text, keys in a field that holds text and keys under a dialog leave the approval pending and the file unwritten');

  await composer.click(); await page.keyboard.press(`${command}+Enter`);
  await done('one');
  assert.deepEqual([await decisions(), written('one')], [['once'], 'one']);
  passed('Cmd/Ctrl+Enter in the empty composer allows once and the tool writes the file');

  await ask('写入 two');
  await page.evaluate(() => document.activeElement?.blur());  // no field has the focus
  assert.equal(await page.evaluate(() => document.activeElement === document.body), true);
  await page.keyboard.press(`${command}+Backspace`);
  await done('two');
  assert.deepEqual([(await decisions()).at(-1), written('two')], ['deny', null]);
  await ask('写入 three');
  await composer.click(); await page.keyboard.press(`${command}+Shift+Enter`);
  await done('three');
  assert.deepEqual([(await decisions()).at(-1), written('three')], ['session', 'three']);
  passed('Cmd/Ctrl+Backspace denies and nothing is written; Cmd/Ctrl+Shift+Enter allows for the session');

  // Inside the card the terminal's keys work. Clicking its text focuses it; no key is needed to get there.
  await ask('写入 four');
  await card.locator('h3').click();
  assert.equal(await card.evaluate(node => node === document.activeElement), true);
  await page.screenshot({ path: join(output, 'focused.png') });
  await page.keyboard.press('y');
  await done('four');
  assert.deepEqual([(await decisions()).at(-1), written('four')], ['once', 'four']);
  await ask('写入 five');
  await card.locator('h3').click(); await page.keyboard.press('a');
  await done('five');
  assert.deepEqual([(await decisions()).at(-1), written('five')], ['session', 'five']);
  await ask('写入 six');
  await card.locator('h3').click(); await page.keyboard.press('n');
  await done('six');
  assert.deepEqual([(await decisions()).at(-1), written('six')], ['deny', null]);
  passed('in the focused card Y allows once, A allows for the session and N denies');

  // Escape in the card denies and does not also close the open side panel.
  await ask('写入 seven');
  await page.getByRole('button', { name: '执行详情', exact: true }).click();
  const details = page.locator('.details-panel');
  await expect(details).toBeVisible();
  await card.locator('h3').click(); await page.keyboard.press('Escape');
  await done('seven');
  assert.deepEqual([(await decisions()).at(-1), written('seven')], ['deny', null]);
  await expect(details).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(details).toHaveCount(0);
  // Enter on a focused button presses that button, not the default answer.
  await ask('写入 eight');
  await card.getByRole('button', { name: '拒绝', exact: true }).focus(); await page.keyboard.press('Enter');
  await done('eight');
  assert.deepEqual([(await decisions()).at(-1), written('eight')], ['deny', null]);
  // With the card itself focused, as Tab leaves it, Enter gives the default answer.
  await ask('写入 nine');
  await card.focus(); await page.keyboard.press('Enter');
  await done('nine');
  assert.deepEqual([(await decisions()).at(-1), written('nine')], ['once', 'nine']);
  passed('Escape denies and leaves the side panel open, Enter on 拒绝 denies, and Enter on the card allows once');

  // A held key answers one request, not the next one too.
  await ask('写入 ten');
  await composer.click(); await page.keyboard.down(command); await page.keyboard.down('Enter');
  await done('ten');
  assert.equal(written('ten'), 'ten');
  await composer.fill('写入 eleven'); await page.getByRole('button', { name: '发送', exact: true }).click();
  await expect(card).toHaveCount(1); await expect(card.getByRole('button', { name: '允许一次', exact: true })).toBeEnabled();
  await composer.click();
  await page.keyboard.down('Enter'); await page.keyboard.down('Enter');  // still held: these are repeats
  await waiting();
  await page.keyboard.up('Enter'); await page.keyboard.up(command);
  assert.equal(written('eleven'), null);
  await composer.click(); await page.keyboard.press(`${command}+Backspace`);
  await done('eleven');
  assert.equal(written('eleven'), null);
  passed('a key still held when the next approval arrives does not answer it');

  // A request the user cannot see is not answered: the first press brings it into view.
  await ask('写入 twelve');
  const scroller = page.locator('.messages-scroll');
  const inView = () => page.evaluate(() => { const row = document.querySelector('.approval .actions').getBoundingClientRect(), view = document.querySelector('.messages-scroll').getBoundingClientRect(); return row.top >= view.top && row.bottom <= view.bottom; });
  await scroller.evaluate(node => { node.scrollTop = 0; });
  await expect.poll(inView).toBe(false);
  await composer.click(); await page.keyboard.press(`${command}+Enter`);
  await expect.poll(inView).toBe(true);
  await waiting();
  assert.equal(written('twelve'), null);
  await page.keyboard.press(`${command}+Enter`);
  await done('twelve');
  assert.equal(written('twelve'), 'twelve');
  passed('a request scrolled out of sight is brought into view by the first press and answered by the second');
  assert.deepEqual(await decisions(), ['once', 'deny', 'session', 'once', 'session', 'deny', 'deny', 'deny', 'once', 'once', 'deny', 'once']);
  assert.deepEqual(errors, []); assert.deepEqual(externalRequests, []);
} catch (error) {
  failure = error; console.error(error);
  if (app) await bounded((async () => {
    const page = await app.firstWindow();
    console.error((await page.locator('body').innerText()).slice(-2500));
    console.error('DECISIONS', JSON.stringify(await page.evaluate(() => window.__decisions)));
    console.error('TOOL_RESULTS', JSON.stringify(requests.at(-1)?.messages.filter(message => message.role === 'tool').map(message => String(message.content).slice(0, 160))));
    console.error('STATE', JSON.stringify(await page.evaluate(async () => { const boot = await window.astra.bootstrap(); return boot.sessions.map(state => ({ id: state.id, busy: state.busy, status: state.status, approvals: state.approvals.map(approval => [approval.request_id, approval.choices]) })); })));
    await page.screenshot({ path: join(output, 'failure.png'), timeout: 2000 });
  })(), 'Failure diagnostics', 3000).catch(error => console.error(error));
} finally {
  if (app) {
    try { await bounded(app.close(), 'Electron close', 30000); }
    catch (error) { failure ||= error; console.error(error); await bounded(terminateFixture(app), 'Fixture process cleanup', 6000).catch(error => { failure ||= error; console.error(error); }); }
  }
  await bounded(new Promise((done, reject) => { model.close(error => error ? reject(error) : done()); model.closeAllConnections(); }), 'Fixture HTTP close', 3000).catch(error => { failure ||= error; console.error(error); });
}
writeFileSync(join(output, 'approval-keys-result.json'), JSON.stringify({ checks, errors, externalRequests, folder, success: !failure, ...(failure ? { failure: String(failure) } : {}) }, null, 2));
if (failure) throw failure;
console.log(JSON.stringify({ checks: checks.length, output, folder }));
