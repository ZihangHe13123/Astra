// The memory page on a real desktop with an isolated home: it shows the core files with their room and the
// saved memories, and the person's edits, additions and forgets reach the stores the agent reads.
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { launchElectron } from './electron-launch.mjs';

const root = resolve(fileURLToPath(new URL('../..', import.meta.url)));
const folder = mkdtempSync(join(tmpdir(), 'astra-memory-page-'));
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
  ASTRA_WORKSPACE: workspace, LEARNING_REVIEW_AUTO: '0', ASTRA_PHONE_PORT: '0', ASTRA_CHANNEL_CONFIG: join(folder, 'channels.json') };

const run = code => execFileSync(python, ['-c', code], { cwd: root, env, encoding: 'utf8' }).trim();
run(`
from agent.runtime.memory import MemoryStore
store = MemoryStore()
store.add_core("user", "称呼：小林")
store.remember(kind="preference", summary="喜欢简短的回答", content="回答尽量简短，先说结论。")
store.remember(kind="user_fact", summary="养了一只猫", content="养了一只猫，叫团子。")
`);
const core = () => JSON.parse(run('import json\nfrom agent.runtime.memory import MemoryStore\nprint(json.dumps([e["content"] for e in MemoryStore().list_core("user")], ensure_ascii=False))'));
const active = () => JSON.parse(run('import json\nfrom agent.runtime.memory import MemoryStore\nprint(json.dumps(sorted(r.content for r in MemoryStore().record_store.list_records(status="active")), ensure_ascii=False))'));

let app;
try {
  app = await launchElectron({ args: [join(root, 'ui-gui')], env, timeout: 30000 });
  const page = await app.firstWindow();
  const errors = []; page.on('pageerror', error => errors.push(error.message));
  await page.getByRole('button', { name: '记忆', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '记忆' });
  const user = dialog.locator('section.memory-core[data-scope="user"]');
  await user.getByText('称呼：小林').waitFor();
  assert.match(await user.innerText(), /关于你[\s\S]*\d+ \/ 1375 字/);
  await dialog.locator('.memory-row.saved', { hasText: '喜欢简短的回答' }).waitFor();
  console.log('PASS the memory page shows the core files with their room and the saved memories');

  await user.locator('.memory-row', { hasText: '称呼：小林' }).getByRole('button', { name: '编辑' }).click();
  await user.getByLabel('编辑这条记忆').fill('称呼：林林');
  await user.getByRole('button', { name: '保存' }).click();
  await user.getByText('称呼：林林').waitFor();
  await user.getByLabel('在关于你里加一条').fill('时区：Asia/Singapore');
  await user.getByRole('button', { name: '添加' }).click();
  await user.getByText('时区：Asia/Singapore').waitFor();
  assert.deepEqual(core(), ['称呼：林林', '时区：Asia/Singapore']);
  console.log('PASS editing and adding a core entry writes USER.md');

  page.once('dialog', confirm => confirm.accept());
  const cat = dialog.locator('.memory-row.saved', { hasText: '养了一只猫' });
  await cat.locator('.memory-head').click();
  await cat.getByRole('button', { name: '忘记' }).click();
  await cat.waitFor({ state: 'detached' });
  assert.deepEqual(active(), ['回答尽量简短，先说结论。']);
  await dialog.getByLabel('显示旧版本和已忘记的').check();
  await dialog.locator('.memory-row.saved', { hasText: '养了一只猫' }).getByText('已忘记').waitFor();
  console.log('PASS forgetting a saved memory takes it out of use and keeps it in the history');
  assert.deepEqual(errors, []);
} finally {
  if (app) await app.close().catch(() => {});
  rmSync(folder, { recursive: true, force: true });
}
