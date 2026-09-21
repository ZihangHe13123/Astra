import React, { useEffect, useState } from "react";
import { ChevronRight, Users } from "lucide-react";
import { delegateActive, delegateStatusLabel, orderedDelegates, type DelegateView } from "@astra/ui-core/delegates";
import { Markdown } from "./messages.js";

export function DelegateSummary({ delegates, open }: { delegates: Record<string, DelegateView>; open: () => void }) {
  const items = orderedDelegates(delegates);
  if (!items.length) return null;
  const active = items.filter(delegateActive);
  const visible = (active.length ? active : items).slice(0, 3);
  return <button className="delegate-summary" data-testid="delegate-summary" onClick={open}>
    <span className="delegate-heading"><Users size={15}/><strong>子代理</strong><span className="muted">{active.length ? `${active.length} 项进行中` : "最近委派"}</span><ChevronRight size={15}/></span>
    {visible.map(d => <span className="delegate-brief" key={d.process_id}><span className={`status-dot ${d.status}`}/><span title={d.goal}>{d.goal}</span><small>{delegateStatusLabel(d.status)}{d.status === "running" && d.current_tool ? ` · ${d.current_tool}` : ""}</small></span>)}
    {active.length > visible.length && <small className="muted">另有 {active.length - visible.length} 项进行中</small>}
  </button>;
}

export function DelegateCards({ delegates, runtime, fail, openFile }: { delegates: Record<string, DelegateView>; runtime?: string; fail: (e: unknown) => void; openFile?: (path: string) => void }) {
  const [limit, setLimit] = useState(20);
  const items = orderedDelegates(delegates);
  const running = items.some(delegateActive);
  const [now, setNow] = useState(Date.now());
  useEffect(() => { if (!running) return; setNow(Date.now()); const id = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(id); }, [running]);
  useEffect(() => setLimit(20), [runtime]);
  if (!items.length) return null;
  return <section className="delegates" aria-label="委派任务"><h3>子代理 <span className="count">{items.length}</span></h3>
    {items.some(d => d.history_truncated) && <p className="muted small">仅恢复了最近的部分委派记录。</p>}
    {items.slice(0, limit).map(d => <DelegateCard key={d.process_id} item={d} now={now} runtime={runtime} fail={fail} openFile={openFile}/>)}
    {items.length > limit && <button className="text-button" onClick={() => setLimit(n => n + 20)}>显示更多委派（还有 {items.length - limit} 项）</button>}
  </section>;
}
export function DelegateHistory({ delegates, fail }: { delegates: DelegateView[]; fail: (e: unknown) => void }) {
  const [open, setOpen] = useState(false);
  if (!delegates.length) return null;
  return <details className="delegate-history" onToggle={e => setOpen(e.currentTarget.open)}><summary>委派记录 · {delegates.length} 项</summary>
    {open && <DelegateCards delegates={Object.fromEntries(delegates.map(d => [d.process_id, d]))} fail={fail}/>}
  </details>;
}
function DelegateCard({ item: d, now, runtime, fail, openFile }: { item: DelegateView; now: number; runtime?: string; fail: (e: unknown) => void; openFile?: (path: string) => void }) {
  const [open, setOpen] = useState(false);
  const elapsed = delegateActive(d)
    ? d.started_at ? Math.max(0, now - d.started_at * 1000) : d.duration_ms
    : d.duration_ms;
  const duration = elapsed == null ? "" : elapsed >= 60000 ? `${Math.floor(elapsed / 60000)} 分 ${Math.floor(elapsed / 1000) % 60} 秒` : `${Math.floor(elapsed / 1000)} 秒`;
  return <article className="delegate-card" data-delegate-id={d.process_id} data-status={d.status}>
    <div className="delegate-heading"><span className={`status-dot ${d.status}`}/><strong>{delegateStatusLabel(d.status)}</strong><span className="muted small">{duration}</span></div>
    <p className="delegate-goal">{d.goal}{d.goal_truncated && "…"}</p>
    {d.status === "running" && <p className="delegate-activity">{d.current_tool ? `正在调用 ${d.current_tool}` : "正在处理任务"}</p>}
    {d.status === "queued" && <p className="delegate-activity">等待执行名额</p>}
    {d.status === "idle" && <p className="delegate-activity">等待新消息</p>}
    {d.status === "interrupted" && <p className="delegate-activity">连接或进程已中断，未确认最终结果。</p>}
    {d.turns_used != null && <p className="delegate-meta">已执行 {d.turns_used} 轮{d.max_turns ? ` / 上限 ${d.max_turns} 轮` : ""}</p>}
    {d.error && <p className="error-text">{d.error}{d.error_truncated && "…"}</p>}
    {d.result && <details className="delegate-result" onToggle={e => setOpen(e.currentTarget.open)}><summary>{d.partial || d.status === "partial" ? "查看部分结果" : "查看结果"}</summary>
      {open && <div className="message message-body"><Markdown text={d.result} runtime={runtime} fail={fail}/></div>}
      {d.result_truncated && <p className="muted small">结果较长，此处已截断显示。</p>}
    </details>}
    {d.artifact_path && openFile && <button className="text-button" onClick={() => openFile(d.artifact_path!)}>查看完整记录</button>}
  </article>;
}
