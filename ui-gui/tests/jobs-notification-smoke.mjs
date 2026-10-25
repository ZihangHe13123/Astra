// A reminder due in an isolated home: the desktop starts its background host for it, the host runs it,
// and the desktop shows it as a system notification, once. No channel is switched on.
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { existsSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { launchElectron } from './electron-launch.mjs';

const root = resolve(fileURLToPath(new URL('../..', import.meta.url)));
const folder = mkdtempSync(join(tmpdir(), 'astra-jobs-notification-'));
const workspace = join(folder, 'workspace');
mkdirSync(workspace);
writeFileSync(join(folder, '.env'), '');
const python = process.env.AGENT_PYTHON || join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
const cleanEnv = Object.fromEntries(Object.entries(process.env).filter(([key]) => !/API_KEY|TOKEN|SECRET|^AGENT_|^ASTRA_|^LLM_|^SANDBOX_|^ELECTRON_/.test(key)));
const env = { ...cleanEnv, ASTRA_GUI_DISABLE_APPSHOT: '1', AGENT_PROJECT_ROOT: root, AGENT_PYTHON: python,
  ASTRA_ENV_FILE: join(folder, '.env'), ASTRA_HOME: folder, AGENT_SETTINGS_PATH: join(folder, 'settings.json'),
  AGENT_SESSION_DIR: join(folder, 'sessions'), AGENT_TASK_DB: join(folder, 'tasks.db'), ASTRA_APPROVAL_DB: join(folder, 'approvals.db'),
  ASTRA_EVENT_DB: join(folder, 'events.db'), AGENT_MEMORY_PATH: join(folder, 'memory.db'), AGENT_LEARNING_PATH: join(folder, 'learning.db'),
  AGENT_SKILLS_PATH: join(folder, 'skills'), AGENT_USER_MODELS_FILE: join(folder, 'missing-models.yaml'), AGENT_LOG_DIR: join(folder, 'logs'),
  AGENT_MCP_CONFIG: join(folder, 'missing-mcp.json'), AGENT_TOOL_POLICY: 'locked', SANDBOX_DOCKER: 'false', SANDBOX_WORKDIR: workspace,
  ASTRA_WORKSPACE: workspace, LEARNING_REVIEW_AUTO: '0', ASTRA_PHONE_PORT: '0',
  // No messaging channel: the job alone must bring the background host up.
  ASTRA_CHANNEL_CONFIG: join(folder, 'channels.json'), ASTRA_JOBS_TICK_SECONDS: '0.5', ASTRA_JOBS_PUSH_SECONDS: '0.5' };

// Due in a few seconds, pushed to the desktop only.
const seed = `
import sys, time
from pathlib import Path
from agent.runtime.jobs.store import JobStore
JobStore(Path(sys.argv[1])).add(name="喝水", prompt="该喝水了", original_request="提醒我喝水", kind="reminder",
                                workdir=Path(sys.argv[2]), next_at=time.time() + 4, targets=["desktop"])
`;
execFileSync(python, ['-c', seed, folder, workspace], { cwd: root, env });
const marker = join(folder, 'jobs', 'active');
assert.ok(existsSync(marker), 'the job store asks for a background host');

let app;
try {
  app = await launchElectron({ args: [join(root, 'ui-gui')], env, timeout: 30000 });
  await app.firstWindow();
  await app.evaluate(({ Notification }) => {
    globalThis.__astraShown = [];
    const show = Notification.prototype.show;
    Notification.prototype.show = function () { globalThis.__astraShown.push({ title: this.title, body: this.body }); return show.call(this); };
  });
  const deadline = Date.now() + 60000;
  let shown = [];
  while (Date.now() < deadline) {
    shown = await app.evaluate(() => globalThis.__astraShown);
    if (shown.length) break;
    await new Promise(r => setTimeout(r, 250));
  }
  assert.deepEqual(shown, [{ title: '提醒：喝水', body: '该喝水了' }]);
  console.log('PASS the background host ran the reminder and the desktop showed it');
  await new Promise(r => setTimeout(r, 3000));
  assert.equal((await app.evaluate(() => globalThis.__astraShown)).length, 1, 'shown once');
  assert.ok(!existsSync(marker), 'nothing left to run or push');
  console.log('PASS shown once; the job store no longer asks for a host');
} finally {
  if (app) await app.close().catch(() => {});
  rmSync(folder, { recursive: true, force: true });
}
