import assert from "node:assert/strict";
import { reduceAgentTeamEvent } from "./agent-team-state.js";

let teams = reduceAgentTeamEvent([], {
  type: "agent_team",
  kind: "agent_team",
  event: "team_created",
  team_id: "team-1",
  name: "reviewers",
  goal: "review changes",
  lead_agent_id: "lead-1",
  status: "active",
});

teams = reduceAgentTeamEvent(teams, {
  type: "agent_team",
  kind: "agent_team",
  event: "team_agent_started",
  team_id: "team-1",
  agent_id: "agent-1",
  parent_agent_id: "lead-1",
  name: "explorer",
  role: "research",
  process_id: "process-1",
  status: "running",
});

teams = reduceAgentTeamEvent(teams, {
  type: "agent_team",
  kind: "agent_team",
  event: "team_message",
  team_id: "team-1",
  sender_agent_id: "agent-1",
  recipient_agent_ids: ["lead-1"],
  message_count: 1,
});

teams = reduceAgentTeamEvent(teams, {
  type: "agent_team",
  kind: "agent_team",
  event: "team_task_claimed",
  team_id: "team-1",
  team_task_id: "task-1",
  title: "inspect",
  owner_agent_id: "agent-1",
  status: "running",
});

assert.equal(teams.length, 1);
assert.equal(teams[0].agents.length, 2);
assert.equal(teams[0].agents.find((agent) => agent.id === "lead-1")?.unread, 1);
assert.equal(teams[0].tasks[0].ownerId, "agent-1");
assert.equal(teams[0].messageCount, 1);

teams = reduceAgentTeamEvent(teams, {
  type: "agent_team",
  kind: "agent_team",
  event: "team_stopped",
  team_id: "team-1",
  status: "stopped",
});
teams = reduceAgentTeamEvent(teams, {
  type: "agent_team",
  kind: "agent_team",
  event: "team_resumed",
  team_id: "team-1",
  lead_agent_id: "lead-1",
  status: "active",
});

assert.equal(teams[0].status, "active");
assert.equal(teams[0].agents.find((agent) => agent.id === "lead-1")?.status, "running");
assert.equal(teams[0].agents.find((agent) => agent.id === "agent-1")?.status, "cancelled");

// A conversation's named subagents arrive as a crew: each with its group, and closed when ended for good.
let crew = reduceAgentTeamEvent([], {
  type: "agent_team", kind: "agent_team", event: "team_created", team_id: "crew-1", team_kind: "crew",
  name: "subagents", lead_agent_id: "parent-1", status: "active",
});
crew = reduceAgentTeamEvent(crew, {
  type: "agent_team", kind: "agent_team", event: "team_agent_created", team_id: "crew-1", agent_id: "a-1",
  parent_agent_id: "parent-1", name: "parser", role: "worker", group: "core", status: "starting",
});
crew = reduceAgentTeamEvent(crew, {
  type: "agent_team", kind: "agent_team", event: "team_agent_terminal", team_id: "crew-1", agent_id: "a-1",
  status: "completed",
});
crew = reduceAgentTeamEvent(crew, {
  type: "agent_team", kind: "agent_team", event: "team_agent_closed", team_id: "crew-1", agent_id: "a-1",
});
const parser = crew[0].agents.find((agent) => agent.id === "a-1");
assert.equal(crew[0].kind, "crew");
assert.deepEqual([parser?.group, parser?.status, parser?.closed], ["core", "completed", true]);
assert.equal(teams[0].kind, undefined, "an Agent Team of earlier versions stays a Team");
