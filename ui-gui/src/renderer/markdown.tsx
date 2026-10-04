import React, { memo, useEffect, useMemo, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import type { Element } from "hast";
import { resolveAgainst } from "./doc-preview.js";
import { CopyButton } from "./copy-button.js";
import { codeText, rehypeCodeBudget, rehypeCodeHighlight, remarkCodeMetadata } from "./code-markdown.js";
import { normalizeMathMarkdown, remarkMathOptions, rehypeSafeKatex } from "./math-markdown.js";
import { MermaidBlock } from "./mermaid-block.js";

function InlineImage({ src, alt, runtime, base }: { src?: string; alt?: string; runtime?: string; base?: string }) {
  const [url, setURL] = useState<string>();
  useEffect(() => {
    let live = true; setURL(undefined);
    if (src && /^https:\/\//i.test(src)) setURL(src);
    else if (src && runtime) void window.astra.file(runtime, resolveAgainst(base, src), "preview").then(value => { if (live) setURL(value.data); }).catch(() => {});
    return () => { live = false; };
  }, [src, runtime, base]);
  return url ? <img src={url} alt={alt || "图片"} loading="lazy"/> : <span className="muted">{alt || "图片预览不可用"}</span>;
}

function CodeBlock({ children, node, fail }: { children?: React.ReactNode; node?: Element; fail: (e: unknown) => void }) {
  const code = node?.children.find((child): child is Element => child.type === "element" && child.tagName === "code");
  const classes = Array.isArray(code?.properties.className) ? code.properties.className : [];
  const language = String(classes.find(name => String(name).startsWith("language-")) || "").slice(9);
  const source = code ? codeText(code) : "";
  if (language.toLowerCase() === "mermaid") return <MermaidBlock source={source} complete={code?.properties["data-fence-complete"] === "true"} fail={fail}/>;
  return <div className="code-block"><div className="code-toolbar"><span className="code-language">{language || "代码"}</span><CopyButton label="复制代码" text={source} fail={fail}/></div><pre>{children}</pre></div>;
}

/** One renderer for chat, delegated results, and saved Markdown previews. */
export const Markdown = memo(function Markdown({ text, runtime, fail, base }: { text: string; runtime?: string; fail: (e: unknown) => void; base?: string }) {
  const rendered = useMemo(() => normalizeMathMarkdown(text), [text]);
  const components = useMemo(() => ({
    img: ({ src, alt }: { src?: string; alt?: string }) => <InlineImage src={src} alt={alt} runtime={runtime} base={base}/>,
    a: ({ href, children }: React.ComponentProps<"a">) => <a href={href} onClick={event => { event.preventDefault(); if (!href || href.startsWith("#")) return;
      if (/^https?:\/\//i.test(href)) void window.astra.openExternal(href).catch(fail);
      else if (runtime) void window.astra.file(runtime, resolveAgainst(base, href), "open").catch(fail);
    }}>{children}</a>,
    pre: ({ children, node }: React.ComponentProps<"pre"> & { node?: Element }) => <CodeBlock node={node} fail={fail}>{children}</CodeBlock>,
  }), [runtime, fail, base]);
  return <ReactMarkdown remarkPlugins={[remarkGfm, [remarkMath, remarkMathOptions], remarkCodeMetadata]} rehypePlugins={[rehypeSafeKatex, rehypeCodeBudget, rehypeCodeHighlight]} skipHtml
    urlTransform={url => /^(?:https?:\/\/|\/|[A-Za-z]:[\\/]|\.\.?\/)/.test(url) || !/^[a-z][a-z\d+.-]*:/i.test(url) ? url : ""} components={components}>{rendered}</ReactMarkdown>;
});
