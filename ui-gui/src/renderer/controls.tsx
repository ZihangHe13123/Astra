import { connectionFlow, filterCommands, moveSelection, opensCommandInterface } from "./control-state.js";
import { modelConnection } from "./model-connection.js";
import React, { useState } from "react";
import { X, Check, ChevronRight, ExternalLink, LoaderCircle } from "lucide-react";
import type { CommandDescription } from "../bridge.js";
import type { SessionState, UIEvent } from "@astra/ui-core/session-state";

export const names: Record<string, string> = {
  image:"添加图片", bar:"酒吧", minimal:"极简模式", sip:"小酌", reset:"清空对话", compress:"压缩上下文",
  undo:"撤销回复", retry:"重试回复", changes:"查看改动", think:"推理显示", model:"选择模型", mode:"推理强度", connect:"连接模型",
  persona:"选择人格", search:"搜索来源", tool:"工具结果", gallery:"图片画廊", memory:"记忆", skills:"技能", learn:"技能学习",
  tools:"可用工具", browser:"浏览器控制", computer:"电脑控制", conclave:"专家研究", appshot:"窗口捕获", tasks:"任务", budget:"时间预算",
  resume:"恢复任务", cancel:"停止任务", goal:"目标", today:"今日任务", session:"会话管理", handoff:"交接", theme:"外观", timeline:"时间线",
  health:"运行健康", doctor:"诊断", diagnostics:"诊断详情", maintenance:"维护", sandbox:"沙箱", "vision-tiles":"图像分块",
  "context-index":"上下文索引", mcp:"MCP 集成", yolo:"权限模式", permissions:"工具权限", reload:"热重载", reconnect:"重新连接",
  restart:"受控重启", wakeup:"会话提醒", help:"帮助",
};
export function Modal({ title, close, children, wide = false, initialFocus }: {
  title: string; close: () => void; children: React.ReactNode; wide?: boolean; initialFocus?: string;
}) {
  const ref = React.useRef<HTMLDivElement>(null);
  React.useEffect(() => {
    const prior = document.activeElement as HTMLElement | null;
    const panel = ref.current;
    const explicit = panel?.querySelector<HTMLElement>(initialFocus || "[data-autofocus]");
    const field = panel?.querySelector<HTMLElement>("input:not(:disabled),select:not(:disabled),textarea:not(:disabled)");
    if (explicit && !explicit.matches(":disabled")) explicit.focus();
    else if (!panel?.contains(document.activeElement)) (field || panel)?.focus();
    return () => { if (prior?.isConnected) prior.focus(); };
  }, []);
  return <div className="scrim" onMouseDown={e => { if (e.target === e.currentTarget) close(); }}>
    <div className={`modal ${wide ? "wide" : ""}`} role="dialog" aria-modal="true" aria-label={title} ref={ref} tabIndex={-1}
      onKeyDown={e => {
        if (e.key === "Escape") { e.stopPropagation(); close(); }
        if (e.key === "Tab") {
          const elements = [...(ref.current?.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled),select:not(:disabled),textarea:not(:disabled),a[href],[tabindex="0"]') || [])]
            .filter(element => element.tabIndex >= 0 && !element.hidden && element.getClientRects().length > 0);
          if (!elements.length) { e.preventDefault(); ref.current?.focus(); return; }
          if (e.shiftKey && (document.activeElement === elements[0] || document.activeElement === ref.current)) { e.preventDefault(); elements.at(-1)?.focus(); }
          else if (!e.shiftKey && (document.activeElement === elements.at(-1) || document.activeElement === ref.current)) { e.preventDefault(); elements[0].focus(); }
        }
      }}>
      <header><h2>{title}</h2><button className="icon" onClick={close} aria-label="关闭"><X size={18}/></button></header>{children}
    </div>
  </div>;
}
export function CommandPalette({ commands, run, close }: { commands: CommandDescription[]; run: (command: string) => void; close: () => void }) {
  const [filter, setFilter] = useState("");
  const [selected, setSelected] = useState<CommandDescription>();
  const [sub, setSub] = useState("");
  const [args, setArgs] = useState("");
  const [active, setActive] = useState(0);
  const search = React.useRef<HTMLInputElement>(null);
  const form = React.useRef<HTMLFormElement>(null);
  const listId = React.useId();
  const items = filterCommands(commands, filter, names);
  const activeIndex = Math.min(active, Math.max(0, items.length - 1));
  const execute = (command: string) => { close(); run(command); };
  const choose = (command: CommandDescription) => {
    const typed = filter.trim();
    if (typed.startsWith(`${command.command} `)) { execute(typed); return; }
    if (opensCommandInterface(command.command)) { execute(command.command); return; }
    setSelected(command);
  };
  const submit = () => { if (selected) execute(`${sub || selected.command} ${args}`.trim()); };
  React.useEffect(() => {
    if (selected) form.current?.querySelector<HTMLElement>("select,textarea")?.focus();
    else search.current?.focus();
  }, [selected]);
  React.useEffect(() => { document.getElementById(`${listId}-${activeIndex}`)?.scrollIntoView({ block: "nearest" }); }, [activeIndex, listId, filter]);
  return <Modal title="命令与功能" close={close} initialFocus=".command-search">
    {selected ? <form ref={form} onSubmit={e => { e.preventDefault(); submit(); }} className="form">
      <button className="text-button" type="button" onClick={() => { setSelected(undefined); setSub(""); setArgs(""); }}>← 全部功能</button>
      <h3>{names[selected.command.slice(1)]} <code>{selected.command}</code></h3><p className="muted">{selected.description}</p>
      {!!selected.options.length && <label>操作<select value={sub} onChange={e => setSub(e.target.value)}>
        <option value="">默认操作</option>{selected.options.map((o, i) => <option key={i} value={o.completion.trim()}>{o.command} · {o.description}</option>)}
      </select></label>}
      <label>参数（可选）<textarea rows={3} value={args} onChange={e => setArgs(e.target.value)} placeholder="名称、路径或操作内容；保留后端支持的全部参数"/></label>
      <div className="command-preview"><code>{sub || selected.command}{args && ` ${args}`}</code></div>
      <p className="muted small">操作由 Astra 后端执行，沿用当前会话的权限与模式。</p><button className="primary">执行</button>
    </form> : <><input ref={search} className="search-field command-search" value={filter} onChange={e => { setFilter(e.target.value); setActive(0); }}
      placeholder="搜索功能或输入命令…" aria-label="搜索命令与功能" role="combobox" aria-expanded="true" aria-controls={listId}
      aria-activedescendant={items.length ? `${listId}-${activeIndex}` : undefined} autoComplete="off"
      onKeyDown={e => {
        if (e.nativeEvent.isComposing) return;
        if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); setActive(moveSelection(activeIndex, items.length, e.key === "ArrowDown" ? 1 : -1)); }
        if (e.key === "Enter" && items[activeIndex]) { e.preventDefault(); choose(items[activeIndex]); }
      }}/>
      <div className="command-list" id={listId} role="listbox" aria-label="功能列表">{items.map((c, i) => <button key={c.id} id={`${listId}-${i}`} role="option" aria-selected={i === activeIndex}
        className={i === activeIndex ? "selected" : ""} tabIndex={-1} onMouseEnter={() => setActive(i)} onClick={() => choose(c)}>
        <span><strong>{names[c.command.slice(1)] || c.command}</strong><small>{c.description}</small></span><code>{c.command}</code><ChevronRight size={15}/>
      </button>)}{!items.length && <p className="muted" role="status">没有匹配的功能</p>}</div></>}
  </Modal>;
}
export function ModelPicker({ state, send, onConfigure, close }: {
  state?: SessionState; send: (cmd: UIEvent) => Promise<void>; onConfigure: (modelKey?: string) => void; close: () => void;
}) {
  const info = state?.info.model_info;
  const [filter, setFilter] = useState("");
  const [selecting, setSelecting] = useState("");
  const [error, setError] = useState("");
  const selectingRef = React.useRef(false);
  const mounted = React.useRef(true);
  React.useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const models: UIEvent[] = (info?.models || []).filter((model: UIEvent) => `${model.name} ${model.provider} ${model.key}`.toLowerCase().includes(filter.toLowerCase()));
  const canSelect = state?.status === "ready" && !state.busy && !selecting;
  const choose = async (model: UIEvent) => {
    if (selectingRef.current) return;
    if (modelConnection(info, model).configured === false) { onConfigure(model.key); return; }
    if (!canSelect) return;
    if (model.key === info?.model_key) { close(); return; }
    selectingRef.current = true; setSelecting(model.key); setError("");
    try { await send({ type: "select_model", model_key: model.key, request_id: crypto.randomUUID() }); if (mounted.current) close(); }
    catch (e) { if (mounted.current) setError(e instanceof Error ? e.message : String(e)); }
    finally { selectingRef.current = false; setSelecting(""); }
  };
  return <Modal title="选择模型" close={close} initialFocus=".model-search"><div className="model-picker">
    <input className="search-field model-search" aria-label="搜索模型" placeholder="搜索模型或提供商" value={filter} onChange={e => setFilter(e.target.value)}/>
    {state?.busy && <p className="muted small" role="status">当前回复完成或停止后可切换模型。</p>}
    {connectionFlow(state?.info).connecting && <button className="connection-progress" onClick={() => onConfigure()}>模型连接正在进行 · 查看进度</button>}
    <div className="models">{models.map(model => {
      const connection = modelConnection(info, model);
      return <button key={model.key} disabled={!!selecting || connection.configured !== false && !canSelect} onClick={() => { void choose(model); }} className={model.key === info?.model_key ? "selected" : ""}>
        <span><strong>{model.name}</strong><small>{model.provider} · {connection.configured === false ? "未连接 · 点击配置" : connection.configured ? "已配置" : "连接状态待确认"}</small></span>
        {selecting === model.key ? <LoaderCircle size={16} className="spin"/> : model.key === info?.model_key && <Check size={16}/>}
      </button>;
    })}{!models.length && <p className="muted" role="status">没有匹配的模型</p>}</div>
    {error && <p className="error-text small" role="alert">{error}</p>}
    <div className="model-picker-footer"><button className="text-button" onClick={() => onConfigure()}>模型与账号 <ChevronRight size={14}/></button></div>
  </div></Modal>;
}
export function ModelSettings({ state, send, close, initialModelKey }: {
  state?: SessionState; send: (cmd: UIEvent) => Promise<void>; close: () => void; initialModelKey?: string;
}) {
  const info = state?.info.model_info;
  const initialModel = info?.models?.find((model: UIEvent) => model.key === initialModelKey || model.name === initialModelKey);
  const initialConnection = initialModel ? modelConnection(info, initialModel) : undefined;
  const initialFlow = connectionFlow(state?.info);
  const [filter, setFilter] = useState("");
  const [route, setRoute] = useState((initialFlow.connecting ? initialFlow.routeId : initialConnection?.route?.id || initialFlow.routeId) || "");
  const [url, setURL] = useState(initialFlow.connecting ? "" : initialConnection?.endpoint || "");
  const [key, setKey] = useState("");
  const [keyEnv, setKeyEnv] = useState("");
  const [manual, setManual] = useState("");
  const [pending, setPending] = useState(initialFlow.requestId);
  const [selecting, setSelecting] = useState("");
  const selectingRef = React.useRef(false);
  const [feedback, setFeedback] = useState("");
  const [switchError, setSwitchError] = useState("");
  const credentialInput = React.useRef<HTMLInputElement>(null);
  const routes: UIEvent[] = info?.connection_routes || [];
  const { requestId, auth, result, connecting } = connectionFlow(state?.info, pending);
  const appliedInitialModel = React.useRef(!initialFlow.connecting && initialModel ? initialModelKey : undefined);
  React.useEffect(() => {
    if (!initialModel || !initialModelKey || appliedInitialModel.current === initialModelKey || connecting) return;
    const connection = modelConnection(info, initialModel);
    setRoute(connection.route?.id || ""); setURL(connection.endpoint); setKey(""); setKeyEnv("");
    appliedInitialModel.current = initialModelKey;
  }, [initialModelKey, initialModel, info, connecting]);
  const canSelect = state?.status === "ready" && !state.busy && !selecting;
  const choose = async (modelKey: string) => {
    if (!modelKey || selectingRef.current || !canSelect) return;
    const model = info?.models?.find((m: UIEvent) => m.key === modelKey || m.name === modelKey);
    const connection = modelConnection(info, model);
    setSwitchError("");
    if (model && connection.configured === false) {
      if (connecting) { setFeedback("连接正在进行，请先完成或取消当前连接，再配置其他模型。"); return; }
      setRoute(connection.route?.id || ""); setURL(connection.endpoint); setKey(""); setKeyEnv("");
      setFeedback(`${connection.label} 尚未连接。请在右侧登录或填写 API Key，再选择模型；当前模型保持不变。`);
      requestAnimationFrame(() => credentialInput.current?.focus());
      return;
    }
    if (modelKey === info?.model_key) return;
    selectingRef.current = true; setSelecting(modelKey); setFeedback("");
    try {
      await send({ type: "select_model", model_key: modelKey, request_id: crypto.randomUUID() });
      setFeedback("模型已切换。");
    } catch (error) { setSwitchError(error instanceof Error ? error.message : String(error)); }
    finally { selectingRef.current = false; setSelecting(""); }
  };
  const connect = async () => {
    if (connecting) return;
    const r = routes.find(r => r.id === route);
    const request_id = crypto.randomUUID(); setPending(request_id); setSwitchError("");
    try {
      await send({ type: "connect_provider", request_id, route_id: route, base_url: url || r?.base_url || "", api_key: key, api_key_env: key ? "" : keyEnv || (r?.key_available ? r.api_key_env : "") });
    } catch (error) { setPending(""); setSwitchError(error instanceof Error ? error.message : String(error)); }
    finally { setKey(""); }
  };
  return <Modal title="模型与账号" close={close} wide initialFocus={initialModelKey ? ".connection-route" : ".model-settings-search"}>
    <div className="settings-grid"><section>
      <h3>选择模型</h3><p className="muted small">每台设备、每个独立安装分别连接账号。未连接的模型会先打开配置。</p>
      <input className="model-settings-search" aria-label="搜索模型或提供商" value={filter} onChange={e => setFilter(e.target.value)} placeholder="搜索模型或提供商"/>
      {state?.busy && <p className="muted small" role="status">当前回复完成或停止后可切换模型。</p>}
      <div className="models">{(info?.models || []).filter((m: UIEvent) => `${m.name} ${m.provider}`.toLowerCase().includes(filter.toLowerCase())).map((model: UIEvent) => {
        const connection = modelConnection(info, model);
        return <button key={model.key} disabled={!canSelect} onClick={() => { void choose(model.key); }} className={model.key === info?.model_key ? "selected" : ""}>
          <span><strong>{model.name}</strong><small>{model.provider} · {connection.configured === false ? "未连接 · 点击配置" : connection.configured ? "已配置" : "连接状态待确认"} · {model.context_limit ? `${Math.round(model.context_limit / 1000)}K` : "窗口未知"}</small></span>
          {selecting === model.key ? <LoaderCircle size={16} className="spin"/> : model.key === info?.model_key && <Check size={16}/>}
        </button>;
      })}</div>
      <div className="inline"><input aria-label="精确模型 ID" value={manual} onChange={e => setManual(e.target.value)} placeholder="精确模型 ID"/><button disabled={!canSelect || !manual.trim()} onClick={() => { void choose(manual.trim()); }}>选择</button></div>
      {feedback && <p className="model-feedback muted small" role="status">{feedback}</p>}
      {switchError && <p className="model-feedback error-text small" role="alert">{switchError}</p>}
    </section><section className="form">
      <h3>添加连接</h3><label>提供商 / 计费路线<select className="connection-route" value={route} disabled={connecting} onChange={e => { setRoute(e.target.value); setURL(""); setKey(""); setKeyEnv(""); setFeedback(""); }}><option value="">选择路线</option>{routes.map(r => <option value={r.id} key={r.id}>{r.provider} · {r.label}</option>)}</select></label>
      {routes.find(r => r.id === route)?.auth_mode !== "oauth" && <>
        <label>API 地址<input value={url} onChange={e => setURL(e.target.value)} placeholder={routes.find(r => r.id === route)?.base_url || "https://…"}/></label>
        <label>API Key<input ref={credentialInput} type="password" autoComplete="off" value={key} onChange={e => setKey(e.target.value)}/></label>
        <label>或使用环境变量<input value={keyEnv} onChange={e => setKeyEnv(e.target.value)} placeholder="例如 OPENAI_API_KEY"/></label>
      </>}
      <button className="primary" disabled={!route || connecting} onClick={() => { void connect(); }}>{routes.find(r => r.id === route)?.auth_mode === "oauth" ? "登录 ChatGPT" : "保存连接"}</button>
      {auth && !result && <div className="auth-card"><p>在浏览器中完成设备授权</p><strong className="auth-code">{auth.user_code}</strong>
        <button onClick={() => { void window.astra.openExternal(auth.verification_uri); }}>打开授权页面 <ExternalLink size={14}/></button>
        <button onClick={() => { void send({ type: "cancel_connection", request_id: requestId }).catch(error => setSwitchError(String(error))); }}>取消登录</button></div>}
      {connecting && !auth && <div role="status"><p className="muted"><LoaderCircle size={14}/> 正在连接…</p>
        <button onClick={() => { void send({ type: "cancel_connection", request_id: requestId }).catch(error => setSwitchError(String(error))); }}>取消连接</button></div>}
      {result && <p role="status" className={result.error ? "error-text" : "muted"}>{result.error || result.notice || "连接已保存，请选择模型。"}</p>}
      <h3>连接状态</h3>{(info?.providers || []).map((p: UIEvent) => <div className="provider" key={p.id}><div><strong>{p.label}</strong><small>{p.connected ? "已配置" : "未连接"} · {p.source}</small>{p.error && <small className="error-text">{p.error}</small>}</div><button onClick={() => { void send({ type: "refresh_models", provider_id: p.id, force: true }).catch(error => setSwitchError(String(error))); }}>刷新</button></div>)}
    </section></div>
  </Modal>;
}
export function Approval({ event, send }: { event: UIEvent; send: (c: UIEvent) => Promise<void> }) {
  const [pending, setPending] = useState(false);
  return <section className="approval interactive-card"><span className="eyebrow">需要你的许可</span><h3>{event.approval_title || event.tool_name}</h3>
    <p>{event.approval_question || event.reason}</p><pre>{event.approval_summary || event.detail || event.target || JSON.stringify(event.arguments, null, 2)}</pre>
    {event.scope && <p className="small muted">范围：{event.scope}</p>}
    <div className="actions">{(event.choices || ["once", "session", "deny"]).map((decision: string) => <button key={decision} disabled={pending} className={decision === "once" ? "primary" : ""} onClick={() => {
      setPending(true); void send({ type: "tool_approval_response", request_id: event.request_id, decision }).catch(() => setPending(false));
    }}>{({ once: "允许一次", session: "本会话允许", deny: "拒绝" } as Record<string, string>)[decision]}</button>)}</div>
  </section>;
}
export function Question({ event, send }: { event: UIEvent; send: (c: UIEvent) => Promise<void> }) {
  const [answers, setAnswers] = useState<Record<string, { selected: string[]; custom?: string }>>({});
  const [pending, setPending] = useState(false);
  React.useEffect(() => { setPending(false); }, [event]);
  return <form className="interactive-card" onSubmit={e => { e.preventDefault(); setPending(true); void send({ type: "user_question_response", request_id: event.request_id,
    answers: event.questions.map((q: UIEvent) => ({ id: q.id, selected: answers[q.id]?.selected || [], custom: answers[q.id]?.custom || "" })) }).catch(() => setPending(false)); }}>
    <span className="eyebrow">需要你的意见</span>{event.questions.map((q: UIEvent) => <fieldset key={q.id}><legend>{q.question}</legend>
      {(q.options || []).map((o: UIEvent) => <label className="option" key={o.label}><input type={q.multi_select ? "checkbox" : "radio"} name={q.id} checked={answers[q.id]?.selected?.includes(o.label) || false}
        onChange={e => setAnswers(old => ({ ...old, [q.id]: { ...old[q.id], selected: q.multi_select ? e.target.checked ? [...(old[q.id]?.selected || []), o.label] : (old[q.id]?.selected || []).filter(x => x !== o.label) : [o.label] } }))}/>
        <span>{o.label}{o.description && <small>{o.description}</small>}</span></label>)}
      <textarea rows={2} placeholder="也可以直接输入你的想法" value={answers[q.id]?.custom || ""} onChange={e => setAnswers(old => ({ ...old, [q.id]: { selected: old[q.id]?.selected || [], custom: e.target.value } }))}/>
    </fieldset>)}<div className="actions"><button className="primary" disabled={pending}>发送答复</button><button type="button" onClick={() => { setPending(true); void send({ type: "user_question_cancel", request_id: event.request_id }).catch(() => setPending(false)); }}>取消</button></div>
  </form>;
}
