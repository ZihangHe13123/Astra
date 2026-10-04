import React, { useEffect, useState } from "react";
import { Check, Copy } from "lucide-react";

export function CopyButton({ text, label = "复制消息", fail }: { text: string | (() => string); label?: string; fail: (e: unknown) => void }) {
  const [copied, setCopied] = useState(false);
  useEffect(() => { if (!copied) return; const timer = setTimeout(() => setCopied(false), 1600); return () => clearTimeout(timer); }, [copied]);
  return <button className="icon" aria-label={copied ? "已复制" : label} title={copied ? "已复制" : label} onClick={() => {
    void window.astra.copyText(typeof text === "function" ? text() : text).then(() => setCopied(true)).catch(fail);
  }}>{copied ? <Check size={14}/> : <Copy size={14}/>}</button>;
}
