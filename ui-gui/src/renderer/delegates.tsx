import React, { useEffect, useState } from "react";
import { ChevronRight, Users } from "lucide-react";
import { delegateActive, delegateStatusLabel, orderedDelegates, type DelegateView } from "@astra/ui-core/delegates";
import { Markdown } from "./messages.js";
import type { AgentTeamAgentView, AgentTeamView } from "@astra/ui-core/agent-team-state";
import { byGroup, subagentGroups, type SubagentMember } from "./subagents.js";
import { RawDetails, statusLabel } from "./raw-details.js";

export function DelegateSummary({ delegates, teams, open }: { delegates: Record<string, DelegateView>; teams?: Record<string, AgentTeamView>; open: () => void }) {
  const items = orderedDelegates(delegates);
  if (!items.length) return null;
  const active = items.filter(delegateActive);
  const visible = (active.length ? active : items).slice(0, 3);
  // The count is the subagent page's: a team member still starting is going on too.
  const going = subagentGroups(delegates, teams || {}).active;
  return <button className="delegate-summary" data-testid="delegate-summary" onClick={open}>
    <span className="delegate-heading"><Users size={15}/><strong>子代理</strong><span className="muted">{going ? `${going} 项进行中` : "最近委派"}</span><ChevronRight size={15}/></span>
    {visible.map(d => <span className="delegate-brief" key={d.process_id}><span className={`status-dot ${d.status}`}/><span title={d.goal}>{d.goal}</span><small>{delegateStatusLabel(d.status)}{d.status === "running" && d.current_tool ? ` · ${d.current_tool}` : ""}</small></span>)}
    {going > visible.filter(delegateActive).length && <small className="muted">另有 {going - visible.filter(delegateActive).length} 项进行中</small>}
  </button>;
}

/** The subagent page: the conversation's named subagents by group, Agent Teams of earlier versions, then unnamed runs. */
export function SubagentPanel({ delegates, teams, runtime, fail, openFile }: { delegates: Record<string, DelegateView>; teams?: Record<string, AgentTeamView>; runtime?: string; fail: (e: unknown) => void; openFile?: (path: string) => void }) {
  const [limit, setLimit] = useState(20);
  const groups = subagentGroups(delegates, teams || {});
  const now = useClock(groups.active > 0);
  useEffect(() => setLimit(20), [runtime]);
  const card = (d: DelegateView, member?: AgentTeamAgentView) => <DelegateCard key={d.process_id} item={d} member={member} now={now} runtime={runtime} fail={fail} openFile={openFile}/>;
  const member = ({ agent, latest, earlier }: SubagentMember) => latest ? <React.Fragment key={agent.id}>{card(latest, agent)}
    {earlier.length > 0 && <details className="delegate-earlier"><summary>{agent.name} 更早的任务 · {earlier.length} 项</summary>{earlier.map(d => card(d, agent))}</details>}</React.Fragment>
    : <div className="team-member" key={agent.id}><span className={`status-dot ${agent.status}`}/><span>{agent.name}<small>{agent.role}</small></span><span>{agent.closed ? "已结束" : statusLabel(agent.status)}{agent.unread > 0 && ` · ${agent.unread} 条未读`}</span></div>;
  if (!groups.total && !groups.teams.length) return <section className="subagents" aria-label="子代理"><h3>子代理</h3>
    <p className="muted">这个对话还没有子代理。主代理委派任务后，会显示在这里。</p></section>;
  return <section className="subagents" aria-label="子代理"><h3>子代理 <span className="muted small">{groups.total} 个{groups.active > 0 ? ` · ${groups.active} 个进行中` : ""}</span></h3>
    {groups.solo.some(d => d.history_truncated) && <p className="muted small">仅恢复了最近的部分委派记录。</p>}
    {byGroup(groups.named).map(([group, members]) => group
      ? <section className="subagent-team" key={`group:${group}`} aria-label={`小组 ${group}`}>
          <div className="subagent-team-head"><Users size={14}/><strong>{group}</strong><span className="muted small">小组 · 可以互发消息</span></div>
          {members.map(member)}</section>
      : <React.Fragment key="ungrouped">{members.map(member)}</React.Fragment>)}
    {groups.teams.map(({ team, lead, members }) => <section className="subagent-team" key={team.id} aria-label={`Team ${team.name}`}>
      <div className="subagent-team-head"><Users size={14}/><strong>{team.name}</strong><span className="muted small">Team · {statusLabel(team.status)}</span></div>
      {team.goal && <p className="muted small">{team.goal}</p>}
      {!!lead?.unread && <p className="small">主代理有 {lead.unread} 条未读消息</p>}
      {members.map(member)}
      {!members.length && <p className="muted small">还没有成员。</p>}
      {team.tasks.length > 0 && <div className="subagent-tasks"><h4>任务</h4>{team.tasks.map(task => <p className="small" key={task.id}>{task.status === "completed" ? "✓" : "·"} {task.title} <span className="muted">{statusLabel(task.status)}</span></p>)}</div>}
      <RawDetails label={`诊断详情 · ${team.messageCount} 条消息`} value={team}/>
    </section>)}
    {groups.solo.length > 0 && <section className="subagent-solo" aria-label="单次委派">{(groups.teams.length > 0 || groups.named.length > 0) && <h4>单次委派</h4>}
      {groups.solo.slice(0, limit).map(d => card(d))}
      {groups.solo.length > limit && <button className="text-button" onClick={() => setLimit(n => n + 20)}>显示更多委派（还有 {groups.solo.length - limit} 项）</button>}
    </section>}
  </section>;
}
/** Read-only history: the delegations a saved conversation recorded, folded under the transcript. */
export function DelegateHistory({ delegates, fail }: { delegates: DelegateView[]; fail: (e: unknown) => void }) {
  const [open, setOpen] = useState(false);
  if (!delegates.length) return null;
  return <details className="delegate-history" onToggle={e => setOpen(e.currentTarget.open)}><summary>委派记录 · {delegates.length} 项</summary>
    {open && <SubagentPanel delegates={Object.fromEntries(delegates.map(d => [d.process_id, d]))} fail={fail}/>}
  </details>;
}
/** The time, every second while something runs, so the cards' durations move. */
function useClock(running: boolean): number {
  const [now, setNow] = useState(Date.now());
  useEffect(() => { if (!running) return; setNow(Date.now()); const id = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(id); }, [running]);
  return now;
}
function DelegateCard({ item: d, member, now, runtime, fail, openFile }: { item: DelegateView; member?: AgentTeamAgentView; now: number; runtime?: string; fail: (e: unknown) => void; openFile?: (path: string) => void }) {
  const [open, setOpen] = useState(false);
  const elapsed = delegateActive(d)
    ? d.started_at ? Math.max(0, now - d.started_at * 1000) : d.duration_ms
    : d.duration_ms;
  const duration = elapsed == null ? "" : elapsed >= 60000 ? `${Math.floor(elapsed / 60000)} 分 ${Math.floor(elapsed / 1000) % 60} 秒` : `${Math.floor(elapsed / 1000)} 秒`;
  return <article className="delegate-card" data-delegate-id={d.process_id} data-status={d.status}>
    <div className="delegate-heading"><span className={`status-dot ${d.status}`}/>{member ? <><strong>{member.name}</strong>{member.role && <span className="delegate-role muted small">{member.role}</span>}<span className="delegate-state small">{delegateStatusLabel(d.status)}{duration && ` · ${duration}`}</span>{!!member.unread && <span className="delegate-unread">{member.unread} 条未读</span>}</> : <><strong>{delegateStatusLabel(d.status)}</strong><span className="muted small">{duration}</span></>}</div>
    <p className="delegate-goal">{d.goal}{d.goal_truncated && "…"}</p>
    {d.status === "running" && <p className="delegate-activity">{d.current_tool ? `正在调用 ${d.current_tool}` : "正在处理任务"}</p>}
    {d.status === "queued" && <p className="delegate-activity">等待执行名额</p>}
    {d.status === "idle" && <p className="delegate-activity">等待新消息</p>}
    {d.status === "interrupted" && <p className="delegate-activity">连接或进程已中断，未确认最终结果。</p>}
    {d.turns_used != null && <p className="delegate-meta">已执行 {d.turns_used} 轮{d.max_turns ? ` / 上限 ${d.max_turns} 轮` : ""}</p>}
    {d.error && <p className="error-text">{d.error}{d.error_truncated && "…"}</p>}
    {d.result && <details className="delegate-result" onToggle={e => setOpen(e.currentTarget.open)}><summary>{d.partial || d.status === "partial" ? "查看部分结果" : "查看结果"}</summary>
      {open && <div className="message message-body"><Markdown text={d.result} runtime={runtime} fail={fail}/></div>}
      {d.result_truncated && <p className="muted small">结果较长，此处已截断显示。</p>}
    </details>}
    {d.artifact_path && openFile && <button className="text-button" onClick={() => openFile(d.artifact_path!)}>查看完整记录</button>}
  </article>;
}
