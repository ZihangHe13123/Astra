import React, { memo, useEffect, useState } from "react";
import { renderMermaid, mermaidErrorMessage, type MermaidTheme } from "./mermaid-renderer.js";

const previewLimit = 50_000;
function currentTheme(): MermaidTheme {
  return typeof document !== "undefined" && getComputedStyle(document.documentElement).colorScheme.includes("dark") ? "dark" : "light";
}

/** Source remains available when a streamed fence is incomplete or rendering fails. */
export const MermaidBlock = memo(function MermaidBlock({ source, complete, fail }: {
  source: string; complete: boolean; fail: (error: unknown) => void;
}) {
  const [theme, setTheme] = useState<MermaidTheme>(currentTheme);
  const [view, setView] = useState<"diagram" | "source">("diagram");
  const [rendered, setRendered] = useState<{ source: string; theme: MermaidTheme; url?: string; error?: string }>();
  const [copied, setCopied] = useState(false);
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    const update = () => setTheme(currentTheme());
    const observer = new MutationObserver(update);
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme", "style", "class"] });
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    media.addEventListener("change", update); update();
    return () => { observer.disconnect(); media.removeEventListener("change", update); };
  }, []);
  useEffect(() => {
    setRendered(undefined);
    if (!complete) return;
    let active = true;
    let url: string | undefined;
    // Complete blocks can still change during edits. Collapse rapid revisions
    // before entering Mermaid's serialized renderer; stale results stay private.
    const timer = setTimeout(() => {
      void renderMermaid(source, theme).then(svg => {
        if (!active) return;
        url = URL.createObjectURL(new Blob([svg], { type: "image/svg+xml" }));
        setRendered({ source, theme, url });
      }).catch(error => {
        if (active) setRendered({ source, theme, error: mermaidErrorMessage(error) });
      });
    }, 180);
    return () => { active = false; clearTimeout(timer); if (url) URL.revokeObjectURL(url); };
  }, [source, complete, theme, retry]);
  useEffect(() => { if (!copied) return; const timer = setTimeout(() => setCopied(false), 1600); return () => clearTimeout(timer); }, [copied]);
  const result = rendered?.source === source && rendered.theme === theme ? rendered : undefined;
  const showSource = !complete || view === "source" || !result?.url;
  return <section className="mermaid-block" aria-label="Mermaid 图表与源码">
    <div className="mermaid-toolbar"><span>Mermaid</span>
      {complete && result?.url && <button onClick={() => setView(view === "diagram" ? "source" : "diagram")}>{view === "diagram" ? "查看源码" : "查看图表"}</button>}
      {complete && result?.error && <button onClick={() => setRetry(value => value + 1)}>重新绘制</button>}
      <button aria-label="复制 Mermaid 源码" onClick={() => { void window.astra.copyText(source).then(() => setCopied(true)).catch(fail); }}>{copied ? "已复制" : "复制 Mermaid 源码"}</button>
    </div>
    {!complete ? <p className="small muted" role="status">图表输入中…</p> : result?.error ? <p className="small muted" role="status">{result.error}</p> : !result?.url && <p className="small muted" role="status">正在绘制图表…</p>}
    {showSource ? <pre className="mermaid-source"><code>{source.slice(0, previewLimit)}</code></pre>
      : <img className="mermaid-image" src={result!.url} alt="Mermaid 图表"/>}
    {showSource && source.length > previewLimit && <p className="small muted">源码较长，仅展示前 {previewLimit.toLocaleString()} 个字符；复制可获取完整内容。</p>}
  </section>;
});
