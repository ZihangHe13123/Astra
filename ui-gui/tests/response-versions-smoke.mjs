/** Reply branches: real Electron/Python with an isolated loopback model. */
import { _electron as electron } from 'playwright';
import { expect } from '@playwright/test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { existsSync, mkdtempSync, mkdirSync, readFileSync, statSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(fileURLToPath(new URL('../..', import.meta.url)));
const folder = mkdtempSync(join(tmpdir(), 'astra-response-versions-'));
const workspace = join(folder, 'workspace'); mkdirSync(workspace);
const sessionDir = join(folder, 'sessions'); mkdirSync(sessionDir);
const output = join(root, 'output/playwright/response-versions'); mkdirSync(output, { recursive: true });
const target = join(workspace, 'write-once.txt');
const imagePath = join(workspace, 'branch-image.png');
const imageBytes = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+ip1sAAAAASUVORK5CYII=', 'base64');
const imageMarker = '[Image: branch-image.png]';
writeFileSync(imagePath, imageBytes);
const answers = { a: 'ANSWER_BRANCH_ALPHA', b: 'ANSWER_BRANCH_BETA', a2: 'CONTINUATION_ALPHA_ONLY', b2: 'CONTINUATION_BETA_ORIGINAL', b2v2: 'CONTINUATION_BETA_REVISED' };
let phase = 'original', releaseRegeneration, releaseCancellation;
const regenerationGate = new Promise(resolve => { releaseRegeneration = resolve; });
const cancellationGate = new Promise(resolve => { releaseCancellation = resolve; });
const requests = [];
const server = createServer(async (req, res) => {
  if (req.method === 'GET') { res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify({ data: [{ id: 'versions-test' }] })); return; }
  if (!req.url.endsWith('/chat/completions')) { res.writeHead(404); res.end(); return; }
  let body = ''; for await (const part of req) body += part;
  const payload = JSON.parse(body), requestPhase = phase;
  requests.push({ phase: requestPhase, payload });
  if (requestPhase === 'failure') {
    res.writeHead(400, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: { message: 'EXPECTED_REGENERATION_FAILURE', type: 'invalid_request_error' } })); return;
  }
  res.setHeader('Content-Type', 'text/event-stream');
  const chunk = (delta, finish_reason = null) => { if (!res.destroyed) res.write('data: ' + JSON.stringify({ id: 'versions-' + requests.length, object: 'chat.completion.chunk', created: Math.floor(Date.now() / 1000), model: 'versions-test', choices: [{ index: 0, delta, finish_reason }] }) + '\n\n'); };
  const call = (id, content) => {
    chunk({ role: 'assistant', tool_calls: [{ index: 0, id, type: 'function', function: { name: 'write_file', arguments: JSON.stringify({ path: target, content }) } }] });
    chunk({}, 'tool_calls');
  };
  if (requestPhase === 'original' && !payload.messages.some(message => message.role === 'tool')) call('original-write', 'ORIGINAL_EFFECT');
  else if (requestPhase === 'forbidden-tool') call('forbidden-retry-write', 'FORBIDDEN_EFFECT');
  else {
    const response = requestPhase === 'original' ? answers.a : requestPhase === 'regenerate-b' ? answers.b
      : requestPhase === 'continue-a' ? answers.a2 : requestPhase === 'continue-b' ? answers.b2
      : requestPhase === 'nested' ? answers.b2v2 : 'CANCELLED_REGENERATION_PARTIAL';
    chunk({ role: 'assistant', content: response });
    if (requestPhase === 'regenerate-b') await regenerationGate;
    if (requestPhase === 'cancel') await cancellationGate;
    chunk({}, 'stop');
  }
  if (!res.destroyed) res.end('data: [DONE]\n\n');
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const port = server.address().port;
writeFileSync(join(folder, '.env'), '');
writeFileSync(join(folder, 'settings.json'), JSON.stringify({ selected_model: 'versions-test' }));
writeFileSync(join(folder, 'models.yaml'), `version: 1\nproviders:\n  test:\n    label: Local fixture\n    provider: openai-compatible\n    base_url: http://127.0.0.1:${port}/v1\n    api_key_env: ''\n    context_limit: 131072\n    capabilities: [tools, streaming, vision]\nmodels:\n  versions-test:\n    provider: openai-compatible\n    base_url: http://127.0.0.1:${port}/v1\n    api_key_env: ''\n    context_limit: 131072\n    capabilities: [tools, streaming, vision]\n`);
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
const errors = [], externalRequests = [], checks = [];
const passed = name => { checks.push(name); console.log('PASS', name); };
try {
  app = await electron.launch({ args: [join(root, 'ui-gui')], env, timeout: 30000 });
  const page = await app.firstWindow(); page.setDefaultTimeout(30000);
  page.on('pageerror', error => errors.push(error.message));
  page.on('request', request => { if (/^https?:/i.test(request.url())) externalRequests.push(request.url()); });
  let navigations = 0;
  page.on('framenavigated', frame => { if (frame === page.mainFrame()) navigations++; });
  await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].setContentSize(1320, 820));
  await page.waitForFunction(() => innerWidth === 1320);
  const originalToken = await page.evaluate(() => window.__responseVersionsToken = crypto.randomUUID());
  console.log('START', folder);
  const composer = page.getByRole('textbox', { name: '消息', exact: true });
  const state = async () => page.evaluate(async () => { const boot = await window.astra.bootstrap(); return boot.sessions.find(item => item.id === boot.active); });
  const backendPids = () => app.evaluate(() => process._getActiveHandles().filter(handle => Array.isArray(handle.spawnargs) && handle.spawnargs.includes('agent.cli.backend') && handle.exitCode === null).map(handle => handle.pid).sort());
  const reply = text => page.locator('.message.assistant').filter({ has: page.getByText(text, { exact: true }) });
  const retry = text => reply(text).getByRole('button', { name: '重新生成回复', exact: true });
  const submit = async text => { await composer.fill(text); await page.getByRole('button', { name: '发送', exact: true }).click(); };
  const idle = async () => {
    await expect.poll(async () => { const current = await state(); return current?.status === 'ready' && !current.busy && !current.responseOperation && !current.regeneration; }, { timeout: 30000 }).toBe(true);
    await expect(page.locator('[data-response-pending="true"]')).toHaveCount(0);
  };
  const completed = async text => { await expect(reply(text)).toBeVisible(); await idle(); await expect(retry(text)).toBeEnabled(); };
  const select = async (text, direction, expected) => {
    const before = requests.length;
    await reply(text).getByRole('button', { name: direction === 'previous' ? '上一个回复版本' : '下一个回复版本', exact: true }).click();
    await expect(reply(expected)).toBeVisible(); await idle();
    assert.equal(requests.length, before, 'selecting a stored branch must not call the model');
  };
  const assertPayload = (requestPhase, present, absent) => {
    const matching = requests.filter(request => request.phase === requestPhase);
    assert.equal(matching.length, 1, `${requestPhase} must cause exactly one model request`);
    const payloadText = JSON.stringify(matching[0].payload.messages);
    for (const value of present) assert.ok(payloadText.includes(value), `${requestPhase} context must contain ${value}`);
    for (const value of absent) assert.ok(!payloadText.includes(value), `${requestPhase} context must exclude ${value}`);
    return matching[0].payload;
  };
  const activeLog = async text => page.evaluate(async text => {
    const boot = await window.astra.bootstrap(), current = boot.sessions.find(item => item.id === boot.active);
    const message = current.messages.find(item => item.role === 'assistant' && item.content === text);
    const branch = current.branchId || current.responseVersions?.branch_id;
    if (!message?.source_ref || !branch) return null;
    const log = await window.astra.query('session_log', { name: current.session, mode: current.mode, branch_id: branch, source_ref: message.source_ref });
    return { source: message.source_ref, branch, returnedBranch: log.branch_id, revision: log.revision, status: log.target_status, records: log.records.map(record => record.raw) };
  }, text);

  // Exercise the real GUI image admission path, which saves text/Image parts
  // and later normalizes them when staging a conversation version.
  await app.evaluate(({ dialog }, path) => { dialog.showOpenDialog = async () => ({ canceled: false, filePaths: [path] }); }, imagePath);
  await page.getByRole('button', { name: '添加附件', exact: true }).click();
  await expect(page.getByRole('button', { name: '移除附件', exact: true })).toHaveCount(1);
  await composer.fill('ROOT_BRANCH_QUESTION');
  await page.screenshot({ path: join(output, 'image-root-input.png') });
  await submit('ROOT_BRANCH_QUESTION');
  await page.getByRole('button', { name: '允许一次', exact: true }).waitFor();
  assert.equal(existsSync(target), false);
  await page.getByRole('button', { name: '允许一次', exact: true }).click();
  await completed(answers.a);
  await expect(page.getByRole('button', { name: '移除附件', exact: true })).toHaveCount(0);
  const initialImageUser = requests[0].payload.messages.find(message => message.role === 'user');
  assert.ok(Array.isArray(initialImageUser.content), 'the original vision request must carry structured image content');
  assert.ok(initialImageUser.content.some(part => part.type === 'image_url' && part.image_url?.url.startsWith('data:image/png;base64,')), 'the original provider receives actual fixture image pixels');
  assert.equal(readFileSync(target, 'utf8'), 'ORIGINAL_EFFECT');
  const originalWriteTime = statSync(target, { bigint: true }).mtimeNs;
  const originalState = await state(), originalPids = await backendPids();
  assert.equal(originalPids.length, 1, 'one live fixture backend should own this conversation');
  const originalUser = originalState.messages.find(message => message.role === 'user' && message.content.includes('ROOT_BRANCH_QUESTION'));
  assert.ok(originalUser);
  await expect.poll(() => activeLog(answers.a)).not.toBeNull();
  const logA = await activeLog(answers.a);
  assert.equal(logA.status, 'found');
  const originalCanonicalUser = logA.records.map(record => JSON.parse(record)).find(message => message.role === 'user');
  assert.ok(Array.isArray(originalCanonicalUser.content), 'the canonical user must use the real Msg image storage representation');
  assert.ok(originalCanonicalUser.content.some(part => part.type === 'text' && part.text === imageMarker));
  assert.ok(originalCanonicalUser.content.some(part => part.type === 'text' && part.text.includes('ROOT_BRANCH_QUESTION')));
  await composer.fill('DRAFT_MUST_SURVIVE_REGENERATION');
  phase = 'regenerate-b';
  const beforeRegenerate = requests.length, beforeNavigation = navigations;
  await retry(answers.a).click();
  await page.locator('[data-response-pending="true"]').waitFor();
  await expect.poll(() => requests.length).toBe(beforeRegenerate + 1);
  await expect.poll(() => page.getByRole('button', { name: '重新生成回复', exact: true }).evaluateAll(buttons => buttons.every(button => button.disabled))).toBe(true);
  await expect.poll(() => page.getByRole('group', { name: '回复版本', exact: true }).locator('button').evaluateAll(buttons => buttons.every(button => button.disabled))).toBe(true);
  assert.equal((await state()).id, originalState.id);
  assert.deepEqual(await backendPids(), originalPids);
  assert.equal(await page.evaluate(() => window.__responseVersionsToken), originalToken);
  assert.equal(navigations, beforeNavigation);
  assert.equal(await composer.inputValue(), 'DRAFT_MUST_SURVIVE_REGENERATION');
  assert.equal((await state()).messages.filter(message => message.role === 'user' && message.content.includes('ROOT_BRANCH_QUESTION')).length, 1);
  const regeneratedPayload = assertPayload('regenerate-b', ['ROOT_BRANCH_QUESTION', imageMarker, 'original-write', 'ORIGINAL_EFFECT'], [answers.a]);
  const regeneratedUsers = regeneratedPayload.messages.filter(message => message.role === 'user');
  assert.equal(regeneratedUsers.length, 1, 'normalizing an image seed must not duplicate or lose the original user');
  assert.equal(typeof regeneratedUsers[0].content, 'string', 'regeneration uses the normalized saved image placeholder');
  assert.ok(regeneratedUsers[0].content.includes(imageMarker));
  assert.ok(!regeneratedUsers[0].content.includes('data:image/'), 'regeneration must not invent a second image admission');
  assert.equal(regeneratedPayload.tools?.length || 0, 0, 'reply regeneration must not advertise executable tools');
  releaseRegeneration(); await completed(answers.b);
  assert.equal((await state()).id, originalState.id);
  assert.deepEqual(await backendPids(), originalPids);
  assert.equal(navigations, beforeNavigation);
  assert.equal(await page.evaluate(() => window.__responseVersionsToken), originalToken);
  assert.equal(await composer.inputValue(), 'DRAFT_MUST_SURVIVE_REGENERATION');
  await expect(reply(answers.b).getByRole('group', { name: '回复版本', exact: true })).toContainText('2 / 2');
  passed('regeneration preserves an image user, draft, renderer and backend while normalizing the saved seed and retaining tool background');

  await select(answers.b, 'previous', answers.a);
  await expect(reply(answers.b)).toHaveCount(0);
  await expect.poll(() => activeLog(answers.a)).not.toBeNull();
  const restoredA = await activeLog(answers.a);
  assert.equal(restoredA.status, 'found');
  assert.ok(restoredA.records.some(record => record.includes(answers.a)));
  assert.deepEqual(restoredA.records.map(record => JSON.parse(record)).find(message => message.role === 'user'), originalCanonicalUser, 'regenerating an image reply leaves the original branch storage parts unchanged');
  await select(answers.a, 'next', answers.b);
  await expect.poll(() => activeLog(answers.b)).not.toBeNull();
  const logB = await activeLog(answers.b);
  assert.equal(logB.status, 'found');
  assert.equal(logB.returnedBranch, logB.branch);
  assert.notEqual(logB.branch, restoredA.branch);
  assert.ok(logB.records.some(record => record.includes(answers.b)));
  assert.notDeepEqual(logB.source, logA.source);
  assert.notEqual(logB.revision, restoredA.revision, 'canonical log revision follows the selected branch');
  const staleBranchRead = await page.evaluate(async previous => {
    const boot = await window.astra.bootstrap(), current = boot.sessions.find(item => item.id === boot.active);
    try { await window.astra.query('session_log', { name: current.session, mode: current.mode, branch_id: previous.branch, source_ref: previous.source }); return { rejected: false }; }
    catch (error) { return { rejected: true, error: String(error) }; }
  }, restoredA);
  assert.equal(staleBranchRead.rejected, true, 'an old branch log request must be rejected after selection changes');
  await page.screenshot({ path: join(output, 'two-root-versions.png') });
  passed('version arrows select saved replies without model calls and log references follow the selected branch');

  await select(answers.b, 'previous', answers.a);
  phase = 'continue-a'; await submit('FOLLOWUP_FOR_ALPHA'); await completed(answers.a2);
  assertPayload('continue-a', ['ROOT_BRANCH_QUESTION', imageMarker, answers.a, 'FOLLOWUP_FOR_ALPHA'], [answers.b, 'FOLLOWUP_FOR_BETA', answers.b2]);
  await select(answers.a, 'next', answers.b);
  await expect(reply(answers.a2)).toHaveCount(0);
  phase = 'continue-b'; await submit('FOLLOWUP_FOR_BETA'); await completed(answers.b2);
  assertPayload('continue-b', ['ROOT_BRANCH_QUESTION', imageMarker, answers.b, 'FOLLOWUP_FOR_BETA'], [answers.a, 'FOLLOWUP_FOR_ALPHA', answers.a2]);
  await select(answers.b, 'previous', answers.a);
  await expect(reply(answers.a2)).toBeVisible(); await expect(reply(answers.b2)).toHaveCount(0);
  await select(answers.a, 'next', answers.b);
  await expect(reply(answers.b2)).toBeVisible(); await expect(reply(answers.a2)).toHaveCount(0);
  passed('each root version retains its own follow-up, and continued requests contain only the selected branch context');

  phase = 'nested'; await retry(answers.b2).click(); await completed(answers.b2v2);
  const nestedPayload = assertPayload('nested', [answers.b, 'FOLLOWUP_FOR_BETA'], [answers.a, answers.a2, 'FOLLOWUP_FOR_ALPHA', answers.b2]);
  assert.equal(nestedPayload.tools?.length || 0, 0);
  await select(answers.b2v2, 'previous', answers.b2);
  await select(answers.b2, 'next', answers.b2v2);
  await select(answers.b, 'previous', answers.a);
  await expect(reply(answers.a2)).toBeVisible();
  await select(answers.a, 'next', answers.b);
  await expect(reply(answers.b2v2)).toBeVisible(); await expect(reply(answers.b2)).toHaveCount(0);
  await page.screenshot({ path: join(output, 'nested-branches.png') });
  passed('nested reply selection remains attached to its parent when switching root branches');

  await composer.fill('DRAFT_SURVIVES_RENDERER_RELOAD');
  await expect.poll(async () => {
    const boot = await page.evaluate(() => window.astra.bootstrap()), current = boot.sessions.find(item => item.id === boot.active);
    return boot.preferences.drafts[`${current.mode}:${current.session}`];
  }).toBe('DRAFT_SURVIVES_RENDERER_RELOAD');
  const beforeReload = requests.length;
  await page.reload(); await expect(composer).toBeVisible();
  await expect(reply(answers.b)).toBeVisible(); await completed(answers.b2v2);
  assert.equal(await composer.inputValue(), 'DRAFT_SURVIVES_RENDERER_RELOAD');
  assert.equal((await state()).id, originalState.id);
  assert.deepEqual(await backendPids(), originalPids);
  assert.equal(requests.length, beforeReload);
  passed('renderer reload restores both selected reply levels, the draft and the same backend without a model call');

  phase = 'failure'; const beforeFailure = requests.length;
  await retry(answers.b2v2).click();
  await expect.poll(() => requests.length).toBe(beforeFailure + 1); await idle();
  await expect(reply(answers.b2v2)).toBeVisible();
  await select(answers.b2v2, 'previous', answers.b2);
  await select(answers.b2, 'next', answers.b2v2);
  phase = 'cancel'; const beforeCancel = requests.length;
  await retry(answers.b2v2).click(); await page.locator('[data-response-pending="true"]').waitFor();
  await expect.poll(() => requests.length).toBe(beforeCancel + 1);
  await page.getByRole('button', { name: '停止', exact: true }).click(); await idle();
  releaseCancellation();
  await expect(reply(answers.b2v2)).toBeVisible();
  await expect(reply(answers.b2v2).getByRole('group', { name: '回复版本', exact: true })).toContainText('2 / 2');
  await select(answers.b2v2, 'previous', answers.b2);
  await select(answers.b2, 'next', answers.b2v2);
  passed('failed and cancelled generations keep completed versions reachable without creating a partial version');

  phase = 'forbidden-tool'; const beforeTool = requests.length;
  await retry(answers.b2v2).click();
  await expect.poll(() => requests.length).toBe(beforeTool + 1); await idle();
  await expect(reply(answers.b2v2)).toBeVisible();
  assert.equal(requests.length, beforeTool + 1, 'a refused tool call must not enter the tool execution loop');
  assert.equal((await state()).approvals.length, 0, 'regeneration must not request approval for a new tool');
  assert.equal(readFileSync(target, 'utf8'), 'ORIGINAL_EFFECT');
  assert.equal(statSync(target, { bigint: true }).mtimeNs, originalWriteTime, 'neither version changes nor regeneration may rewrite an old side effect');
  assert.equal(requests.at(-1).payload.tools?.length || 0, 0);
  passed('a provider tool call during regeneration is refused and the original file effect is never repeated');

  const beforeClose = requests.length, sessionName = (await state()).session;
  await page.getByRole('button', { name: /^会话操作：/ }).first().click();
  await page.getByRole('menuitem', { name: '关闭会话后端', exact: true }).click();
  await expect.poll(async () => (await page.evaluate(() => window.astra.bootstrap())).sessions.some(item => item.id === originalState.id)).toBe(false);
  const historyRow = page.locator('.session-row').filter({ hasText: 'ROOT_BRANCH_QUESTION' });
  await historyRow.locator('button').first().click();
  await page.getByRole('button', { name: '继续此会话', exact: true }).click();
  await completed(answers.b2v2);
  assert.equal((await state()).session, sessionName);
  assert.notEqual((await state()).id, originalState.id);
  assert.equal(requests.length, beforeClose, 'reopening must restore the selected branch without contacting the model');
  await select(answers.b, 'previous', answers.a); await expect(reply(answers.a2)).toBeVisible();
  await select(answers.a, 'next', answers.b); await expect(reply(answers.b2v2)).toBeVisible();
  await page.screenshot({ path: join(output, 'reopened-branch-selection.png') });
  assert.equal(statSync(target, { bigint: true }).mtimeNs, originalWriteTime);
  assert.deepEqual(readFileSync(imagePath), imageBytes, 'the original attached image remains unchanged across every branch operation');
  passed('closing and reopening the backend restores the selected nested branch and all alternate continuations');
  assert.deepEqual(errors, []); assert.deepEqual(externalRequests, []);
} catch (error) {
  failure = error; console.error(error);
  if (app) await bounded((async () => {
    const page = await app.firstWindow();
    console.error((await page.locator('body').innerText()).slice(-6000));
    console.error('REQUEST_PHASES', requests.map(request => request.phase));
    console.error('STATE', JSON.stringify(await page.evaluate(async () => { const boot = await window.astra.bootstrap(); return boot.sessions.map(state => ({ id: state.id, busy: state.busy, status: state.status, versions: state.responseVersions, regeneration: state.regeneration, operation: state.responseOperation, messages: state.messages.map(message => ({ id: message.id, role: message.role, content: message.content.slice(0, 100), source: message.source_ref })) })); })));
    await page.screenshot({ path: join(output, 'failure.png'), timeout: 2000 });
  })(), 'Failure diagnostics', 3000).catch(error => console.error(error));
} finally {
  releaseRegeneration(); releaseCancellation();
  if (app) {
    if (failure) await bounded(app.evaluate(({ dialog }) => {
      const original = dialog.showMessageBox.bind(dialog);
      dialog.showMessageBox = (...args) => {
        const options = args.at(-1), stop = options?.buttons?.indexOf('停止并退出') ?? -1;
        return options?.message === '仍有任务或会话提醒在运行' && stop >= 0 ? Promise.resolve({ response: stop, checkboxChecked: false }) : original(...args);
      };
    }), 'Fixture quit confirmation', 2000).catch(error => console.error(error));
    try { await bounded(app.close(), 'Electron close', 30000); }
    catch (error) { failure ||= error; console.error(error); await bounded(terminateFixture(app), 'Fixture process cleanup', 6000).catch(error => { failure ||= error; console.error(error); }); }
  }
  await bounded(new Promise((resolve, reject) => { server.close(error => error ? reject(error) : resolve()); server.closeAllConnections(); }), 'Fixture HTTP close', 3000).catch(error => { failure ||= error; console.error(error); });
}
writeFileSync(join(output, 'response-versions-result.json'), JSON.stringify({ checks, errors, externalRequests, folder, requestPhases: requests.map(request => request.phase), success: !failure, ...(failure ? { failure: String(failure) } : {}) }, null, 2));
if (failure) throw failure;
console.log(JSON.stringify({ checks: checks.length, output, folder }));
