/** Rich Markdown acceptance: real Electron and Python, isolated local model. */
import { _electron as electron } from 'playwright';
import { expect } from '@playwright/test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { mkdtempSync, mkdirSync, realpathSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(fileURLToPath(new URL('../..', import.meta.url)));
const folder = mkdtempSync(join(tmpdir(), 'astra-markdown-smoke-'));
const workspace = join(folder, 'workspace'); mkdirSync(workspace);
const sessionDir = join(folder, 'sessions'); mkdirSync(sessionDir);
const output = join(root, 'output/playwright/markdown'); mkdirSync(output, { recursive: true });
const graph = 'flowchart TD\n  A[输入] --> B[计算]\n  B --> C[结果]';
const source = [
  'RICH_MARKDOWN_COMPLETE',
  '行内美元：$E=mc^2$；括号：\\(a^2+b^2=c^2\\)。',
  '$$\n\\frac{a+b}{c}=\\sum_{i=1}^{n} i\n$$',
  '\\[\n\\int_0^1 x^2\\,dx=\\frac{1}{3}\n\\]',
  '```python\ndef square(x):\n    return x ** 2\n```',
  '```mermaid\n' + graph + '\n```',
].join('\n\n');
const partial = 'STREAMING_MARKDOWN\n\n```mermaid\n' + graph;
const completed = partial + '\n```\n\n' + source;
const invalid = 'INVALID_MARKDOWN_COMPLETE\n\n$$\\unknowncommand{broken}$$\n\n```mermaid\nflowchart TD\n A[Unclosed\n```\n\n```mermaid\nflowchart TD\n A["<img src=\'https://blocked-markdown.invalid/remote.png\' />"]\n```\n\nAFTER_INVALID_MARKDOWN';
const documentDir = join(workspace, 'documents'); mkdirSync(documentDir);
const documentPath = join(documentDir, 'rich-markdown.md');
const documentSource = 'DOCUMENT_RICH_MARKDOWN\n\n' + source + '\n\n![本地图片](./pixel.png)\n\n[本地参考](./reference.txt)';
writeFileSync(documentPath, documentSource);
writeFileSync(join(documentDir, 'reference.txt'), 'LOCAL_DOCUMENT_REFERENCE');
writeFileSync(join(documentDir, 'pixel.png'), Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+ip1sAAAAASUVORK5CYII=', 'base64'));
const oldSession = 'rich-markdown-history';
const archive = Array.from({ length: 261 }, (_, i) => ({ role: i % 2 ? 'assistant' : 'user', content: `HISTORY_${i}\n\n${'Earlier context. '.repeat(12)}` }));
archive[260] = { role: 'assistant', content: 'HISTORICAL_RICH_MARKDOWN\n\n' + source };
writeFileSync(join(sessionDir, oldSession + '.json'), JSON.stringify({ messages: archive }));
let releaseStream;
const streamGate = new Promise(resolve => { releaseStream = resolve; });
const requests = [];
const server = createServer(async (req, res) => {
  if (req.method === 'GET') { res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify({ data: [{ id: 'markdown-test' }] })); return; }
  if (!req.url.endsWith('/chat/completions')) { res.writeHead(404); res.end(); return; }
  let body = ''; for await (const part of req) body += part;
  const payload = JSON.parse(body); requests.push(payload);
  const text = String(payload.messages.findLast(message => message.role === 'user')?.content || '');
  res.setHeader('Content-Type', 'text/event-stream');
  const chunk = (delta, finish_reason = null) => res.write('data: ' + JSON.stringify({ id: 'markdown-' + requests.length, object: 'chat.completion.chunk', created: Math.floor(Date.now() / 1000), model: 'markdown-test', choices: [{ index: 0, delta, finish_reason }] }) + '\n\n');
  if (text.includes('STREAM_MARKDOWN')) {
    chunk({ role: 'assistant', content: partial });
    await streamGate;
    chunk({ content: '\n```\n\n' + source });
  } else chunk({ role: 'assistant', content: invalid });
  chunk({}, 'stop'); res.end('data: [DONE]\n\n');
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const port = server.address().port;
writeFileSync(join(folder, '.env'), '');
writeFileSync(join(folder, 'settings.json'), JSON.stringify({ selected_model: 'markdown-test' }));
writeFileSync(join(folder, 'models.yaml'), `version: 1\nproviders:\n  test:\n    label: Local fixture\n    provider: openai-compatible\n    base_url: http://127.0.0.1:${port}/v1\n    api_key_env: ''\n    context_limit: 131072\n    capabilities: [tools, streaming]\nmodels:\n  markdown-test:\n    provider: openai-compatible\n    base_url: http://127.0.0.1:${port}/v1\n    api_key_env: ''\n    context_limit: 131072\n    capabilities: [tools, streaming]\n`);
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
const errors = [], cspErrors = [], externalRequests = [], failedFonts = [], checks = [];
const passed = name => { checks.push(name); console.log('PASS', name); };
try {
  app = await electron.launch({ args: [join(root, 'ui-gui')], env, timeout: 30000 });
  const page = await app.firstWindow(); page.setDefaultTimeout(30000);
  page.on('pageerror', error => errors.push(error.message));
  page.on('console', message => { if (/content.security.policy|violates.*directive|refused to (?:load|execute)/i.test(message.text())) cspErrors.push(message.text()); });
  page.on('request', request => { if (/^https?:/i.test(request.url())) externalRequests.push(request.url()); });
  page.on('requestfailed', request => { if (/\.(?:woff2?|ttf|otf)(?:[?#]|$)/i.test(request.url())) failedFonts.push(request.url()); });
  await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].setContentSize(1320, 900));
  await page.waitForFunction(() => innerWidth === 1320);
  console.log('START', folder);
  const clipboardText = () => app.evaluate(({ clipboard }) => clipboard.readText());
  const waitForImage = async container => {
    await expect.poll(() => container.locator('img[alt="Mermaid 图表"]').evaluateAll(images => images.length > 0 && images.every(image => image.complete && image.naturalWidth > 0))).toBe(true);
  };
  const submit = async text => { await page.getByRole('textbox', { name: '消息' }).fill(text); await page.getByRole('button', { name: '发送', exact: true }).click(); };
  const waitIdle = async () => {
    // Async IPC must be awaited by expect.poll; waitForFunction(async ...) can
    // accept a Promise before the state it represents has become true.
    await expect.poll(async () => {
      const boot = await page.evaluate(() => window.astra.bootstrap());
      return boot.sessions.some(state => state.id === boot.active && !state.busy && state.info.task_status?.task?.status === 'completed');
    }, { timeout: 30000 }).toBe(true);
  };
  const assertMeasuredRows = async () => {
    await expect.poll(async () => page.locator('.measured-message').evaluateAll(rows => rows.every(row => {
      const child = row.firstElementChild;
      return child && row.getBoundingClientRect().height >= child.getBoundingClientRect().height;
    }))).toBe(true);
    assert.ok(await page.locator('.measured-message').count() < 80, 'history must remain virtualized');
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'rich content must not overflow the window');
  };

  await submit('STREAM_MARKDOWN');
  const streaming = page.locator('.message.assistant').filter({ hasText: 'STREAMING_MARKDOWN' });
  await streaming.locator('code').filter({ hasText: 'flowchart TD' }).waitFor();
  assert.equal(await streaming.locator('.mermaid-block img[alt="Mermaid 图表"]').count(), 0, 'an unfinished diagram remains source');
  passed('an unclosed streamed Mermaid fence stays readable source');
  releaseStream();
  await streaming.getByText('RICH_MARKDOWN_COMPLETE', { exact: true }).waitFor();
  await waitIdle();
  await expect(streaming.locator('.katex')).toHaveCount(4);
  await expect(streaming.locator('.katex-display')).toHaveCount(2);
  await expect(streaming.locator('code.hljs span').first()).toBeVisible();
  await expect(streaming.locator('.mermaid-block img[alt="Mermaid 图表"]')).toHaveCount(2);
  await expect(streaming.getByRole('img', { name: /Mermaid/ }).first()).toBeVisible();
  await waitForImage(streaming);
  assert.ok(await streaming.locator('.mermaid-block img').first().evaluate(image => {
    const rect = image.getBoundingClientRect(); return rect.width <= 360 && rect.height <= 600;
  }), 'a simple three-node diagram must retain a readable intrinsic size instead of being enlarged to the full chat width');
  passed('all four math delimiters, highlighted code, and completed accessible diagrams render in live chat');

  await streaming.getByRole('button', { name: '复制消息', exact: true }).click();
  await expect.poll(clipboardText).toBe(completed);
  const python = streaming.locator('.code-block').filter({ has: page.locator('code.language-python') });
  await python.getByRole('button', { name: '复制代码', exact: true }).click();
  await expect.poll(clipboardText).toBe('def square(x):\n    return x ** 2\n');
  const diagram = streaming.locator('.mermaid-block').last();
  await diagram.getByRole('button', { name: '查看源码', exact: true }).click();
  await expect(diagram.locator('code')).toHaveText(graph + '\n');
  await diagram.getByRole('button', { name: '复制 Mermaid 源码', exact: true }).click();
  await expect.poll(clipboardText).toBe(graph + '\n');
  await diagram.getByRole('button', { name: '查看图表', exact: true }).click();
  await expect(diagram.locator('img[alt="Mermaid 图表"]')).toBeVisible();
  await waitForImage(diagram);
  passed('message copy preserves Markdown, while code and diagram copy preserve their source');

  await page.evaluate(() => document.fonts.ready);
  assert.ok(await page.evaluate(() => [...document.fonts].some(font => /KaTeX/.test(font.family) && font.status === 'loaded')), 'KaTeX fonts must load locally');
  // Custom app protocol font fetches need not appear in Resource Timing.
  // Inspect font-face rules alongside FontFaceSet's loaded state instead.
  const fonts = await page.evaluate(() => [...document.styleSheets].flatMap(sheet => [...sheet.cssRules].flatMap(rule => {
    if (!(rule instanceof CSSFontFaceRule) || !/KaTeX/.test(rule.style.fontFamily)) return [];
    return [...rule.style.getPropertyValue('src').matchAll(/url\(["']?([^"')]+)["']?\)/g)].map(match => new URL(match[1], sheet.href || location.href).href);
  })));
  assert.ok(fonts.length > 0, 'loaded KaTeX fonts have corresponding bundled font-face rules');
  assert.ok(fonts.every(value => { const url = new URL(value); return url.protocol === 'file:' || url.protocol === 'astra:' && url.host === 'app' || /^data:font\/(?:woff2?|ttf);base64,/.test(value); }), 'formula fonts are bundled, never fetched from a CDN');
  passed('KaTeX uses bundled fonts under the existing Electron content policy');

  const themeColors = [];
  for (const theme of ['light', 'dark']) {
    const oldURL = await diagram.locator('img[alt="Mermaid 图表"]').getAttribute('src');
    const previousTheme = await page.evaluate(() => document.documentElement.dataset.theme === 'system'
      ? matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
      : document.documentElement.dataset.theme);
    await page.evaluate(theme => { document.documentElement.dataset.theme = theme; }, theme);
    if (previousTheme !== theme) await expect.poll(() => diagram.locator('img[alt="Mermaid 图表"]').getAttribute('src').catch(() => null)).not.toBe(oldURL);
    await expect(diagram.locator('img[alt="Mermaid 图表"]')).toBeVisible();
    await waitForImage(diagram);
    if (previousTheme !== theme) assert.notEqual(await diagram.locator('img[alt="Mermaid 图表"]').getAttribute('src'), oldURL, 'theme changes must produce a newly rendered diagram');
    await streaming.locator('.katex').last().scrollIntoViewIfNeeded();
    themeColors.push(await streaming.locator('.katex').first().evaluate(el => getComputedStyle(el).color));
    await page.screenshot({ path: join(output, `math-${theme}.png`) });
    await diagram.scrollIntoViewIfNeeded();
    await page.screenshot({ path: join(output, `diagram-${theme}.png`) });
  }
  assert.notEqual(themeColors[0], themeColors[1], 'math foreground must follow the selected light/dark theme');
  await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].setContentSize(1000, 820));
  await page.waitForFunction(() => innerWidth === 1000);
  await assertMeasuredRows();
  await diagram.scrollIntoViewIfNeeded();
  await page.screenshot({ path: join(output, 'narrow-rich-markdown.png') });
  passed('light, dark, and 1000px layouts keep rich content visible without window overflow');

  await submit('INVALID_MARKDOWN');
  const broken = page.locator('.message.assistant').filter({ hasText: 'INVALID_MARKDOWN_COMPLETE' });
  await broken.getByText('AFTER_INVALID_MARKDOWN', { exact: true }).waitFor();
  await waitIdle();
  await expect(broken.locator('code').filter({ hasText: 'A[Unclosed' })).toBeVisible();
  await expect(broken.locator('code').filter({ hasText: 'https://blocked-markdown.invalid/remote.png' })).toBeVisible();
  assert.match(await broken.innerText(), /unknowncommand/);
  assert.equal(await broken.locator('.mermaid-block img[alt="Mermaid 图表"]').count(), 0);
  await broken.getByRole('button', { name: '复制消息', exact: true }).click();
  await expect.poll(clipboardText).toBe(invalid);
  passed('invalid math and diagrams preserve source and the rest of the message remains usable');

  // Select a fixture through the normal file chooser IPC. The renderer never
  // receives an injected preview or arbitrary file-read capability.
  await app.evaluate(({ dialog, shell }, path) => {
    dialog.showOpenDialog = async () => ({ canceled: false, filePaths: [path] });
    globalThis.__markdownOpenedPaths = [];
    shell.openPath = async target => { globalThis.__markdownOpenedPaths.push(target); return ''; };
  }, documentPath);
  await page.getByRole('button', { name: '执行详情', exact: true }).click();
  await page.getByRole('tab', { name: '文件', exact: true }).click();
  await page.getByRole('button', { name: '选择文件预览', exact: true }).click();
  const preview = page.getByRole('region', { name: '文件预览', exact: true });
  await expect(preview.locator('.katex')).toHaveCount(4);
  await expect(preview.locator('code.hljs span').first()).toBeVisible();
  await waitForImage(preview);
  await expect.poll(() => preview.getByRole('img', { name: '本地图片', exact: true }).evaluate(image => image.complete && image.naturalWidth > 0)).toBe(true);
  await preview.getByRole('link', { name: '本地参考', exact: true }).click();
  await expect.poll(() => app.evaluate(() => globalThis.__markdownOpenedPaths)).toEqual([realpathSync(join(documentDir, 'reference.txt'))]);
  await preview.getByRole('button', { name: '源码', exact: true }).click();
  await expect(preview.locator('pre.file-preview')).toHaveText(documentSource);
  await preview.getByRole('button', { name: '预览', exact: true }).click();
  await waitForImage(preview);
  await page.screenshot({ path: join(output, 'file-rich-markdown.png') });
  await page.getByRole('button', { name: '关闭详情', exact: true }).click();
  passed('Markdown file previews share math, code, and diagrams while resolving local images and links relative to the document');

  const countBeforeHistory = requests.length;
  await page.locator('.session-row').filter({ hasText: oldSession }).locator('button').first().click();
  await page.getByText('只读浏览历史，不会启动模型。').waitFor();
  const historical = page.locator('.message.assistant').filter({ hasText: 'HISTORICAL_RICH_MARKDOWN' });
  await expect(historical.locator('.katex')).toHaveCount(4);
  await expect(historical.locator('.mermaid-block img[alt="Mermaid 图表"]')).toHaveCount(1);
  await waitForImage(historical);
  const historyDiagram = historical.locator('.mermaid-block');
  await historyDiagram.getByRole('button', { name: '查看源码', exact: true }).click();
  await expect(historyDiagram.locator('code')).toContainText(graph);
  await assertMeasuredRows();
  await historyDiagram.getByRole('button', { name: '查看图表', exact: true }).click();
  await expect(historyDiagram.locator('img[alt="Mermaid 图表"]')).toBeVisible();
  await waitForImage(historyDiagram);
  await assertMeasuredRows();
  await page.locator('.messages-scroll').evaluate(el => { el.scrollTop = 0; });
  await expect(historical).toHaveCount(0);
  await page.locator('.messages-scroll').evaluate(el => { el.scrollTop = el.scrollHeight; });
  await expect(historical.locator('.mermaid-block img[alt="Mermaid 图表"]')).toBeVisible();
  await waitForImage(historical);
  await assertMeasuredRows();
  assert.equal(requests.length, countBeforeHistory, 'opening historical rich text must not start the model');
  await page.screenshot({ path: join(output, 'history-rich-markdown.png') });
  passed('historical rich text survives virtual unmounts and diagram height changes without starting a runtime');
  assert.deepEqual(errors, []);
  assert.deepEqual(cspErrors, []);
  assert.deepEqual(externalRequests, []);
  assert.deepEqual(failedFonts, []);
  passed('no renderer exceptions, CSP violations, failed fonts, or external renderer requests');
} catch (error) {
  failure = error; console.error(error);
  if (app) await bounded((async () => {
    const page = await app.firstWindow();
    console.error((await page.locator('body').innerText()).slice(-5000));
    console.error(JSON.stringify({ errors, cspErrors, externalRequests, failedFonts }));
    await page.screenshot({ path: join(output, 'failure.png'), timeout: 2000 });
  })(), 'Failure diagnostics', 3000).catch(error => console.error(error));
} finally {
  releaseStream();
  if (app) {
    if (failure) await bounded(app.evaluate(({ dialog }) => {
      const original = dialog.showMessageBox.bind(dialog);
      dialog.showMessageBox = (...args) => {
        const options = args.at(-1), stop = options?.buttons?.indexOf('停止并退出') ?? -1;
        return options?.message === '仍有任务或会话提醒在运行' && stop >= 0 ? Promise.resolve({ response: stop, checkboxChecked: false }) : original(...args);
      };
    }), 'Fixture quit confirmation', 2000).catch(error => console.error(error));
    try { await bounded(app.close(), 'Electron close', 30000); }
    catch (error) {
      failure ||= error; console.error(error);
      await bounded(terminateFixture(app), 'Fixture process cleanup', 6000).catch(error => { failure ||= error; console.error(error); });
    }
  }
  await bounded(new Promise((resolve, reject) => { server.close(error => error ? reject(error) : resolve()); server.closeAllConnections(); }), 'Fixture HTTP close', 3000).catch(error => { failure ||= error; console.error(error); });
}
writeFileSync(join(output, 'markdown-result.json'), JSON.stringify({ checks, errors, cspErrors, externalRequests, failedFonts, folder, success: !failure, ...(failure ? { failure: String(failure) } : {}) }, null, 2));
if (failure) throw failure;
console.log(JSON.stringify({ checks: checks.length, output, folder }));
