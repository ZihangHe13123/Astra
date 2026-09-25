import React, { useEffect, useRef, useState } from "react";
import { FileDiff, Terminal, Layers, Users, X, ExternalLink, FolderOpen } from "lucide-react";
import type { SessionState, UIEvent } from "@astra/ui-core/session-state";
import { DelegateCards } from "./delegates.js";
import { toolEmptyOutput, toolExpanded, toolStatus } from "./tool-status.js";

export type Panel = "tools" | "changes" | "files" | "context" | "team";
export function Details({ state, panel, setPanel, selection, width, onWidth, close, fail }: { state: SessionState; panel: Panel; setPanel: (p: Panel) => void; selection: { index?: number; serial: number }; width: number; onWidth: (width: number) => void; close: () => void; fail: (e: unknown) => void }) {
  const content = useRef<HTMLDivElement>(null);
  const previewRequest = useRef(0);
  const locatedSelection = useRef("");
  const panelRef = useRef<HTMLElement>(null);
  const drag = useRef<{ x: number; width: number }>();
  const [changes, setChanges] = useState<any>();
  const [turn, setTurn] = useState(1);
  const [file, setFile] = useState<number>();
  const [preview, setPreview] = useState<{ text?: string; data?: string; path?: string; truncated?: boolean }>();
  useEffect(() => {
    if (!selection.index || !["tools", "files"].includes(panel)) return;
    const target = `${state.id}:${panel}:${selection.serial}`;
    if (locatedSelection.current === target) return;
    if (preview) { setPreview(undefined); return; }
    locatedSelection.current = target;
    const node = content.current?.querySelector<HTMLElement>(`[data-result-index="${selection.index}"]`);
    if (node) { if (node instanceof HTMLDetailsElement) node.open = true; node.scrollIntoView({ block: "start" }); }
    else fail(`当前记录中没有${panel === "files" ? "图片" : "工具"}结果 ${selection.index}。`);
  }, [selection, panel, state.id, preview]);
  useEffect(() => {
    if (panel !== "changes" || state.status !== "ready") return;
    let cancelled = false;
    void window.astra.query("changes", { turn, ...(file === undefined ? {} : { index: file }) }, state.id)
      .then(result => { if (!cancelled) setChanges(result); }).catch(fail);
    return () => { cancelled = true; };
  }, [panel, turn, file, state.id, state.mode, state.session, state.status, state.info.turn_changes]);
  const openFile = (path: string) => { const request = ++previewRequest.current; void window.astra.file(state.id, path, "preview").then(value => { if (request === previewRequest.current) setPreview(value); }).catch(fail); };
  const tabs = [{ id: "tools", label: "执行", icon: Terminal }, { id: "changes", label: "改动", icon: FileDiff }, { id: "files", label: "文件", icon: FolderOpen }, { id: "context", label: "上下文", icon: Layers }, { id: "team", label: "Team", icon: Users }] as const;
  const galleries = state.tools.flatMap((tool, index) => {
    if (tool.name !== "search_images" || tool.error) return [];
    try { const result = JSON.parse(tool.output); return result.type === "image_search" && result.success && Array.isArray(result.images) ? [{ ...result, index: tool.result_index || index + 1 }] : []; }
    catch { return []; }
  });
  const artifacts = [...new Set([...state.tools.map(t => t.artifact_path), ...Object.values(state.processes).map(p => p.artifact_path)].filter(Boolean))];
  useEffect(() => { previewRequest.current++; setFile(undefined); setTurn(1); setPreview(undefined); setChanges(undefined); }, [state.id, state.mode, state.session]);
  return <aside className="details-panel" ref={panelRef} style={{ width }} aria-label="执行详情面板">
    <div className="panel-resizer" role="separator" tabIndex={0} aria-label="调整详情宽度" aria-orientation="vertical" aria-valuemin={260} aria-valuemax={720} aria-valuenow={Math.round(width)}
      onKeyDown={e => { if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) { e.preventDefault(); onWidth(e.key === "Home" ? 260 : e.key === "End" ? 720 : Math.max(260, Math.min(720, width + (e.key === "ArrowLeft" ? 20 : -20)))); } }}
      onPointerDown={e => { drag.current = { x: e.clientX, width: panelRef.current!.getBoundingClientRect().width }; e.currentTarget.setPointerCapture(e.pointerId); e.preventDefault(); }}
      onPointerMove={e => { if (drag.current && panelRef.current) panelRef.current.style.width = `${Math.max(260, Math.min(720, drag.current.width + drag.current.x - e.clientX))}px`; }}
      onPointerUp={e => { if (drag.current) { onWidth(Math.max(260, Math.min(720, drag.current.width + drag.current.x - e.clientX))); drag.current = undefined; e.currentTarget.releasePointerCapture(e.pointerId); } }}
      onPointerCancel={() => { drag.current = undefined; if (panelRef.current) panelRef.current.style.width = `${width}px`; }}/>
    <div className="panel-tabs" role="tablist">{tabs.map(t => <button key={t.id} role="tab" aria-selected={panel === t.id} onClick={() => { previewRequest.current++; setPreview(undefined); setPanel(t.id); }}><t.icon size={15}/>{t.label}</button>)}<button className="icon" onClick={close} aria-label="关闭详情"><X size={15}/></button></div>
    <div className="panel-content" ref={content}>
      {preview ? <><button className="text-button" onClick={() => setPreview(undefined)}>← 返回</button><h3>{preview.path?.split(/[\\/]/).pop()}</h3>
        <div className="actions"><button onClick={() => { void window.astra.file(state.id, preview.path!, "open").catch(fail); }}><ExternalLink size={14}/> 系统打开</button><button onClick={() => { void window.astra.file(state.id, preview.path!, "reveal").catch(fail); }}><FolderOpen size={14}/> 定位</button></div>
        {preview.data ? <img className="preview-image" src={preview.data} alt="文件预览"/> : <pre className="file-preview">{preview.text}</pre>}{preview.truncated && <p className="muted">仅预览前 256 KiB，原文件保持完整。</p>}
      </> : panel === "tools" ? <>
        <DelegateCards delegates={state.delegates} runtime={state.id} fail={fail} openFile={openFile}/>
        <h3>工具与进程 <span className="count">{state.tools.length}</span></h3>
        {!state.tools.length && <p className="muted">工具运行后，结果会显示在这里。</p>}
        {[...state.tools].sort((a, b) => Number(toolStatus(b) === "running") - Number(toolStatus(a) === "running")).map((tool, i) => <details className="tool-detail" data-result-index={tool.result_index} key={tool.call_id || i} open={toolExpanded(tool)}><summary><span className={`status-dot ${toolStatus(tool)}`}/><strong>{tool.result_index ? `${tool.result_index}. ` : ""}{tool.name}</strong><span className="muted small">{statusLabel(toolStatus(tool))}{tool.duration_ms != null ? ` · ${tool.duration_ms >= 1000 ? `${(tool.duration_ms / 1000).toFixed(1)} 秒` : `${tool.duration_ms} ms`}` : ""}</span></summary>
          {tool.arguments && <details><summary>参数</summary><pre>{typeof tool.arguments === "string" ? tool.arguments : JSON.stringify(tool.arguments, null, 2)}</pre></details>}
          {tool.progress && toolStatus(tool) === "running" && <p>{tool.progress.message || tool.progress.stage}</p>}
          {tool.error && <p className="error-text">{tool.error}</p>}{(tool.output || toolEmptyOutput(tool)) && <pre>{tool.output || toolEmptyOutput(tool)}</pre>}
          {tool.artifact_path && <button onClick={() => openFile(tool.artifact_path)}>查看完整结果</button>}
          {tool.output_truncated && <p className="muted small">显示内容已截断；完整输出见产物。</p>}
        </details>)}
        {Object.values(state.processes).filter(p => p.kind !== "subagent" || !state.delegates[p.process_id]).map((p: UIEvent) => <details key={p.process_id} className="tool-detail"><summary>{p.label} · {statusLabel(p.status)}</summary><RawDetails value={p}/>{p.artifact_path && <button onClick={() => openFile(p.artifact_path)}>查看日志</button>}</details>)}
      </> : panel === "changes" ? <>
        <h3>文件改动</h3><p className="muted small">查看已完成回合的快照差异。</p>
        {changes?.turns?.length ? <select value={turn} onChange={e => { setTurn(Number(e.target.value)); setFile(undefined); }}>{changes.turns.map((t: UIEvent, i: number) => <option key={t.turn_seq} value={i + 1}>回合 {t.turn_seq} · {t.files} 个文件{!t.available && !t.empty ? " · 快照已淘汰" : ""}</option>)}</select> : <p className="muted">还没有可查看的回合。</p>}
        {changes?.record?.empty && <p className="muted">本回合没有净改动。</p>}
        {changes?.record && !changes.available && !changes.record.empty && <p className="muted">快照不可用或已淘汰，不代表没有改动。</p>}
        {changes?.files?.map((f: UIEvent, i: number) => <button className={`file-row ${file === i ? "selected" : ""}`} key={i} onClick={() => setFile(i)}><FileDiff size={16}/><span>{f.display || f.path}{!f.confirmed && <small>未能确认 · {f.reason}</small>}</span><small>+{f.added ?? "?"} −{f.removed ?? "?"}</small></button>)}
        {changes?.sides && <><p className="small muted">{changes.files[file!]?.reason || changes.files[file!]?.compare}</p><div className="diff">
          {changes.sides.binary ? <p>二进制内容不做文本比较。</p> : changes.sides.unified != null ? <pre className="unified">{changes.sides.unified.split('\n').map((line: string, i: number) => <span key={i} className={line.startsWith('+') ? 'added' : line.startsWith('-') ? 'removed' : line.startsWith('@@') ? 'hunk' : ''}>{line}{'\n'}</span>)}</pre> : <>
          <section><h4>修改前 · {changes.sides.before_state}</h4><pre>{changes.sides.before ?? (changes.sides.before_state === "absent" ? "文件不存在" : "无可用快照")}</pre></section><section><h4>修改后 · {changes.sides.after_state}</h4><pre>{changes.sides.after ?? (changes.sides.after_state === "absent" ? "文件不存在" : "无可用快照")}</pre></section></>}
          {changes.sides.truncated && <p className="muted small">较大快照仅显示开头；增减行数仍取自原始账本。</p>}</div></>}
      </> : panel === "files" ? <>
        <h3>文件与图片</h3><div className="actions"><button onClick={() => { void window.astra.choose("files").then(paths => { if (paths[0]) openFile(paths[0]); }).catch(fail); }}>选择文件预览</button></div>
        {artifacts.map(path => <button className="file-row" key={path} onClick={() => openFile(path)}><FolderOpen size={15}/><span>{path.split(/[\\/]/).pop()}</span></button>)}
        {galleries.map(g => <section key={g.index} className="gallery" data-result-index={g.index}><h4>搜图 · 工具结果 {g.index}</h4>{g.images.slice(0, 10).map((image: UIEvent, i: number) => <article key={image.id || i}>
          {/^https:\/\//.test(image.image_url) && <img loading="lazy" src={image.image_url} alt={image.title || "搜索结果"}/>}
          <p>{i + 1}. {image.title}</p><button onClick={() => { void window.astra.openExternal(image.source_url).catch(fail); }}>查看来源</button></article>)}</section>)}
        {!artifacts.length && !galleries.length && <p className="muted">工具产物和搜索图片会显示在这里；对话中的文件链接也可直接打开。</p>}
      </> : panel === "team" ? <>
        <h3>团队与工作计划</h3>{Object.values(state.teams).map(t => <section className="team-card" key={t.id}>
          <h4>{t.name} <span className="muted">{statusLabel(t.status)}</span></h4>{t.goal && <p className="muted small">{t.goal}</p>}
          {t.agents.map(a => <div className="team-member" key={a.id}><span className={`status-dot ${a.status}`}/><span>{a.name}<small>{a.role}</small></span><span>{statusLabel(a.status)}{a.unread > 0 && ` · ${a.unread} 条未读`}</span></div>)}
          {t.tasks.map(task => <p className="small" key={task.id}>{task.status === "completed" ? "✓" : "·"} {task.title} <span className="muted">{statusLabel(task.status)}</span></p>)}
          <RawDetails label={`诊断详情 · ${t.messageCount} 条消息`} value={t}/>
        </section>)}
        {!Object.keys(state.teams).length && <p className="muted">当前没有 Team 活动。</p>}
        {state.info.working_memory && <><h4>当前计划</h4><PlanSummary memory={state.info.working_memory.memory}/></>}
      </> : <>
        <h3>当前上下文</h3><dl><dt>工作区</dt><dd>{state.workspace}</dd><dt>会话</dt><dd>{state.session}</dd><dt>模式</dt><dd>{state.mode}</dd><dt>模型</dt><dd>{state.info.model_info?.model || "未连接"}</dd><dt>上下文</dt><dd>{state.info.model_info?.context_used?.toLocaleString() || 0} / {state.info.model_info?.context_limit?.toLocaleString() || "—"}</dd></dl>
        {Number(state.info.model_info?.context_limit) > 0 && <div className="context-meter"><progress aria-label="上下文使用量" value={Number(state.info.model_info.context_used || 0)} max={Number(state.info.model_info.context_limit)}/><small className="muted">按当前模型的上下文预算显示</small></div>}
        {state.info.wakeup_status?.plan && <section className="summary-card"><h4>定时提醒 · {statusLabel(state.info.wakeup_status.plan.state)}</h4><p>{state.info.wakeup_status.message || state.info.wakeup_status.plan.prompt || "等待下一次唤醒"}</p></section>}
        <h4 className="diagnostics-title">原始诊断</h4>
        {Object.entries(state.info).filter(([key]) => !["history", "model_info", "session_list", "backend_hello", "connection_auth", "connection_pending"].includes(key)).map(([key, event]) => <RawDetails key={key} label={eventLabels[key] || key} value={event}/>)}
      </>}
    </div>
  </aside>;
}

const eventLabels: Record<string, string> = { task_status: "任务状态", generation_progress: "生成进度", working_memory: "工作计划", yolo_status: "工具权限", computer_status: "电脑控制", gui_appshot: "窗口捕获", wakeup_status: "定时提醒", peer_message: "会话互通", turn_changes: "文件改动", usage: "用量", gui_model_result: "模型切换结果" };
function statusLabel(status?: string) { return ({ running: "运行中", active: "进行中", scheduled: "已安排", completed: "已完成", idle: "待命", pending: "等待中", in_progress: "进行中", done: "已完成", cancelled: "已取消", failed: "失败", interrupted: "已中断", paused: "已暂停", ready: "就绪" } as Record<string, string>)[status || ""] || status || ""; }
function RawDetails({ value, label = "诊断详情" }: { value: unknown; label?: string }) {
  const [open, setOpen] = useState(false);
  return <details className="tool-detail" onToggle={e => setOpen(e.currentTarget.open)}><summary className="muted small">{label}</summary>{open && <pre>{JSON.stringify(value, null, 2)}</pre>}</details>;
}
function PlanSummary({ memory }: { memory?: UIEvent }) {
  if (!memory) return null;
  const steps = Array.isArray(memory.plan) ? memory.plan : Array.isArray(memory.steps) ? memory.steps : [];
  return <section className="summary-card">{memory.goal && <p>{String(memory.goal)}</p>}{typeof memory.plan === "string" && <p>{memory.plan}</p>}{steps.map((step: any, i: number) => <p key={i} className="small">{typeof step === "string" ? step : `${statusLabel(step.status)} · ${step.text || step.description || step.title || step.step || ""}`}</p>)}<RawDetails label="完整计划数据" value={memory}/></section>;
}
