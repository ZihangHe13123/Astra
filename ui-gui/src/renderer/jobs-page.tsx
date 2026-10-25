import React from "react";
import { jobState, resultLabel, resultText, targetsLabel, timeLabel, type JobsOverview } from "./jobs-view.js";

/**
 * Scheduled jobs and their results. Read from the `jobs` query; the person's own changes go through
 * `astra jobs` (window.astra.jobs), so they need no approval. Creating a job is the model's, in a conversation.
 */
export function JobsPanel({ fail }: { fail: (error: unknown) => void }) {
  const [view, setView] = React.useState<JobsOverview>();
  const [open, setOpen] = React.useState<string>();
  const [busy, setBusy] = React.useState(false);
  const [quiet, setQuiet] = React.useState({ start: "23:00", end: "08:00" });
  const load = React.useCallback(async () => {
    const next = await window.astra.query("jobs") as JobsOverview;
    setView(next);
    if (next.quiet_hours) setQuiet(next.quiet_hours);
  }, []);
  React.useEffect(() => {
    void load().catch(fail);
    const timer = setInterval(() => { void load().catch(() => undefined); }, 5000);
    return () => clearInterval(timer);
  }, [load, fail]);
  const act = async (action: Parameters<typeof window.astra.jobs>[0], value?: string, extra?: string) => {
    setBusy(true);
    try { await window.astra.jobs(action, value, extra); await load(); } catch (error) { fail(error); } finally { setBusy(false); }
  };
  if (!view) return <p className="muted small">正在读取定时任务…</p>;
  return <div className="jobs-page">
    <p className="muted small">在对话里说「每天早上 8 点提醒我……」「工作日看看……有新的才告诉我」，Astra 会请你批准后建好。桌面端开着（包括在托盘里）时它们按时运行，结果发到你选的地方，这里都留一份。</p>
    <h3>任务</h3>
    {!view.jobs.length && <p className="muted small">还没有定时任务。</p>}
    <ul className="job-list">{view.jobs.map(job => {
      const state = jobState(job);
      return <li key={job.id} className="job-row" data-job={job.id}>
        <div className="job-head"><strong>{job.name}</strong><span className={`job-state ${state.tone}`}>{state.label}</span></div>
        <div className="muted small">{job.schedule}{job.next_at ? ` · 下次 ${timeLabel(job.next_at)}` : ""} · {job.kind === "reminder" ? "提醒" : `只读检查${job.web || job.mail ? `（${[job.web && "可联网", job.mail && "可读邮件"].filter(Boolean).join("、")}）` : ""}`}{job.notify === "on_change" ? " · 有变化才提醒" : ""} · {targetsLabel(job.targets)}</div>
        <div className="job-actions">
          {job.state === "enabled" && <button disabled={busy} onClick={() => { void act("pause", job.id); }}>暂停</button>}
          {job.state === "paused" && <button disabled={busy} onClick={() => { void act("resume", job.id); }}>恢复</button>}
          <button disabled={busy} onClick={() => { void act("run", job.id); }}>立即运行一次</button>
          <button disabled={busy} onClick={() => { if (window.confirm(`删除「${job.name}」？历史结果会保留。`)) void act("remove", job.id); }}>删除</button>
        </div>
      </li>;
    })}</ul>
    <h3>安静时段</h3>
    <div className="quiet-hours small">
      {view.quiet_hours ? <span>{view.quiet_hours.start}–{view.quiet_hours.end} 之间的检查结果先攒着，到点再发；定了时间的提醒照常发。</span> : <span>已关闭：结果随时推送。</span>}
      <label>从 <input aria-label="安静时段开始" type="time" value={quiet.start} onChange={event => setQuiet({ ...quiet, start: event.target.value })}/></label>
      <label>到 <input aria-label="安静时段结束" type="time" value={quiet.end} onChange={event => setQuiet({ ...quiet, end: event.target.value })}/></label>
      <button disabled={busy} onClick={() => { void act("quiet", quiet.start, quiet.end); }}>保存</button>
      {view.quiet_hours && <button disabled={busy} onClick={() => { void act("quiet", "off"); }}>关闭</button>}
    </div>
    <h3>收件箱</h3>
    {!view.inbox.length && <p className="muted small">还没有结果。</p>}
    <ul className="inbox-list">{view.inbox.map(row => {
      const expanded = open === row.id;
      return <li key={row.id} className={`inbox-row ${row.read ? "" : "unread"}`} data-run={row.id}>
        <button className="inbox-head" aria-expanded={expanded} onClick={() => {
          setOpen(expanded ? undefined : row.id);
          if (!expanded && !row.read) void act("read", row.id);
        }}><span>{row.name}</span><span className="muted small">{resultLabel(row)} · {timeLabel(row.time)}{row.sent.length ? ` · 已${targetsLabel(row.sent)}` : ""}</span></button>
        <p className={`inbox-text small ${expanded ? "" : "clamped"}`}>{resultText(row)}</p>
        {expanded && !!row.usage.prompt_tokens && <p className="muted small">用量：输入 {row.usage.prompt_tokens} · 输出 {row.usage.completion_tokens ?? 0} tokens</p>}
      </li>;
    })}</ul>
  </div>;
}
