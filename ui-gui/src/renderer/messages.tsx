import React, { memo, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Check, Copy, RotateCcw } from "lucide-react";
import type { Message } from "@astra/ui-core/session-state";
import { windowRange, messageOffsets } from "./message-window.js";

function InlineImage({ src, alt, runtime }: { src?: string; alt?: string; runtime?: string }) {
  const [url, setURL] = useState<string>();
  useEffect(() => {
    let live = true; setURL(undefined);
    if (src && /^https:\/\//i.test(src)) setURL(src);
    else if (src && runtime) void window.astra.file(runtime, src, "preview").then(value => { if (live) setURL(value.data); }).catch(() => {});
    return () => { live = false; };
  }, [src, runtime]);
  return url ? <img src={url} alt={alt || "图片"} loading="lazy"/> : <span className="muted">{alt || "图片预览不可用"}</span>;
}
function CopyButton({ text, label = "复制消息", fail }: { text: string | (() => string); label?: string; fail: (e: unknown) => void }) {
  const [copied, setCopied] = useState(false);
  useEffect(() => { if (!copied) return; const t = setTimeout(() => setCopied(false), 1600); return () => clearTimeout(t); }, [copied]);
  return <button className="icon" aria-label={copied ? "已复制" : label} title={copied ? "已复制" : label} onClick={() => {
    void navigator.clipboard.writeText(typeof text === "function" ? text() : text).then(() => setCopied(true)).catch(fail);
  }}>{copied ? <Check size={14}/> : <Copy size={14}/>}</button>;
}
function CodeBlock({ children, fail }: { children?: React.ReactNode; fail: (e: unknown) => void }) {
  const code = useRef<HTMLPreElement>(null);
  return <div className="code-block"><div className="copy-code"><CopyButton label="复制代码" text={() => code.current?.textContent || ""} fail={fail}/></div><pre ref={code}>{children}</pre></div>;
}
const Markdown = memo(function Markdown({ text, runtime, fail }: { text: string; runtime?: string; fail: (e: unknown) => void }) {
  const components = useMemo(() => ({
    img: ({ src, alt }: { src?: string; alt?: string }) => <InlineImage src={src} alt={alt} runtime={runtime}/>,
    a: ({ href, children }: React.ComponentProps<"a">) => <a href={href} onClick={e => { e.preventDefault(); if (!href) return;
      if (/^https?:\/\//i.test(href)) void window.astra.openExternal(href).catch(fail);
      else if (runtime) void window.astra.file(runtime, href, "open").catch(fail);
    }}>{children}</a>,
    pre: ({ children }: React.ComponentProps<"pre">) => <CodeBlock fail={fail}>{children}</CodeBlock>,
  }), [runtime, fail]);
  return <ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml urlTransform={url => /^(?:https?:\/\/|\/|[A-Za-z]:[\\/]|\.\.?\/)/.test(url) || !/^[a-z][a-z\d+.-]*:/i.test(url) ? url : ""} components={components}>{text}</ReactMarkdown>;
});
const MessageRow = memo(function MessageRow({ message: m, runtime, timeline, reasoning, expanded, retry, fail }: {
  message: Message; runtime?: string; timeline: boolean; reasoning: boolean; expanded: Map<string, boolean>; retry?: () => void; fail: (e: unknown) => void;
}) {
  const [open, setOpen] = useState(() => expanded.get(m.id) || false);
  const toggle = (e: React.SyntheticEvent<HTMLDetailsElement>) => { expanded.set(m.id, e.currentTarget.open); setOpen(e.currentTarget.open); };
  return <article data-message-id={m.id} className={`message ${m.role}`}>
    {m.role === "reasoning" ? reasoning && <details className="reasoning" open={open} onToggle={toggle}><summary>推理 / 摘要</summary><Markdown text={m.content} runtime={runtime} fail={fail}/></details> : m.role === "tool" ? <details className="command-result" open={open} onToggle={toggle}><summary>操作结果</summary><Markdown text={m.content} runtime={runtime} fail={fail}/></details> : <>
      <div className="message-head"><span>{m.role === "user" ? "你" : m.role === "assistant" ? "Astra" : m.role === "error" ? "请求未完成" : "提示"}</span>{timeline && m.timestamp && <time>{new Date(m.timestamp * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</time>}</div>
      <div className="message-body"><Markdown text={m.content} runtime={runtime} fail={fail}/></div>
      {m.submissionState === "rejected" ? <p className="small error-text" role="status">未发送：{m.submissionError || "后端拒收；可检查并重新发送草稿。"}</p> : m.submissionState === "unknown" ? <p className="small muted" role="status">接收状态未知，请先确认后端状态，避免重复发送。</p> : m.pending && <span className="muted small">正在等待接收确认…</span>}
      <div className="message-actions"><CopyButton text={m.content} fail={fail}/>{retry && <button className="icon" aria-label="重试回复" onClick={retry}><RotateCcw size={14}/></button>}</div>
    </>}
  </article>;
});

/** Measured window: all loaded messages remain reachable; only viewport + overscan mount.
 * Heights are kept by message id, so prepending a page preserves the visible anchor. */
export const MessageList = memo(function MessageList({ messages, runtime, timeline, reasoning, scroller, follow, retry, fail }: {
  messages: Message[]; runtime?: string; timeline: boolean; reasoning: boolean; scroller: React.RefObject<HTMLDivElement>; follow: boolean; retry?: () => void; fail: (e: unknown) => void;
}) {
  const list = useRef<HTMLDivElement>(null);
  const sizes = useRef(new Map<string, number>());
  const expanded = useRef(new Map<string, boolean>());
  const [sizeRevision, resize] = useState(0);
  const [viewport, setViewport] = useState({ top: Number.MAX_SAFE_INTEGER, height: 800 });
  const offsets = useMemo(() => messageOffsets(messages, sizes.current), [messages, sizeRevision]);
  const range = windowRange(offsets, viewport.top, viewport.height);
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
    const observer = new ResizeObserver(() => update()); observer.observe(el);
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
  return <div className="message-window" ref={list} data-total-messages={messages.length}>
    <div aria-hidden="true" style={{ height: offsets[range.start] }}/>
    {messages.slice(range.start, range.end).map(m => <div className="measured-message" data-row-id={m.id} key={m.id}>
      <MessageRow message={m} runtime={runtime} timeline={timeline} reasoning={reasoning} expanded={expanded.current} retry={runtime && m.role === "assistant" && m === messages.at(-1) ? retry : undefined} fail={fail}/>
    </div>)}
    <div aria-hidden="true" style={{ height: offsets[messages.length] - offsets[range.end] }}/>
  </div>;
});
