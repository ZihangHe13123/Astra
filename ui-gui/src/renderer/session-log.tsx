import React, { useEffect, useRef, useState } from "react";
import { ArrowLeft, RefreshCw, X } from "lucide-react";

/** The public query worker only resolves these persisted session namespaces. */
export const supportsSessionLog = (mode: string) => ["work", "bar", "minimal"].includes(mode);

export type SourceRef = { index: number; digest: string };
export type LogTarget = { source_ref?: SourceRef; call_id?: string; serial: number };
export type SessionLogRecord = {
  source_ref: SourceRef; role: string; tool_call_ids: string[]; raw: string; raw_truncated: boolean; raw_bytes: number;
  chat_position: number | null; chat_source_ref?: SourceRef | null;
};
export type SessionLogPage = {
  records: SessionLogRecord[]; before: number; has_more: boolean; total: number; revision: string;
  target_status: "none" | "found" | "stale" | "missing" | "ambiguous"; target_index: number | null;
};
export const sameSource = (a?: SourceRef | null, b?: SourceRef | null) => !!a && !!b && a.index === b.index && a.digest === b.digest;
export function logTargetNotice(status: SessionLogPage["target_status"]) {
  return ({ none: "", found: "", stale: "记录已变化或被压缩，原位置不再匹配。请刷新记录后重新选择。", missing: "未找到对应的持久化记录。它可能尚未保存，或来自未保留来源的旧会话。", ambiguous: "此工具调用对应多个记录，无法唯一定位；请从记录列表选择。" })[status];
}
export function recordChatSource(record: SessionLogRecord): SourceRef | undefined {
  return record.chat_source_ref || (record.chat_position !== null ? record.source_ref : undefined);
}
export function SessionLogCard({ record, selected, select, locate }: { record: SessionLogRecord; selected: boolean; select: () => void; locate: () => void }) {
  const linked = !!recordChatSource(record);
  return <article className={`session-log-record ${selected ? "selected" : ""}`} data-log-index={record.source_ref.index}>
    <button className="session-log-heading" aria-expanded={selected} onClick={select}><strong>记录 {record.source_ref.index + 1} · {record.role}</strong><span className="muted small">{record.raw_bytes.toLocaleString()} 字节</span></button>
    {record.tool_call_ids.length > 0 && <p className="small muted">工具调用：{record.tool_call_ids.join("、")}</p>}
    {selected && <><button className="small" disabled={!linked} onClick={locate}>定位到聊天</button>
      {!linked && <p className="small muted">此原始记录没有对应的可见聊天消息，可能是工具结果、旧记录或已压缩内容。</p>}
      <pre className="session-log-raw">{record.raw}</pre>
      {record.raw_truncated && <p className="small muted">原始记录较大，仅显示前 16 KiB；显示截断不影响来源校验。</p>}
    </>}
  </article>;
}

/** Read-only persisted session records. Requests never create or control a runtime. */
export function SessionLogPanel({ session, mode, target, width, close, locate }: {
  session: string; mode: string; target: LogTarget; width: number; close: () => void; locate: (record: SessionLogRecord, revision: string, isCurrent: () => boolean) => Promise<void>;
}) {
  const [page, setPage] = useState<SessionLogPage>();
  const [selected, setSelected] = useState<number>();
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [locating, setLocating] = useState(false);
  const [cursors, setCursors] = useState<(number | undefined)[]>([undefined]);
  const serial = useRef(0);
  const content = useRef<HTMLDivElement>(null);
  const scope = `${mode}:${session}`;
  const scopeRef = useRef(scope); scopeRef.current = scope;
  const request = async (params: Record<string, unknown>, stack: (number | undefined)[], expectedRevision?: string) => {
    const generation = ++serial.current, requestScope = scope;
    setBusy(true); setLocating(false); setError(""); setNotice("");
    try {
      const value: SessionLogPage = await window.astra.query("session_log", { name: session, mode, limit: 25, ...params });
      if (generation !== serial.current || requestScope !== scopeRef.current) return;
      if (expectedRevision && value.revision !== expectedRevision) {
        setPage(undefined); setSelected(undefined); setCursors([undefined]);
        setNotice("会话记录已更新，分页位置可能变化。请刷新记录后继续。"); return;
      }
      setPage(value); setCursors(stack); setSelected(value.target_status === "found" ? value.target_index ?? undefined : undefined);
      setNotice(logTargetNotice(value.target_status));
    } catch (cause) { if (generation === serial.current && requestScope === scopeRef.current) setError(String(cause).includes("Session does not exist") ? "此会话尚无已保存记录；消息保存后可刷新查看。" : String(cause)); }
    finally { if (generation === serial.current && requestScope === scopeRef.current) setBusy(false); }
  };
  useEffect(() => {
    setPage(undefined); setSelected(undefined); setLocating(false);
    void request(target.source_ref ? { source_ref: target.source_ref } : target.call_id ? { call_id: target.call_id } : {}, [undefined]);
    return () => { serial.current++; };
  }, [scope, target.serial]);
  useEffect(() => {
    if (selected !== undefined) content.current?.querySelector<HTMLElement>(`[data-log-index="${selected}"]`)?.scrollIntoView({ block: "nearest" });
  }, [selected, page?.revision]);
  const locateRecord = async (record: SessionLogRecord) => {
    if (!page || locating) return;
    const generation = serial.current, requestScope = scope;
    setLocating(true); setError("");
    try { await locate(record, page.revision, () => generation === serial.current && requestScope === scopeRef.current); }
    catch (cause) { if (generation === serial.current && requestScope === scopeRef.current) setError(String(cause)); }
    finally { if (generation === serial.current && requestScope === scopeRef.current) setLocating(false); }
  };
  return <aside className="details-panel session-log-panel" style={{ width }} aria-label="原始会话记录面板">
    <div className="panel-tabs"><strong>原始会话记录</strong><button className="icon" disabled={busy} aria-label="刷新会话记录" onClick={() => { void request({}, [undefined]); }}><RefreshCw size={15}/></button><button className="icon" onClick={close} aria-label="关闭会话记录"><X size={15}/></button></div>
    <div className="panel-content" ref={content} aria-busy={busy || locating}>
      <p className="small muted">只读查看已保存的消息与工具调用记录。这里不包含完整的提供商网络事件。</p>
      {error && <p className="error-text" role="alert">{error}</p>}{notice && <p className="notice" role="status">{notice}</p>}
      {busy && <p className="muted" role="status">正在读取记录…</p>}{locating && <p className="muted" role="status">正在定位聊天…</p>}
      {!busy && page && !page.records.length && !notice && <p className="muted">此会话暂无持久化记录。</p>}
      {page?.records.map(record => <SessionLogCard key={`${record.source_ref.index}:${record.source_ref.digest}`} record={record} selected={selected === record.source_ref.index} select={() => setSelected(selected === record.source_ref.index ? undefined : record.source_ref.index)} locate={() => { void locateRecord(record); }}/>) }
      {page && <div className="actions session-log-pagination"><button disabled={busy || cursors.length < 2} onClick={() => { const next = cursors.slice(0, -1); void request(next.at(-1) === undefined ? {} : { before: next.at(-1) }, next, page.revision); }}><ArrowLeft size={13}/>较新记录</button><span className="small muted">共 {page.total.toLocaleString()} 条</span><button disabled={busy || !page.has_more} onClick={() => { void request({ before: page.before }, [...cursors, page.before], page.revision); }}>较早记录</button></div>}
    </div>
  </aside>;
}
