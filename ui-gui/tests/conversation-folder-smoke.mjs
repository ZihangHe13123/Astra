import { expect } from '@playwright/test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, readFileSync, realpathSync, renameSync, writeFileSync } from 'node:fs';
import { createServer } from 'node:http';
import { tmpdir } from 'node:os';
import { basename, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { launchElectron } from './electron-launch.mjs';

const root = resolve(fileURLToPath(new URL('../..', import.meta.url)));
const fixture = realpathSync(mkdtempSync(join(tmpdir(), 'astra-conversation-folder-')));
const projectA = join(fixture, 'A');
const projectB = join(fixture, 'B');
const projectC = join(fixture, 'C');
const data = join(fixture, 'state');
for (const folder of [projectA, projectB, projectC, data]) mkdirSync(folder);

const firstPrompt = 'Save this conversation in its original folder';
const continuedPrompt = 'Continue this conversation from the sidebar';
const movedPrompt = 'Continue after moving this saved conversation';
const firstReply = 'FIRST_FOLDER_REPLY';
const continuedReply = 'CONTINUED_FOLDER_REPLY';
const movedReply = 'MOVED_FOLDER_REPLY';
const modelName = 'folder-test';
const requests = [];
const model = createServer(async (request, response) => {
  if (request.method === 'GET' && request.url?.endsWith('/models')) {
    response.setHeader('Content-Type', 'application/json');
    response.end(JSON.stringify({ data: [{ id: modelName }] }));
    return;
  }
  if (request.method !== 'POST' || !request.url?.endsWith('/chat/completions')) {
    response.writeHead(404).end();
    return;
  }
  let body = '';
  for await (const chunk of request) body += chunk;
  const payload = JSON.parse(body);
  requests.push(payload);
  const user = JSON.stringify(payload.messages.findLast(message => message.role === 'user')?.content ?? '');
  const reply = user.includes(movedPrompt) ? movedReply : user.includes(continuedPrompt) ? continuedReply : firstReply;
  response.setHeader('Content-Type', 'text/event-stream');
  const chunk = (delta, finish_reason = null) => response.write(`data: ${JSON.stringify({
    id: `folder-${requests.length}`, object: 'chat.completion.chunk', created: Math.floor(Date.now() / 1000),
    model: modelName, choices: [{ index: 0, delta, finish_reason }],
  })}\n\n`);
  chunk({ role: 'assistant', content: reply });
  chunk({}, 'stop');
  response.end('data: [DONE]\n\n');
});
await new Promise(done => model.listen(0, '127.0.0.1', done));

const port = model.address().port;
const profile = `    provider: openai-compatible\n    base_url: http://127.0.0.1:${port}/v1\n    api_key_env: ''\n    context_limit: 131072\n    capabilities: [tools, streaming]\n`;
const cleanEnv = Object.fromEntries(Object.entries(process.env).filter(([key]) => !/API_KEY|TOKEN|SECRET|^AGENT_|^ASTRA_|^LLM_|^SANDBOX_|^ELECTRON_/.test(key)));
const env = {
  ...cleanEnv,
  ASTRA_GUI_DISABLE_APPSHOT: '1',
  ASTRA_HOME: data,
  ASTRA_ENV_FILE: join(data, '.env'),
  ASTRA_PHONE_PORT: '0',
  ASTRA_CHANNEL_CONFIG: join(data, 'missing-channels.json'),
  AGENT_PROJECT_ROOT: root,
  AGENT_PYTHON: process.env.AGENT_PYTHON || join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python'),
  AGENT_SETTINGS_PATH: join(data, 'settings.json'),
  AGENT_SESSION_DIR: join(data, 'sessions'),
  AGENT_TASK_DB: join(data, 'tasks.db'),
  ASTRA_APPROVAL_DB: join(data, 'approvals.db'),
  ASTRA_EVENT_DB: join(data, 'events.db'),
  AGENT_MEMORY_PATH: join(data, 'memory.db'),
  AGENT_LEARNING_PATH: join(data, 'learning.db'),
  AGENT_SKILLS_PATH: join(data, 'skills'),
  AGENT_MODELS_FILE: join(data, 'models.yaml'),
  AGENT_USER_MODELS_FILE: join(data, 'missing-models.yaml'),
  AGENT_LOG_DIR: join(data, 'logs'),
  AGENT_MCP_CONFIG: join(data, 'missing-mcp.json'),
  AGENT_TOOL_POLICY: 'locked',
  LEARNING_REVIEW_AUTO: '0',
  SANDBOX_DOCKER: 'false',
  SANDBOX_WORKDIR: projectB,
  ASTRA_WORKSPACE: projectB,
};

async function waitForReply(page, id, reply) {
  await expect.poll(() => page.evaluate(async ({ id, reply }) => {
    const runtime = (await window.astra.bootstrap()).sessions.find(state => state.id === id);
    return runtime?.status === 'ready' && !runtime.busy
      && runtime.messages.some(message => message.role === 'assistant' && message.content === reply);
  }, { id, reply }), { timeout: 30000 }).toBe(true);
}

async function reopenedInA(page, session, action) {
  let state;
  await expect.poll(async () => {
    state = await page.evaluate(async session => {
      const boot = await window.astra.bootstrap();
      return boot.sessions.find(runtime => runtime.id === boot.active && runtime.session === session && runtime.mode === 'work');
    }, session);
    return ['ready', 'disconnected'].includes(state?.status);
  }, { timeout: 30000 }).toBe(true);
  const mismatch = state.notices.find(event => event.code === 'workspace_mismatch');
  assert.ok(!mismatch, `${action} opened ${state.workspace}: workspace_mismatch: ${mismatch?.message}`);
  assert.equal(realpathSync(state.workspace), projectA, `${action} must open the recorded workspace`);
  assert.equal(state.status, 'ready', `${action} must start a usable runtime`);
  return state.id;
}

async function readyRuntimeIn(page, session, folder) {
  let runtimes;
  await expect.poll(async () => {
    runtimes = await page.evaluate(async session => (await window.astra.bootstrap()).sessions.filter(state => state.session === session && state.mode === 'work'), session);
    return runtimes.map(({ status, workspace, notices }) => ({
      status, workspace, workspaceMismatch: notices.some(event => event.code === 'workspace_mismatch'),
    }));
  }, { timeout: 30000 }).toEqual([{ status: 'ready', workspace: folder, workspaceMismatch: false }]);
  return runtimes[0].id;
}

const p3Failures = [];
async function checkProjectStep(name, check) {
  try {
    await check();
    console.log(`PASS ${name}`);
  } catch (error) {
    p3Failures.push(name);
    console.error(`FAIL ${name}\n${error.message}`);
  }
}

let app;
try {
  writeFileSync(env.ASTRA_ENV_FILE, '');
  writeFileSync(env.AGENT_SETTINGS_PATH, JSON.stringify({ selected_model: modelName }));
  writeFileSync(env.AGENT_MODELS_FILE, `version: 1\nproviders:\n  test:\n    label: Local fixture\n${profile}models:\n  ${modelName}:\n${profile}`);
  app = await launchElectron({ args: [join(root, 'ui-gui')], env, timeout: 30000 });
  const page = await app.firstWindow();
  page.setDefaultTimeout(30000);
  await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].setContentSize(1320, 900));
  const workspace = page.locator('.workspace');
  await expect(workspace).toHaveAttribute('title', projectB);

  const initial = await page.evaluate(async ({ workspace, text }) => {
    const state = await window.astra.create({ workspace });
    await window.astra.send(state.id, { type: 'message', text, submission_id: crypto.randomUUID() });
    return { id: state.id, session: state.session };
  }, { workspace: projectA, text: firstPrompt });
  await waitForReply(page, initial.id, firstReply);
  await page.evaluate(id => window.astra.close(id), initial.id);
  await page.evaluate(() => window.astra.preferences({ workspaces: {} }));
  await page.reload();
  await expect(workspace).toHaveAttribute('title', projectB);
  const preferences = await page.evaluate(async () => (await window.astra.bootstrap()).preferences);
  assert.equal(preferences.workspaces[`work:${initial.session}`], undefined);

  const submit = async text => {
    await page.getByRole('textbox', { name: '消息', exact: true }).fill(text);
    await page.getByRole('button', { name: '发送', exact: true }).click();
  };
  await page.locator(`.sidebar .session-row button[title=${JSON.stringify(initial.session)}]`).click();
  const continueButton = page.getByRole('button', { name: '继续此会话', exact: true });
  await continueButton.click();
  const continued = await reopenedInA(page, initial.session, 'Continue button');
  assert.notEqual(continued, initial.id);
  await submit(continuedPrompt);
  await waitForReply(page, continued, continuedReply);
  await expect(page.locator('.message.assistant', { hasText: continuedReply })).toBeVisible();
  assert.ok(requests.some(request => request.messages.some(message => message.role === 'user'
    && JSON.stringify(message.content).includes(continuedPrompt))));
  console.log('PASS Continue reopened the saved conversation in A and displayed the new reply');

  await page.evaluate(id => window.astra.close(id), continued);
  await expect(page.locator('.topbar .title')).toHaveText('新对话');
  await page.reload();
  await expect(workspace).toHaveAttribute('title', projectB);
  await submit(`/session ${initial.session}`);
  const reopened = await reopenedInA(page, initial.session, '/session <name>');
  assert.notEqual(reopened, continued);
  console.log('PASS /session <name> reopened the saved conversation in A');

  await submit('/session --all');
  const listing = page.locator('details.command-result', { hasText: 'All sessions (' });
  await listing.waitFor();
  if (await listing.getAttribute('open') === null) await listing.locator('summary').click();
  await expect(listing.getByText(/All sessions \(/)).toBeVisible();
  await expect(listing).toContainText(initial.session);
  const boot = await page.evaluate(() => window.astra.bootstrap());
  assert.equal(boot.active, reopened);
  assert.ok(boot.sessions.every(state => state.session !== '--all'));
  assert.ok(boot.sessions.every(state => state.notices.every(event => event.code !== 'workspace_mismatch')));
  console.log('PASS /session --all displayed the backend list in the same runtime');

  const sidebar = page.locator('.sidebar');
  const group = folder => sidebar.locator(`.project-group[data-folder=${JSON.stringify(folder)}]`);
  const projectButton = folder => sidebar.locator(`button.project[title=${JSON.stringify(folder)}]`);
  const sessionButton = session => `.session-row button[title=${JSON.stringify(session)}]`;
  await app.evaluate(({ dialog }, folders) => {
    const showOpenDialog = dialog.showOpenDialog;
    dialog.showOpenDialog = async () => {
      const folder = folders.shift();
      if (!folders.length) dialog.showOpenDialog = showOpenDialog;
      return { canceled: false, filePaths: [folder] };
    };
  }, [projectA, projectB]);
  const addProject = sidebar.getByRole('button', { name: '添加项目', exact: true });
  for (const folder of [projectA, projectB]) {
    await addProject.click();
    await expect(projectButton(folder)).toBeVisible();
  }
  await page.reload();
  await expect(projectButton(projectB)).toBeVisible();
  assert.equal(await page.evaluate(async () => (await window.astra.bootstrap()).workspace), projectB);
  await checkProjectStep('P3 1 saved conversation appears under the added project', async () => {
    await expect(group(projectA).locator(sessionButton(initial.session))).toBeVisible();
    await expect(sidebar.locator('.other-sessions').locator(sessionButton(initial.session))).toHaveCount(0);
    const output = join(root, 'output/playwright/gui');
    mkdirSync(output, { recursive: true });
    await sidebar.screenshot({ path: join(output, 'project-groups.png') });
  });

  await projectButton(projectB).click();
  await submit('Start a new conversation in project B');
  let projectBSession;
  await expect.poll(async () => {
    projectBSession = await page.evaluate(async () => {
      const { active, sessions } = await window.astra.bootstrap();
      return sessions.find(runtime => runtime.id === active);
    });
    return !!projectBSession && projectBSession.session !== initial.session;
  }, { timeout: 30000 }).toBe(true);
  await waitForReply(page, projectBSession.id, firstReply);
  await checkProjectStep('P3 2 open conversation appears in B and in open shortcuts', async () => {
    assert.equal(realpathSync(projectBSession.workspace), projectB);
    await expect(sidebar.getByRole('heading', { name: '已打开', exact: true })).toBeVisible();
    const shortcut = sidebar.locator(`.session-scroll > ${sessionButton(projectBSession.session)}`);
    await expect(shortcut).toBeVisible();
    await expect(group(projectB).locator(sessionButton(projectBSession.session))).toBeVisible();
    await expect(shortcut.locator('.session-project')).toHaveText(basename(projectB));
  });

  const headerPath = session => join(env.AGENT_SESSION_DIR, `${session}.header.json`);
  await page.evaluate(async session => {
    const { sessions } = await window.astra.bootstrap();
    for (const state of sessions) if (state.session === session && state.mode === 'work') await window.astra.close(state.id);
  }, initial.session);
  renameSync(projectA, join(fixture, 'A-moved'));
  await page.reload();
  await sidebar.locator(sessionButton(initial.session)).click();
  await continueButton.click();
  const banner = page.locator('.history-banner');
  const toast = page.locator('.toast');
  const move = page.getByRole('button', { name: '在当前项目继续', exact: true });
  await checkProjectStep('P3 3 missing folder leaves the conversation in read-only history', async () => {
    await expect.poll(async () => await move.isVisible() || await toast.isVisible()).toBe(true);
    assert.equal((await toast.allTextContents()).join(''), '', 'Opening the missing folder must not show an error toast');
    await expect(banner).toContainText('不在了');
    await expect(move).toBeVisible();
    const { sessions } = await page.evaluate(() => window.astra.bootstrap());
    assert.ok(sessions.every(state => state.session !== initial.session || state.mode !== 'work'));
  });

  await checkProjectStep('P3 4 continuing in B shows the move and saves the new folder', async () => {
    await expect(move).toBeVisible();
    await move.click();
    const moved = await readyRuntimeIn(page, initial.session, projectB);
    await expect(toast).toContainText('Moved session');
    await expect(toast).toContainText(projectB);
    await submit(movedPrompt);
    await waitForReply(page, moved, movedReply);
    assert.equal(JSON.parse(readFileSync(headerPath(initial.session), 'utf8')).workspace, projectB);
  });

  await page.evaluate(id => window.astra.close(id), projectBSession.id);
  await expect(sidebar.locator(`${sessionButton(projectBSession.session)} .session-status`)).toHaveCount(0);
  await expect.poll(() => page.evaluate(async session => {
    const { lastSession } = (await window.astra.bootstrap()).preferences;
    return lastSession?.session === session && lastSession.mode === 'work';
  }, projectBSession.session)).toBe(false);
  await page.reload();
  await checkProjectStep('P3 5 stale project metadata reopens once in C after closing the rejected runtime', async () => {
    const staleRow = group(projectB).locator(sessionButton(projectBSession.session));
    await expect(staleRow).toBeVisible();
    const path = headerPath(projectBSession.session);
    const header = JSON.parse(readFileSync(path, 'utf8'));
    assert.equal(header.workspace, projectB);
    writeFileSync(path, JSON.stringify({ ...header, workspace: projectC }));
    assert.ok(await staleRow.isVisible(), 'The sidebar refreshed and no longer has the stale row in B');
    await staleRow.click();
    await continueButton.click();
    await readyRuntimeIn(page, projectBSession.session, projectC);
  });

  if (p3Failures.length) throw new Error(`Failed project steps: ${p3Failures.join(', ')}`);
} catch (error) {
  console.error('Isolated fixture state:', data);
  throw error;
} finally {
  try {
    if (app) {
      const child = app.process();
      const stop = setTimeout(() => {
        if (child.exitCode === null && child.signalCode === null) child.kill('SIGKILL');
      }, 15000);
      try { await app.close(); } finally { clearTimeout(stop); }
    }
  } finally {
    model.closeAllConnections();
    await new Promise(done => model.close(done));
  }
}
