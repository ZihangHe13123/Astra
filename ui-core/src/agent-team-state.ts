import type { PyEvent } from "./types.js";

export type AgentTeamAgentView = {
  id: string;
  parentId?: string;
  name: string;
  role?: string;
  status: string;
  processId?: string;
  unread: number;
};

export type AgentTeamTaskView = {
  id: string;
  title: string;
  status: string;
  ownerId?: string;
};

export type AgentTeamView = {
  id: string;
  name: string;
  goal?: string;
  status: string;
  leadAgentId?: string;
  agents: AgentTeamAgentView[];
  tasks: AgentTeamTaskView[];
  messageCount: number;
};

type AgentTeamEvent = Extract<PyEvent, { type: "agent_team" }>;

function upsertAgent(
  agents: AgentTeamAgentView[],
  id: string,
  patch: Partial<AgentTeamAgentView>,
): AgentTeamAgentView[] {
  const index = agents.findIndex((agent) => agent.id === id);
  if (index < 0) {
    return [...agents, { id, name: patch.name || id.slice(0, 8), status: patch.status || "starting", unread: 0, ...patch }];
  }
  return agents.map((agent, itemIndex) => itemIndex === index ? { ...agent, ...patch } : agent);
}

function upsertTask(
  tasks: AgentTeamTaskView[],
  id: string,
  patch: Partial<AgentTeamTaskView>,
): AgentTeamTaskView[] {
  const index = tasks.findIndex((task) => task.id === id);
  if (index < 0) {
    return [...tasks, { id, title: patch.title || id.slice(0, 8), status: patch.status || "pending", ...patch }];
  }
  return tasks.map((task, itemIndex) => itemIndex === index ? { ...task, ...patch } : task);
}

export function reduceAgentTeamEvent(
  teams: AgentTeamView[],
  event: AgentTeamEvent,
): AgentTeamView[] {
  const existing = teams.find((team) => team.id === event.team_id);
  const base: AgentTeamView = existing ?? {
    id: event.team_id,
    name: event.name || event.team_id.slice(0, 12),
    goal: event.goal,
    status: event.status || "active",
    leadAgentId: event.lead_agent_id,
    agents: [],
    tasks: [],
    messageCount: 0,
  };
  let next = { ...base, agents: [...base.agents], tasks: [...base.tasks] };

  if (event.event === "team_created") {
    next = {
      ...next,
      name: event.name || next.name,
      goal: event.goal || next.goal,
      status: event.status || "active",
      leadAgentId: event.lead_agent_id || next.leadAgentId,
    };
    if (event.lead_agent_id) {
      next.agents = upsertAgent(next.agents, event.lead_agent_id, {
        name: "lead", role: "coordinator", status: "running",
      });
    }
  } else if (event.event === "team_resumed") {
    next = {
      ...next,
      name: event.name || next.name,
      goal: event.goal || next.goal,
      status: event.status || "active",
      leadAgentId: event.lead_agent_id || next.leadAgentId,
    };
    if (next.leadAgentId) {
      next.agents = upsertAgent(next.agents, next.leadAgentId, {
        name: "lead",
        role: "coordinator",
        status: "running",
      });
    }
  } else if (event.event.startsWith("team_agent_") && event.agent_id) {
    next.agents = upsertAgent(next.agents, event.agent_id, {
      ...(event.name ? { name: event.name } : {}),
      ...(event.parent_agent_id ? { parentId: event.parent_agent_id } : {}),
      ...(event.role ? { role: event.role } : {}),
      status: event.status || (event.event === "team_agent_created" ? "starting" : "running"),
      ...(event.process_id ? { processId: event.process_id } : {}),
    });
  } else if (event.event === "team_message") {
    next.messageCount += event.message_count ?? event.recipient_agent_ids?.length ?? 1;
    for (const recipient of event.recipient_agent_ids ?? []) {
      const agent = next.agents.find((item) => item.id === recipient);
      next.agents = upsertAgent(next.agents, recipient, { unread: (agent?.unread ?? 0) + 1 });
    }
  } else if (event.event === "team_inbox_read" && event.agent_id && event.acknowledged) {
    next.agents = upsertAgent(next.agents, event.agent_id, { unread: 0 });
  } else if (event.event.startsWith("team_task_") && event.team_task_id) {
    next.tasks = upsertTask(next.tasks, event.team_task_id, {
      ...(event.title ? { title: event.title } : {}),
      status: event.status || "pending",
      ...(event.owner_agent_id ? { ownerId: event.owner_agent_id } : {}),
    });
  } else if (event.event === "team_stopped") {
    next.status = event.status || "stopped";
    next.agents = next.agents.map((agent) =>
      ["starting", "running", "idle", "waiting"].includes(agent.status)
        ? { ...agent, status: "cancelled" }
        : agent,
    );
  }

  return existing
    ? teams.map((team) => team.id === event.team_id ? next : team)
    : [...teams, next];
}
