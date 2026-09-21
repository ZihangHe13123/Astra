import { app, BrowserWindow, clipboard, dialog, ipcMain, Menu, nativeImage, net, protocol, shell, Tray } from "electron";
import { createHash, randomUUID } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync, existsSync, realpathSync, statSync } from "node:fs";
import { mkdir, writeFile } from "node:fs/promises";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { homedir } from "node:os";
import type { DesktopEvent, Preferences } from "../bridge.js";
import { Runtime } from "./runtime.js";
import { QueryClient } from "./query-client.js";
import { sessionAction } from "./session-actions.js";
import { hasOngoingWork } from "./ongoing-work.js";
import { PreferenceWriter } from "./preferences.js";
import { filePreview } from "./file-preview.js";
import { ClipboardFiles } from "./clipboard-files.js";
import { DesktopAppshot } from "./appshot.js";
import { checkedCommand, checkedPreferences, checkedString, externalURL, inside } from "./validation.js";

const root = resolve(process.env.AGENT_PROJECT_ROOT || join(__dirname, "../.."));
const python = process.env.AGENT_PYTHON || join(root, ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
let workspace = resolve(process.env.ASTRA_WORKSPACE || process.cwd());
const data = resolve(process.env.ASTRA_HOME || join(root, ".astra"));
const gui = join(data, "gui");
mkdirSync(gui, { recursive: true, mode: 0o700 });
const identity = createHash("sha256").update(`${root}\0${data}`).digest("hex").slice(0, 16);
app.setName("Astra");
app.setPath("userData", join(gui, `electron-${identity}`));
protocol.registerSchemesAsPrivileged([{ scheme: "astra", privileges: { standard: true, secure: true, supportFetchAPI: true } }]);
let window: BrowserWindow | undefined;
let tray: Tray | undefined;
let quitting = false;
let quitFinished = false;
let closePrompt = false;
let active = "";
const runtimes = new Map<string, Runtime>();
let queries = new QueryClient(root, python);
const appshot = new DesktopAppshot(() => active,
  id => process.env.ASTRA_GUI_DISABLE_APPSHOT !== "1" && !!runtimes.get(id) && runtimes.get(id)?.state.status !== "disconnected",
  (id, event) => runtimes.get(id)?.accept(event));
appshot.client.on("change", () => broadcast({ appshot: { connection: appshot.client.state.connection, enabled: appshot.client.state.enabled } }));
const selectedFiles = new Set<string>();
const clipboardFiles = new ClipboardFiles();
const preferencePath = join(gui, "preferences.json");
let preferences: Preferences = { theme: "system", drafts: {}, attachments: {}, workspaces: {}, pinned: [], projects: [], titles: {}, timeline: true };
try { preferences = { ...preferences, ...checkedPreferences(JSON.parse(readFileSync(preferencePath, "utf8"))) }; } catch { /* first launch or invalid cache */ }
const preferenceWriter = new PreferenceWriter(preferencePath, error => {
  // Report once per failed write without inserting configuration errors into chat.
  console.error("Astra GUI preferences could not be saved:", error);
});
const savePreferences = () => preferenceWriter.schedule(preferences);
const ready = () => {
  if (process.env.ASTRA_GUI_READY_FILE) writeFileSync(process.env.ASTRA_GUI_READY_FILE,
    JSON.stringify({ ready: true, pid: process.pid }), { mode: 0o600 });
};
let queued: DesktopEvent[] = [];
let flushTimer: ReturnType<typeof setTimeout> | undefined;
function broadcast(event: DesktopEvent) {
  queued.push(event);
  if (!flushTimer) flushTimer = setTimeout(() => {
    const events = queued; queued = []; flushTimer = undefined;
    if (window && !window.isDestroyed()) window.webContents.send("astra:events", events);
  }, 32);
}
function runtime(id: unknown): Runtime {
  const rt = runtimes.get(checkedString(id, "runtime", 100));
  if (!rt) throw new Error("Session is not open");
  return rt;
}
function authorizedFile(id: unknown, value: unknown): string {
  const rt = runtime(id);
  const supplied = checkedString(value, "file path", 32768);
  const path = realpathSync(supplied.startsWith("~/") ? join(homedir(), supplied.slice(2)) : resolve(rt.state.workspace, supplied));
  const roots = [rt.state.workspace, data, process.env.AGENT_SESSION_DIR || join(root, ".sessions")].filter(p => p && existsSync(p)).map(p => realpathSync(p));
  if (!selectedFiles.has(path) && !roots.some(base => inside(base, path))) throw new Error("Use the file picker to open a file outside this workspace");
  if (!statSync(path).isFile()) throw new Error("Not a regular file");
  return path;
}
function checkSender(event: Electron.IpcMainEvent | Electron.IpcMainInvokeEvent) {
  if (!window || event.sender !== window.webContents || event.senderFrame !== window.webContents.mainFrame
      || !event.senderFrame?.url.startsWith("astra://app/")) throw new Error("Untrusted desktop sender");
}
function register(method: string, handler: (...args: any[]) => any) {
  ipcMain.handle(`astra:${method}`, (event, ...args) => {
    checkSender(event);
    if (quitting && !["bootstrap", "preferences"].includes(method)) throw new Error("应用正在退出，请稍候。");
    return handler(...args);
  });
}
// Only the final validated preference flush is synchronous, during unload.
ipcMain.on("astra:flushPreferences", (event, value) => {
  try { checkSender(event); preferences = { ...preferences, ...checkedPreferences(value) }; preferenceWriter.flushSync(preferences); event.returnValue = true; }
  catch (error) { event.returnValue = String(error); }
});
register("bootstrap", () => ({ sessions: [...runtimes.values()].map(r => r.state), active, workspace, preferences, appshot: { connection: appshot.client.state.connection, enabled: appshot.client.state.enabled } }));
register("create", (options: { session?: string; mode?: string; workspace?: string } = {}) => {
  if (!options || typeof options !== "object") throw new Error("Invalid session options");
  const mode = options.mode || "work";
  if (!["work", "local", "minimal", "bar"].includes(mode)) throw new Error("Unknown mode");
  const work = resolve(options.workspace || workspace);
  if (!statSync(work).isDirectory()) throw new Error("Workspace does not exist");
  // Serialize creation in the host, including concurrent renderer requests.
  if (!options.session && mode === "work") {
    const draft = [...runtimes.values()].find(r => r.state.isDraft && r.state.mode === mode
      && r.workspace === work && r.state.status !== "disconnected" && !r.isClosing && !r.state.busy
      && !r.state.approvals.length && !r.state.questions.length);
    if (draft) { active = draft.id; appshot.update(); return draft.state; }
  }
  const name = options.session || `session_${new Date().toISOString().replace(/[^0-9]/g, "").slice(0, 14)}_${randomUUID().slice(0, 8)}`;
  if (!/^[\p{L}\p{N}_.-]+$/u.test(name) || [".", ".."].includes(name) || name.length > 200) throw new Error("Invalid session name");
  if (sessionOperations.has(`${mode}:${name}`)) throw new Error("该会话的操作尚未完成，请稍后再打开。");
  const existing = [...runtimes.values()].find(r => r.state.session === name && r.state.mode === mode && r.state.status !== "disconnected");
  if (existing) {
    if (existing.isClosing) throw new Error("会话正在关闭，请稍后再打开。");
    active = existing.id; return existing.state;
  }
  const rt = new Runtime(root, python, work, mode === "work" ? name : `gui_${randomUUID().replaceAll("-", "")}`, mode,
    (r, event) => { broadcast({ runtime: r.id, revision: r.state.revision, event }); appshot.handle(r.id, event);
      if (event.type === "gui_ready") appshot.update(); });
  // Special modes enter their separate namespace after the Work backend is ready.
  if (mode !== "work") rt.initialModeSession = name;
  rt.state.isDraft = !options.session && mode === "work";
  runtimes.set(rt.id, rt); active = rt.id; appshot.update();
  return rt.state;
});
register("select", id => { active = id === "" ? "" : runtime(id).id; appshot.update(); });
function forgetRuntime(rt: Runtime) {
  runtimes.delete(rt.id);
  if (active === rt.id) { active = ""; preferences.lastSession = null; savePreferences(); }
  appshot.forget(rt.id);
  broadcast({ removed: rt.id });
}
register("close", async id => {
  const rt = runtime(id);
  if (hasOngoingWork(rt.state)) throw new Error("该会话仍有任务或提醒，请先停止后再关闭。");
  await rt.close(); forgetRuntime(rt);
});
register("send", async (id, value) => {
  const rt = runtime(id); const command = checkedCommand(value);
  if (command.type === "image") authorizedFile(id, command.path);
  clipboardFiles.sent(command);
  try { await rt.send(appshot.prepare(rt.id, command as any)); }
  catch (error) {
    appshot.handle(rt.id, { type: "submission_status", submission_id: command.submission_id, status: "unknown" });
    throw error;
  }
});
register("appshot", async (id, action, value = "") => {
  const rt = runtime(id);
  checkedString(action, "Appshot action", 100); checkedString(value, "Appshot value", 512);
  if (action === "remove") { appshot.remove(rt.id, value); return; }
  if (action !== "command") throw new Error("Unsupported Appshot action");
  if (value === "/appshot pending status") {
    const pending = appshot.get(rt.id).pending;
    if (pending) await rt.send({ type: "submission_status", submission_id: pending.submissionId });
    else rt.accept({ type: "gui_notice", message: "没有待确认的 Appshot 请求。" });
  } else rt.accept({ type: "gui_notice", message: await appshot.command(rt.id, value) });
});
register("query", (method, params = {}, id) => {
  if (!["sessions", "history", "commands", "changes"].includes(method)) throw new Error("Unsupported query");
  if (!params || typeof params !== "object" || Array.isArray(params) || JSON.stringify(params).length > 32768) throw new Error("Invalid query");
  return method === "changes" ? runtime(id).query(method, params) : queries.query(method, params);
});
const sessionOperations = new Set<string>();
register("sessionAction", async (action, entry) => {
  if (!["export", "delete"].includes(action) || !entry || typeof entry !== "object") throw new Error("Invalid session action");
  const name = checkedString(entry.name, "session", 200);
  const mode = checkedString(entry.mode, "mode", 20);
  if (!/^[\p{L}\p{N}_.-]+$/u.test(name) || [".", ".."].includes(name) || !["work", "minimal", "bar"].includes(mode)) throw new Error("Invalid session");
  const key = `${mode}:${name}`;
  if (sessionOperations.has(key)) throw new Error("该会话的操作尚未完成。");
  sessionOperations.add(key);
  try {
    if (action === "export") {
      const choice = await dialog.showSaveDialog(window!, { title: "导出会话", defaultPath: `${name}.md`, filters: [{ name: "Markdown", extensions: ["md"] }] });
      if (choice.canceled || !choice.filePath) return { cancelled: true };
      const result = await sessionAction(root, python, "export", name, mode);
      await writeFile(choice.filePath, result.markdown, { encoding: "utf8", mode: 0o600 });
      shell.showItemInFolder(choice.filePath);
      return { path: choice.filePath };
    }
    const owned = [...runtimes.values()].filter(r => r.state.session === name && r.state.mode === mode || mode === "work" && r.session === name);
    if (owned.some(r => hasOngoingWork(r.state))) throw new Error("该会话仍有任务或提醒，请先停止后再删除。");
    for (const rt of owned) {
      if (hasOngoingWork(rt.state)) throw new Error("该会话仍有任务或提醒，请先停止后再删除。");
      await rt.close(); forgetRuntime(rt);
    }
    const result = await sessionAction(root, python, "delete", name, mode);
    for (const field of ["titles", "drafts", "attachments", "workspaces"] as const) delete preferences[field][key];
    preferences.pinned = preferences.pinned.filter(k => k !== key);
    if (preferences.lastSession?.session === name && preferences.lastSession.mode === mode) preferences.lastSession = null;
    savePreferences(); broadcast({ refresh: true });
    return result;
  } finally { sessionOperations.delete(key); }
});
register("preferences", value => {
  preferences = { ...preferences, ...checkedPreferences(value) }; savePreferences(); return preferences;
});
register("choose", async kind => {
  if (!["files", "folder"].includes(kind)) throw new Error("Invalid picker");
  const result = await dialog.showOpenDialog(window!, { properties: kind === "folder" ? ["openDirectory"] : ["openFile", "multiSelections"] });
  for (const path of result.filePaths) if (kind === "files") selectedFiles.add(realpathSync(path));
  if (kind === "folder" && result.filePaths[0]) { workspace = result.filePaths[0]; preferences.projects = [...new Set([...preferences.projects, workspace])]; savePreferences(); }
  return result.filePaths;
});
register("clipboardImage", async () => {
  const items = await clipboard.read();
  const item = items.find(i => i.types.includes("image/png"));
  if (!item) return null;
  const blob = await item.getType("image/png");
  if (!("arrayBuffer" in blob) || blob.size > 20 * 1024 * 1024) throw new Error("剪贴板图片过大或格式不受支持");
  const bytes = Buffer.from(await blob.arrayBuffer());
  const folder = join(gui, "attachments"); await mkdir(folder, { recursive: true, mode: 0o700 });
  const path = join(folder, `${randomUUID()}.png`); await writeFile(path, bytes, { mode: 0o600 }); selectedFiles.add(path); clipboardFiles.add(path); return path;
});
register("openExternal", value => shell.openExternal(externalURL(value)));
register("file", async (id, value, action) => {
  const path = authorizedFile(id, value);
  if (action === "reveal") { shell.showItemInFolder(path); return { path }; }
  if (action === "open") { const error = await shell.openPath(path); if (error) throw new Error(error); return { path }; }
  if (action !== "preview") throw new Error("Unknown file action");
  return filePreview(path);
});

async function quit() {
  if (quitting) return;
  quitting = true;
  await Promise.all([...runtimes.values()].map(rt => rt.close()));
  await Promise.all([appshot.client.close(), queries.close()]);
  // Let beforeunload supply the newest draft before taking the final snapshot.
  if (window && !window.isDestroyed()) {
    const closingWindow = window;
    await new Promise<void>(resolve => { closingWindow.once("closed", () => resolve()); closingWindow.close(); });
  }
  try { preferenceWriter.flushSync(preferences); await preferenceWriter.flush(); }
  catch (error) {
    quitting = false; queries = new QueryClient(root, python); showWindow();
    await dialog.showMessageBox(window!, { type: "error", message: "草稿未能保存，已取消退出", detail: `请先复制需要保留的内容。\n${String(error)}` });
    return;
  }
  await clipboardFiles.cleanup(preferences.attachments, preferences.drafts).catch(error => console.error("Astra clipboard cleanup:", error));
  quitFinished = true;
  app.quit();
}
function showWindow() {
  if (window && !window.isDestroyed()) { window.show(); window.focus(); return; }
  window = new BrowserWindow({ title: "Astra", width: 1320, height: 900, minWidth: 850, minHeight: 600,
    backgroundColor: "#f8f8f7", ...(process.platform === "darwin" ? { titleBarStyle: "hiddenInset" as const, trafficLightPosition: { x: 18, y: 18 } } : {}),
    webPreferences: { preload: join(__dirname, "preload.cjs"), contextIsolation: true, sandbox: true, nodeIntegration: false } });
  window.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  window.webContents.on("will-navigate", event => event.preventDefault());
  window.webContents.session.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
  window.on("focus", () => { if (process.env.ASTRA_GUI_DISABLE_APPSHOT !== "1") appshot.activity(); });
  window.webContents.on("before-input-event", (_event, input) => {
    if (input.type === "keyDown" && process.env.ASTRA_GUI_DISABLE_APPSHOT !== "1") appshot.activity();
  });
  window.on("close", event => {
    if (quitting) return;
    event.preventDefault();
    const ongoing = [...runtimes.values()].some(r => hasOngoingWork(r.state));
    if (!ongoing) { void quit(); return; }
    if (closePrompt) return;
    closePrompt = true;
    void dialog.showMessageBox(window!, { type: "question", message: "仍有任务或会话提醒在运行", detail: "后台继续会保留后端运行，可从托盘重新打开。", buttons: ["后台继续", "停止并退出", "返回"], cancelId: 2, defaultId: 0 })
      .then(({ response }) => { if (response === 0) window?.hide(); else if (response === 1) void quit(); })
      .finally(() => { closePrompt = false; });
  });
  window.webContents.once("did-finish-load", ready);
  void window.loadURL("astra://app/index.html");
}

if (!app.requestSingleInstanceLock({ workspace })) { ready(); app.quit(); }
else {
  app.on("second-instance", (_event, _argv, _cwd, extra) => { const requested = extra as { workspace?: string }; if (typeof requested?.workspace === "string") workspace = requested.workspace; showWindow(); broadcast({ refresh: true }); });
  app.on("before-quit", event => { if (!quitFinished) { event.preventDefault(); if (!quitting) void quit(); } });
  // quit() flushes the last renderer draft after beforeunload, then exits explicitly.
  app.on("window-all-closed", () => {});
  app.on("activate", showWindow);
  void app.whenReady().then(async () => {
    const staticRoot = realpathSync(join(__dirname, "renderer"));
    protocol.handle("astra", request => {
      const url = new URL(request.url);
      if (url.hostname !== "app") return new Response("Not found", { status: 404 });
      const path = resolve(staticRoot, `.${decodeURIComponent(url.pathname)}`);
      if (!inside(staticRoot, path) || !existsSync(path) || !inside(staticRoot, realpathSync(path))) return new Response("Not found", { status: 404 });
      return net.fetch(pathToFileURL(path).toString());
    });
    Menu.setApplicationMenu(Menu.buildFromTemplate([
      ...(process.platform === "darwin" ? [{ role: "appMenu" as const }] : []),
      { role: "editMenu" }, { role: "viewMenu" }, { role: "windowMenu" },
    ]));
    // Native trays accept bitmap images on every supported desktop platform.
    const pixels = Buffer.alloc(22 * 22 * 4);
    for (let y = 2; y < 21; y++) for (let x = 1; x < 21; x++) {
      if (Math.abs(x - 11) <= y / 2 && !(y > 9 && y < 16 && Math.abs(x - 11) < (y - 7) / 2)) pixels[(y * 22 + x) * 4 + 3] = 255;
    }
    const icon = nativeImage.createFromBitmap(pixels, { width: 22, height: 22 });
    if (process.platform === "darwin") icon.setTemplateImage(true);
    tray = new Tray(icon); tray.setToolTip("Astra");
    tray.setContextMenu(Menu.buildFromTemplate([{ label: "打开 Astra", click: showWindow }, { label: "停止并退出", click: () => { void quit(); } }]));
    tray.on("click", showWindow);
    showWindow();
  });
}
