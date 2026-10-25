import { app, BrowserWindow, clipboard, dialog, ipcMain, Menu, nativeImage, net, Notification, protocol, shell, Tray } from "electron";
import { execFile } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import { appendFileSync, mkdirSync, readFileSync, writeFileSync, existsSync, realpathSync, statSync } from "node:fs";
import { mkdir, writeFile } from "node:fs/promises";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { homedir } from "node:os";
import type { DesktopEvent, Preferences } from "../bridge.js";
import type { SessionState } from "@astra/ui-core/session-state";
import { Runtime } from "./runtime.js";
import { ChannelHost } from "./channel-host.js";
import { Attention, type AttentionNotice } from "./attention.js";
import { QueryClient } from "./query-client.js";
import { sessionAction } from "./session-actions.js";
import { hasOngoingWork } from "./ongoing-work.js";
import { QuitCoordinator, type QuitReason } from "./quit.js";
import { PreferenceWriter } from "./preferences.js";
import { DocumentPreviewService, sourceVersion } from "./file-preview.js";
import { configurePackagedRuntime, acquirePackagedLease } from "./packaged-runtime.js";
import { ClipboardFiles } from "./clipboard-files.js";
import { DesktopAppshot } from "./appshot.js";
import { checkedCommand, checkedPreferences, checkedString, externalURL, inside } from "./validation.js";
import { captureHostResponse, cardFrameResponse, guardCardFrames } from "./card-frame.js";
import { PageSnapshots } from "./page-snapshot.js";
import { CARD_THEME_COLORS, checkedCardTheme } from "../card-theme.js";
import { SHARED_PREFERENCE_KEYS, type PhoneInvokeMethod, type PhoneStatus } from "../phone-protocol.js";
import { PhoneDevices } from "./phone-devices.js";
import { PhoneGateway } from "./phone-gateway.js";
import { tailscaleName } from "./phone-tailscale.js";

const packaged = configurePackagedRuntime({ isPackaged: app.isPackaged, resourcesPath: process.resourcesPath,
  appData: app.getPath("appData"), platform: process.platform, arch: process.arch });
const root = packaged?.root ?? resolve(process.env.AGENT_PROJECT_ROOT || join(__dirname, "../.."));
const python = packaged?.python ?? process.env.AGENT_PYTHON ?? join(root, ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
let workspace = resolve(process.env.ASTRA_WORKSPACE || process.cwd());
const data = packaged?.data ?? resolve(process.env.ASTRA_HOME || join(root, ".astra"));
const gui = join(data, "gui");
mkdirSync(gui, { recursive: true, mode: 0o700 });
// Speech for a paired phone is handed over as files here (agent/speech/player.py PhonePlayer); backends inherit it.
const phoneAudio = join(gui, "phone-audio");
process.env.ASTRA_PHONE_AUDIO_DIR = phoneAudio;
const identity = createHash("sha256").update(`${root}\0${data}`).digest("hex").slice(0, 16);
app.setName("Astra");
app.setPath("userData", join(gui, `electron-${identity}`));
protocol.registerSchemesAsPrivileged([{ scheme: "astra", privileges: { standard: true, secure: true, supportFetchAPI: true } }]);
let window: BrowserWindow | undefined;
let tray: Tray | undefined;
let quitting = false;
let quitFinished = false;
let leaseLost = false;
let active = "";
const runtimes = new Map<string, Runtime>();
const previews = new DocumentPreviewService();
const previewRequests = new Map<string, AbortController>();
let desktopLease: { close(): Promise<void> } | undefined;
let queries = new QueryClient(root, python);
const appshot = new DesktopAppshot(() => active,
  id => process.env.ASTRA_GUI_DISABLE_APPSHOT !== "1" && !!runtimes.get(id) && runtimes.get(id)?.state.status !== "disconnected",
  (id, event) => runtimes.get(id)?.accept(event));
appshot.client.on("change", () => broadcast({ appshot: { connection: appshot.client.state.connection, enabled: appshot.client.state.enabled } }));
const selectedFiles = new Set<string>();
// A page's picture uses the colours the window is showing now. The window is ours, so it is simply asked.
const themeQuery = `(names => { const style = getComputedStyle(document.documentElement), colors = {}; for (const name of names) { const value = style.getPropertyValue(name).trim(); if (value) colors[name] = value; } return { dark: String(style.colorScheme || "").includes("dark"), colors }; })(${JSON.stringify(CARD_THEME_COLORS)})`;
const snapshots = new PageSnapshots(join(gui, "page-snapshots"), join(__dirname, "preload.cjs"),
  async () => window && !window.isDestroyed() ? checkedCardTheme(await window.webContents.executeJavaScript(themeQuery)) : undefined);
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
// Paired phones follow the same sessions as the window (specs/phone-companion.md, phase 1: read-only).
const phoneDevices = new PhoneDevices(join(gui, "phone-devices.json"));
const phone = new PhoneGateway(phoneDevices, { staticRoot: join(__dirname, "phone"), rendererRoot: join(__dirname, "renderer"), audioRoot: phoneAudio,
  invoke: (device, method, args) => phoneInvoke(device, method, args), port: Number(process.env.ASTRA_PHONE_PORT || 8930) });
let phoneError = "";
let phoneHost: { name?: string; at: number } = { at: 0 };
async function syncPhone() {
  try {
    if (phoneDevices.enabled) await phone.start(); else await phone.stop();
    phoneError = "";
  } catch (error) {
    phoneError = (error as NodeJS.ErrnoException)?.code === "EADDRINUSE" ? `端口 ${phone.port} 已被占用。` : String(error);
  }
}
async function phoneStatus(): Promise<PhoneStatus> {
  // Settings asks every second while a code is shown; Tailscale is asked at most once a minute.
  if (phoneDevices.enabled && Date.now() - phoneHost.at > 60_000) phoneHost = { name: await tailscaleName(), at: Date.now() };
  return { enabled: phoneDevices.enabled, port: phone.port, running: phone.running, ...(phoneError ? { error: phoneError } : {}),
    ...(phoneDevices.enabled && phoneHost.name ? { url: `https://${phoneHost.name}` } : {}), devices: phoneDevices.list(),
    ...(phoneDevices.currentPairing() ? { pairing: phoneDevices.currentPairing() } : {}) };
}
const attention = new Attention({ focused: () => !!window && !window.isDestroyed() && window.isFocused() });
// Held until closed or clicked, so a notification is not collected before its click handler runs.
const notices = new Set<Notification>();
function notify(notice: AttentionNotice | undefined) {
  if (!notice || !Notification.isSupported()) return;
  const shown = new Notification({ title: notice.title, body: notice.body });
  notices.add(shown);
  shown.on("click", () => { notices.delete(shown); showWindow(); });
  shown.on("close", () => notices.delete(shown));
  shown.show();
}
function broadcast(event: DesktopEvent) {
  phone.publish(event);
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
    if ((quitting || leaseLost) && !["bootstrap", "preferences"].includes(method)) throw new Error("应用正在退出，请稍候。");
    return handler(...args);
  });
}
// Only the final validated preference flush is synchronous, during unload.
ipcMain.on("astra:flushPreferences", (event, value) => {
  try { checkSender(event); preferences = { ...preferences, ...checkedPreferences(value) }; preferenceWriter.flushSync(preferences); event.returnValue = true; }
  catch (error) { event.returnValue = String(error); }
});
register("bootstrap", () => ({ sessions: [...runtimes.values()].map(r => r.state), active, workspace, preferences, appshot: { connection: appshot.client.state.connection, enabled: appshot.client.state.enabled } }));
/** Opens a session or a fresh draft. The window's call selects it; a phone's call leaves the window's selection alone. */
function openSession(options: { session?: string; mode?: string; workspace?: string } = {}, select = true): SessionState {
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
    if (draft) { if (select) { active = draft.id; appshot.update(); } return draft.state; }
  }
  const name = options.session || `session_${new Date().toISOString().replace(/[^0-9]/g, "").slice(0, 14)}_${randomUUID().slice(0, 8)}`;
  if (!/^[\p{L}\p{N}_.-]+$/u.test(name) || [".", ".."].includes(name) || name.length > 200) throw new Error("Invalid session name");
  if (sessionOperations.has(`${mode}:${name}`)) throw new Error("该会话的操作尚未完成，请稍后再打开。");
  const existing = [...runtimes.values()].find(r => r.state.session === name && r.state.mode === mode && r.state.status !== "disconnected");
  if (existing) {
    if (existing.isClosing) throw new Error("会话正在关闭，请稍后再打开。");
    if (select) active = existing.id;
    return existing.state;
  }
  const rt = new Runtime(root, python, work, mode === "work" ? name : `gui_${randomUUID().replaceAll("-", "")}`, mode,
    (r, event) => { broadcast({ runtime: r.id, revision: r.state.revision, event }); appshot.handle(r.id, event);
      notify(attention.session(r.id, "", r.state.busy, event));
      if (event.type === "backend_hello") voiceRoutes.delete(r.id);
      if (event.type === "gui_ready") appshot.update(); });
  // Special modes enter their separate namespace after the Work backend is ready.
  if (mode !== "work") rt.initialModeSession = name;
  // The backend reads what it is allowed to on its own; the page it names is only drawn here, in isolation.
  rt.pageSnapshot = path => snapshots.capture(path);
  rt.state.isDraft = !options.session && mode === "work";
  runtimes.set(rt.id, rt);
  if (select) { active = rt.id; appshot.update(); }
  return rt.state;
}
register("create", (options: { session?: string; mode?: string; workspace?: string } = {}) => openSession(options));
register("select", id => { active = id === "" ? "" : runtime(id).id; appshot.update(); });
/** Where each session's speech plays: on this Mac, or on the phone the conversation was last used from. */
const voiceRoutes = new Map<string, "mac" | "phone">();
async function routeVoice(rt: Runtime, command: Record<string, any>, where: "mac" | "phone") {
  if (!["message", "image", "command"].includes(command.type) || voiceRoutes.get(rt.id) === where) return;
  voiceRoutes.set(rt.id, where);
  try { await rt.send({ type: "command", cmd: `/voice route ${where}` }); } catch (error) { voiceRoutes.delete(rt.id); throw error; }
}
function forgetRuntime(rt: Runtime) {
  voiceRoutes.delete(rt.id);
  for (const [key, controller] of previewRequests) if (key.startsWith(`${rt.id}:`)) { controller.abort(); previewRequests.delete(key); }
  runtimes.delete(rt.id);
  if (active === rt.id) { active = ""; preferences.lastSession = null; savePreferences(); }
  appshot.forget(rt.id);
  broadcast({ removed: rt.id });
}
async function closeSession(id: unknown) {
  const rt = runtime(id);
  if (hasOngoingWork(rt.state)) throw new Error("该会话仍有任务或提醒，请先停止后再关闭。");
  await rt.close(); forgetRuntime(rt);
}
register("close", closeSession);
register("send", async (id, value) => {
  const rt = runtime(id); const command = checkedCommand(value);
  if (command.type === "image") authorizedFile(id, command.path);
  clipboardFiles.sent(command);
  await routeVoice(rt, command, "mac");
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
function runQuery(method: any, params: any = {}, id?: unknown) {
  if (!["sessions", "history", "commands", "changes", "request_timeline", "session_log", "jobs", "activity", "memory"].includes(method)) throw new Error("Unsupported query");
  if (!params || typeof params !== "object" || Array.isArray(params) || JSON.stringify(params).length > 32768) throw new Error("Invalid query");
  return method === "changes" ? runtime(id).query(method, params) : queries.query(method, params);
}
register("query", runQuery);
/** The person's own changes to scheduled jobs, from the jobs page: the same as `astra jobs` in a terminal. */
function runJobs(action: unknown, value?: unknown, extra?: unknown): Promise<unknown> {
  let args: string[];
  if (action === "quiet") {
    const clock = (text: unknown) => { const v = checkedString(text, "time", 5); if (!/^([01]?\d|2[0-3]):[0-5]\d$/.test(v)) throw new Error("Invalid time"); return v; };
    args = value === "off" ? ["quiet", "off"] : ["quiet", clock(value), clock(extra)];
  } else {
    const id = checkedString(value, "job", 32);
    if (!/^[0-9a-f]{32}$/.test(id)) throw new Error("Invalid job");
    if (action === "read") args = ["inbox", "--read", id, "--limit", "1"];
    else if (action === "run") args = ["run", id, "--queue"];
    else if (action === "pause" || action === "resume" || action === "remove") args = [action, id];
    else throw new Error("Unsupported job action");
  }
  return new Promise((done, fail) => execFile(python, ["-m", "agent.cli.jobs_commands", ...args],
    { cwd: root, env: process.env, timeout: 30_000, windowsHide: true, encoding: "utf8" },
    (error, stdout, stderr) => error ? fail(new Error(String(stderr).trim().split("\n").at(-1) || error.message)) : done(JSON.parse(stdout || "null"))));
}
register("jobs", runJobs);
/** The person's own change to their memory, from the memory page: no approval, it is theirs. */
function runMemory(action: unknown, ...values: unknown[]): Promise<unknown> {
  const allowed: Record<string, number> = { "core-add": 2, "core-edit": 3, "core-remove": 1, forget: 1, correct: 2, accept: 1, dismiss: 1 };
  const name = checkedString(action, "action", 20);
  if (!(name in allowed) || values.length !== allowed[name]) throw new Error("Unsupported memory change");
  const args = values.map(value => checkedString(value, "memory", 4000));
  return new Promise((done, fail) => execFile(python, ["-m", "agent.ui.memory_page", name, ...args],
    { cwd: root, env: process.env, timeout: 30_000, windowsHide: true, encoding: "utf8" },
    (_error, stdout) => {
      let result: { ok?: boolean; message?: string; error?: string } = {};
      try { result = JSON.parse(String(stdout).trim().split("\n").at(-1) || "{}"); } catch { /* reported below */ }
      result.ok ? done(result.message) : fail(new Error(result.error || "记忆没有改成功。"));
    }));
}
register("memory", runMemory);
const sessionOperations = new Set<string>();
async function runSessionAction(action: any, entry: any) {
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
}
register("sessionAction", runSessionAction);
const phoneAuditPath = join(gui, "phone-audit.jsonl");
/** What a paired phone did, for the user to read later; message text is not kept, only its length. */
function phoneAudit(device: string, entry: Record<string, unknown>) {
  try { appendFileSync(phoneAuditPath, `${JSON.stringify({ at: new Date().toISOString(), device, ...entry })}\n`, { mode: 0o600 }); }
  catch (error) { console.error("Astra phone audit:", error); }
}
/** A paired phone's calls (specs/phone-companion.md). The phone runs this renderer; Mac-only calls are answered on the phone. */
async function phoneInvoke(device: string, method: PhoneInvokeMethod, args: any[]) {
  if (quitting || leaseLost) throw new Error("应用正在退出，请稍候。");
  if (method === "bootstrap") return { sessions: [...runtimes.values()].map(r => r.state), active: "", workspace,
    preferences: Object.fromEntries(SHARED_PREFERENCE_KEYS.map(key => [key, preferences[key]])),
    appshot: { connection: "disconnected", enabled: false } };
  if (method === "create") return openSession(args[0] ?? {}, false);
  if (method === "query") return runQuery(args[0], args[1], args[2]);
  if (method === "jobs") { phoneAudit(device, { method, action: String(args[0]).slice(0, 20) }); return runJobs(args[0], args[1], args[2]); }
  if (method === "memory") { phoneAudit(device, { method, action: String(args[0]).slice(0, 20) }); return runMemory(args[0], ...args.slice(1)); }
  if (method === "close") { phoneAudit(device, { method, runtime: String(args[0]) }); return closeSession(args[0]); }
  if (method === "deleteSession") { phoneAudit(device, { method, session: args[0]?.name, mode: args[0]?.mode }); return runSessionAction("delete", args[0]); }
  const rt = runtime(args[0]); const command = checkedCommand(args[1]);
  phoneAudit(device, { method, runtime: rt.id, type: command.type,
    ...(command.type === "message" ? { chars: String(command.text).length } : {}),
    ...(command.type === "command" ? { cmd: String(command.cmd).slice(0, 200) } : {}),
    ...(command.type === "tool_approval_response" ? { request: command.request_id, decision: command.decision } : {}),
    ...(command.type.startsWith("user_question") ? { request: command.request_id } : {}) });
  if (command.type === "image") authorizedFile(rt.id, command.path);
  await routeVoice(rt, command, "phone");
  await rt.send(command as any);
}
register("phone", async (action, value) => {
  if (action === "enable" || action === "disable") { phoneDevices.setEnabled(action === "enable"); await syncPhone(); }
  else if (action === "pair") { if (!phoneDevices.enabled) throw new Error("先打开“允许手机连接”。"); phoneDevices.startPairing(); }
  else if (action === "revoke") phoneDevices.revoke(checkedString(value, "device", 100));
  else if (action !== "status") throw new Error("Unknown phone action");
  return phoneStatus();
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
// The window denies every web permission, so the renderer cannot use navigator.clipboard to write.
register("copyText", text => clipboard.writeText(checkedString(text, "clipboard text", 16 * 1024 * 1024)));
register("openExternal", value => shell.openExternal(externalURL(value)));
register("cancelPreview", (id, requestId) => {
  const rt = runtime(id);
  const key = `${rt.id}:${checkedString(requestId, "preview request", 100)}`;
  previewRequests.get(key)?.abort();
});
register("file", async (id, value, action, requestId) => {
  const path = authorizedFile(id, value);
  if (action === "reveal") { shell.showItemInFolder(path); return { path }; }
  if (action === "open") { const error = await shell.openPath(path); if (error) throw new Error(error); return { path }; }
  if (action === "version") return { path, sourceVersion: await sourceVersion(path) };
  if (action === "snapshot") return { path: (await snapshots.capture(path)).path, kind: "image" as const };
  if (action !== "preview") throw new Error("Unknown file action");
  const key = `${runtime(id).id}:${requestId === undefined ? randomUUID() : checkedString(requestId, "preview request", 100)}`;
  if (previewRequests.has(key)) throw new Error("Preview request is already pending");
  const controller = new AbortController(); previewRequests.set(key, controller);
  try { return await previews.preview(path, controller.signal, () => authorizedFile(id, value)); }
  finally { if (previewRequests.get(key) === controller) previewRequests.delete(key); }
});

/** WeChat and QQ answer while the desktop is open, whether or not a conversation is open in the window. */
const channelHost = new ChannelHost({
  configPath: process.env.ASTRA_CHANNEL_CONFIG || join(root, ".astra", "channels.json"),
  // Kept by the job store while a scheduled job may run or a result still waits to be pushed.
  jobsMarker: join(data, "jobs", "active"),
  // The channel host takes WeChat over from other backends, such as an old terminal session. It also
  // runs scheduled jobs; their results become notifications here.
  create: () => new Runtime(root, python, workspace, "channel_host", "work", (_r, event) => notify(attention.host(event)),
    { ASTRA_CHANNEL_HOST: "1" }),
});

async function performQuit() {
  if (quitting) return;
  quitting = true;
  await Promise.all([...runtimes.values()].map(rt => rt.close()));
  await Promise.all([appshot.client.close(), queries.close(), phone.dispose(), channelHost.stop()]);
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
  await previews.dispose();
  await clipboardFiles.cleanup(preferences.attachments, preferences.drafts).catch(error => console.error("Astra clipboard cleanup:", error));
  await desktopLease?.close();
  quitFinished = true;
  app.quit();
}
const quitCoordinator = new QuitCoordinator({
  inspect: () => [...runtimes.values()].some(r => hasOngoingWork(r.state)),
  confirm: async reason => {
    const background = reason === "window";
    const options: Electron.MessageBoxOptions = { type: "question", message: "仍有任务或会话提醒在运行",
      detail: background ? "后台继续会保留后端运行，可从托盘重新打开。" : "退出会停止所有会话后端，已安排的提醒也将在应用关闭期间暂停。",
      buttons: background ? ["后台继续", "停止并退出", "返回"] : ["停止并退出", "返回"],
      cancelId: background ? 2 : 1, defaultId: background ? 0 : 1 };
    const { response } = window && !window.isDestroyed() && window.isVisible()
      ? await dialog.showMessageBox(window, options) : await dialog.showMessageBox(options);
    if (background && response === 0) return "background";
    return response === (background ? 1 : 0) ? "quit" : "cancel";
  },
  perform: performQuit,
  hide: () => window?.hide(),
  focus: () => { if (process.platform === "darwin") app.focus({ steal: true }); },
});
function requestQuit(reason: QuitReason) {
  if (quitting) return;
  void quitCoordinator.request(reason).catch(error => {
    console.error("Astra quit cancelled:", error);
    void dialog.showMessageBox({ type: "error", message: "退出未完成", detail: "任务状态或确认窗口不可用，请稍后再试。" });
  });
}
function showWindow() {
  if (window && !window.isDestroyed()) { window.show(); window.focus(); return; }
  window = new BrowserWindow({ title: "Astra", width: 1320, height: 900, minWidth: 850, minHeight: 600,
    backgroundColor: "#f8f8f7", ...(process.platform === "darwin" ? { titleBarStyle: "hiddenInset" as const, trafficLightPosition: { x: 18, y: 18 } } : {}),
    // The preload also runs in card frames, only to strip what a policy cannot (see src/preload); it exposes the bridge to the window alone.
    webPreferences: { preload: join(__dirname, "preload.cjs"), contextIsolation: true, sandbox: true, nodeIntegration: false, nodeIntegrationInSubFrames: true } });
  window.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  window.webContents.on("will-navigate", event => event.preventDefault());
  guardCardFrames(window.webContents);
  window.webContents.session.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
  window.on("focus", () => { if (process.env.ASTRA_GUI_DISABLE_APPSHOT !== "1") appshot.activity(); });
  window.webContents.on("before-input-event", (_event, input) => {
    if (input.type === "keyDown" && process.env.ASTRA_GUI_DISABLE_APPSHOT !== "1") appshot.activity();
  });
  window.on("close", event => {
    if (quitting) return;
    event.preventDefault();
    requestQuit("window");
  });
  window.webContents.once("did-finish-load", ready);
  void window.loadURL("astra://app/index.html");
}

if (!app.requestSingleInstanceLock({ workspace })) { ready(); app.quit(); }
else {
  app.on("second-instance", (_event, _argv, _cwd, extra) => { const requested = extra as { workspace?: string }; if (typeof requested?.workspace === "string") workspace = requested.workspace; showWindow(); broadcast({ refresh: true }); });
  app.on("before-quit", event => { if (!quitFinished) { event.preventDefault(); requestQuit("quit"); } });
  // performQuit() flushes the last renderer draft after beforeunload, then exits explicitly.
  app.on("window-all-closed", () => {});
  app.on("activate", showWindow);
  void app.whenReady().then(async () => {
    desktopLease = await acquirePackagedLease(packaged, { onLost: error => {
      if (quitting || leaseLost) return;
      leaseLost = true;
      console.error("Astra installation protection lost:", error);
      void dialog.showMessageBox({ type: "error", message: "安装保护进程已退出",
        detail: "为避免与更新发生冲突，Astra 将保存草稿并停止当前任务后退出。请重新打开应用。",
        buttons: ["停止并退出"] }).then(() => performQuit()).catch(cause => console.error("Astra protected exit failed:", cause));
    } });
    const staticRoot = realpathSync(join(__dirname, "renderer"));
    protocol.handle("astra", request => {
      const url = new URL(request.url);
      if (url.hostname === "card") return cardFrameResponse(url.pathname);
      if (url.hostname === "capture") return captureHostResponse(url.pathname);
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
    tray.setContextMenu(Menu.buildFromTemplate([{ label: "打开 Astra", click: showWindow }, { label: "停止并退出", click: () => requestQuit("quit") }]));
    tray.on("click", showWindow);
    await syncPhone();
    channelHost.start();
    showWindow();
  }).catch(async error => {
    console.error("Astra desktop startup failed:", error);
    await dialog.showMessageBox({ type: "error", message: "Astra 启动失败", detail: String(error) });
    await performQuit();
  });
}
