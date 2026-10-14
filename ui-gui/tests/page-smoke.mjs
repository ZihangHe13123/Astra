/** Pages in the desktop: real Electron/Python, a loopback model that writes page files, and a page that tries to get out. */
import { _electron as electron } from 'playwright';
import { expect } from '@playwright/test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { createServer as createTcpServer } from 'node:net';
import { createSocket } from 'node:dgram';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { mkdtempSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from 'node:fs';
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
  <div style="position: absolute; left: 0; top: 0; width: 20px; height: 20px; background: var(--astra-accent)"></div>
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
// A first screen laid out against the window, then more: stretched to its whole height it would be a page nobody sees.
const screenful = `<!doctype html><title>screen</title><body style="margin: 0"><div style="height: 100vh; background: rgb(0, 90, 160)"></div><div style="height: 900px; background: rgb(160, 90, 0)"></div></body>`;
// Looked at rather than opened: it turns green only if it sees nothing of the host, and tries the network at once.
const watched = `<!doctype html><title>watched</title><body style="margin: 0"><script>
  const alone = typeof window.astra === 'undefined' && typeof RTCPeerConnection === 'undefined' && (() => { try { return !parent.document; } catch { return true; } })();
  document.documentElement.style.background = alone ? 'rgb(0, 128, 0)' : 'rgb(200, 0, 0)';
  fetch('${out}/watched-fetch').catch(() => {}); new Image().src = '${out}/watched-image';
  try { navigator.sendBeacon('${out}/watched-beacon', 'x'); } catch {}
  try { new WebSocket('ws://127.0.0.1:${probePort}/watched-websocket'); } catch {}
  try { window.open('${out}/watched-popup'); } catch {}
  try { top.location = '${out}/watched-top'; } catch {}
  const sheet = document.createElement('link'); sheet.rel = 'stylesheet'; sheet.href = '${out}/watched-sheet'; document.head.append(sheet);
  const nested = document.createElement('iframe'); nested.src = '${out}/watched-frame'; document.body.append(nested);
</script></body>`;
// It leaves at once, before any picture can be taken.
const leaver = `<!doctype html><title>leaver</title><p>leaving</p><script>location.href = '${out}/leaver-navigation';</script>`;
// What the model does when asked: one call that writes or edits a file, then a line of text.
const writes = {
  画布: { path: 'demo.page.html', content: demo('V1') },
  更新: { tool: 'edit_file', path: 'demo.page.html', old: 'CANVAS V1', new: 'CANVAS V2' },
  普通: { path: 'plain.html', content: '<!doctype html><h1>ORDINARY</h1>' },
  逃逸: { path: 'hostile.page.html', content: hostilePage },
  技能: { path: 'lesson.page.html', content: taught },
  报错: { path: 'broken.page.html', content: broken },
  一屏: { path: 'screen.page.html', content: screenful },
  旁观: { path: 'watched.page.html', content: watched },
  出走: { path: 'leaver.page.html', content: leaver },
  // Looking at a page: the same tool that looks at an image, given the page's path.
  瞧瞧画布: { tool: 'read_image', path: 'demo.page.html', question: 'layout' },
  瞧瞧技能: { tool: 'read_image', path: 'lesson.page.html' },
  瞧瞧报错: { tool: 'read_image', path: 'broken.page.html' },
  瞧瞧一屏: { tool: 'read_image', path: 'screen.page.html' },
  瞧瞧旁观: { tool: 'read_image', path: 'watched.page.html' },
  瞧瞧出走: { tool: 'read_image', path: 'leaver.page.html' },
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
  const write = Object.entries(writes).sort(([a], [b]) => b.length - a.length).find(([word]) => text.includes(word))?.[1];
  const sawPicture = text.includes('data:image/png;base64,');
  res.setHeader('Content-Type', 'text/event-stream');
  const chunk = (delta, finish_reason = null) => res.write('data: ' + JSON.stringify({ id: 'page-' + requests.length, object: 'chat.completion.chunk', created: Math.floor(Date.now() / 1000), model: 'page-test', choices: [{ index: 0, delta, finish_reason }] }) + '\n\n');
  if (write && !answered) {
    const { tool = 'write_file', ...args } = write;
    chunk({ role: 'assistant', tool_calls: [{ index: 0, id: 'page-call-' + requests.length, type: 'function', function: { name: tool, arguments: JSON.stringify(args) } }] });
    chunk({}, 'tool_calls');
  } else { chunk({ role: 'assistant', content: sawPicture ? 'SAW_PICTURE' : write ? `${write.tool === 'read_image' ? 'NO_PICTURE' : 'WROTE'} ${write.path}` : 'PLAIN_REPLY' }); chunk({}, 'stop'); }
  res.end('data: [DONE]\n\n');
});
const port = await listen(model);
writeFileSync(join(folder, '.env'), '');
writeFileSync(join(folder, 'settings.json'), JSON.stringify({ selected_model: 'page-test' }));
writeFileSync(join(folder, 'models.yaml'), `version: 1\nproviders:\n  test:\n    label: Local fixture\n    provider: openai-compatible\n    base_url: http://127.0.0.1:${port}/v1\n    api_key_env: ''\n    context_limit: 131072\n    capabilities: [tools, streaming, vision]\nmodels:\n  page-test:\n    provider: openai-compatible\n    base_url: http://127.0.0.1:${port}/v1\n    api_key_env: ''\n    context_limit: 131072\n    capabilities: [tools, streaming, vision]\n`);
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

// Passive, bounded metadata only: never retain page HTML or message payloads.
const loadEvents = [], frameIds = new WeakMap();
let nextFrameId = 0, loadDiagnostics;
const frameId = frame => { if (!frameIds.has(frame)) frameIds.set(frame, ++nextFrameId); return frameIds.get(frame); };
const recordLoad = event => { loadEvents.push({ at: Date.now(), ...event }); if (loadEvents.length > 64) loadEvents.shift(); };
const diagnosticAddress = value => {
  try {
    const url = new URL(value);
    if (url.protocol === 'astra:') return url.hostname === 'card' && ['/page.html', '/frame.html'].includes(url.pathname) ? `astra://card${url.pathname}` : 'astra:[other]';
    return ['about:blank', 'about:srcdoc'].includes(value) ? value : url.protocol;
  } catch { return '[invalid URL]'; }
};

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
  for (const event of ['frameattached', 'framenavigated', 'framedetached']) page.on(event, frame => recordLoad({ event, frame: frameId(frame), address: diagnosticAddress(frame.url()) }));
  page.on('request', request => { if (request.url().startsWith('astra:')) recordLoad({ event: 'request', address: diagnosticAddress(request.url()), resource: request.resourceType() }); });
  page.on('response', response => { if (response.url().startsWith('astra:')) recordLoad({ event: 'response', address: diagnosticAddress(response.url()), status: response.status() }); });
  page.on('requestfailed', request => { if (request.url().startsWith('astra:')) recordLoad({ event: 'requestfailed', address: diagnosticAddress(request.url()), code: request.failure()?.errorText?.match(/(?:net::)?ERR_[A-Z_]+/)?.[0] || '[unclassified]' }); });
  await app.evaluate(({ BrowserWindow }) => {
    const window = BrowserWindow.getAllWindows()[0], contents = window.webContents;
    globalThis.__pageSmokeLoadEvents = [];
    const record = event => { globalThis.__pageSmokeLoadEvents.push({ at: Date.now(), ...event }); if (globalThis.__pageSmokeLoadEvents.length > 64) globalThis.__pageSmokeLoadEvents.shift(); };
    for (const event of ['did-start-loading', 'did-stop-loading']) contents.on(event, () => record({ event }));
    contents.on('did-fail-load', (_event, code, _description, _url, isMainFrame, processId, routingId) => record({ event: 'did-fail-load', code, isMainFrame, processId, routingId }));
    contents.on('render-process-gone', (_event, details) => record({ event: 'render-process-gone', reason: details.reason, exitCode: details.exitCode }));
    window.on('unresponsive', () => record({ event: 'unresponsive' }));
  });
  // A refused request is still announced; an answer or an opened socket means one got out.
  page.on('response', response => { if (/^https?:/i.test(response.url())) answered.push(response.url()); });
  page.on('websocket', socket => answered.push(socket.url()));
  const resize = async (width, height) => { await app.evaluate(({ BrowserWindow }, size) => BrowserWindow.getAllWindows()[0].setContentSize(...size), [width, height]); await page.waitForFunction(width => innerWidth === width, width); };
  await resize(1320, 900);
  const composer = page.getByRole('textbox', { name: '消息', exact: true });
  await composer.evaluate(() => {
    window.__pageSmokeMessages = [];
    window.addEventListener('message', event => {
      const data = event.data;
      if (!data || typeof data !== 'object' || data.astraPage !== true) return;
      const frames = [...document.querySelectorAll('iframe.page-frame')];
      window.__pageSmokeMessages.push({ at: Date.now(), type: ['ready', 'pong', 'error'].includes(data.type) ? data.type : '[unrecognized]', id: Number.isSafeInteger(data.id) ? data.id : null, sourceMatches: frames.some(frame => event.source === frame.contentWindow), frameCount: frames.length });
      if (window.__pageSmokeMessages.length > 64) window.__pageSmokeMessages.shift();
    });
  });
  console.log('START', folder);
  const state = async () => page.evaluate(async () => { const boot = await window.astra.bootstrap(); return boot.sessions.find(item => item.id === boot.active); });
  const idle = async () => expect.poll(async () => { const current = await state(); return current?.status === 'ready' && !current.busy; }, { timeout: 30000 }).toBe(true);
  // Writing a file asks first; the person allows it, and the model then says what it wrote.
  const ask = async (text, path) => {
    await composer.fill(text); await page.getByRole('button', { name: '发送', exact: true }).click();
    await page.getByRole('button', { name: '允许一次', exact: true }).click({ timeout: 15000 });
    await expect(page.locator('.message.assistant').filter({ hasText: `WROTE ${path}` }).last()).toBeVisible();
    await idle();
  };
  // Looking needs no leave: the model calls, and its next request holds what the tool said and any picture that came with it.
  const look = async text => {
    const before = requests.length;
    await composer.fill(text); await page.getByRole('button', { name: '发送', exact: true }).click();
    await expect.poll(() => requests.length, { timeout: 40000 }).toBeGreaterThanOrEqual(before + 2);
    await expect(page.locator('.message.assistant').filter({ hasText: /SAW_PICTURE|NO_PICTURE/ }).last()).toBeVisible();
    await idle();
    const messages = requests.at(-1).messages, at = messages.findLastIndex(message => message.role === 'tool');
    const pictures = messages.slice(at + 1).flatMap(message => Array.isArray(message.content) ? message.content : []).filter(part => part.type === 'image_url').map(part => part.image_url.url);
    // The result reaches the model between a heading and a footer; a failure has no payload inside.
    const said = String(messages[at].content); let result = said; try { result = JSON.parse(said.slice(said.indexOf('{'), said.lastIndexOf('}') + 1)); } catch { /* plain text */ }
    return { result, pictures, reply: (await page.locator('.message.assistant').last().innerText()).trim().split('\n').pop() };
  };
  // A picture as the model received it: its size, and the colour at each point asked about.
  const seen = (url, points) => app.evaluate(({ nativeImage }, [url, points]) => {
    const image = nativeImage.createFromDataURL(url), { width, height } = image.getSize(), bitmap = image.toBitmap();
    return { width, height, colours: points.map(([x, y]) => { const at = (y * width + x) * 4; return [bitmap[at + 2], bitmap[at + 1], bitmap[at]]; }) };
  }, [url, points]);
  const near = (actual, expected, label) => assert.ok(actual.every((value, index) => Math.abs(value - expected[index]) <= 4), `${label}: ${actual} is not ${expected}`);
  const rgb = colour => { const hex = /^#([0-9a-f]{6})$/i.exec(colour); return hex ? [0, 2, 4].map(at => parseInt(hex[1].slice(at, at + 2), 16)) : colour.match(/\d+/g).slice(0, 3).map(Number); };
  const pictureFolder = join(folder, 'gui', 'page-snapshots');
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

  // The model looks at the page it wrote. The desktop draws it out of sight, in the same frame, and the model gets the
  // picture: the page's own ground, the app's accent where the page used it, three screens of a page that is longer.
  const looked = await look('瞧瞧画布');
  assert.equal(looked.reply, 'SAW_PICTURE');
  assert.equal(looked.pictures.length, 1);
  const demoPicture = await seen(looked.pictures[0], [[10, 10], [500, 8], [500, 2090]]);
  assert.deepEqual([demoPicture.width, demoPicture.height], [1000, 2100]);
  near(demoPicture.colours[0], rgb(hostAccent), 'the accent swatch');
  near(demoPicture.colours[1], [16, 32, 48], 'the page ground');
  near(demoPicture.colours[2], [16, 32, 48], 'the third screen');
  assert.equal(looked.result.type, 'image_attachment');
  assert.equal(looked.result.page.endsWith('demo.page.html'), true);
  assert.equal(looked.result.shown_height, 2100);
  assert.ok(looked.result.page_height > 2400 && looked.result.page_height < 3000, `page height ${looked.result.page_height}`);
  assert.match(looked.result.message, new RegExp(`${looked.result.page_height}px tall; the picture shows its first 2100px`));
  assert.equal('page_script_error' in looked.result, false);
  // The picture is a file of the desktop's own, named after the page; looking changed nothing on screen.
  assert.equal(looked.result.image_paths[0].startsWith(pictureFolder), true);
  assert.match(looked.result.image_paths[0], /demo\.page-[0-9a-f]{10}\.png$/);
  assert.equal(readdirSync(pictureFolder).length, 1);
  await expect(inside.locator('#n')).toHaveText('2');
  assert.equal(await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().length), 1, 'the window the page was drawn in is gone');
  // The model is told it can do this only where something can draw the page.
  const readImage = requests.at(-1).tools.find(tool => tool.function.name === 'read_image').function;
  assert.match(readImage.description, /HTML page \(\.html\)/);
  passed('the model looks at its page through the image tool and receives the page as the desktop draws it, with what the picture leaves out said beside it');

  // The person can send the same picture: it lands in the message being written, where the enlarged panel would hide it.
  await preview.getByRole('button', { name: '放大', exact: true }).click();
  await expect(page.locator('.conversation')).toBeHidden();
  await preview.getByRole('button', { name: '发给 Astra 看' }).click();
  await expect(page.locator('.attachment-list')).toHaveText(/demo\.page-[0-9a-f]{10}\.png/);
  await expect(page.locator('.conversation')).toBeVisible();
  await expect(frame).toHaveCount(1);
  assert.equal(readdirSync(pictureFolder).length, 1, 'the same page in the same theme is the same picture');
  await page.getByRole('button', { name: '移除附件' }).click();
  await expect(page.locator('.attachment-list')).toHaveCount(0);
  passed('the page preview sends its picture to the message being written');

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
  // Looked at as the skill says to, it is one screen, and the picture is of the page as it loads: the first plan chosen.
  const lookedLesson = await look('瞧瞧技能');
  assert.equal(lookedLesson.reply, 'SAW_PICTURE');
  assert.deepEqual((({ width, height }) => [width, height])(await seen(lookedLesson.pictures[0], [])), [1000, 700]);
  assert.equal('page_height' in lookedLesson.result, false);
  writeFileSync(join(output, 'page-lesson-seen.png'), Buffer.from(lookedLesson.pictures[0].split(',')[1], 'base64'));
  await expect(inside.getByRole('button', { name: '方案 B' })).toHaveAttribute('aria-pressed', 'true');
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

  // Looked at, it is drawn all the same, and the model is told what its script said.
  const lookedBroken = await look('瞧瞧报错');
  assert.equal(lookedBroken.reply, 'SAW_PICTURE');
  assert.match(lookedBroken.result.page_script_error, /PAGE_FIXTURE_FAILURE/);
  assert.match(lookedBroken.result.message, /A script on the page raised an error while it was drawn: .*PAGE_FIXTURE_FAILURE/);
  assert.deepEqual((({ width, height }) => [width, height])(await seen(lookedBroken.pictures[0], [])), [1000, 700]);
  // A page laid out against its window is shown as its first screen, not stretched into one nobody sees.
  await ask('一屏', 'screen.page.html');
  const lookedScreen = await look('瞧瞧一屏');
  const screenPicture = await seen(lookedScreen.pictures[0], [[500, 10], [500, 690]]);
  assert.deepEqual([screenPicture.width, screenPicture.height], [1000, 700]);
  near(screenPicture.colours[0], [0, 90, 160], 'the top of the first screen');
  near(screenPicture.colours[1], [0, 90, 160], 'the bottom of the first screen');
  assert.deepEqual([lookedScreen.result.shown_height, lookedScreen.result.page_height], [700, 1600]);
  passed('a failing page is drawn with its error reported, and a page laid out against its window is shown as its first screen');

  // Out of sight is no way out. A page being looked at sees nothing of the host and reaches nothing; one that leaves gives no picture.
  await ask('旁观', 'watched.page.html');
  const lookedWatched = await look('瞧瞧旁观');
  assert.equal(lookedWatched.reply, 'SAW_PICTURE');
  near((await seen(lookedWatched.pictures[0], [[500, 350]])).colours[0], [0, 128, 0], 'the page saw neither the bridge, nor WebRTC, nor its host');
  await ask('出走', 'leaver.page.html');
  const lookedLeaver = await look('瞧瞧出走');
  console.log('LEAVER', JSON.stringify({ reply: lookedLeaver.reply, result: lookedLeaver.result, pictures: lookedLeaver.pictures.length }));
  assert.equal(lookedLeaver.reply, 'NO_PICTURE leaver.page.html');
  assert.equal(lookedLeaver.pictures.length, 0);
  assert.match(JSON.stringify(lookedLeaver.result), /page_rendering_failed|页面没有应答/);
  await new Promise(done => setTimeout(done, 1500));
  assert.deepEqual(reached, [], 'nothing a page tried while it was looked at reached a listener');
  assert.equal(await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().length), 1);
  passed('a page being looked at is as isolated as one on screen, and a page that leaves its frame gives no picture');

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
  if (app) {
    const metadata = await Promise.allSettled([
      bounded((async () => {
        const page = await app.firstWindow();
        const frames = page.frames().map(frame => ({ id: frameId(frame), address: diagnosticAddress(frame.url()), detached: frame.isDetached() }));
        const messages = await page.evaluate(() => window.__pageSmokeMessages || []);
        return { frames, messages };
      })(), 'Page load metadata', 2000),
      bounded(app.evaluate(({ BrowserWindow }) => ({
        events: globalThis.__pageSmokeLoadEvents || [],
        windows: BrowserWindow.getAllWindows().map(window => {
          const contents = window.webContents, destroyed = contents.isDestroyed();
          return { id: contents.id, destroyed, ...(destroyed ? {} : { loading: contents.isLoading(), loadingMainFrame: contents.isLoadingMainFrame(), processId: contents.getOSProcessId() }) };
        }),
      })), 'WebContents load metadata', 2000),
    ]);
    loadDiagnostics = { events: loadEvents, renderer: metadata[0].status === 'fulfilled' ? metadata[0].value : '[unavailable]', main: metadata[1].status === 'fulfilled' ? metadata[1].value : '[unavailable]' };
    console.error('PAGE_LOAD_DIAGNOSTICS', JSON.stringify(loadDiagnostics));
  }
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
writeFileSync(join(output, 'page-result.json'), JSON.stringify({ checks, errors, reached, answered, probed, folder, success: !failure, ...(failure ? { failure: String(failure), loadDiagnostics } : {}) }, null, 2));
if (failure) throw failure;
console.log(JSON.stringify({ checks: checks.length, output, folder }));
