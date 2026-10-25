import React from "react";
import { groupByDay, sourceLabel, timeLabel, toolLabel, type ActivityEntry } from "./jobs-view.js";

const DECISIONS: Record<string, string> = { once: "已批准", session: "已批准（本会话）", deny: "已拒绝", denied: "已拒绝", expired: "超时未答" };

/** What Astra did in the last week: conversation turns from every surface, with their effects and approvals, and job runs. */
export function ActivityPanel({ fail, openSession }: { fail: (error: unknown) => void; openSession: (name: string) => void }) {
  const [entries, setEntries] = React.useState<ActivityEntry[]>();
  const [quiet, setQuiet] = React.useState(true);
  React.useEffect(() => {
    void window.astra.query("activity", { days: 7 }).then(result => setEntries(result.entries as ActivityEntry[])).catch(fail);
  }, [fail]);
  if (!entries) return <p className="muted small">正在读取活动…</p>;
  // A turn that changed nothing and asked nothing is a conversation, not an action; shown on request.
  const shown = quiet ? entries.filter(entry => entry.kind === "job" ? !entry.quiet : entry.effects?.length || entry.approvals?.length) : entries;
  return <div className="activity-page">
    <p className="muted small">最近 7 天 Astra 做过的事：桌面、终端、微信里的对话，以及定时任务。默认只列改过东西、执行过命令或请你批准过的。</p>
    <label className="activity-toggle small"><input type="checkbox" checked={!quiet} onChange={event => setQuiet(!event.target.checked)}/> 也显示只聊天、没有动作的对话</label>
    {!shown.length && <p className="muted small">这段时间没有需要列出的动作。</p>}
    {groupByDay(shown).map(group => <section key={group.label} className="activity-day">
      <h3>{group.label}</h3>
      <ul>{group.entries.map(entry => <li key={`${entry.kind}:${entry.id}`} className="activity-row" data-kind={entry.kind}>
        <div className="activity-head"><span className="activity-source">{sourceLabel(entry.source)}</span><strong>{entry.title || "（无文字）"}</strong>
          <span className="muted small">{timeLabel(entry.time)}</span></div>
        {entry.kind === "job" && <p className="small">{entry.status === "completed" ? entry.quiet ? "没有新内容" : entry.summary : `没能完成：${entry.summary || entry.status}`}</p>}
        {!!entry.effects?.length && <ul className="activity-effects small">{entry.effects.map((effect, index) =>
          <li key={index}><span>{toolLabel(effect.name)}</span>{effect.summary && <code>{effect.summary}</code>}{effect.status !== "completed" && <span className="muted">（{effect.status}）</span>}</li>)}</ul>}
        {!!entry.approvals?.length && <ul className="activity-approvals small">{entry.approvals.map((approval, index) =>
          <li key={index}>{DECISIONS[approval.decision] || approval.decision}：{approval.title}</li>)}</ul>}
        {entry.session && <button className="link small" onClick={() => openSession(entry.session!)}>打开这次对话</button>}
      </li>)}</ul>
    </section>)}
  </div>;
}
