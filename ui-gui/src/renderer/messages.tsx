import React, { memo, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { RotateCcw, FileText } from "lucide-react";
import type { Message } from "@astra/ui-core/session-state";
import { expandRestoredReasoning } from "./restored-reasoning.js";
import { windowRange, messageOffsets } from "./message-window.js";
import type { SourceRef } from "./session-log.js";
import { CopyButton } from "./copy-button.js";
import { responseForMessage, ResponseVersionNav, type ResponseControls } from "./response-versions.js";
import { Markdown } from "./markdown.js";
export { Markdown } from "./markdown.js";

export const MessageRow = memo(function MessageRow({ message: m, runtime, timeline, reasoning, expanded, responses, fail, openLog, located }: {
  message: Message; runtime?: string; timeline: boolean; reasoning: boolean; expanded: Map<string, boolean>; responses?: ResponseControls; fail: (e: unknown) => void; openLog?: (source: SourceRef) => void; located?: boolean;
}) {
  const response = responseForMessage(m, responses);
  const [open, setOpen] = useState(() => expanded.get(m.id) || false);
  const toggle = (e: React.SyntheticEvent<HTMLDetailsElement>) => { expanded.set(m.id, e.currentTarget.open); setOpen(e.currentTarget.open); };
  return <article data-message-id={m.id} data-response-group-id={response.group?.id} data-response-pending={response.pending || undefined} className={`message ${m.role}${m.source_ref && openLog ? " has-source" : ""}${located ? " located-message" : ""}`}>
    {m.role === "reasoning" ? reasoning && <details className="reasoning" open={open} onToggle={toggle}><summary>推理 / 摘要</summary><Markdown text={m.content} runtime={runtime} fail={fail}/></details> : m.role === "tool" ? <details className="command-result" open={open} onToggle={toggle}><summary>操作结果</summary><Markdown text={m.content} runtime={runtime} fail={fail}/></details> : <>
      <div className="message-head"><span>{m.role === "user" ? "你" : m.role === "assistant" ? "Astra" : m.role === "error" ? "请求未完成" : "提示"}</span>{timeline && m.timestamp && <time>{new Date(m.timestamp * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</time>}</div>
      <div className="message-body">{response.pending && !response.content ? <p className="response-pending-label" role="status"><span className="pulse"/>正在重新生成回复…</p> : <Markdown text={response.content} runtime={runtime} fail={fail}/>}</div>
      {m.submissionState === "rejected" ? <p className="small error-text" role="status">未发送：{m.submissionError || "后端拒收；可检查并重新发送草稿。"}</p> : m.submissionState === "unknown" ? <p className="small muted" role="status">接收状态未知，请先确认后端状态，避免重复发送。</p> : m.pending && <span className="muted small">正在等待接收确认…</span>}
      <div className="message-actions"><CopyButton text={response.content} fail={fail}/>{!response.pending && openLog && m.source_ref && <button className="icon" aria-label="查看消息记录" title="查看已保存的会话记录" onClick={() => openLog(m.source_ref!)}><FileText size={14}/></button>}{response.target && responses && <button className="icon" aria-label="重新生成回复" title="重新生成这条回复；保留已有版本" disabled={responses.busy} onClick={() => responses.regenerate(response.target!.source_ref)}><RotateCcw size={14}/></button>}{response.group && responses && <ResponseVersionNav group={response.group} busy={responses.busy} select={responses.select}/>}</div>
    </>}
    {openLog && m.source_ref && (m.role === "reasoning" && reasoning || m.role === "tool") && <div className="message-actions"><button className="icon" aria-label="查看消息记录" title="查看已保存的会话记录" onClick={() => openLog(m.source_ref!)}><FileText size={14}/></button></div>}
  </article>;
});

/** Measured window: all loaded messages remain reachable; only viewport + overscan mount.
 * Heights are kept by message id, so prepending a page preserves the visible anchor. */
export const MessageList = memo(function MessageList({ messages: sourceMessages, runtime, timeline, reasoning, scroller, follow, responses, fail, openLog, locate }: {
  messages: Message[]; runtime?: string; timeline: boolean; reasoning: boolean; scroller: React.RefObject<HTMLDivElement>; follow: boolean; responses?: ResponseControls; fail: (e: unknown) => void; openLog?: (source: SourceRef) => void; locate?: { id: string; serial: number };
}) {
  const messages = useMemo(() => expandRestoredReasoning(sourceMessages, reasoning), [sourceMessages, reasoning]);
  const list = useRef<HTMLDivElement>(null);
  const sizes = useRef(new Map<string, number>());
  const expanded = useRef(new Map<string, boolean>());
  const [sizeRevision, resize] = useState(0);
  const [viewport, setViewport] = useState({ top: Number.MAX_SAFE_INTEGER, height: 800 });
  const offsets = useMemo(() => messageOffsets(messages, sizes.current), [messages, sizeRevision]);
  const range = windowRange(offsets, viewport.top, viewport.height);
  const locatedSerial = useRef<number>();
  const anchor = useRef<{ id: string; offset: number }>();
  const restoredScroll = useRef<number>();
  const layout = useRef({ messages, offsets });
  const rootTop = () => list.current && scroller.current ? list.current.getBoundingClientRect().top - scroller.current.getBoundingClientRect().top + scroller.current.scrollTop : 0;
  useLayoutEffect(() => {
    const el = scroller.current;
    if (!el || !el.clientHeight) return;
    if (follow) { el.scrollTop = el.scrollHeight; restoredScroll.current = el.scrollTop; }
    else if (anchor.current) {
      const i = messages.findIndex(m => m.id === anchor.current!.id);
      if (i >= 0) { el.scrollTop = rootTop() + offsets[i] + anchor.current.offset; restoredScroll.current = el.scrollTop; }
    }
    layout.current = { messages, offsets };
    const update = () => {
      if (!el.clientHeight) return;
      const top = el.scrollTop - rootTop();
      setViewport(old => old.top === top && old.height === el.clientHeight ? old : { top, height: el.clientHeight });
    };
    update();
  }, [offsets, follow]);
  const locateInViewport = useRef<() => void>(() => {});
  locateInViewport.current = () => {
    const el = scroller.current;
    if (!el || !el.clientHeight || !locate || locatedSerial.current === locate.serial) return;
    const index = messages.findIndex(message => message.id === locate.id);
    if (index < 0) return;
    locatedSerial.current = locate.serial;
    anchor.current = { id: locate.id, offset: -16 };
    el.scrollTop = Math.max(0, rootTop() + offsets[index] - 16);
    restoredScroll.current = el.scrollTop;
    setViewport({ top: el.scrollTop - rootTop(), height: el.clientHeight });
  };
  useLayoutEffect(() => { locateInViewport.current(); }, [locate, messages, offsets]);
  useEffect(() => {
    const el = scroller.current; if (!el) return;
    const update = (capture = false) => {
      if (!el.clientHeight) return;
      const top = el.scrollTop - rootTop();
      if (capture && (restoredScroll.current === undefined || Math.abs(el.scrollTop - restoredScroll.current) > 1)) {
        const { messages: rows, offsets: heights } = layout.current;
        const i = windowRange(heights, top, 0, 0).start;
        if (rows[i]) anchor.current = { id: rows[i].id, offset: top - heights[i] };
      }
      if (capture) restoredScroll.current = undefined;
      setViewport(old => old.top === top && old.height === el.clientHeight ? old : { top, height: el.clientHeight });
    };
    const scroll = () => update(true);
    el.addEventListener("scroll", scroll, { passive: true });
    const observer = new ResizeObserver(() => { locateInViewport.current(); update(); }); observer.observe(el);
    return () => { el.removeEventListener("scroll", scroll); observer.disconnect(); };
  }, [scroller]);
  useLayoutEffect(() => {
    if (!list.current) return;
    let frame = 0;
    const observer = new ResizeObserver(entries => {
      let changed = false;
      for (const entry of entries) {
        const id = (entry.target as HTMLElement).dataset.rowId!;
        const height = entry.borderBoxSize[0]?.blockSize || entry.target.getBoundingClientRect().height;
        if (height > 0 && Math.abs((sizes.current.get(id) || 0) - height) > .5) { sizes.current.set(id, height); changed = true; }
      }
      if (changed && !frame) frame = requestAnimationFrame(() => { frame = 0; resize(n => n + 1); });
    });
    for (const row of list.current.querySelectorAll<HTMLElement>("[data-row-id]")) observer.observe(row);
    return () => { observer.disconnect(); cancelAnimationFrame(frame); };
  }, [range.start, range.end, messages]);
  return <div className="message-window" ref={list} data-total-messages={sourceMessages.length}>
    <div aria-hidden="true" style={{ height: offsets[range.start] }}/>
    {messages.slice(range.start, range.end).map(m => <div className="measured-message" data-row-id={m.id} key={m.id}>
      <MessageRow message={m} openLog={openLog} located={m.id === locate?.id} runtime={runtime} timeline={timeline} reasoning={reasoning} expanded={expanded.current} responses={responses} fail={fail}/>
    </div>)}
    <div aria-hidden="true" style={{ height: offsets[messages.length] - offsets[range.end] }}/>
  </div>;
});
