import React, { memo, useEffect, useLayoutEffect, useRef, useState } from "react";
import { cardSourceKey, cardTheme } from "./card-state.js";
import { PAGE_FRAME_URL, pageMessage } from "./page-state.js";

// Before paint in the window, so the frame's first report cannot be missed; a plain effect where nothing is painted.
const useBeforePaint = typeof window === "undefined" ? useEffect : useLayoutEffect;
const loadTimeout = 5000;
// Writing the page into its frame is itself a load, so a load alone does not mean the page left.
// After each one the frame is asked whether it is still the page; a busy page answers late, a
// document that replaced it never does.
const answerTimeout = 3000;
function currentTheme() { return cardTheme(getComputedStyle(document.documentElement)); }

/**
 * A whole page the model wrote, running in the isolated page frame. The host hands the frame the
 * source and the app's colours, and takes back only an error text. A new source is a new frame:
 * nothing of the page before it is left running.
 */
export const PageFrame = memo(function PageFrame({ source }: { source: string }) {
  const frame = useRef<HTMLIFrameElement>(null);
  const [problem, setProblem] = useState("");
  const [stopped, setStopped] = useState(false);
  const asked = useRef<{ id: number; timer: ReturnType<typeof setTimeout> }>();
  const asks = useRef(0);
  // The frame paints its canvas before it is told the theme; the address tells it which one to start with.
  const [address] = useState(() => `${PAGE_FRAME_URL}${typeof document !== "undefined" && currentTheme().dark ? "?dark" : ""}`);
  useBeforePaint(() => {
    if (stopped) return;
    setProblem("");
    let loaded = false;
    const target = () => frame.current?.contentWindow;
    const listener = (event: MessageEvent) => {
      if (!target() || event.source !== target()) return;
      const message = pageMessage(event.data);
      if (!message) return;
      loaded = true;
      if (message.type === "ready") target()!.postMessage({ astraPage: true, type: "render", html: source, theme: currentTheme() }, "*");
      else if (message.type === "pong") { if (asked.current?.id === message.id) { clearTimeout(asked.current.timer); asked.current = undefined; } }
      else setProblem(`页面脚本出错：${message.message}`);
    };
    const repaint = () => target()?.postMessage({ astraPage: true, type: "theme", theme: currentTheme() }, "*");
    const observer = new MutationObserver(repaint);
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme", "style", "class"] });
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    media.addEventListener("change", repaint);
    window.addEventListener("message", listener);
    const timer = setTimeout(() => { if (!loaded) setProblem("页面没有载入，可以查看源码。"); }, loadTimeout);
    return () => { clearTimeout(timer); clearTimeout(asked.current?.timer); asked.current = undefined; window.removeEventListener("message", listener); media.removeEventListener("change", repaint); observer.disconnect(); };
  }, [source, stopped]);
  useEffect(() => { setStopped(false); }, [source]);
  return <div className="page-preview">
    {stopped ? <p className="small muted" role="status">页面已停止：它试图离开自己的框。<button className="text-button small" onClick={() => setStopped(false)}>重新运行</button></p>
      : problem && <p className="small muted" role="status">{problem}</p>}
    {!stopped && <iframe key={cardSourceKey(source)} ref={frame} className="page-frame" title="页面预览" sandbox="allow-scripts" referrerPolicy="no-referrer" src={address}
      onLoad={() => {
        clearTimeout(asked.current?.timer);
        const id = ++asks.current;
        asked.current = { id, timer: setTimeout(() => { if (asked.current?.id === id) setStopped(true); }, answerTimeout) };
        frame.current?.contentWindow?.postMessage({ astraPage: true, type: "ping", id }, "*");
      }}/>}
  </div>;
});
