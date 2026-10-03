/** New inspection UI acceptance: real Electron, real Python, isolated local model. */
import { _electron as electron } from 'playwright';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
const root = resolve(fileURLToPath(new URL('../..', import.meta.url)));
const folder = mkdtempSync(join(tmpdir(), 'astra-inspector-smoke-'));
const workspace = join(folder, 'workspace'); mkdirSync(workspace);
const sessionDir = join(folder, 'sessions'); mkdirSync(sessionDir);
const output = join(root, 'output/playwright/inspector'); mkdirSync(output, { recursive: true });
const target = join(workspace, 'receipt.txt'); writeFileSync(target, 'READ_ONLY_FIXTURE');
const oldSession = 'inspector-history';
const historical = { messages: Array.from({ length: 431 }, (_, i) => ({ role: i % 2 ? 'assistant' : 'user', content: `ARCHIVE_MESSAGE_${i}\n\n${'Historical context. '.repeat(25)}` })) };
const archivePath = join(sessionDir, `${oldSession}.json`); writeFileSync(archivePath, JSON.stringify(historical));
let releaseArguments, releaseBackground;
const argumentsGate = new Promise(resolve => { releaseArguments = resolve; });
const backgroundGate = new Promise(resolve => { releaseBackground = resolve; });
const requests = [];
const server = createServer(async (req, res) => {
  if (req.method === 'GET') { res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify({ data: [{ id: 'inspector-test' }] })); return; }
  if (!req.url.endsWith('/chat/completions')) { res.writeHead(404); res.end(); return; }
  let body = ''; for await (const part of req) body += part;
  const payload = JSON.parse(body); requests.push(payload);
  const lastUser = payload.messages.findLastIndex(message => message.role === 'user');
  const text = String(payload.messages[lastUser]?.content || '');
  const answered = payload.messages.slice(lastUser + 1).some(message => message.role === 'tool');
  res.setHeader('Content-Type', 'text/event-stream');
  const chunk = (delta, finish_reason = null) => res.write('data: ' + JSON.stringify({ id: 'inspector-' + requests.length, object: 'chat.completion.chunk', created: Math.floor(Date.now()/1000), model: 'inspector-test', choices: [{ index: 0, delta, finish_reason }] }) + '\n\n');
  if (text.includes('PREPARE_INSPECTOR') && !answered) {
    const args = JSON.stringify({ path: target });
    chunk({ role: 'assistant', tool_calls: [{ index: 0, id: 'inspector-call', type: 'function', function: { name: 'read_file', arguments: args.slice(0, -2) } }] });
    await argumentsGate;
    chunk({ tool_calls: [{ index: 0, function: { arguments: args.slice(-2) } }] }); chunk({}, 'tool_calls');
  } else if (text.includes('BACKGROUND_INSPECTOR')) {
    chunk({ role: 'assistant', content: 'BACKGROUND_STILL_RUNNING' }); await backgroundGate;
    chunk({ content: ' BACKGROUND_DONE' }); chunk({}, 'stop');
  } else {
    chunk({ role: 'assistant', content: answered ? 'INSPECTOR_DONE' : 'HELLO_INSPECTOR' }); chunk({}, 'stop');
  }
  res.end('data: [DONE]\n\n');
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const port = server.address().port;
writeFileSync(join(folder, '.env'), '');
writeFileSync(join(folder, 'settings.json'), JSON.stringify({ selected_model: 'inspector-test' }));
writeFileSync(join(folder, 'models.yaml'), `version: 1\nproviders:\n  test:\n    label: Local fixture\n    provider: openai-compatible\n    base_url: http://127.0.0.1:${port}/v1\n    api_key_env: ''\n    context_limit: 131072\n    capabilities: [tools, streaming]\nmodels:\n  inspector-test:\n    provider: openai-compatible\n    base_url: http://127.0.0.1:${port}/v1\n    api_key_env: ''\n    context_limit: 131072\n    capabilities: [tools, streaming]\n`);
const cleanEnv = Object.fromEntries(Object.entries(process.env).filter(([key]) => !/API_KEY|TOKEN|SECRET|^AGENT_|^ASTRA_|^LLM_|^SANDBOX_|^ELECTRON_/.test(key)));
const env = { ...cleanEnv, ASTRA_GUI_DISABLE_APPSHOT: '1', AGENT_PROJECT_ROOT: root, AGENT_PYTHON: process.env.AGENT_PYTHON || join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python'), ASTRA_ENV_FILE: join(folder, '.env'), ASTRA_HOME: folder, AGENT_SETTINGS_PATH: join(folder, 'settings.json'), AGENT_SESSION_DIR: sessionDir, AGENT_TASK_DB: join(folder, 'tasks.db'), ASTRA_APPROVAL_DB: join(folder, 'approvals.db'), ASTRA_EVENT_DB: join(folder, 'events.db'), AGENT_MEMORY_PATH: join(folder, 'memory.db'), AGENT_LEARNING_PATH: join(folder, 'learning.db'), AGENT_SKILLS_PATH: join(folder, 'skills'), AGENT_MODELS_FILE: join(folder, 'models.yaml'), AGENT_USER_MODELS_FILE: join(folder, 'missing-models.yaml'), AGENT_LOG_DIR: join(folder, 'logs'), AGENT_MCP_CONFIG: join(folder, 'missing-mcp.json'), AGENT_TOOL_POLICY: 'locked', SANDBOX_DOCKER: 'false', SANDBOX_WORKDIR: workspace, ASTRA_WORKSPACE: workspace, LEARNING_REVIEW_AUTO: '0' };
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
  if (process.platform === 'win32') await runFile('taskkill.exe', ['/PID', String(pid), '/T', '/F'], { timeout: 5000, windowsHide: true });
  else {
    const { stdout } = await runFile('ps', ['-axo', 'pid=,ppid='], { timeout: 3000 });
    const rows = stdout.trim().split('\n').map(line => line.trim().split(/\s+/).map(Number));
    const owned = new Set([pid]);
    for (let changed = true; changed;) { changed = false; for (const [id, parent] of rows) if (owned.has(parent) && !owned.has(id)) { owned.add(id); changed = true; } }
    for (const id of [...owned].reverse()) { try { process.kill(id, 'SIGKILL'); } catch (error) { if (error.code !== 'ESRCH') throw error; } }
  }
}
let app, failure;
const errors = [];
const checks = [];
const passed = name => { checks.push(name); console.log('PASS', name); };
try {
  app = await electron.launch({ args: [join(root, 'ui-gui')], env, timeout: 30000 });
  const page = await app.firstWindow(); page.setDefaultTimeout(30000);
  // The first scenarios inspect chat and records side by side. Native launch
  // bounds may be narrower on CI; the later 1000px case tests automatic close.
  await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].setContentSize(1320, 768));
  await page.waitForFunction(() => window.innerWidth === 1320);
  console.log("START", folder);
  page.on('pageerror', error => errors.push(error.message));
  const waitForLocatedViewport = async id => {
    // Visibility only establishes a mounted row. ResizeObserver then measures
    // its height and a later frame restores the scroll anchor using new offsets.
    await page.waitForFunction(id => {
      const el = [...document.querySelectorAll('.located-message')].find(node => node.dataset.messageId === id);
      if (!el) return false;
      const rect = el.getBoundingClientRect(), viewport = el.closest('.messages-scroll').getBoundingClientRect();
      const visible = rect.height > 0 && rect.top >= viewport.top - 40 && rect.top < viewport.bottom;
      const previous = window.__inspectorViewportProbe;
      const stable = visible && previous?.visible && previous.id === id && Math.abs(previous.top - rect.top) < 1 && Math.abs(previous.height - rect.height) < 1;
      const since = stable ? previous.since : performance.now();
      window.__inspectorViewportProbe = { id, visible, top: rect.top, height: rect.height, viewportTop: viewport.top, viewportBottom: viewport.bottom, since };
      return visible && performance.now() - since >= 100;
    }, id, { polling: 'raf', timeout: 10000 });
  };
  const submit = async text => { await page.getByRole('textbox', { name: '消息' }).fill(text); await page.getByRole('button', { name: '发送', exact: true }).click(); };
  await page.getByRole('textbox', { name: '消息' }).waitFor();
  await submit('PREPARE_INSPECTOR');
  await page.getByLabel('工具参数准备进度').waitFor();
  assert.match(await page.getByLabel('工具参数准备进度').innerText(), /尚未执行/);
  let snapshot = await page.evaluate(() => window.astra.bootstrap());
  const runtime = snapshot.active;
  assert.equal(snapshot.sessions.find(s => s.id === runtime).tools.length, 0);
  await page.screenshot({ path: join(output, 'preparing-unexecuted.png') });
  await page.getByRole('button', { name: '查看会话记录', exact: true }).click();
  await page.getByLabel('原始会话记录面板').waitFor();
  const beforeCall = await page.evaluate(async () => { const b = await window.astra.bootstrap(); const s = b.sessions.find(s => s.id === b.active); try { return await window.astra.query('session_log', { name: s.session, mode: s.mode, call_id: 'inspector-call' }); } catch (error) { if (!String(error).includes('Session does not exist')) throw error; return { target_status: 'missing' }; } });
  assert.equal(beforeCall.target_status, 'missing');
  assert.equal((await page.evaluate(() => window.astra.bootstrap())).active, runtime);
  await page.getByRole('button', { name: '关闭会话记录' }).click();
  passed('partial arguments are visible without admitted tools; log reads do not select or execute a runtime');
  releaseArguments();
  await page.getByText('INSPECTOR_DONE', { exact: true }).waitFor({ timeout: 30000 });
  await page.waitForFunction(async () => { const b = await window.astra.bootstrap(); const s = b.sessions.find(s => s.id === b.active); return !s.busy && s.messages.some(m => m.content === 'INSPECTOR_DONE' && m.source_ref) && s.messages.some(m => m.role === 'user' && m.source_ref); });
  assert.equal(await page.getByLabel('工具参数准备进度').count(), 0);
  assert.equal(readFileSync(target, 'utf8'), 'READ_ONLY_FIXTURE');
  await page.locator('.message.assistant').filter({ hasText: 'INSPECTOR_DONE' }).getByRole('button', { name: '查看消息记录' }).click();
  await page.locator('.session-log-record.selected .session-log-raw').waitFor();
  assert.match(await page.locator('.session-log-record.selected .session-log-raw').innerText(), /INSPECTOR_DONE/);
  await page.getByRole('button', { name: '定位到聊天' }).click();
  await page.locator('.located-message').filter({ hasText: 'INSPECTOR_DONE' }).waitFor();
  passed('live user and streamed assistant messages receive persisted source references and round-trip to raw records');
  await page.getByRole('button', { name: '关闭会话记录' }).click();
  await page.getByRole('button', { name: '执行详情', exact: true }).click();
  const toolCard = page.locator('.tool-detail').filter({ hasText: 'read_file' });
  if (!(await toolCard.evaluate(el => el.open))) await toolCard.locator('summary').first().click();
  await toolCard.getByRole('button', { name: '查看会话记录' }).click();
  await page.locator('.session-log-record.selected .session-log-raw').waitFor();
  assert.match(await page.locator('.session-log-record.selected .session-log-raw').innerText(), /inspector-call/);
  const previousLocationId = await page.locator('.located-message').getAttribute('data-message-id');
  await page.getByRole('button', { name: '定位到聊天' }).click();
  // This tool-only message is absent from the live chat. Wait for this query's
  // read-only window, not the highlight left by the previous answer lookup.
  await page.getByText('只读定位窗口，显示该记录附近的消息。').waitFor();
  await page.waitForFunction(previous => {
    const located = document.querySelector('.located-message');
    return located && located.getAttribute('data-message-id') !== previous;
  }, previousLocationId);
  await page.locator('.located-message').waitFor();
  assert.equal((await page.evaluate(() => window.astra.bootstrap())).active, runtime);
  await page.getByRole('button', { name: '返回已打开的会话' }).click();
  await page.locator('.history-banner').waitFor({ state: 'detached' });
  await page.getByRole('button', { name: '关闭会话记录' }).click();
  await page.getByRole('textbox', { name: '消息' }).waitFor();
  passed('tool call cards locate their canonical call record without switching or stopping the backend');
  await submit('BACKGROUND_INSPECTOR');
  await page.getByText('BACKGROUND_STILL_RUNNING', { exact: true }).waitFor();
  const requestsBeforeHistory = requests.length;
  await page.locator('.session-row').filter({ hasText: oldSession }).locator('button').first().click();
  await page.getByText('只读浏览历史，不会启动模型。').waitFor();
  await page.getByRole('button', { name: '查看会话记录', exact: true }).click();
  await page.locator('.session-log-record').first().waitFor();
  for (let i = 0; i < 8; i++) {
    const first = await page.locator('.session-log-record').first().getAttribute('data-log-index');
    await page.getByRole('button', { name: '较早记录' }).click();
    await page.waitForFunction(previous => document.querySelector('.session-log-record')?.getAttribute('data-log-index') !== previous, first);
  }
  const oldRecord = page.locator('.session-log-record').first();
  const oldIndex = Number(await oldRecord.getAttribute('data-log-index'));
  assert.ok(oldIndex < 231, `target ${oldIndex} must be absent from the initial 200-message history window`);
  await oldRecord.locator('.session-log-heading').click();
  await page.getByRole('button', { name: '定位到聊天' }).click();
  const located = page.locator(`.located-message[data-message-id="work:${oldSession}:${oldIndex}"]`);
  await located.waitFor();
  await waitForLocatedViewport(`work:${oldSession}:${oldIndex}`);
  assert.ok(await located.evaluate(el => { const parent = el.closest('.messages-scroll'); const rect = el.getBoundingClientRect(); const viewport = parent.getBoundingClientRect(); return rect.top >= viewport.top - 40 && rect.top < viewport.bottom; }));
  assert.ok(await page.locator('.measured-message').count() < 100);
  snapshot = await page.evaluate(() => window.astra.bootstrap());
  assert.equal(snapshot.sessions.find(s => s.id === runtime).busy, true);
  assert.equal(requests.length, requestsBeforeHistory);
  await page.screenshot({ path: join(output, 'history-log-location.png') });
  passed('bounded log paging loads an absent history window and scrolls the virtual list while another runtime keeps running');
  await page.getByRole('button', { name: '加载更早历史' }).click();
  await page.getByText('只读定位窗口，显示该记录附近的消息。').waitFor();
  await page.getByRole('button', { name: '加载更早历史' }).waitFor({ state: 'detached' });
  await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].setContentSize(1000, 820));
  await page.waitForFunction(() => window.innerWidth === 1000);
  assert.equal(await page.locator('.conversation').isVisible(), false);
  await page.getByRole('button', { name: '定位到聊天' }).click();
  await page.getByLabel('原始会话记录面板').waitFor({ state: 'detached' });
  await located.waitFor();
  await waitForLocatedViewport(`work:${oldSession}:${oldIndex}`);
  assert.ok(await located.evaluate(el => { const rect = el.getBoundingClientRect(); const viewport = el.closest('.messages-scroll').getBoundingClientRect(); return rect.height > 0 && rect.top >= viewport.top - 40 && rect.top < viewport.bottom; }));
  await page.screenshot({ path: join(output, 'narrow-history-location.png') });
  await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].setContentSize(1320, 768));
  await located.getByRole('button', { name: '查看消息记录' }).click();
  await page.locator('.session-log-record.selected .session-log-raw').waitFor();
  passed('loading older records preserves the location window; narrow-window navigation reveals and scrolls the hidden chat');
  historical.messages[oldIndex].content = 'CHANGED_CANONICAL_RECORD'; writeFileSync(archivePath, JSON.stringify(historical));
  await page.getByRole('button', { name: '定位到聊天' }).click();
  await page.getByText('会话记录已更新，请刷新记录后重新定位。').waitFor();
  await located.waitFor();
  assert.equal(requests.length, requestsBeforeHistory);
  passed('changed revision refuses a stale jump and preserves the currently viewed conversation');
  releaseBackground();
  await page.waitForFunction(async runtime => {
    const state = (await window.astra.bootstrap()).sessions.find(s => s.id === runtime);
    return !state.busy && state.info.task_status?.task?.status === 'completed'
      && state.messages.some(message => message.role === 'assistant' && message.content.includes('BACKGROUND_DONE'));
  }, runtime);
  assert.deepEqual(errors, []);
} catch (error) {
  failure = error; console.error(error);
  if (app) await bounded((async () => { const page = await app.firstWindow(); console.error((await page.locator('body').innerText()).slice(-6000)); console.error('Viewport diagnostic', await page.evaluate(() => ({ width: innerWidth, probe: window.__inspectorViewportProbe }))); await page.screenshot({ path: join(output, 'failure.png'), timeout: 2000 }); })(), 'Failure diagnostics', 3000).catch(error => console.error(error));
} finally {
  releaseArguments(); releaseBackground();
  if (app) {
    // After a failed assertion, stop only this isolated fixture's remaining
    // work through the ordinary quit confirmation. Never dismiss other dialogs.
    if (failure) await bounded(app.evaluate(({ dialog }) => {
      const original = dialog.showMessageBox.bind(dialog);
      dialog.showMessageBox = (...args) => {
        const options = args.at(-1);
        const stop = options?.buttons?.indexOf('停止并退出') ?? -1;
        return options?.message === '仍有任务或会话提醒在运行' && stop >= 0
          ? Promise.resolve({ response: stop, checkboxChecked: false }) : original(...args);
      };
    }), 'Fixture quit confirmation', 2000).catch(error => console.error(error));
    try { await bounded(app.close(), 'Electron close', 30000); }
    catch (error) {
      failure ||= error; console.error(error);
      await bounded(terminateFixture(app), 'Fixture process cleanup', 6000).catch(error => { failure ||= error; console.error(error); });
    }
  }
  await bounded(new Promise((resolve, reject) => {
    server.close(error => error ? reject(error) : resolve());
    server.closeAllConnections();
  }), 'Fixture HTTP close', 3000).catch(error => { failure ||= error; console.error(error); });
}
writeFileSync(join(output, 'inspector-result.json'), JSON.stringify({ checks, errors, folder, success: !failure, ...(failure ? { failure: String(failure) } : {}) }, null, 2));
if (failure) throw failure;
console.log(JSON.stringify({ checks: checks.length, output, folder }));
