import { test } from "node:test";
import assert from "node:assert/strict";
import { initialSession, projectEvent, type SessionState } from "@astra/ui-core/session-state";
import { createBackendEventReceiver, EventDeliveryState } from "@astra/ui-core/backend-protocol";
import { hasOngoingWork, holdsUp } from "../src/main/ongoing-work.js";

const session = (fields: Partial<SessionState>): SessionState => ({ ...initialSession("s"), ...fields });

test("a personal thread that is only starting holds up neither quitting nor a wipe; its own work does", () => {
  // The background host restarts it on its own, as after a wipe: quitting then must not ask about it.
  assert.equal(holdsUp(session({ personal: true, status: "connecting" })), false);
  assert.equal(holdsUp(session({ personal: true, status: "ready", busy: true })), true);
  assert.equal(holdsUp(session({ personal: true, status: "ready", approvals: [{ request_id: "r" }] })), true);
  assert.equal(holdsUp(session({ personal: true, status: "ready" })), false);
  // An ordinary session still holds things up while it starts, as before.
  assert.equal(holdsUp(session({ status: "connecting" })), true);
  assert.equal(hasOngoingWork(session({ personal: true, status: "connecting" })), true, "closing and deleting keep their own rule");
});

test("idle named subagents let a session close; one at work, or an active Agent Team, does not", () => {
  const crew = (status: string) => ({ c: { id: "c", kind: "crew" as const, name: "subagents", status: "active", leadAgentId: "p",
    tasks: [], messageCount: 0, agents: [{ id: "p", name: "lead", status: "running", unread: 0 }, { id: "a", name: "reader", status, unread: 0 }] } });
  const ready = { status: "ready" as const };
  assert.equal(hasOngoingWork(session({ ...ready, teams: crew("completed") })), false, "the parent lead runs, but is not work");
  assert.equal(hasOngoingWork(session({ ...ready, teams: crew("running") })), true);
  const team = { t: { id: "t", name: "reviewers", status: "active", leadAgentId: "l", tasks: [], messageCount: 0,
    agents: [{ id: "l", name: "lead", status: "running", unread: 0 }] } };
  assert.equal(hasOngoingWork(session({ ...ready, teams: team })), true, "an Agent Team keeps its rule");
});

test("a replayed older event cannot bring back a finished run or turn and block closing or a wipe", () => {
  // The desktop's own path: backend lines through the shared receiver into the session projection.
  let state = projectEvent(initialSession("s"), { type: "gui_ready" });
  const gaps: number[] = [];
  const receive = createBackendEventReceiver(event => { state = projectEvent(state, event); }, () => assert.fail(),
    { delivery: new EventDeliveryState(), onGap: cursor => gaps.push(cursor) });
  const run = (status: string) => ({ type: "process_status", process_id: "p1", kind: "shell", label: "npm test", status, started_at: 1 });
  const turn = (status: string) => ({ type: status === "running" ? "task_started" : "task_status", task: { id: "t1", status } });
  const live = (sequence: number, event: object) => receive(JSON.stringify({ ...event, generation: "g", sequence, cursor: sequence, replayable: true, event_id: `E${sequence}` }));
  const replayed = (cursor: number, event: object) => receive(JSON.stringify({ ...event, cursor, sequence: cursor, replayable: true, replayed: true, event_id: `E${cursor}` }));
  live(1, { type: "done" });
  // The start of the turn and of its run are lost live; their ends arrive.
  live(4, run("completed"));
  live(5, turn("completed"));
  assert.deepEqual(gaps, [1]);
  replayed(2, turn("running"));
  replayed(3, run("running"));
  replayed(4, run("completed"));
  replayed(5, turn("completed"));
  receive(JSON.stringify({ type: "event_replay_complete", after_cursor: 1, next_cursor: 5, has_more: false }));
  assert.equal(hasOngoingWork(state), false);
  assert.equal(holdsUp(state), false);
});
