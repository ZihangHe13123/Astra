import { delegateActive, orderedDelegates, type DelegateView } from "@astra/ui-core/delegates";
import type { AgentTeamAgentView, AgentTeamView } from "@astra/ui-core/agent-team-state";

/** One named subagent as the subagent page shows it: its latest assignment, and the ones before. */
export type SubagentMember = { agent: AgentTeamAgentView; latest?: DelegateView; earlier: DelegateView[] };
export type SubagentTeam = { team: AgentTeamView; lead?: AgentTeamAgentView; members: SubagentMember[] };
/** `named`: the conversation's subagents (a crew), `teams`: Agent Teams of earlier versions, `solo`: runs without a name. */
export type SubagentGroups = { named: SubagentMember[]; teams: SubagentTeam[]; solo: DelegateView[]; active: number; total: number };

const AGENT_ACTIVE = ["starting", "running", "idle", "waiting"];

/** Every subagent of a conversation in one place: the named ones with what each was given, Agent Teams of
 * earlier versions, then runs without a name. A member's runs are its delegations (by agent or process id);
 * one whose team is unknown here, as in a restored history, stands with the unnamed ones. */
export function subagentGroups(delegates: Record<string, DelegateView>, teams: Record<string, AgentTeamView>): SubagentGroups {
  const ordered = orderedDelegates(delegates);
  const claimed = new Set<string>();
  const membersOf = (team: AgentTeamView) => team.agents.filter(agent => agent.id !== team.leadAgentId).map(agent => {
    const runs = ordered.filter(d => d.team_id === team.id && d.agent_id === agent.id || !!agent.processId && d.process_id === agent.processId);
    runs.forEach(d => claimed.add(d.process_id));
    const [latest, ...earlier] = runs;
    return { agent, latest, earlier };
  });
  const named = Object.values(teams).filter(team => team.kind === "crew").flatMap(membersOf);
  const grouped = Object.values(teams).filter(team => team.kind !== "crew").map(team => {
    const lead = team.agents.find(agent => agent.id === team.leadAgentId);
    return { team, lead, members: membersOf(team) };
  });
  const solo = ordered.filter(d => !claimed.has(d.process_id));
  const members = grouped.flatMap(group => group.members);
  // A named subagent waits idle between assignments at no cost: only its work counts as going on.
  const active = solo.filter(delegateActive).length
    + named.filter(m => m.latest ? delegateActive(m.latest) : ["starting", "running"].includes(m.agent.status)).length
    + members.filter(m => m.latest ? delegateActive(m.latest) : AGENT_ACTIVE.includes(m.agent.status)).length;
  return { named, teams: grouped, solo, active, total: solo.length + named.length + members.length };
}

/** The named subagents by the group they work in, ungrouped ones first. */
export function byGroup(named: SubagentMember[]): [string, SubagentMember[]][] {
  const groups = new Map<string, SubagentMember[]>();
  for (const member of named) groups.set(member.agent.group || "", [...groups.get(member.agent.group || "") || [], member]);
  return [...groups.entries()].sort(([a], [b]) => a === "" ? -1 : b === "" ? 1 : a.localeCompare(b));
}
