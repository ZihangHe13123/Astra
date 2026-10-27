import { delegateActive, orderedDelegates, type DelegateView } from "@astra/ui-core/delegates";
import type { AgentTeamAgentView, AgentTeamView } from "@astra/ui-core/agent-team-state";

/** One member of a team as the subagent page shows it: its latest assignment, and the ones before. */
export type SubagentMember = { agent: AgentTeamAgentView; latest?: DelegateView; earlier: DelegateView[] };
export type SubagentTeam = { team: AgentTeamView; lead?: AgentTeamAgentView; members: SubagentMember[] };
export type SubagentGroups = { teams: SubagentTeam[]; solo: DelegateView[]; active: number; total: number };

const AGENT_ACTIVE = ["starting", "running", "idle", "waiting"];

/** Every subagent of a conversation in one place: a team's members with what each was given, then one-off
 * delegations. A member's runs are its delegations (by agent or process id); one whose team is unknown
 * here, as in a restored history, stands with the one-off ones. */
export function subagentGroups(delegates: Record<string, DelegateView>, teams: Record<string, AgentTeamView>): SubagentGroups {
  const ordered = orderedDelegates(delegates);
  const claimed = new Set<string>();
  const grouped = Object.values(teams).map(team => {
    const lead = team.agents.find(agent => agent.id === team.leadAgentId);
    const members = team.agents.filter(agent => agent.id !== team.leadAgentId).map(agent => {
      const runs = ordered.filter(d => d.team_id === team.id && d.agent_id === agent.id || !!agent.processId && d.process_id === agent.processId);
      runs.forEach(d => claimed.add(d.process_id));
      const [latest, ...earlier] = runs;
      return { agent, latest, earlier };
    });
    return { team, lead, members };
  });
  const solo = ordered.filter(d => !claimed.has(d.process_id));
  const members = grouped.flatMap(group => group.members);
  const active = solo.filter(delegateActive).length
    + members.filter(m => m.latest ? delegateActive(m.latest) : AGENT_ACTIVE.includes(m.agent.status)).length;
  return { teams: grouped, solo, active, total: solo.length + members.length };
}
