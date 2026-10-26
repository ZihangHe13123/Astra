import React, { memo, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useTheme } from "./theme-context.js";
import { RotateCcw, FileText, AlarmClock, ChevronRight, Sparkles, Wrench } from "lucide-react";
import type { Message, TurnChangeNote, UIEvent } from "@astra/ui-core/session-state";
import { withActivity, type Row } from "./activity-state.js";
import { Activity, TurnChanges } from "./activity.js";
import { expandRestoredReasoning } from "./restored-reasoning.js";
import { windowRange, messageOffsets } from "./message-window.js";
import type { SourceRef } from "./session-log.js";
import { CopyButton } from "./copy-button.js";
import { changeSource, type ChangeSource } from "./change-navigation.js";
import { responseForMessage, ResponseVersionNav, type ResponseControls } from "./response-versions.js";
import { Markdown } from "./markdown.js";
import { activitySummary, chatRows, jobResult } from "./chat-view.js";

/** Open a side panel from the conversation; for the tools panel, at the call with this result number. */
export type OpenPanel = (panel: "tools" | "changes", resultIndex?: number, source?: ChangeSource) => void;
export { Markdown } from "./markdown.js";

/** In the chat, a step's tool calls are one line; it opens to the usual list. */
function ChatActivity({ tools, open, toggle, openDetails }: { tools: UIEvent[]; open: boolean; toggle: () => void; openDetails?: (tool: UIEvent) => void }) {
  return <div className="chat-activity">
    <button type="button" className="chat-activity-line" aria-expanded={open} onClick={toggle}><Wrench size={13}/>{activitySummary(tools)}<ChevronRight size={13} className={open ? "turned" : ""}/></button>
    {open && <Activity tools={tools} openDetails={openDetails}/>}
  </div>;
}

/** A scheduled job's result in the chat: a card naming the job, its text shortened until opened. */
function JobCard({ title, body, open, toggle, runtime, fail }: { title: string; body: string; open: boolean; toggle: () => void; runtime?: string; fail: (e: unknown) => void }) {
  const long = body.length > 280 || body.split("\n").length > 8;
  return <div className="job-card-body">
    <div className="job-card-title"><AlarmClock size={13}/>{title}</div>
    <div className={`message-body${long && !open ? " clamped" : ""}`}><Markdown text={body} runtime={runtime} fail={fail}/></div>
    {long && <button type="button" className="text-button small" onClick={toggle}>{open ? "收起" : "展开全文"}</button>}
  </div>;
}

export const MessageRow = memo(function MessageRow({ message: m, runtime, timeline, reasoning, expanded, responses, fail, openLog, located, openPanel, chat }: {
  message: Row; openPanel?: OpenPanel; runtime?: string; timeline: boolean; reasoning: boolean; expanded: Map<string, boolean>; responses?: ResponseControls; fail: (e: unknown) => void; openLog?: (source: SourceRef) => void; located?: boolean; chat?: boolean;
}) {
  const response = responseForMessage(m, responses);
  const [open, setOpen] = useState(() => expanded.get(m.id) || false);
  const toggle = (e: React.SyntheticEvent<HTMLDetailsElement>) => { expanded.set(m.id, e.currentTarget.open); setOpen(e.currentTarget.open); };
  const flip = () => { expanded.set(m.id, !open); setOpen(!open); };
  const { roles } = useTheme().wording;
  const openTool = openPanel && ((tool: UIEvent) => openPanel("tools", Number(tool.result_index) || undefined));
  if (m.role === "time") return <div className="chat-time" role="separator">{m.content}</div>;
  if (m.role === "activity") return chat ? <ChatActivity tools={m.tools || []} open={open} toggle={flip} openDetails={openTool}/> : <Activity tools={m.tools || []} openDetails={openTool}/>;
  const job = chat && m.role === "assistant" ? jobResult(response.content) : undefined;
  if (job) return <article data-message-id={m.id} className="message assistant job-card">
    <span className="chat-avatar" aria-hidden="true"><Sparkles size={14}/></span>
    <JobCard title={job.title} body={job.body} open={open} toggle={flip} runtime={runtime} fail={fail}/>
  </article>;
  if (m.role === "changes") return m.changes ? <TurnChanges note={m.changes} open={openPanel && (() => openPanel("changes", undefined, changeSource(m.changes!)))}/> : null;
  return <article data-message-id={m.id} data-response-group-id={response.group?.id} data-response-pending={response.pending || undefined} className={`message ${m.role}${m.source_ref && openLog ? " has-source" : ""}${located ? " located-message" : ""}`}>
    {chat && m.role === "assistant" && <span className="chat-avatar" aria-hidden="true"><Sparkles size={14}/></span>}
    {m.role === "reasoning" ? reasoning && <details className="reasoning" open={open} onToggle={toggle}><summary>{roles.reasoning}</summary><Markdown text={m.content} runtime={runtime} fail={fail}/></details> : m.role === "tool" ? <details className="command-result" open={open} onToggle={toggle}><summary>操作结果</summary><Markdown text={m.content} runtime={runtime} fail={fail}/></details> : <>
      <div className="message-head"><span>{roles[m.role === "user" || m.role === "assistant" || m.role === "error" ? m.role : "system"]}</span>{timeline && m.timestamp && <time>{new Date(m.timestamp * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</time>}</div>
      <div className="message-body">{response.pending && !response.content ? <p className="response-pending-label" role="status"><span className="pulse"/>正在重新生成回复…</p> : <Markdown text={response.content} runtime={runtime} fail={fail} maskSecrets={m.role === "user"}/>}</div>
      {m.submissionState === "rejected" ? <p className="small error-text" role="status">未发送：{m.submissionError || "后端拒收；可检查并重新发送草稿。"}</p> : m.submissionState === "unknown" ? <p className="small muted" role="status">接收状态未知，请先确认后端状态，避免重复发送。</p> : m.pending && <span className="muted small">正在等待接收确认…</span>}
      <div className="message-actions"><CopyButton text={response.content} fail={fail}/>{!response.pending && openLog && m.source_ref && <button className="icon" aria-label="查看消息记录" title="查看已保存的会话记录" onClick={() => openLog(m.source_ref!)}><FileText size={14}/></button>}{response.target && responses && <button className="icon" aria-label="重新生成回复" title="重新生成这条回复；保留已有版本" disabled={responses.busy} onClick={() => responses.regenerate(response.target!.source_ref)}><RotateCcw size={14}/></button>}{response.group && responses && <ResponseVersionNav group={response.group} busy={responses.busy} select={responses.select}/>}</div>
    </>}
    {openLog && m.source_ref && (m.role === "reasoning" && reasoning || m.role === "tool") && <div className="message-actions"><button className="icon" aria-label="查看消息记录" title="查看已保存的会话记录" onClick={() => openLog(m.source_ref!)}><FileText size={14}/></button></div>}
  </article>;
});

/** Measured window: all loaded messages remain reachable; only viewport + overscan mount.
 * Heights are kept by message id, so prepending a page preserves the visible anchor. */
export const MessageList = memo(function MessageList({ messages: sourceMessages, runtime, timeline, reasoning, scroller, follow, responses, fail, openLog, locate, tools, changes, openPanel, chat }: {
  messages: Message[]; tools?: UIEvent[]; changes?: TurnChangeNote[]; openPanel?: OpenPanel; runtime?: string; timeline: boolean; reasoning: boolean; scroller: React.RefObject<HTMLDivElement>; follow: boolean; responses?: ResponseControls; fail: (e: unknown) => void; openLog?: (source: SourceRef) => void; locate?: { id: string; serial: number };
  /** The personal thread: bubbles, time headings, steps folded to a line, job results as cards. */
  chat?: boolean;
}) {
  const messages = useMemo(() => {
    const rows = withActivity(expandRestoredReasoning(sourceMessages, reasoning), tools, changes).rows;
    return chat ? chatRows(rows) : rows;
  }, [sourceMessages, reasoning, tools, changes, chat]);
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
  // Where the list was last seen: set by every scroll this list makes and every scroll event, never cleared.
  const lastTop = useRef<number>();
  const layout = useRef({ messages, offsets });
  const rootTop = () => list.current && scroller.current ? list.current.getBoundingClientRect().top - scroller.current.getBoundingClientRect().top + scroller.current.scrollTop : 0;
  useLayoutEffect(() => {
    const el = scroller.current;
    if (!el || !el.clientHeight) return;
    // A measurement can land between a scroll and its event. Until the event, the list stays where that scroll put it:
    // while following it pins only from where it was last seen or from near the bottom, and an anchor taken before the
    // scroll is not put back over it. The event then turns following off or takes a new anchor.
    const moved = lastTop.current !== undefined && Math.abs(el.scrollTop - lastTop.current) > 1;
    if (follow) {
      if (!moved || el.scrollHeight - el.scrollTop - el.clientHeight < 120) { el.scrollTop = el.scrollHeight; restoredScroll.current = lastTop.current = el.scrollTop; }
    } else if (anchor.current && !moved) {
      const i = messages.findIndex(m => m.id === anchor.current!.id);
      if (i >= 0) { el.scrollTop = rootTop() + offsets[i] + anchor.current.offset; restoredScroll.current = lastTop.current = el.scrollTop; }
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
    restoredScroll.current = lastTop.current = el.scrollTop;
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
    const scroll = () => { lastTop.current = el.scrollTop; update(true); };
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
      <MessageRow message={m} openPanel={openPanel} openLog={openLog} located={m.id === locate?.id} runtime={runtime} timeline={timeline && !chat} reasoning={reasoning} expanded={expanded.current} responses={responses} fail={fail} chat={chat}/>
    </div>)}
    <div aria-hidden="true" style={{ height: offsets[messages.length] - offsets[range.end] }}/>
  </div>;
});
