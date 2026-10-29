import { test } from "node:test";
import assert from "node:assert/strict";
import { initialSession, type SessionState } from "@astra/ui-core/session-state";
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
