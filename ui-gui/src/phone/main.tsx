import React, { useCallback, useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { textContent, type SessionState } from "@astra/ui-core/session-state";
import { indexedDbStorage, PhoneClient, PhoneUnpairedError, TokenExpiredError, type PhoneDeviceRecord } from "./client.js";
import { applyStream, emptySessions, sessionTitle, type PhoneSessions } from "./state.js";
import "./phone.css";

type Connection = "connecting" | "connected" | "retrying";
const client = new PhoneClient(indexedDbStorage());
const ROLES: Record<string, string> = { user: "你", assistant: "Astra", system: "系统", error: "错误", tool: "工具" };

function Pair({ onPaired, notice }: { onPaired: (record: PhoneDeviceRecord) => void; notice: string }) {
  const [code, setCode] = useState("");
  const [name, setName] = useState("iPhone");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const submit = async (event: React.FormEvent) => {
    event.preventDefault(); setBusy(true); setError("");
    try { onPaired(await client.pair(code, name)); } catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
    finally { setBusy(false); }
  };
  return <form className="pair" onSubmit={submit}>
    <h1>连接 Mac 上的 Astra</h1>
    {notice && <p className="notice">{notice}</p>}
    <p className="muted">在 Mac 上打开 Astra 的“设置 → 手机”，点“添加手机”，输入显示的配对码。</p>
    <label>配对码<input value={code} onChange={event => setCode(event.target.value.toUpperCase())} autoCapitalize="characters"
      autoComplete="one-time-code" autoCorrect="off" spellCheck={false} maxLength={12} inputMode="text" required/></label>
    <label>这台手机的名字<input value={name} onChange={event => setName(event.target.value)} maxLength={60}/></label>
    {error && <p className="error" role="alert">{error}</p>}
    <button disabled={busy || code.replace(/[\s-]/g, "").length < 8}>{busy ? "正在配对…" : "配对"}</button>
  </form>;
}

function Conversation({ session, back }: { session: SessionState; back: () => void }) {
  const end = useRef<HTMLDivElement>(null);
  useEffect(() => { end.current?.scrollIntoView({ block: "end" }); }, [session.revision]);
  const waiting = session.approvals.length + session.questions.length;
  return <section className="conversation">
    <header><button className="back" onClick={back} aria-label="返回">‹</button><h2>{sessionTitle(session)}</h2>
      {session.busy && <span className="pulse" aria-label="正在工作"/>}</header>
    <div className="messages">
      {session.messages.map(message => <article key={message.id} className={`message ${message.role}`}>
        <span className="role">{ROLES[message.role] ?? message.role}</span>
        <div className="text">{textContent(message.content)}</div>
      </article>)}
      {session.busy && session.stream && <article className="message assistant streaming"><span className="role">Astra</span><div className="text">{session.stream}</div></article>}
      {waiting > 0 && <p className="waiting">{waiting} 项等你处理（审批或提问）。这一阶段请在 Mac 上处理，下一阶段可以在手机上直接批。</p>}
      <div ref={end}/>
    </div>
  </section>;
}

function Sessions({ state, open }: { state: PhoneSessions; open: (id: string) => void }) {
  if (!state.order.length) return <p className="muted empty">Mac 上的 Astra 现在没有打开的会话。</p>;
  return <ul className="sessions">{state.order.map(id => {
    const session = state.sessions[id];
    const waiting = session.approvals.length + session.questions.length;
    return <li key={id}><button onClick={() => open(id)}>
      <span className="title">{sessionTitle(session)}</span>
      <span className="meta">{session.busy ? "正在工作" : session.status === "disconnected" ? "已断开" : "空闲"}
        {waiting > 0 && <b> · {waiting} 项待处理</b>}{id === state.active && " · 电脑上正在看"}</span>
    </button></li>;
  })}</ul>;
}

function App() {
  const [device, setDevice] = useState<PhoneDeviceRecord | null | undefined>(undefined);
  const [notice, setNotice] = useState("");
  const [state, setState] = useState<PhoneSessions>(emptySessions);
  const [connection, setConnection] = useState<Connection>("connecting");
  const [open, setOpen] = useState("");
  const generation = useRef(0);
  const stream = useRef<AbortController>();

  useEffect(() => { void client.device().then(record => setDevice(record ?? null)).catch(() => setDevice(null)); }, []);

  const connect = useCallback((record: PhoneDeviceRecord) => {
    // One stream at a time: a reconnect ends the one before it.
    const run = ++generation.current;
    stream.current?.abort();
    const controller = new AbortController();
    stream.current = controller;
    let delay = 1000;
    const loop = async () => {
      let token = "";
      while (run === generation.current) {
        try {
          setConnection("connecting");
          if (!token) token = await client.signIn(record);
          setConnection("connected"); delay = 1000;
          await client.follow(token, message => setState(current => applyStream(current, message)), controller.signal);
        } catch (error) {
          if (run !== generation.current) return;
          if (error instanceof PhoneUnpairedError) {
            await client.forget(); setNotice("这台手机已在电脑上移除，请重新配对。"); setDevice(null); return;
          }
          if (error instanceof TokenExpiredError) token = "";
        }
        if (run !== generation.current) return;
        setConnection("retrying");
        await new Promise(resolve => setTimeout(resolve, delay)); delay = Math.min(delay * 2, 10_000);
      }
    };
    void loop();
    return () => { generation.current++; controller.abort(); };
  }, []);

  useEffect(() => { if (device) return connect(device); }, [device, connect]);
  // An event for a session opened after the snapshot: reconnect for a snapshot that has it.
  useEffect(() => { if (state.stale && device) { setState(current => ({ ...current, stale: false })); connect(device); } }, [state.stale, device, connect]);
  useEffect(() => {
    const wake = () => { if (document.visibilityState === "visible" && device) connect(device); };
    document.addEventListener("visibilitychange", wake);
    return () => document.removeEventListener("visibilitychange", wake);
  }, [device, connect]);

  if (device === undefined) return null;
  if (!device) return <Pair notice={notice} onPaired={record => { setNotice(""); setDevice(record); }}/>;
  const session = open ? state.sessions[open] : undefined;
  return <main>
    {!session && <header className="top"><h1>Astra</h1><span className={`connection ${connection}`}>
      {connection === "connected" ? "已连接" : connection === "connecting" ? "正在连接…" : "重新连接中…"}</span></header>}
    {session ? <Conversation session={session} back={() => setOpen("")}/> : <Sessions state={state} open={setOpen}/>}
  </main>;
}

if ("serviceWorker" in navigator) void navigator.serviceWorker.register("./sw.js").catch(() => undefined);
createRoot(document.getElementById("root")!).render(<App/>);
