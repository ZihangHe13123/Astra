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
