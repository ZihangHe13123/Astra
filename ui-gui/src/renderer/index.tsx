import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { ArrowUp, Square, Plus, Search, PanelLeft, PanelRight, ChevronDown, Folder, Settings, Paperclip, X, Copy, RotateCcw, ChevronRight, Command, Pin, Moon, Sun, Monitor, ArrowLeft, LoaderCircle, SlidersHorizontal, FileDiff, Terminal, Check } from "lucide-react";
import { projectEvent, textContent, type SessionState, type UIEvent, type Message } from "@astra/ui-core/session-state";
import type { CommandDescription, Preferences, SessionEntry } from "../bridge.js";
import { Approval, CommandPalette, Modal, ModelPicker, ModelSettings, Question } from "./controls.js";
import { Details, type Panel } from "./details.js";
import { modelConnection } from "./model-connection.js";
import { SessionMenu, SessionActionDialog, type SessionAction } from "./session-actions.js";
import { modeCommand, modeChoices, localCommandEntry } from "../local-mode.js";
import { migrateBlankDraft } from "./drafts.js";
import { MessageList } from "./messages.js";
import { DelegateHistory, DelegateSummary } from "./delegates.js";
import { CommandInput } from "./command-input.js";
import { commandNames, describeCommands, modeSessionTarget, type CommandMode } from "./command-help.js";
import { sessionTitle, sidebarGroups } from "./sidebar.js";
import "./style.css";

const defaults: Preferences = { theme: "system", drafts: {}, attachments: {}, workspaces: {}, pinned: [], projects: [], titles: {}, timeline: true };
const builtinCommandModes: CommandMode[] = [
  { mode: "bar", command: "/bar", label: "酒吧", description: "进入独立酒吧场景；原工作上下文保留" },
  { mode: "minimal", command: "/minimal", label: "极简", description: "进入轻量独立对话；保留只读网页工具" },
];
function App() {
  const [states, setStates] = useState<Record<string, SessionState>>({});
  const [active, setActive] = useState("");
  const [history, setHistory] = useState<SessionEntry[]>([]);
  const [preview, setPreview] = useState<any>();
  const [workspace, setWorkspace] = useState("");
  const [nativeCapture, setNativeCapture] = useState({ connection: "disconnected", enabled: false });
  const captureStarting = useRef(false);
  const [prefs, setPrefs] = useState(defaults);
  const prefsRef = useRef(prefs); prefsRef.current = prefs;
  const [catalog, setCatalog] = useState<CommandDescription[]>([]);
  const [search, setSearch] = useState("");
  const [side, setSide] = useState(true);
  const [panel, setPanel] = useState<Panel>();
  const [modal, setModal] = useState<"commands" | "models" | "model-picker" | "settings" | "permissions" | "sessions" | "persona">();
  const [sessionMode, setSessionMode] = useState<string>();
  const [commandQuery, setCommandQuery] = useState("");
  const [sessionDialog, setSessionDialog] = useState<{ action: "rename" | "delete"; entry: SessionEntry; title: string }>();
  const [toast, setToast] = useState("");
  const [selection, setSelection] = useState<{ index?: number; serial: number }>({ serial: 0 });
  const [historyLoading, setHistoryLoading] = useState(false);
  const [initialModelKey, setInitialModelKey] = useState<string>();
  const [olderLoading, setOlderLoading] = useState(false);
  const [follow, setFollow] = useState(true);
  const scroller = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  const loaded = useRef(false);
  const earlyEvents = useRef<Record<string, any[]>>({});
  const historyRequest = useRef(0);
  const [sending, setSending] = useState(false);
  const [opening, setOpening] = useState(false);
  const fail = useCallback((e: unknown) => setToast(e instanceof Error ? e.message : String(e)), []);
  const state = states[active];
  const localDefinition = state?.info.local_mode_info?.definition;
  const commandModes = useMemo(() => [...builtinCommandModes, ...(localDefinition ? [{ mode: "local", ...localDefinition }] : [])], [localDefinition]);
  const key = preview ? `${preview.mode}:${preview.session_id}` : state && !state.isDraft ? `${state.mode}:${state.session}` : `new:${state?.workspace || workspace}`;
  const draft = prefs.drafts[key] || "";
  const files = prefs.attachments[key] || [];
  const hasAppshot = !!state?.info.gui_appshot?.attachments?.length;
  const refresh = useCallback(() => { void window.astra.query("sessions").then(setHistory).catch(fail); }, []);
  const showSessions = (mode?: string) => { setSessionMode(mode); setSearch(""); refresh(); setModal("sessions"); };
  const showCommands = (query = "") => { setCommandQuery(query); setModal("commands"); };
  const savePrefs = (update: Partial<Preferences>) => { setPrefs(old => ({ ...old, ...update })); };
  useEffect(() => {
    const buffered: any[] = [];
    const apply = (events: any[]) => setStates(old => {
      const next = { ...old };
      for (const message of events) {
        if (message.refresh) { refresh(); continue; }
        if (message.appshot) { setNativeCapture(message.appshot); continue; }
        if (message.removed) {
          const removed = next[message.removed];
          if (removed && prefsRef.current.lastSession?.session === removed.session && prefsRef.current.lastSession?.mode === removed.mode) setPrefs(p => ({ ...p, lastSession: null }));
          delete next[message.removed]; delete earlyEvents.current[message.removed]; setActive(id => id === message.removed ? "" : id); continue; }
        const current = next[message.runtime];
        if (!current) { (earlyEvents.current[message.runtime] ||= []).push(message); continue; }
        if (message.revision > current.revision) next[message.runtime] = { ...projectEvent(current, message.event), revision: message.revision };
      }
      return next;
    });
    const stop = window.astra.onEvents(events => { if (!loaded.current) buffered.push(...events); else apply(events); });
    void window.astra.bootstrap().then(boot => {
      setStates(Object.fromEntries(boot.sessions.map(s => [s.id, s]))); setActive(boot.active); setWorkspace(boot.workspace); setNativeCapture(boot.appshot); setPrefs(migrateBlankDraft(boot.preferences, boot.workspace));
      loaded.current = true; if (buffered.length) apply(buffered); refresh();
      if (!boot.active && boot.preferences.lastSession) void create(boot.preferences.lastSession).catch(fail);
    }).catch(fail);
    void window.astra.query("commands").then(setCatalog).catch(fail);
    return stop;
  }, []);
  useEffect(() => { document.documentElement.dataset.theme = prefs.theme; }, [prefs.theme]);
  useEffect(() => {
    if (!loaded.current) return;
    const timer = setTimeout(() => { void window.astra.preferences(prefs).catch(fail); }, 250);
    return () => clearTimeout(timer);
  }, [prefs]);
  useEffect(() => {
    const flush = () => { if (loaded.current) window.astra.flushPreferences(prefsRef.current); };
    window.addEventListener("beforeunload", flush); return () => window.removeEventListener("beforeunload", flush);
  }, []);
  useEffect(() => {
    const listener = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "n") { e.preventDefault(); newChat(); }
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); showCommands(); }
      if ((e.metaKey || e.ctrlKey) && e.key === ".") { e.preventDefault(); void send({ type: "command", cmd: "/cancel" }); }
      if ((e.metaKey || e.ctrlKey) && e.shiftKey && e.key.toLowerCase() === "o") { e.preventDefault(); setPanel("tools"); }
      if ((e.metaKey || e.ctrlKey) && e.shiftKey && e.key.toLowerCase() === "l") { e.preventDefault(); setPanel("context"); }
      if (e.key === "Escape" && !modal) setPanel(undefined);
    };
    window.addEventListener("keydown", listener); return () => window.removeEventListener("keydown", listener);
  }, [active, modal, workspace, state?.isDraft, preview]);
  useEffect(() => { setFollow(true); setOlderLoading(false); }, [active, preview?.session_id, preview?.mode]);
  useEffect(() => { if (follow && scroller.current) scroller.current.scrollTop = scroller.current.scrollHeight; }, [state?.revision, active, preview, follow]);
  useEffect(() => {
    const event = state?.info.restart_ready;
    if (!event || event.replayed || document.hidden) return;
    const frame = requestAnimationFrame(() => { void window.astra.send(active, { type: "restart_ack", request_id: event.request_id }).catch(fail); });
    return () => cancelAnimationFrame(frame);
  }, [state?.info.restart_ready, active]);
  const create = async (options: { session?: string; mode?: string; workspace?: string } = {}) => {
    historyRequest.current++;
    setOpening(true);
    try {
    const rt = await window.astra.create({ workspace, ...options });
    setStates(old => {
      let current = old[rt.id]?.revision > rt.revision ? old[rt.id] : rt;
      for (const e of earlyEvents.current[rt.id] || []) if (e.revision > current.revision) current = { ...projectEvent(current, e.event), revision: e.revision };
      delete earlyEvents.current[rt.id];
      return { ...old, [rt.id]: current };
    });
    setActive(rt.id); setPreview(undefined); setHistoryLoading(false); setFollow(true);
    const sessionKey = `${options.mode || "work"}:${options.session || rt.session}`;
    if (options.session || rt.session) setPrefs(old => ({ ...old, workspaces: { ...old.workspaces, [sessionKey]: options.workspace || workspace } }));
    return rt;
    } finally { setOpening(false); }
  };
  const ensureSession = async () => {
    if (preview) return create({ session: preview.session_id, mode: preview.mode, workspace: prefs.workspaces[`${preview.mode}:${preview.session_id}`] || workspace });
    if (state?.status === "disconnected") return create(state.isDraft ? { workspace: state.workspace } : { session: state.session, mode: state.mode, workspace: state.workspace });
    return state || create();
  };
  const openSessionSettings = async (next: "models" | "model-picker" | "permissions" | "persona") => {
    try { await ensureSession(); setModal(next); } catch (e) { fail(e); }
  };
  const newChat = (work = workspace) => {
    historyRequest.current++; setPreview(undefined); setHistoryLoading(false); setFollow(true); setPanel(undefined);
    setWorkspace(work); savePrefs({ lastSession: null });
    if (!state?.isDraft || state.mode !== "work" || state.status === "disconnected" || state.workspace !== work) {
      setActive(""); void window.astra.select("").catch(fail);
    }
    requestAnimationFrame(() => input.current?.focus());
  };
  useEffect(() => {
    if (!loaded.current || active || preview || captureStarting.current || !nativeCapture.enabled || nativeCapture.connection !== "connected") return;
    captureStarting.current = true;
    void create().catch(fail).finally(() => { captureStarting.current = false; });
  }, [active, preview, nativeCapture.connection, nativeCapture.enabled, workspace]);
  const select = (id: string) => { historyRequest.current++; setActive(id); setPreview(undefined); setHistoryLoading(false); void window.astra.select(id).catch(fail); };
  const send = async (command: UIEvent) => {
    try {
      if (!active || !state) throw new Error("先创建或继续一个会话。");
      await window.astra.send(active, command);
    } catch (e) { fail(e); }
  };
  const respond = async (value: UIEvent) => {
    try { await window.astra.send(active, value); } catch (error) { fail(error); throw error; }
  };
  const command = async (value: string, target?: SessionState) => {
    if (value === "/model" || value === "/connect") { void openModels(); return; }
    if (value === "/session" || value === "/session list") { showSessions(); return; }
    const historyMode = modeSessionTarget(value, commandModes);
    if (historyMode) { showSessions(historyMode); return; }
    if (value === "/permissions") { await openSessionSettings("permissions"); return; }
    if (value === "/persona") { await openSessionSettings("persona"); return; }
    if (value === "/help") { showCommands(); return; }
    if (/^\/(tool|gallery)(?:\s+\d+)?$/.test(value)) {
      const [name, number] = value.split(/\s+/);
      if (number && Number(number) < 1) { fail("结果编号从 1 开始。"); return false; }
      setSelection(old => ({ index: number ? Number(number) : undefined, serial: old.serial + 1 }));
      setPanel(name === "/tool" ? "tools" : "files"); return;
    }
    if (value === "/changes") { setPanel("changes"); return; }
    if (value.startsWith("/theme")) { const t = value.split(/\s+/)[1]; if (t) {
      if (!["system", "light", "dark", "hermes", "glitchcity", "classic", "nord", "dracula", "solarized", "gruvbox", "lyra", "moonlit", "phosphor", "obsidian"].includes(t)) { fail("未知主题，请使用命令面板选择主题。"); return false; }
      savePrefs({ theme: t });
    } else setModal("settings"); return; }
    if (/^\/timeline(?:\s|$)/.test(value)) {
      const option = value.split(/\s+/)[1];
      if (option && !["on", "off"].includes(option)) { fail("用法：/timeline [on|off]"); return false; }
      savePrefs({ timeline: option ? option === "on" : !prefs.timeline }); return;
    }
    if (value === "/reconnect") { if (state?.status === "disconnected") void create({ session: state.session, mode: state.mode, workspace: state.workspace }).catch(fail); else fail("当前后端仍在线；需要重新加载代码时使用受控重启。"); return; }
    const session = /^\/session\s+([^\s]+)$/.exec(value);
    if (session && !["list", "export", "rename", "delete"].includes(session[1])) { void create({ session: session[1], workspace }).catch(fail); return; }
    try {
      const rt = target || await ensureSession();
      if (/^\/appshot(?:\s|$)/.test(value)) { await window.astra.appshot(rt.id, "command", value); return; }
      if (value === "/image") { attach(await window.astra.choose("files")); return; }
      if (value.startsWith("/image ")) {
        const match = value.slice(7).trim().match(/^(?:"([^"]+)"|'([^']+)'|(\S+))(?:\s+([\s\S]*))?$/);
        if (!match) throw new Error("用法：/image <路径> [说明]");
        await window.astra.send(rt.id, { type: "image", path: match[1] || match[2] || match[3], prompt: match[4] || "Describe this image.", submission_id: crypto.randomUUID() });
      } else await window.astra.send(rt.id, { type: "command", cmd: value });
    } catch (error) { fail(error); return false; }
  };
  const openModels = () => openSessionSettings("model-picker");
  const configureModels = (modelKey?: string) => { setInitialModelKey(modelKey); void openSessionSettings("models"); };
  const commandRef = useRef(command); commandRef.current = command;
  const retry = useCallback(() => { void commandRef.current("/retry"); }, []);
  const submit = async () => {
    let submittedKey = key;
    let submittedRuntime = "";
    if (sending || (!draft.trim() && !files.length && !hasAppshot)) return;
    setSending(true);
    try {
      const target = !state || preview ? await create(preview ? { session: preview.session_id, mode: preview.mode, workspace: prefs.workspaces[`${preview.mode}:${preview.session_id}`] || workspace } : { workspace }) : state;
      submittedRuntime = target.id;
      submittedKey = `${target.mode}:${target.session}`;
      if (submittedKey !== key) setPrefs(old => ({ ...old,
        drafts: { ...old.drafts, [submittedKey]: draft },
        attachments: { ...old.attachments, [submittedKey]: files } }));
      if (draft.trim().startsWith("/") && !files.length) { if (await command(draft.trim(), target) === false) return; }
      else {
        const text = [draft.trim(), ...files.map(p => `"${p.replaceAll('"', '\\"')}"`)].filter(Boolean).join("\n");
        await window.astra.send(target.id, { type: "message", text, submission_id: crypto.randomUUID() });
        if (!prefs.titles[`${target.mode}:${target.session}`]) setPrefs(old => ({ ...old, workspaces: { ...old.workspaces, [`${target.mode}:${target.session}`]: target.workspace }, titles: { ...old.titles, [`${target.mode}:${target.session}`]: [...draft.trim().split('\n')[0]].slice(0, 36).join('') || '附件对话' } }));
      }
      setPrefs(old => ({ ...old, drafts: { ...old.drafts, [key]: old.drafts[key] === draft ? "" : old.drafts[key],
        [submittedKey]: old.drafts[submittedKey] === draft ? "" : old.drafts[submittedKey] } }));
      setPrefs(old => ({ ...old, attachments: { ...old.attachments, [key]: (old.attachments[key] || []).filter(p => !files.includes(p)),
        [submittedKey]: (old.attachments[submittedKey] || []).filter(p => !files.includes(p)) } })); setFollow(true);
    } catch (e) {
      // Before admission the blank page still owns its draft. If a real turn
      // was created, the session copy owns it instead. Never hide either copy.
      if (submittedRuntime && submittedKey !== key) {
        try {
          const rt = (await window.astra.bootstrap()).sessions.find(s => s.id === submittedRuntime);
          if (rt && !rt.isDraft) setPrefs(old => ({ ...old,
            drafts: { ...old.drafts, [key]: old.drafts[key] === draft ? "" : old.drafts[key] },
            attachments: { ...old.attachments, [key]: (old.attachments[key] || []).filter(p => !files.includes(p)) } }));
        } catch { /* Retain both copies when receipt ownership is unknown. */ }
      }
      fail(e);
    }
    finally { setSending(false); }
  };
  const attach = (paths: string[]) => setPrefs(old => ({ ...old, attachments: { ...old.attachments, [key]: [...new Set([...(old.attachments[key] || []), ...paths])] } }));
  const browse = (entry: SessionEntry) => {
    const request = ++historyRequest.current;
    const matches = Object.values(states).filter(s => s.session === entry.name && s.mode === entry.mode);
    const open = matches.find(s => s.status !== "disconnected") || matches.at(-1);
    if (open) select(open.id);
    else { setHistoryLoading(true); void window.astra.query("history", { name: entry.name, mode: entry.mode }).then(p => {
      if (request !== historyRequest.current) return;
      setActive(""); setPreview({ ...p, request }); setHistoryLoading(false); void window.astra.select("").catch(fail);
    }).catch(e => { if (request === historyRequest.current) { setHistoryLoading(false); fail(e); } }); }

  };
  const messages: Message[] = useMemo(() => preview ? preview.messages.map((m: Message) => ({ ...m, content: textContent(m.content) })) : state?.messages || [], [preview?.messages, state?.messages]);
  const loadOlder = async () => {
    if (!preview?.has_more || olderLoading) return;
    const request = historyRequest.current; const before = preview.before; setOlderLoading(true); setFollow(false);
    try {
      let page = await window.astra.query("history", { name: preview.session_id, mode: preview.mode, before });
      if (request !== historyRequest.current) return;
      if (page.revision !== preview.revision) {
        page = await window.astra.query("history", { name: preview.session_id, mode: preview.mode });
        if (request === historyRequest.current) { setPreview({ ...page, request }); setFollow(true); setToast("历史已在其他窗口更新，已载入最新记录。"); }
        return;
      }
      if (request === historyRequest.current) setPreview((old: any) => old?.session_id === page.session_id && old?.mode === page.mode && old?.before === before ? { ...page, request: old.request, messages: [...page.messages, ...old.messages] } : old);
    } catch (error) { fail(error); } finally { if (request === historyRequest.current) setOlderLoading(false); }
  };
  const currentTitle = preview ? sessionTitle({ name: preview.session_id, mode: preview.mode, modified: 0 }, prefs.titles) : !state || state.isDraft ? "新对话" : sessionTitle({ name: state.session, mode: state.mode, modified: 0 }, prefs.titles);
  const currentConnection = modelConnection(state?.info.model_info, state?.info.model_info?.models?.find((m: UIEvent) => m.key === state.info.model_info.model_key));
  const appshotStatus = state?.info.gui_appshot;
  const appshotUnavailable = appshotStatus?.broker?.connection === "disconnected" && appshotStatus?.notice;
  const model = state?.info.model_info;
  const commands = useMemo(() => describeCommands([...catalog, ...localCommandEntry(localDefinition)].map(c => c.command === "/persona" && model?.personas ? { ...c, options: model.personas.map((p: UIEvent) => ({ command: p.name, description: p.description, completion: `/persona ${p.name}` })) } : c), commandModes), [catalog, model?.personas, commandModes, localDefinition]);
  useEffect(() => {
    if (state?.status !== "ready" || !state.session || state.isDraft || preview) return;
    setPrefs(old => ({ ...old, lastSession: { session: state.session, mode: state.mode, workspace: state.workspace },
      workspaces: { ...old.workspaces, [`${state.mode}:${state.session}`]: state.workspace } }));
  }, [state?.session, state?.mode, state?.workspace, state?.status, state?.isDraft, preview?.session_id]);
  useEffect(() => { if (state?.info.task_status?.task?.finished_at) refresh(); }, [state?.info.task_status?.task?.finished_at]);
  const changeMode = async (mode: string) => {
    setModal(undefined);
    if (state?.mode !== "work" && mode !== "work") return;
    const selected = modeCommand(mode === "work" ? state?.mode || "work" : mode, localDefinition);
    if (!selected) { fail("当前安装未提供这个模式。"); return; }
    if (mode === "work" && state && state.mode !== "work") await command(`${selected} leave`);
    else if (mode !== "work") await command(mode === "bar" ? selected : `${selected} new`);
  };
  const sessionAction = async (entry: SessionEntry, action: SessionAction) => {
    const sessionKey = `${entry.mode}:${entry.name}`;
    const title = sessionTitle(entry, prefs.titles);
    if (action === "rename" || action === "delete") { setModal(undefined); setSessionDialog({ action, entry, title }); return; }
    if (action === "pin") { savePrefs({ pinned: prefs.pinned.includes(sessionKey) ? prefs.pinned.filter(k => k !== sessionKey) : [...prefs.pinned, sessionKey] }); return; }
    try {
      if (action === "close") {
        const matching = Object.values(states).filter(s => s.session === entry.name && s.mode === entry.mode);
        for (const live of matching) {
          if (live.busy || live.approvals.length || live.questions.length) throw new Error("请先停止当前任务再关闭会话后端。");
          await window.astra.close(live.id);
        }
      } else {
        const result = await window.astra.sessionAction("export", entry);
        if (result.path) setToast(`已导出：${result.path}`);
      }
    } catch (error) { fail(error); }
  };
  const confirmSessionAction = async (title: string) => {
    if (!sessionDialog) return;
    const { entry, action } = sessionDialog; const sessionKey = `${entry.mode}:${entry.name}`;
    if (action === "rename") {
      const titles = { ...prefs.titles, [sessionKey]: title };
      await window.astra.preferences({ titles }); savePrefs({ titles });
    } else {
      await window.astra.sessionAction("delete", entry);
      const removed = Object.values(states).filter(s => s.session === entry.name && s.mode === entry.mode).map(s => s.id);
      setStates(old => Object.fromEntries(Object.entries(old).filter(([id]) => !removed.includes(id))));
      if (removed.includes(active) || preview?.session_id === entry.name && preview?.mode === entry.mode) {
        setActive(""); setPreview(undefined); void window.astra.select("").catch(fail);
      }
      setPrefs(old => {
        const next = { ...old, titles: { ...old.titles }, drafts: { ...old.drafts }, attachments: { ...old.attachments }, workspaces: { ...old.workspaces }, pinned: old.pinned.filter(k => k !== sessionKey) };
        for (const field of ["titles", "drafts", "attachments", "workspaces"] as const) delete next[field][sessionKey];
        if (next.lastSession?.session === entry.name && next.lastSession.mode === entry.mode) next.lastSession = null;
        return next;
      });
      refresh();
    }
  };
  const rowMenu = (entry: SessionEntry) => <SessionMenu managed={entry.mode !== "local"} title={sessionTitle(entry, prefs.titles)} pinned={prefs.pinned.includes(`${entry.mode}:${entry.name}`)} live={Object.values(states).some(s => s.session === entry.name && s.mode === entry.mode)}
    run={action => { void sessionAction(entry, action); }}/>;
  const groups = useMemo(() => sidebarGroups(history, Object.values(states), prefs, search), [history, states, prefs.titles, prefs.pinned, search]);
  const historyFiltered = groups.all;
  const entryButton = (entry: SessionEntry) => {
    const matching = Object.values(states).filter(s => s.session === entry.name && s.mode === entry.mode);
    const live = matching.find(s => s.status !== "disconnected") || matching.at(-1);
    const status = live ? live.approvals.length || live.questions.length ? "需要处理" : live.busy ? "运行中" : live.status === "disconnected" ? "已断开" : "已打开" : "";
    return <div key={`${entry.mode}:${entry.name}`} className={`session-row ${preview?.session_id === entry.name && preview?.mode === entry.mode || state?.session === entry.name && state?.mode === entry.mode && !preview ? "selected" : ""}`}>
      <button onClick={() => { browse(entry); if (modal === "sessions") setModal(undefined); }} title={entry.name}><span>{sessionTitle(entry, prefs.titles)}</span>{(status || entry.mode !== "work") && <small className="session-status"><i className={`status-dot ${live?.approvals.length || live?.questions.length ? "waiting" : live?.busy ? "running" : ""}`}/>{status}{entry.mode !== "work" && ` · ${entry.mode === "local" ? live?.info.local_mode_info?.definition?.label || "本地模式" : entry.mode === "minimal" ? "极简" : entry.mode === "bar" ? "酒吧" : entry.mode}`}</small>}</button>
      {rowMenu(entry)}
    </div>;
  };
  return <div className={`app ${side ? "" : "sidebar-hidden"}`}>
    <aside className="sidebar"><div className="traffic-space"/><div className="brand"><span className="astra-mark">A</span><strong>Astra</strong><button className="icon" aria-label="隐藏侧栏" onClick={() => setSide(false)}><PanelLeft size={17}/></button></div>
      <button className="new-chat" onClick={() => newChat()}><Plus size={18}/>新对话<span>{navigator.platform.includes("Mac") ? "⌘" : "Ctrl"} N</span></button>
      <button className="nav-button" onClick={() => showCommands()}><Command size={17}/>命令与功能<kbd>{navigator.platform.includes("Mac") ? "⌘" : "Ctrl"} K</kbd></button>
      <div className="sidebar-search"><Search size={15}/><input aria-label="搜索会话" value={search} onChange={e => setSearch(e.target.value)} placeholder="搜索会话"/></div>
      <div className="session-scroll">
        {!!groups.pinned.length && <><h4 className="section-label">置顶</h4>{groups.pinned.map(entryButton)}</>}
        {!!groups.open.length && <><h4 className="section-label">已打开</h4>{groups.open.map(entryButton)}</>}
        <div className="section-label with-action">项目<button className="icon" aria-label="添加项目" onClick={() => { void window.astra.choose("folder").then(paths => { if (paths[0]) { setWorkspace(paths[0]); savePrefs({ projects: [...new Set([...prefs.projects, paths[0]])] }); } }).catch(fail); }}><Plus size={14}/></button></div>
        {prefs.projects.map(p => <button className="project" key={p} title={p} onClick={() => newChat(p)}><Folder size={16}/><span>{p.split(/[\\/]/).pop()}</span></button>)}
        <h4 className="section-label">最近会话 <button className="icon" onClick={refresh} aria-label="刷新会话"><RotateCcw size={12}/></button></h4>
        {groups.recent.map(group => <React.Fragment key={group.label}><div className="history-date">{group.label}</div>{group.entries.map(entryButton)}</React.Fragment>)}
        {!historyFiltered.length && <p className="sidebar-empty">{search ? "没有匹配的会话。" : "你的对话会保存在这里。"}</p>}
      </div>
      <button className="account" onClick={() => setModal("settings")}><span className="account-icon">A</span><span>Astra 本地工作区<small>{model?.model && model.model !== "none" ? model.model : "连接你的模型"}</small></span><Settings size={17}/></button>
    </aside>
    <main className="main"><header className="topbar"><div>{!side && <button className="icon" aria-label="显示侧栏" onClick={() => setSide(true)}><PanelLeft size={18}/></button>}<Folder size={17}/><span className="title" title={currentTitle}>{currentTitle}</span><span className="mode-badge">{preview ? "只读历史" : state?.mode && state.mode !== "work" ? state.mode : ""}</span></div>
      <div><span className="workspace" title={state?.workspace || workspace}>{(state?.workspace || workspace).split(/[\\/]/).pop()}</span>{historyLoading || state?.status === "connecting" || state?.status === "loading" ? <LoaderCircle size={16} className="spin"/> : null}<button className="icon" aria-label="执行详情" onClick={() => setPanel(panel ? undefined : "tools")}><PanelRight size={18}/></button></div></header>
      <div className={`body ${panel && state && !preview ? "with-details" : ""}`}><div className="conversation"><div className="messages-scroll" aria-busy={historyLoading} data-history-request={preview?.request || 0} ref={scroller} onScroll={e => { const t = e.currentTarget; if (!t.clientHeight) return; setFollow(t.scrollHeight - t.scrollTop - t.clientHeight < 120); }}>
        <div className="messages">
          {!messages.length && <section className="welcome"><div className="welcome-mark">A</div><h1>从一个想法开始。</h1><p>对话、研究、编写代码，或把手头的事情交给 Astra。</p><div className="welcome-actions"><button onClick={() => { void openModels(); }}><SlidersHorizontal size={16}/>连接或选择模型</button><button onClick={() => showCommands()}><Command size={16}/>浏览全部功能</button></div></section>}
          {preview?.has_more && <button className="load-more" disabled={olderLoading} onClick={() => { void loadOlder(); }}>{olderLoading ? "加载中…" : "加载更早历史"}</button>}
          <MessageList key={preview ? `preview:${preview.mode}:${preview.session_id}` : active || "blank"} messages={messages} runtime={preview ? undefined : state?.id} timeline={prefs.timeline} reasoning={model?.show_reasoning !== false} scroller={scroller} follow={follow} retry={retry} fail={fail}/>
          {preview?.delegates?.length > 0 && <DelegateHistory key={`${preview.mode}:${preview.session_id}`} delegates={preview.delegates} fail={fail}/>}
          {state && !preview && <>{state.tools.length > 0 && <button className="execution-summary" onClick={() => setPanel("tools")}><Terminal size={15}/>{state.tools.filter(t => t.status === "running").length ? "工具正在执行" : `${state.tools.length} 项工具结果`}<ChevronRight size={15}/></button>}
            <DelegateSummary delegates={state.delegates} open={() => setPanel("tools")}/>
            {state.approvals.map(e => <Approval key={e.request_id} event={e} send={respond}/>)}{state.questions.map(e => <Question key={e.request_id} event={e} send={respond}/>)}
            {state.busy && <div className="generating"><span className="pulse"/>Astra 正在工作<span>{state.info.generation_progress?.phase === "waiting" ? "等待模型响应" : ""}</span></div>}
            {state.status === "disconnected" && <div className="disconnected"><p>后端已断开，已显示的内容仍保留。</p><button onClick={() => command("/reconnect")}>重新连接</button></div>}
          </>}
        </div></div>
        <div className="composer-wrap">
          {!follow && messages.length > 0 && <button className="jump-latest" onClick={() => setFollow(true)}>回到最新消息 ↓</button>}
          {!preview && currentConnection.configured === false && <div className="connection-status" role="status"><span>当前模型尚未连接，请先登录或配置 API Key。</span><button onClick={() => configureModels(model?.model_key)}>连接模型</button></div>}
          {!preview && appshotUnavailable && <div className="connection-status" role="status"><span>窗口捕获暂不可用，不影响对话。</span><button onClick={() => { void window.astra.appshot(active, "command", "/appshot status").catch(fail); }}>检查连接</button></div>}
          {preview ? <div className="history-banner"><span>只读浏览历史，不会启动模型。</span><button className="primary" onClick={() => { void create({ session: preview.session_id, mode: preview.mode, workspace: prefs.workspaces[`${preview.mode}:${preview.session_id}`] || workspace }).catch(fail); }}>继续此会话 <ArrowUp size={14}/></button></div> : <div className="composer" onDragOver={e => e.preventDefault()} onDrop={e => { e.preventDefault(); attach(window.astra.droppedPaths([...e.dataTransfer.files])); }}>
            {!!state?.info.gui_appshot?.attachments?.length && <div className="attachment-list">{state.info.gui_appshot.attachments.map((a: UIEvent) => <span key={a.id}>{a.label}<button className="icon" aria-label="移除窗口捕获" onClick={() => { void window.astra.appshot(active, "remove", a.id).catch(fail); }}><X size={12}/></button></span>)}</div>}
            {state?.info.gui_appshot?.pending && <div className="attachment-list"><span>窗口捕获：{state.info.gui_appshot.pending.status === "unknown" ? "接收状态未知" : "等待接收确认"}</span><button onClick={() => command("/appshot pending status")}>查询状态</button><button onClick={() => command("/appshot pending discard")}>丢弃附件</button></div>}
            {!!files.length && <div className="attachment-list">{files.map(path => <span key={path}><Paperclip size={12}/>{path.split(/[\\/]/).pop()}<button className="icon" aria-label="移除附件" onClick={() => setPrefs(old => ({ ...old, attachments: { ...old.attachments, [key]: files.filter(f => f !== path) } }))}><X size={12}/></button></span>)}</div>}
            <CommandInput inputRef={input} disabled={opening} placeholder={state?.busy ? "补充指令，或告诉 Astra 调整方向…" : "向 Astra 描述你的任务，输入 / 查看指令…"} value={draft}
              commands={commands} modes={commandModes} mode={state?.mode || "work"} attachments={!!files.length || hasAppshot}
              onChange={value => savePrefs({ drafts: { ...prefs.drafts, [key]: value } })} onSubmit={() => { void submit(); }}
              onPaste={e => { if ([...e.clipboardData.items].some(i => i.type.startsWith("image/"))) { e.preventDefault(); void window.astra.clipboardImage().then(p => { if (p) attach([p]); }).catch(fail); } }}/>
            <div className="composer-bottom"><div><button className="icon" aria-label="添加附件" onClick={() => { void window.astra.choose("files").then(attach).catch(fail); }}><Plus size={20}/></button><button className={`permission ${state?.info.yolo_status?.yolo ? "yolo" : ""}`} onClick={() => { void openSessionSettings("permissions"); }} title="工具审批设置">{state?.info.yolo_status?.yolo ? "完全访问" : "按需审批"}</button></div>
              <div><button className="model-button" onClick={() => { void openModels(); }}>{model?.model && model.model !== "none" ? model.model : "选择模型"}<ChevronDown size={13}/></button>
                <select aria-label="推理强度" value={model?.reasoning_effort || "high"} disabled={!model?.reasoning_effort} onChange={e => command(`/mode ${e.target.value}`)}><option value="low">低</option><option value="high">高</option><option value="xhigh">超高</option><option value="max">最高</option></select>
                {state?.busy && !draft.trim() && !files.length && !hasAppshot ? <button className="send" aria-label="停止" onClick={() => command("/cancel")}><Square size={16} fill="currentColor"/></button> : <button className="send" aria-label="发送" disabled={sending || opening || (!draft.trim() && !files.length && !hasAppshot)} onClick={() => { void submit(); }}><ArrowUp size={19}/></button>}
              </div></div>
          </div>}
          <div className="composer-foot"><span>{state?.status === "ready" ? state.busy ? "运行中" : "就绪" : state ? state.status === "disconnected" ? "后端已断开" : "连接后端中…" : "本地运行 · 模型自主选择工具"}</span><span>Enter 发送 · Shift Enter 换行</span></div>
        </div>
      </div>{panel && state && !preview && <Details state={state} panel={panel} setPanel={setPanel} selection={selection} width={prefs.detailWidth || 420} onWidth={detailWidth => savePrefs({ detailWidth })} close={() => setPanel(undefined)} fail={fail}/>}</div>
    </main>
    {toast && <div className="toast" role="alert"><span>{toast}</span><button className="icon" onClick={() => setToast("")} aria-label="关闭提示"><X size={16}/></button></div>}
    {modal === "commands" && <CommandPalette commands={commands} labels={commandNames(commandModes)} initialFilter={commandQuery} close={() => setModal(undefined)} run={command}/>}
    {modal === "model-picker" && <ModelPicker state={state} send={value => window.astra.send(active, value)} onConfigure={configureModels} close={() => setModal(undefined)}/>}
    {modal === "models" && <ModelSettings initialModelKey={initialModelKey} state={state} send={value => window.astra.send(active, value)} close={() => setModal(undefined)}/>}
    {sessionDialog && <SessionActionDialog key={`${sessionDialog.action}:${sessionDialog.entry.mode}:${sessionDialog.entry.name}`} action={sessionDialog.action} title={sessionDialog.title} close={() => setSessionDialog(undefined)} confirm={confirmSessionAction}/>}
    {modal === "sessions" && <Modal title="管理会话" close={() => setModal(undefined)}><div className="form"><label>对话模式<select aria-label="筛选对话模式" value={sessionMode || ""} onChange={e => setSessionMode(e.target.value || undefined)}><option value="">全部模式</option><option value="work">通用</option>{commandModes.map(mode => <option key={mode.mode} value={mode.mode}>{mode.label}</option>)}</select></label><input aria-label="筛选会话" value={search} onChange={e => setSearch(e.target.value)} placeholder="搜索会话…"/>{historyFiltered.filter(entry => !sessionMode || entry.mode === sessionMode).map(entryButton)}{!historyFiltered.some(entry => !sessionMode || entry.mode === sessionMode) && <p className="muted">暂无匹配的会话。</p>}</div></Modal>}
    {modal === "persona" && <Modal title="人格设置" close={() => setModal(undefined)}><div className="form"><p className="muted small">选择当前会话使用的人格。</p>{(model?.personas || []).map((p: UIEvent) => <button key={p.name} onClick={() => { setModal(undefined); void command(`/persona ${p.name}`); }}><span>{p.name}<small>{p.description}</small></span></button>)}</div></Modal>}
    {modal === "permissions" && <Modal title="权限与对话模式" close={() => setModal(undefined)}><div className="form"><h3>工具审批</h3><p className="muted small">控制工具操作是否需要你确认。</p>
      <div className="choice-grid">{[{ value: false, name: "按需审批", description: "按现有权限规则确认操作" }, { value: true, name: "完全访问", description: "直接执行工具操作，跳过审批" }].map(option => <button key={option.name} disabled={state?.status !== "ready"} aria-pressed={!!state?.info.yolo_status?.yolo === option.value} className={!!state?.info.yolo_status?.yolo === option.value ? "selected" : ""} onClick={() => { setModal(undefined); void command(`/yolo ${option.value ? "on" : "off"}`); }}><strong>{option.name}</strong><small>{option.description}</small></button>)}</div>
      <h3>对话模式</h3><div className="choice-grid">{modeChoices(localDefinition).map(option => <button key={option.mode} disabled={state?.status !== "ready" || !!state?.busy || option.mode === state?.mode || state?.mode !== "work" && option.mode !== "work"} aria-pressed={option.mode === (state?.mode || "work")} onClick={() => { void changeMode(option.mode); }}><strong>{option.label}</strong><small>{option.description}</small></button>)}</div>
      {state?.mode && state.mode !== "work" && <p className="muted small">先返回通用模式，再选择其他模式。</p>}
      <button className="text-button" onClick={() => showCommands(commandModes.find(mode => mode.mode === state?.mode)?.command || "")}>查看{commandModes.find(mode => mode.mode === state?.mode)?.label || "全部"}模式指令 <ChevronRight size={14}/></button>
    </div></Modal>}
    {modal === "settings" && <Modal title="设置" close={() => setModal(undefined)}><div className="form"><h3>外观</h3><div className="theme-choices">{[{ name: "system", label: "跟随系统", icon: Monitor }, { name: "light", label: "浅色", icon: Sun }, { name: "dark", label: "深色", icon: Moon }].map(t => <button key={t.name} className={prefs.theme === t.name ? "selected" : ""} onClick={() => savePrefs({ theme: t.name })}><t.icon size={18}/>{t.label}</button>)}</div>
      <label className="option"><input type="checkbox" checked={prefs.timeline} onChange={e => savePrefs({ timeline: e.target.checked })}/>显示消息时间</label><hr/>
      <button onClick={() => configureModels()}>模型与账号 <ChevronRight size={15}/></button>
      <button onClick={() => { void openSessionSettings("permissions"); }}>权限与对话模式 <ChevronRight size={15}/></button>
      <button onClick={() => showSessions()}>管理会话 <ChevronRight size={15}/></button>
      <button onClick={() => { void command("/persona"); }}>人格设置 <ChevronRight size={15}/></button>
      <button onClick={() => { setModal(undefined); void command("/diagnostics"); }}>查看诊断 <ChevronRight size={15}/></button>
      <button onClick={() => { setModal(undefined); void command("/restart"); }}>重启当前后端 <RotateCcw size={15}/></button>
      <button onClick={() => showCommands()}>全部功能与设置 <ChevronRight size={15}/></button>
      {state && <><label>会话显示名称<input value={prefs.titles[`${state.mode}:${state.session}`] || ""} onChange={e => savePrefs({ titles: { ...prefs.titles, [`${state.mode}:${state.session}`]: e.target.value } })} placeholder={state.session}/></label><button onClick={() => { void window.astra.close(active).catch(fail); setModal(undefined); }}>关闭当前会话后端</button></>}
      <p className="muted small">界面设置保存在当前 Astra 数据目录。会话、账号和工具使用现有后端。</p></div></Modal>}
  </div>;
}

createRoot(document.getElementById("root")!).render(<App/>);
