import { test } from "node:test";
import assert from "node:assert/strict";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import type { DelegateView } from "@astra/ui-core/delegates";
import type { AgentTeamView } from "@astra/ui-core/agent-team-state";
import { subagentGroups } from "../src/renderer/subagents.js";
import { SubagentPanel } from "../src/renderer/delegates.js";

const run = (process_id: string, fields: Partial<DelegateView>): DelegateView => ({ process_id, goal: `goal ${process_id}`, status: "completed", ...fields });
const team: AgentTeamView = {
  id: "t1", name: "资料组", goal: "整理三份报告", status: "active", leadAgentId: "lead", messageCount: 4,
  agents: [
    { id: "lead", name: "lead", role: "coordinator", status: "running", unread: 2 },
    { id: "a", name: "reader", role: "读报告", status: "running", unread: 1 },
    { id: "b", name: "writer", role: "写摘要", status: "starting", unread: 0 },
    { id: "c", name: "checker", status: "idle", processId: "p-c", unread: 0 },
  ],
  tasks: [{ id: "k1", title: "读第一份", status: "completed", ownerId: "a" }],
};
const delegates = Object.fromEntries([
  run("p-a1", { team_id: "t1", agent_id: "a", started_at: 100, updated_at: 150 }),
  run("p-a2", { team_id: "t1", agent_id: "a", status: "running", started_at: 200, updated_at: 210, current_tool: "read_file" }),
  run("p-c", { status: "idle", started_at: 120, updated_at: 130 }),
  run("p-solo", { status: "running", started_at: 300, updated_at: 300 }),
  run("p-lost", { team_id: "gone", agent_id: "z", started_at: 50, updated_at: 60 }),
].map(d => [d.process_id, d]));

test("a team's members carry their own runs, newest first, and every other run stands alone", () => {
  const groups = subagentGroups(delegates, { t1: team });
  const [group] = groups.teams;
  assert.equal(group.lead?.id, "lead");
  assert.deepEqual(group.members.map(m => [m.agent.id, m.latest?.process_id, m.earlier.map(d => d.process_id)]),
    [["a", "p-a2", ["p-a1"]], ["b", undefined, []], ["c", "p-c", []]]);
  // A run whose team this conversation does not know (a restored history) is not lost.
  assert.deepEqual(groups.solo.map(d => d.process_id), ["p-solo", "p-lost"]);
  // Running, starting and idle count as going on; the lead is the parent, not a subagent.
  assert.equal(groups.active, 4);
  assert.equal(groups.total, 5);
});

test("without teams the page is the delegations alone, and empty it says what will appear", () => {
  const solo = subagentGroups(delegates, {});
  assert.equal(solo.teams.length, 0);
  assert.equal(solo.solo.length, 5);
  const empty = renderToStaticMarkup(React.createElement(SubagentPanel, { delegates: {}, teams: {}, fail: () => {} }));
  assert.match(empty, /aria-label="子代理"/);
  assert.match(empty, /还没有子代理/);
});

test("the page shows the team, each member by name on its latest run, the tasks, then one-off delegations", () => {
  const html = renderToStaticMarkup(React.createElement(SubagentPanel, { delegates, teams: { t1: team }, fail: () => {} }));
  assert.match(html, /aria-label="Team 资料组"/);
  assert.match(html, /主代理有 2 条未读消息/);
  const cards = [...html.matchAll(/data-delegate-id="([^"]+)"/g)].map(m => m[1]);
  // p-a1 waits folded under its member's earlier tasks; it is in the page, after the member's latest run.
  assert.deepEqual(cards, ["p-a2", "p-a1", "p-c", "p-solo", "p-lost"]);
  assert.match(html, /<strong>reader<\/strong>.*读报告.*运行中/s);
  assert.match(html, /1 条未读/);
  assert.match(html, /reader 更早的任务 · 1 项/);
  // A member that has not run yet is a plain row.
  assert.match(html, /class="team-member".*writer.*启动中/s);
  assert.match(html, /✓ 读第一份/);
  assert.match(html, /<h4>单次委派<\/h4>/);
  assert.ok(html.indexOf("单次委派") > html.indexOf("资料组"));
});
