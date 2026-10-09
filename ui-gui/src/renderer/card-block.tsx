import React, { memo, useEffect, useLayoutEffect, useRef, useState } from "react";
import { CARD_FRAME_URL, MAX_CARD_SOURCE, MIN_CARD_HEIGHT, cardMessage, cardSourceKey, cardTheme, rememberCardHeight, rememberedCardHeight } from "./card-state.js";

const previewLimit = 50_000;
// Before paint in the window, so the frame's first report cannot be missed; a plain effect where nothing is painted.
const useBeforePaint = typeof window === "undefined" ? useEffect : useLayoutEffect;
const loadTimeout = 5000;
function currentTheme() { return cardTheme(getComputedStyle(document.documentElement)); }

/**
 * A card the model wrote, running in the isolated card frame. The host hands the frame the source
 * and the theme, and takes back only a height and an error text. The source is one click away,
 * and is what shows while the block is still being written or when it cannot run.
 */
export const CardBlock = memo(function CardBlock({ source, complete, fail }: {
  source: string; complete: boolean; fail: (error: unknown) => void;
}) {
  const frame = useRef<HTMLIFrameElement>(null);
  const [view, setView] = useState<"card" | "source">("card");
  const [height, setHeight] = useState(() => rememberedCardHeight(source));
  const [problem, setProblem] = useState("");
  const [stopped, setStopped] = useState(false);
  const loads = useRef(0);
  const [copied, setCopied] = useState(false);
  // The frame paints its canvas before it is told the theme; the address tells it which one to start with.
  const [address] = useState(() => `${CARD_FRAME_URL}${typeof document !== "undefined" && currentTheme().dark ? "?dark" : ""}`);
  const runnable = complete && source.trim().length > 0 && source.length <= MAX_CARD_SOURCE;
  const showCard = runnable && view === "card" && !stopped;
  useBeforePaint(() => {
    if (!showCard) return;
    setProblem(""); setHeight(rememberedCardHeight(source)); loads.current = 0;
    let loaded = false;
    const target = () => frame.current?.contentWindow;
    const listener = (event: MessageEvent) => {
      if (!target() || event.source !== target()) return;
      const message = cardMessage(event.data);
      if (!message) return;
      loaded = true;
      if (message.type === "ready") target()!.postMessage({ astraCard: true, type: "render", html: source, theme: currentTheme() }, "*");
      else if (message.type === "height") { setHeight(message.height); rememberCardHeight(source, message.height); }
      else setProblem(`卡片脚本出错：${message.message}`);
    };
    const repaint = () => target()?.postMessage({ astraCard: true, type: "theme", theme: currentTheme() }, "*");
    const observer = new MutationObserver(repaint);
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme", "style", "class"] });
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    media.addEventListener("change", repaint);
    window.addEventListener("message", listener);
    const timer = setTimeout(() => { if (!loaded) setProblem("卡片没有载入，可以查看源码。"); }, loadTimeout);
    return () => { clearTimeout(timer); window.removeEventListener("message", listener); media.removeEventListener("change", repaint); observer.disconnect(); };
  }, [showCard, source]);
  useEffect(() => { setStopped(false); }, [source]);
  useEffect(() => { if (!copied) return; const timer = setTimeout(() => setCopied(false), 1600); return () => clearTimeout(timer); }, [copied]);
  return <section className="card-block" aria-label="可交互卡片与源码">
    <div className="card-toolbar"><span>卡片</span>
      {runnable && <button onClick={() => { if (stopped) setStopped(false); else setView(view === "card" ? "source" : "card"); }}>{stopped ? "重新运行" : view === "card" ? "查看源码" : "查看卡片"}</button>}
      <button aria-label="复制卡片源码" onClick={() => { void window.astra.copyText(source).then(() => setCopied(true)).catch(fail); }}>{copied ? "已复制" : "复制源码"}</button>
    </div>
    {!complete ? <p className="small muted" role="status">卡片输入中…</p>
      : !runnable ? <p className="small muted" role="status">{source.trim() ? "卡片内容过长，只显示源码。" : "卡片内容为空。"}</p>
      : stopped ? <p className="small muted" role="status">卡片已停止：它试图离开自己的框。</p>
      : showCard && problem && <p className="small muted" role="status">{problem}</p>}
    {showCard
      ? <iframe key={cardSourceKey(source)} ref={frame} className="card-frame" title="可交互卡片" sandbox="allow-scripts" referrerPolicy="no-referrer" src={address} style={{ height: height ?? MIN_CARD_HEIGHT }}
          // The card document loads once. A second load means the card navigated its frame, which only ever yields a refused page.
          onLoad={() => { if (loads.current++ > 0) setStopped(true); }}/>
      : <pre className="card-source"><code>{source.slice(0, previewLimit)}</code></pre>}
    {!showCard && source.length > previewLimit && <p className="small muted">源码较长，仅展示前 {previewLimit.toLocaleString()} 个字符；复制可获取完整内容。</p>}
  </section>;
});
