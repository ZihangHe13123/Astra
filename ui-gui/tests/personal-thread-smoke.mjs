/** The personal thread hands project work to a conversation it opens (specs/personal-thread.md D6): real Electron and
 * Python, a loopback model, a real file write approved from the thread, and the result told back in the thread. */
import { expect } from '@playwright/test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { existsSync, mkdtempSync, mkdirSync, readFileSync, realpathSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { launchElectron } from './electron-launch.mjs';

const root = resolve(fileURLToPath(new URL('../..', import.meta.url)));
const folder = mkdtempSync(join(tmpdir(), 'astra-personal-thread-'));
const project = join(folder, 'project'); mkdirSync(project);
const target = join(project, 'handed.txt');
const output = join(root, 'output/playwright/personal-thread'); mkdirSync(output, { recursive: true });

// The thread hands the task over and later relays the result; the project's conversation writes the file.
const requests = [];
const model = createServer(async (req, res) => {
  if (req.method === 'GET') { res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify({ data: [{ id: 'thread-test' }] })); return; }
  if (!req.url.endsWith('/chat/completions')) { res.writeHead(404); res.end(); return; }
  let body = ''; for await (const part of req) body += part;
  const payload = JSON.parse(body); requests.push(payload);
  const messages = payload.messages, last = messages.at(-1);
  const user = JSON.stringify(messages.findLast(message => message.role === 'user')?.content ?? '');
  res.setHeader('Content-Type', 'text/event-stream');
  const chunk = (delta, finish_reason = null) => res.write('data: ' + JSON.stringify({ id: 'thread-' + requests.length, object: 'chat.completion.chunk', created: Math.floor(Date.now() / 1000), model: 'thread-test', choices: [{ index: 0, delta, finish_reason }] }) + '\n\n');
  const call = (name, args) => { chunk({ role: 'assistant', tool_calls: [{ index: 0, id: 'call-' + requests.length, type: 'function', function: { name, arguments: JSON.stringify(args) } }] }); chunk({}, 'tool_calls'); };
  const say = text => { chunk({ role: 'assistant', content: text }); chunk({}, 'stop'); };
  if (last.role === 'tool') say(user.includes('task for you') ? 'HANDED_FILE_WRITTEN' : 'THREAD_HANDED_OVER');
  else if (user.includes('task for you')) call('write_file', { path: target, content: 'handed' });
  else if (user.includes('reply on your task')) say('THREAD_REPORTS_DONE');
  else if (user.includes('交给项目')) call('peer_send', { workspace: project, text: '请写入 handed.txt' });
  else say('plain');
  res.end('data: [DONE]\n\n');
});
const port = await new Promise(done => model.listen(0, '127.0.0.1', () => done(model.address().port)));
writeFileSync(join(folder, '.env'), '');
writeFileSync(join(folder, 'settings.json'), JSON.stringify({ selected_model: 'thread-test' }));
writeFileSync(join(folder, 'models.yaml'), `version: 1\nproviders:\n  test:\n    label: Local fixture\n    provider: openai-compatible\n    base_url: http://127.0.0.1:${port}/v1\n    api_key_env: ''\n    context_limit: 131072\n    capabilities: [tools, streaming]\nmodels:\n  thread-test:\n    provider: openai-compatible\n    base_url: http://127.0.0.1:${port}/v1\n    api_key_env: ''\n    context_limit: 131072\n    capabilities: [tools, streaming]\n`);
const cleanEnv = Object.fromEntries(Object.entries(process.env).filter(([key]) => !/API_KEY|TOKEN|SECRET|^AGENT_|^ASTRA_|^LLM_|^SANDBOX_|^ELECTRON_/.test(key)));
const env = { ...cleanEnv, ASTRA_GUI_DISABLE_APPSHOT: '1', AGENT_PROJECT_ROOT: root, AGENT_PYTHON: process.env.AGENT_PYTHON || join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python'), ASTRA_ENV_FILE: join(folder, '.env'), ASTRA_HOME: folder, AGENT_SETTINGS_PATH: join(folder, 'settings.json'), AGENT_SESSION_DIR: join(folder, 'sessions'), AGENT_TASK_DB: join(folder, 'tasks.db'), ASTRA_APPROVAL_DB: join(folder, 'approvals.db'), ASTRA_EVENT_DB: join(folder, 'events.db'), AGENT_MEMORY_PATH: join(folder, 'memory.db'), AGENT_LEARNING_PATH: join(folder, 'learning.db'), AGENT_SKILLS_PATH: join(folder, 'skills'), AGENT_MODELS_FILE: join(folder, 'models.yaml'), AGENT_USER_MODELS_FILE: join(folder, 'missing-models.yaml'), AGENT_LOG_DIR: join(folder, 'logs'), AGENT_MCP_CONFIG: join(folder, 'missing-mcp.json'), AGENT_TOOL_POLICY: 'locked', SANDBOX_DOCKER: 'false', SANDBOX_WORKDIR: project, ASTRA_WORKSPACE: project, LEARNING_REVIEW_AUTO: '0', ASTRA_PHONE_PORT: '0', ASTRA_CHANNEL_CONFIG: join(folder, 'channels.json') };
const runFile = promisify(execFile);
async function terminate(app) {
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
const errors = [];
try {
  app = await launchElectron({ args: [join(root, 'ui-gui')], env, timeout: 30000 });
  const page = await app.firstWindow(); page.setDefaultTimeout(30000);
  page.on('pageerror', error => errors.push(error.message));
  await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].setContentSize(1320, 900));
  console.log('START', folder);
  await page.getByRole('button', { name: '个人对话', exact: true }).click();
  await page.locator('main.chat-view').waitFor();
  await expect(page.locator('.personal-entry.selected')).toHaveCount(1);
  await page.getByRole('textbox', { name: '消息', exact: true }).fill('交给项目：写一个文件');
  await page.getByRole('button', { name: '发送', exact: true }).click();

  // Handing work over can itself wait for approval in the thread, like any tool that changes something.
  const row = page.locator('.managed-row', { hasText: '请写入 handed.txt' });
  const handed = row.getByRole('button', { name: '允许一次', exact: true }), own = page.locator('.messages > .approval');
  await expect.poll(async () => (await handed.count()) + (await own.count()), { timeout: 60000 }).toBeGreaterThan(0);
  if (await own.count()) await own.getByRole('button', { name: '允许一次', exact: true }).click();
  // The desktop opened a conversation in the project; it shows who manages its task, and waits for approval.
  await handed.waitFor({ timeout: 60000 });
  assert.match(await row.innerText(), /交给了一个对话：请写入 handed\.txt · 在等你/);
  assert.equal(existsSync(target), false);
  const worker = await page.evaluate(async () => (await window.astra.bootstrap()).sessions.find(s => !s.personal && s.status !== 'disconnected'));
  assert.equal(realpathSync(worker.workspace), realpathSync(project));
  await row.getByRole('button', { name: '打开' }).click();
  await expect(page.locator('.managed-banner')).toContainText('个人对话正在管理这个任务：请写入 handed.txt');
  await expect(page.locator('.approval')).toHaveCount(1);
  await expect(page.locator('.personal-entry.selected')).toHaveCount(0);
  await page.screenshot({ path: join(output, 'managed.png') });
  console.log('PASS the thread opened a conversation in the project, which shows that the thread manages its task');

  // Approved from the thread: the file is written there and the result comes back to the thread.
  await page.getByRole('button', { name: '个人对话', exact: true }).click();
  await row.getByRole('button', { name: '允许一次', exact: true }).click();
  await expect.poll(() => existsSync(target) && readFileSync(target, 'utf8'), { timeout: 30000 }).toBe('handed');
  await page.locator('.message.assistant', { hasText: 'THREAD_REPORTS_DONE' }).waitFor({ timeout: 60000 });
  await expect(page.locator('.managed-row')).toHaveCount(0);
  await page.screenshot({ path: join(output, 'reported.png') });
  console.log('PASS approved from the thread, the file was written and the result came back to the thread');
  assert.deepEqual(errors, []);
} catch (error) {
  failure = error; console.error(error);
  if (app) { try { const p = await app.firstWindow(); await p.screenshot({ path: join(output, 'failure.png') }); console.error((await p.locator('body').innerText()).slice(-4000)); } catch { /* best effort */ } }
} finally {
  if (app) { await Promise.race([app.close(), new Promise(done => setTimeout(done, 15000))]).catch(() => {}); await terminate(app).catch(() => {}); }
  model.close();
}
if (failure) process.exit(1);
