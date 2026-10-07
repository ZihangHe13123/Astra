/** Spoken replies in the desktop: real Electron/Python, a loopback model and a loopback speech endpoint, no audio device. */
import { _electron as electron } from 'playwright';
import { expect } from '@playwright/test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { mkdtempSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(fileURLToPath(new URL('../..', import.meta.url)));
const folder = mkdtempSync(join(tmpdir(), 'astra-voice-'));
const workspace = join(folder, 'workspace'); mkdirSync(workspace);
const output = join(root, 'output/playwright/voice'); mkdirSync(output, { recursive: true });
const reply = '第一句先说。第二句完整地读出来，好吗？';
const longVoice = '一个名字特别长的音色 with a long English name';
const listen = server => new Promise(done => server.listen(0, '127.0.0.1', () => done(server.address().port)));

const modelRequests = [];
const model = createServer(async (req, res) => {
  if (req.method === 'GET') { res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify({ data: [{ id: 'voice-test' }] })); return; }
  if (!req.url.endsWith('/chat/completions')) { res.writeHead(404); res.end(); return; }
  let body = ''; for await (const part of req) body += part;
  modelRequests.push(JSON.parse(body));
  res.setHeader('Content-Type', 'text/event-stream');
  const chunk = (delta, finish_reason = null) => res.write('data: ' + JSON.stringify({ id: 'voice-' + modelRequests.length, object: 'chat.completion.chunk', created: Math.floor(Date.now() / 1000), model: 'voice-test', choices: [{ index: 0, delta, finish_reason }] }) + '\n\n');
  chunk({ role: 'assistant', content: reply }); chunk({}, 'stop');
  res.end('data: [DONE]\n\n');
});
// The speech endpoint answers as told: at once, held open until released, or with a failure.
const speech = { mode: 'ok', requests: [], dropped: 0, waiting: [] };
speech.release = () => { for (const end of speech.waiting.splice(0)) end(); };
const speechServer = createServer(async (req, res) => {
  if (req.method !== 'POST' || !req.url.endsWith('/v1/audio/speech')) { res.writeHead(404); res.end(); return; }
  let body = ''; for await (const part of req) body += part;
  const payload = JSON.parse(body); speech.requests.push(payload);
  if (speech.mode === 'fail') { res.writeHead(503, { 'Content-Type': 'application/json' }); res.end(JSON.stringify({ error: { message: 'FIXTURE_SPEECH_DOWN' } })); return; }
  assert.equal(payload.response_format, 'pcm');
  res.writeHead(200, { 'Content-Type': 'audio/pcm' }); res.write(Buffer.alloc(4800));
  res.on('close', () => { if (!res.writableEnded) speech.dropped++; });
  if (speech.mode === 'hold') await new Promise(done => speech.waiting.push(done));
  if (!res.destroyed) res.end();
});
const [modelPort, speechPort] = [await listen(model), await listen(speechServer)];
const settingsPath = join(folder, 'settings.json');
const savedVoice = () => JSON.parse(readFileSync(settingsPath, 'utf8')).voice;
writeFileSync(join(folder, '.env'), '');
writeFileSync(settingsPath, JSON.stringify({ selected_model: 'voice-test', voice: { enabled: false, base_url: `http://127.0.0.1:${speechPort}/v1`, selected: '温暖搭档',
  voices: { '温暖搭档': { voice: 'warm' }, 'Night Owl': { voice: 'owl' }, [longVoice]: { voice: 'long' } } } }));
writeFileSync(join(folder, 'models.yaml'), `version: 1\nproviders:\n  test:\n    label: Local fixture\n    provider: openai-compatible\n    base_url: http://127.0.0.1:${modelPort}/v1\n    api_key_env: ''\n    context_limit: 131072\n    capabilities: [tools, streaming]\nmodels:\n  voice-test:\n    provider: openai-compatible\n    base_url: http://127.0.0.1:${modelPort}/v1\n    api_key_env: ''\n    context_limit: 131072\n    capabilities: [tools, streaming]\n`);
const cleanEnv = Object.fromEntries(Object.entries(process.env).filter(([key]) => !/API_KEY|TOKEN|SECRET|^AGENT_|^ASTRA_|^LLM_|^SANDBOX_|^ELECTRON_/.test(key)));
const env = { ...cleanEnv, ASTRA_GUI_DISABLE_APPSHOT: '1', ASTRA_VOICE_PLAYER: 'null', AGENT_PROJECT_ROOT: root, AGENT_PYTHON: process.env.AGENT_PYTHON || join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python'), ASTRA_ENV_FILE: join(folder, '.env'), ASTRA_HOME: folder, AGENT_SETTINGS_PATH: settingsPath, AGENT_SESSION_DIR: join(folder, 'sessions'), AGENT_TASK_DB: join(folder, 'tasks.db'), ASTRA_APPROVAL_DB: join(folder, 'approvals.db'), ASTRA_EVENT_DB: join(folder, 'events.db'), AGENT_MEMORY_PATH: join(folder, 'memory.db'), AGENT_LEARNING_PATH: join(folder, 'learning.db'), AGENT_SKILLS_PATH: join(folder, 'skills'), AGENT_MODELS_FILE: join(folder, 'models.yaml'), AGENT_USER_MODELS_FILE: join(folder, 'missing-models.yaml'), AGENT_LOG_DIR: join(folder, 'logs'), AGENT_MCP_CONFIG: join(folder, 'missing-mcp.json'), AGENT_TOOL_POLICY: 'locked', SANDBOX_DOCKER: 'false', SANDBOX_WORKDIR: workspace, ASTRA_WORKSPACE: workspace, LEARNING_REVIEW_AUTO: '0' };
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
  const resize = async (width, height) => { await app.evaluate(({ BrowserWindow }, size) => BrowserWindow.getAllWindows()[0].setContentSize(...size), [width, height]); await page.waitForFunction(width => innerWidth === width, width); };
  await resize(1320, 820);
  console.log('START', folder);
  const composer = page.getByRole('textbox', { name: '消息', exact: true });
  const submit = async text => { await composer.fill(text); await page.getByRole('button', { name: '发送', exact: true }).click(); };
  const status = async () => page.evaluate(async () => { const boot = await window.astra.bootstrap(); return boot.sessions.find(item => item.id === boot.active)?.info.voice_status; });
  const panel = page.getByRole('dialog', { name: '语音朗读', exact: true });
  const chosen = () => panel.locator('button[aria-pressed="true"] strong').allTextContents();
  const control = page.locator('.composer-bottom .permission.voice');
  const line = page.locator('.connection-status').filter({ hasText: '朗读' });
  const toast = page.locator('.toast');
  const dismissToast = async () => { await toast.getByRole('button', { name: '关闭提示', exact: true }).click(); await expect(toast).toHaveCount(0); };
  const spoken = () => speech.requests.map(request => [request.voice, request.input]);

  // A blank page has no backend to ask, so it shows no voice control; the command opens one and the panel with it.
  await expect(control).toHaveCount(0);
  await submit('/voice');
  await expect(panel).toBeVisible();
  await expect.poll(chosen).toEqual(['关闭', '温暖搭档']);
  await expect(control).toHaveText('朗读已关闭');
  await expect(panel.getByRole('alert')).toHaveCount(0);
  assert.equal((await status()).configured, true);
  await expect(composer).toHaveValue('');
  passed('the /voice command opens the panel with the saved state: off, endpoint set, the selected voice marked');

  await panel.getByRole('button', { name: /^开启/ }).click();
  await expect.poll(chosen).toEqual(['开启', '温暖搭档']);
  await expect(control).toHaveText('朗读 · 温暖搭档');
  assert.equal(savedVoice().enabled, true);
  await panel.getByRole('button', { name: /^Night Owl/ }).click();
  await expect.poll(chosen).toEqual(['开启', 'Night Owl']);
  assert.equal(savedVoice().selected, 'Night Owl');
  passed('switching on and choosing a voice whose name has a space are saved by the backend and shown from its answer');

  speech.mode = 'hold';
  await panel.getByRole('button', { name: '试听当前音色', exact: true }).click();
  await expect(panel.getByRole('button', { name: '停止朗读', exact: true })).toBeVisible();
  await expect(panel.getByRole('status')).toHaveText('正在朗读');
  assert.deepEqual(spoken().map(([voice]) => voice), ['owl']);
  await expect(panel.getByRole('button', { name: '试听当前音色', exact: true })).toHaveCount(0);
  await panel.getByRole('button', { name: '停止朗读', exact: true }).click();
  await expect(panel.getByRole('button', { name: '试听当前音色', exact: true })).toBeFocused();
  await expect.poll(() => speech.dropped).toBe(1);
  // Stopping must not strand the keyboard outside the dialog.
  await page.keyboard.press('Escape');
  await expect(panel).toHaveCount(0);
  await expect(toast).toHaveCount(0);
  await expect(page.locator('.message')).toHaveCount(0);
  passed('a test line is spoken with the chosen voice and stops from the panel; the panel leaves no notice and no message behind');

  // Typed commands are answered once, in place; the answer reflects what the backend did, not what was typed.
  speech.mode = 'ok';
  await submit('/voice use 温暖');
  await expect(toast).toHaveText('音色已切换为 温暖搭档');
  await expect(control).toHaveText('朗读 · 温暖搭档');
  await dismissToast();
  await submit('/voice use nope');
  await expect(toast).toContainText('朗读：Unknown voice: nope');
  await expect(line).toHaveCount(0);
  await expect(control).toHaveText('朗读 · 温暖搭档');
  await dismissToast();
  passed('typed commands get one answer each: the resolved voice for a prefix, the backend\'s reason for an unknown name');

  // Slash help offers the voices by name; a long one travels quoted and fits a narrow window.
  await composer.fill('/voice ');
  const suggestions = page.getByRole('listbox', { name: '指令建议', exact: true });
  await expect(suggestions.getByRole('option').locator('strong')).toHaveText(['开启朗读', '关闭朗读', '停止朗读', '音色与设置', '试听', '温暖搭档', 'Night Owl', longVoice]);
  await suggestions.getByRole('option', { name: new RegExp(longVoice) }).click();
  await expect(composer).toHaveValue(`/voice use "${longVoice}"`);
  await composer.press('Enter');
  await expect(toast).toHaveText(`音色已切换为 ${longVoice}`);
  await dismissToast();
  const fits = async () => page.evaluate(() => {
    const box = selector => document.querySelector(selector).getBoundingClientRect();
    const row = box('.composer-bottom'), voice = box('.composer-bottom .permission.voice'), send = box('.composer-bottom .send');
    // A name too long for the control is cut at its end; the label still begins with what the control is.
    const first = document.createRange(), label = document.querySelector('.composer-bottom .permission.voice').firstChild;
    first.setStart(label, 0); first.setEnd(label, 1);
    return { inside: voice.left >= row.left && voice.right <= send.left && send.right <= row.right + 0.5, oneLine: voice.height < 40,
      start: first.getBoundingClientRect().left >= voice.left, scroll: document.documentElement.scrollWidth <= innerWidth };
  });
  assert.deepEqual(await fits(), { inside: true, oneLine: true, start: true, scroll: true });
  await page.screenshot({ path: join(output, 'composer-wide.png') });
  await resize(850, 600);  // the smallest window the app allows
  assert.deepEqual(await fits(), { inside: true, oneLine: true, start: true, scroll: true });
  await expect(control).toHaveAttribute('title', '语音朗读设置');
  await page.screenshot({ path: join(output, 'composer-narrow.png') });
  await control.click();
  await expect(panel).toBeVisible();
  await expect.poll(chosen).toEqual(['开启', longVoice]);
  assert.equal(await panel.evaluate(node => node.scrollWidth <= node.clientWidth), true);
  await page.screenshot({ path: join(output, 'panel-narrow.png') });
  await panel.getByRole('button', { name: /^温暖搭档/ }).click();
  await expect.poll(chosen).toEqual(['开启', '温暖搭档']);
  await page.keyboard.press('Escape');
  await resize(1320, 820);
  passed('slash help lists the voices; a long quoted name is selected, and the control and panel fit the smallest window');

  // A reply is read from its first sentence; stopping the speech leaves the reply itself complete.
  speech.mode = 'hold'; speech.requests.length = 0; speech.dropped = 0;
  await submit('你好');
  await expect(page.locator('.message.assistant')).toContainText(reply);
  await expect(line).toHaveText(/正在朗读回复。\s*停止朗读/);
  assert.deepEqual(spoken(), [['warm', '第一句先说。']]);
  await page.screenshot({ path: join(output, 'speaking.png') });
  await line.getByRole('button', { name: '停止朗读', exact: true }).click();
  await expect(line).toHaveCount(0);
  await expect.poll(() => speech.dropped).toBe(1);
  await expect(toast).toHaveCount(0);
  await expect(page.locator('.message.assistant')).toContainText(reply);
  await expect(page.locator('.message')).toHaveCount(2);
  assert.deepEqual(spoken(), [['warm', '第一句先说。']], 'nothing after the stop is synthesized');
  assert.equal(modelRequests.length, 1, 'voice commands and test lines are not turns');
  assert.equal(modelRequests[0].messages.filter(message => message.role === 'user').some(message => /\/voice|Unknown voice/.test(JSON.stringify(message.content))), false, 'voice commands and their answers never reach the model');
  passed('a reply is read with the selected voice; the line above the composer stops it and the reply stays whole');

  // Speech that fails by itself is reported above the composer until speech is asked for again.
  speech.mode = 'fail'; speech.requests.length = 0;
  await submit('再说一次');
  await expect(line).toContainText('朗读没有成功：Speech endpoint answered 503');
  await expect(page.locator('.message')).toHaveCount(4);
  await page.screenshot({ path: join(output, 'failed.png') });
  await line.getByRole('button', { name: '语音设置', exact: true }).click();
  await expect(panel).toBeVisible();
  await expect(line).toHaveCount(0);
  await panel.getByRole('button', { name: '试听当前音色', exact: true }).click();
  await expect(panel.getByRole('alert')).toContainText('Speech endpoint answered 503');
  speech.mode = 'ok';
  await panel.getByRole('button', { name: '试听当前音色', exact: true }).click();
  await expect(panel.getByRole('alert')).toHaveCount(0);
  await expect.poll(() => speech.requests.length).toBeGreaterThanOrEqual(3);
  await page.screenshot({ path: join(output, 'panel.png') });
  await panel.getByRole('button', { name: /^关闭\s*不朗读/ }).click();
  await expect.poll(chosen).toEqual(['关闭', '温暖搭档']);
  assert.equal(savedVoice().enabled, false);
  await page.keyboard.press('Escape');
  await expect(control).toHaveText('朗读已关闭');
  await expect(line).toHaveCount(0);
  passed('a failed reply is reported with the endpoint\'s answer and leads to the panel; a working test line clears it; off is saved');

  // The other ways in: the settings list and the command palette.
  await page.getByRole('button', { name: /Astra 本地工作区/ }).click();
  await page.getByRole('dialog', { name: '设置', exact: true }).getByRole('button', { name: /^语音朗读/ }).click();
  await expect(panel).toBeVisible();
  await page.keyboard.press('Escape');
  await page.keyboard.press(process.platform === 'darwin' ? 'Meta+K' : 'Control+K');
  await page.getByRole('combobox', { name: '搜索命令与功能', exact: true }).fill('朗读');
  await page.keyboard.press('Enter');
  await expect(panel).toBeVisible();
  await expect.poll(chosen).toEqual(['关闭', '温暖搭档']);
  await page.keyboard.press('Escape');
  await expect(toast).toHaveCount(0);
  passed('the settings list and the command palette both open the panel');
  assert.deepEqual(errors, []); assert.deepEqual(externalRequests, []);
} catch (error) {
  failure = error; console.error(error);
  if (app) await bounded((async () => {
    const page = await app.firstWindow();
    console.error((await page.locator('body').innerText()).slice(-3000));
    console.error('SPEECH', JSON.stringify({ mode: speech.mode, dropped: speech.dropped, requests: speech.requests.map(request => [request.voice, request.input]) }));
    console.error('STATE', JSON.stringify(await page.evaluate(async () => { const boot = await window.astra.bootstrap(); return boot.sessions.map(state => ({ id: state.id, busy: state.busy, status: state.status, voice: state.info.voice_status })); })));
    await page.screenshot({ path: join(output, 'failure.png'), timeout: 2000 });
  })(), 'Failure diagnostics', 3000).catch(error => console.error(error));
} finally {
  speech.release();
  if (app) {
    try { await bounded(app.close(), 'Electron close', 30000); }
    catch (error) { failure ||= error; console.error(error); await bounded(terminateFixture(app), 'Fixture process cleanup', 6000).catch(error => { failure ||= error; console.error(error); }); }
  }
  for (const server of [model, speechServer]) await bounded(new Promise((done, reject) => { server.close(error => error ? reject(error) : done()); server.closeAllConnections(); }), 'Fixture HTTP close', 3000).catch(error => { failure ||= error; console.error(error); });
}
writeFileSync(join(output, 'voice-result.json'), JSON.stringify({ checks, errors, externalRequests, folder, success: !failure, ...(failure ? { failure: String(failure) } : {}) }, null, 2));
if (failure) throw failure;
console.log(JSON.stringify({ checks: checks.length, output, folder }));
