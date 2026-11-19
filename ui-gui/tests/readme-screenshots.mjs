/** Screenshots for the README: the real desktop and Python backend with an isolated home and a scripted local model,
 * so every word on screen comes from this file and nothing reads the person's own state or calls a real model.
 * Run from the repository root with `node ui-gui/tests/readme-screenshots.mjs`; pictures go to output/readme.
 * ASTRA_SCREENSHOT_APP points at another built desktop (default: this checkout's ui-gui).
 * docs/images/desktop-*.webp are these captures scaled to 1600 px wide, with rounded corners and a hairline edge. */
import { expect } from '@playwright/test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { mkdtempSync, mkdirSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { launchElectron } from './electron-launch.mjs';

const root = resolve(fileURLToPath(new URL('../..', import.meta.url)));
const desktop = process.env.ASTRA_SCREENSHOT_APP || join(root, 'ui-gui');
const output = process.env.ASTRA_SCREENSHOT_OUT || join(root, 'output/readme'); mkdirSync(output, { recursive: true });
const folder = mkdtempSync(join(tmpdir(), 'astra-readme-'));
const workspace = join(folder, 'weekly-notes'); mkdirSync(workspace);
const MODEL = 'deepseek-flash';

// The scene: two ordinary tasks fill the sidebar, then the personal thread sets up a morning news check and stops
// at its approval card.
const weekly = '周报草稿按项目分成三节：\n\n### 进展\n- 检索评测：p95 延迟从 410 ms 降到 260 ms\n- 桌面版：审批卡支持键盘操作\n\n### 风险\n- Windows 安装包还没有签名证书\n\n### 下周\n- 补齐 Windows 端验收';
const papers = '| 论文 | 方法 | 结论 |\n| --- | --- | --- |\n| A | 检索增强 | 长文问答提升明显 |\n| B | 工具调用微调 | 多步任务更稳 |\n| C | 记忆压缩 | 成本下降，召回略降 |';
const news = { action: 'add', name: 'AI 新闻早报', kind: 'check', time: '08:00', web: true, targets: ['desktop', 'weixin'],
  prompt: '搜索过去 24 小时的 AI 新闻，挑出最重要的 5 条，每条两三句话，附上来源链接。' };
const preface = '好的，我把它建成每天早上 8 点运行的只读检查：联网搜索，结果写进这里，同时通知桌面和微信。创建之前需要你确认。';

const requests = [];
const model = createServer(async (req, res) => {
  if (req.method === 'GET') { res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify({ data: [{ id: MODEL }] })); return; }
  if (!req.url.endsWith('/chat/completions')) { res.writeHead(404); res.end(); return; }
  let body = ''; for await (const part of req) body += part;
  const payload = JSON.parse(body); requests.push(payload);
  const messages = payload.messages, asked = messages.findLastIndex(message => message.role === 'user');
  const user = JSON.stringify(messages[asked]?.content ?? '');
  const results = messages.slice(asked + 1).filter(message => message.role === 'tool').length;
  res.setHeader('Content-Type', 'text/event-stream');
  const chunk = (delta, finish_reason = null) => res.write('data: ' + JSON.stringify({ id: 'readme-' + requests.length, object: 'chat.completion.chunk', created: Math.floor(Date.now() / 1000), model: MODEL, choices: [{ index: 0, delta, finish_reason }] }) + '\n\n');
  const say = text => { chunk({ role: 'assistant', content: text }); chunk({}, 'stop'); };
  const call = (name, args, text = '') => {
    if (text) chunk({ role: 'assistant', content: text });
    chunk({ ...(text ? {} : { role: 'assistant' }), tool_calls: [{ index: 0, id: 'call-' + requests.length, type: 'function', function: { name, arguments: JSON.stringify(args) } }] });
    chunk({}, 'tool_calls');
  };
  if (user.includes('AI 新闻')) {
    if (results === 0) call('tool_search', { family: 'schedule' }, preface);
    else if (results === 1) call('call_tool', { name: 'jobs', arguments: news });
    else say('建好了。明天 08:00 第一次运行，结果会出现在这里。');
  } else if (user.includes('周报')) say(weekly);
  else if (user.includes('论文')) say(papers);
  else say('好的。');
  res.end('data: [DONE]\n\n');
});
const port = await new Promise(done => model.listen(0, '127.0.0.1', () => done(model.address().port)));
writeFileSync(join(folder, '.env'), '');
writeFileSync(join(folder, 'settings.json'), JSON.stringify({ selected_model: MODEL }));
writeFileSync(join(folder, 'models.yaml'), `version: 1\nproviders:\n  test:\n    label: Local fixture\n    provider: openai-compatible\n    base_url: http://127.0.0.1:${port}/v1\n    api_key_env: ''\n    context_limit: 131072\n    capabilities: [tools, streaming]\nmodels:\n  ${MODEL}:\n    provider: openai-compatible\n    base_url: http://127.0.0.1:${port}/v1\n    api_key_env: ''\n    context_limit: 131072\n    capabilities: [tools, streaming]\n`);
const cleanEnv = Object.fromEntries(Object.entries(process.env).filter(([key]) => !/API_KEY|TOKEN|SECRET|^AGENT_|^ASTRA_|^LLM_|^SANDBOX_|^ELECTRON_/.test(key)));
// A fixed time zone keeps the machine's own out of the schedule text on the approval card.
const env = { ...cleanEnv, TZ: 'Asia/Shanghai', ASTRA_GUI_DISABLE_APPSHOT: '1', AGENT_PROJECT_ROOT: root,
  AGENT_PYTHON: process.env.AGENT_PYTHON || join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python'),
  ASTRA_ENV_FILE: join(folder, '.env'), ASTRA_HOME: folder, AGENT_SETTINGS_PATH: join(folder, 'settings.json'),
  AGENT_SESSION_DIR: join(folder, 'sessions'), AGENT_TASK_DB: join(folder, 'tasks.db'), ASTRA_APPROVAL_DB: join(folder, 'approvals.db'),
  ASTRA_EVENT_DB: join(folder, 'events.db'), AGENT_MEMORY_PATH: join(folder, 'memory.db'), AGENT_LEARNING_PATH: join(folder, 'learning.db'),
  AGENT_SKILLS_PATH: join(folder, 'skills'), AGENT_MODELS_FILE: join(folder, 'models.yaml'), AGENT_USER_MODELS_FILE: join(folder, 'missing-models.yaml'),
  AGENT_LOG_DIR: join(folder, 'logs'), AGENT_MCP_CONFIG: join(folder, 'missing-mcp.json'), AGENT_TOOL_POLICY: 'permissive',
  SANDBOX_DOCKER: 'false', SANDBOX_WORKDIR: workspace, ASTRA_WORKSPACE: workspace, LEARNING_REVIEW_AUTO: '0',
  // Lexical history search only: the local embedding model is shared by the machine and large to load.
  ASTRA_CONTEXT_INDEX_EMBEDDING: 'off' };

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
  app = await launchElectron({ args: [desktop], env, timeout: 30000 });
  const page = await app.firstWindow(); page.setDefaultTimeout(30000);
  page.on('pageerror', error => errors.push(error.message));
  // A window cannot be taller than the screen, so the page is zoomed out a little to fit the whole exchange.
  await app.evaluate(({ BrowserWindow }) => { const win = BrowserWindow.getAllWindows()[0]; win.setContentSize(1280, 800); win.webContents.setZoomFactor(0.85); });
  console.log('START', folder);
  const composer = page.getByRole('textbox', { name: '消息', exact: true });
  const state = async () => page.evaluate(async () => { const boot = await window.astra.bootstrap(); return boot.sessions.find(item => item.id === boot.active); });
  const send = async text => {
    await composer.fill(text);
    await page.getByRole('button', { name: '发送', exact: true }).click();
    await expect.poll(async () => {
      const current = await state();
      return current?.status === 'ready' && !current.busy && current.messages.some(message => message.role === 'user' && message.content === text && message.source_ref);
    }, { timeout: 30000, message: `the turn for ${text} finishes` }).toBe(true);
  };

  await send('把这周的会议记录整理成周报');
  await page.getByRole('button', { name: '新对话', exact: false }).click();
  await send('对比一下这三篇论文的方法和结论');

  await page.getByRole('button', { name: '个人对话', exact: true }).click();
  await page.locator('main.chat-view').waitFor();
  await composer.fill('每天早上 8 点帮我整理 AI 新闻，挑最重要的 5 条，附上来源');
  await page.getByRole('button', { name: '发送', exact: true }).click();
  const approval = page.locator('.approval', { hasText: 'AI 新闻早报' }).first();
  await approval.waitFor({ timeout: 60000 });
  await approval.scrollIntoViewIfNeeded();
  await page.waitForTimeout(800);
  for (const theme of ['light', 'dark']) {
    await page.evaluate(name => { document.documentElement.dataset.theme = name; }, theme);
    await page.waitForTimeout(500);
    // Electron's own capture: with the page zoomed, Playwright's screenshot has blank margins.
    const png = await app.evaluate(async ({ BrowserWindow }) => (await BrowserWindow.getAllWindows()[0].webContents.capturePage()).toPNG().toString('base64'));
    writeFileSync(join(output, `desktop-${theme}.png`), Buffer.from(png, 'base64'));
    console.log('SHOT', theme);
  }
  await page.evaluate(() => { document.documentElement.dataset.theme = 'system'; });
  assert.deepEqual(errors, []);
} catch (error) {
  failure = error; console.error(error);
  if (app) { try { const p = await app.firstWindow(); await p.screenshot({ path: join(output, 'failure.png') }); console.error((await p.locator('body').innerText()).slice(-3000)); } catch { /* best effort */ } }
} finally {
  if (app) { await Promise.race([app.close(), new Promise(done => setTimeout(done, 15000))]).catch(() => {}); await terminate(app).catch(() => {}); }
  model.close();
}
if (failure) process.exit(1);
