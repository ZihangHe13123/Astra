import React, { useEffect, useState } from "react";
import { Check, ChevronRight, Minus, X } from "lucide-react";
import type { TurnChangeNote, UIEvent } from "@astra/ui-core/session-state";
import { describeTool, span } from "./activity-state.js";
import { toolEmptyOutput, toolStatus } from "./tool-status.js";
import { useTheme } from "./theme-context.js";

/** Seconds since the epoch, kept current only while something is running. */
function useNow(running: boolean): number {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    if (!running) return;
    setNow(Date.now() / 1000);
    const timer = setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => clearInterval(timer);
  }, [running]);
  return now;
}

/** The tool calls made at one point of a turn, one line each; a line opens to what the tool returned. */
export function Activity({ tools, openDetails }: { tools: UIEvent[]; openDetails?: () => void }) {
  const { wording } = useTheme();
  const [open, setOpen] = useState<string>();
  const running = tools.filter(tool => toolStatus(tool) === "running");
  const now = useNow(running.length > 0);
  const started = Math.min(...running.map(tool => Number(tool.started_at) || now));
  const spent = tools.reduce((total, tool) => total + (Number(tool.duration_ms) || 0), 0) / 1000;
  return <section className={`activity${running.length ? " live" : ""}`} aria-label={running.length ? "正在执行的工具" : "工具结果"}>
    <header><span className="activity-title">{running.length > 0 && <i className="activity-dot" aria-hidden="true"/>}{running.length ? wording.liveTools : wording.archive}<b>{tools.length}</b></span>
      <span className="activity-time">{running.length ? span(Math.max(0, now - started)) : spent ? span(spent) : ""}</span></header>
    {tools.map((tool, index) => {
      const key = String(tool.call_id || tool.result_index || index), step = describeTool(tool), expanded = open === key;
      const time = step.tone === "live" ? span(Math.max(0, now - (Number(tool.started_at) || now))) : typeof tool.duration_ms === "number" ? span(tool.duration_ms / 1000) : "";
      return <div className={`step ${step.tone}`} key={key}>
        <button type="button" className="step-row" aria-expanded={expanded} onClick={() => setOpen(expanded ? undefined : key)}>
          <span className="step-state" aria-hidden="true">{step.tone === "ok" ? <Check size={13}/> : step.tone === "bad" ? <X size={13}/> : step.tone === "live" ? <i/> : <Minus size={13}/>}</span>
          <span className="step-verb">{step.verb}</span><span className="step-target" title={step.target}>{step.target}</span>
          <span className="step-result">{step.result}</span><span className="step-time">{time}</span>
        </button>
        {step.tone === "live" && <i className="step-sweep" aria-hidden="true"/>}
        {expanded && <div className="step-detail">
          {tool.error && <p className="error-text">{tool.error}</p>}
          {(tool.output || toolEmptyOutput(tool)) && <pre>{tool.output || toolEmptyOutput(tool)}</pre>}
          {tool.output_truncated && <p className="muted small">显示内容已截断；完整输出见执行详情。</p>}
          {openDetails && <button type="button" className="text-button small" onClick={openDetails}>在执行详情中查看<ChevronRight size={13}/></button>}
        </div>}
      </div>;
    })}
  </section>;
}

/** The files one turn changed, where the turn ended. */
export function TurnChanges({ note, open }: { note: TurnChangeNote; open?: () => void }) {
  const { wording } = useTheme();
  const files = Array.isArray(note.files) ? note.files : [];
  if (!files.length && !note.unknown_count) return null;
  const count = (value: unknown) => typeof value === "number" && Number.isFinite(value) ? value : 0;
  const largest = Math.max(1, ...files.map(file => count(file.added) + count(file.removed)));
  return <section className="turn-changes" aria-label="本回合改动的文件">
    <header><span className="turn-changes-title">{wording.changes}<b>{files.length}</b></span>
      {files.length > 0 && <span className="turn-changes-total"><ins>+{count(note.totals?.added)}</ins> <del>−{count(note.totals?.removed)}</del></span>}</header>
    {files.map(file => <div className="turn-change" key={file.path}>
      <span className="turn-change-path" title={file.path}>{file.path}</span>
      <span className="turn-change-bar" aria-hidden="true"><i style={{ width: `${count(file.added) / largest * 100}%` }}/><b style={{ width: `${count(file.removed) / largest * 100}%` }}/></span>
      <span className="turn-change-count">{file.added === null && file.removed === null ? file.state : <><ins>+{count(file.added)}</ins> <del>−{count(file.removed)}</del></>}</span>
    </div>)}
    {note.unknown_count > 0 && <p className="muted small">另有 {note.unknown_count} 个路径未能确认。</p>}
    {open && <footer><button type="button" className="primary" onClick={open}>查看改动</button></footer>}
  </section>;
}
