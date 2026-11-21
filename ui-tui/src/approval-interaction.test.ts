import assert from "node:assert/strict";
import test from "node:test";

import { transitionApprovalInput } from "./approval-interaction.js";
import type { ToolApprovalRequest } from "./types.js";

function request(requestId: string): ToolApprovalRequest {
  return {
    type: "tool_approval_request",
    request_id: requestId,
    tool_name: "execute_shell",
    risk: "execute",
    kind: "host_execution",
    reason: "backend fact",
    choices: ["once", "session", "deny"],
  };
}

const first = request("approval-1");
const second = request("approval-2");

test("Y resolves only the FIFO head and resets detail state for the next request", () => {
  const next = transitionApprovalInput(
    { requests: [first, second], detailOpen: true, detailOffset: 7 },
    "y",
    {},
    { lineCount: 20, pageSize: 5 },
  );

  assert.deepEqual(next.response, { requestId: "approval-1", decision: "once" });
  assert.deepEqual(next.requests.map((item) => item.request_id), ["approval-2"]);
  assert.equal(next.detailOpen, false);
  assert.equal(next.detailOffset, 0);
  assert.equal(next.handled, true);
});

test("Enter selects allow-once for the FIFO head", () => {
  const next = transitionApprovalInput(
    { requests: [first, second], detailOpen: false, detailOffset: 0 },
    "",
    { return: true },
    { lineCount: 1, pageSize: 5 },
  );

  assert.deepEqual(next.response, { requestId: "approval-1", decision: "once" });
  assert.deepEqual(next.requests, [second]);
});

test("A selects the offered session decision", () => {
  const next = transitionApprovalInput(
    { requests: [first], detailOpen: false, detailOffset: 0 },
    "A",
    {},
    { lineCount: 1, pageSize: 5 },
  );

  assert.deepEqual(next.response, { requestId: "approval-1", decision: "session" });
  assert.deepEqual(next.requests, []);
});

test("foreground grants require a release for the same FIFO head and an offered choice; legacy input cannot grant", () => {
  const takeover: ToolApprovalRequest = {
    ...first,
    request_id: "takeover-1",
    kind: "computer_foreground_takeover",
    choices: ["once", "session", "deny"],
  };

  const state = { requests: [takeover], detailOpen: true, detailOffset: 0 }, bounds = { lineCount: 8, pageSize: 5 };
  for (const input of ["y", "A", "t", "l", ""]) assert.equal(transitionApprovalInput(state, input, { return: !input }, bounds).response, undefined);
  assert.equal(transitionApprovalInput(state, "", {}, bounds, { requestId: "old", decision: "session" }).response, undefined);
  const next = transitionApprovalInput(state, "", {}, bounds, { requestId: takeover.request_id, decision: "session" });

  assert.deepEqual(next.response, { requestId: "takeover-1", decision: "session" });
  assert.deepEqual(next.requests, []);
  for (const choices of [["once", "deny"] as const, undefined]) {
    const bounded = transitionApprovalInput(
      { requests: [{ ...takeover, choices: choices ? [...choices] : undefined }], detailOpen: false, detailOffset: 0 },
      "", {}, bounds, { requestId: takeover.request_id, decision: "session" },
    );
    assert.equal(bounded.response, undefined);
  }
});

test("V toggles details and always resets the scroll offset", () => {
  const opened = transitionApprovalInput(
    { requests: [first], detailOpen: false, detailOffset: 4 },
    "v",
    {},
    { lineCount: 20, pageSize: 5 },
  );
  const closed = transitionApprovalInput(opened, "V", {}, { lineCount: 20, pageSize: 5 });

  assert.equal(opened.detailOpen, true);
  assert.equal(opened.detailOffset, 0);
  assert.equal(closed.detailOpen, false);
  assert.equal(closed.detailOffset, 0);
  assert.equal(closed.response, undefined);
});

test("Esc collapses expanded details before it can deny", () => {
  const next = transitionApprovalInput(
    { requests: [first], detailOpen: true, detailOffset: 3 },
    "",
    { escape: true },
    { lineCount: 20, pageSize: 5 },
  );

  assert.equal(next.detailOpen, false);
  assert.equal(next.detailOffset, 0);
  assert.equal(next.response, undefined);
  assert.deepEqual(next.requests, [first]);
});

test("N denies while expanded and Esc denies while collapsed", () => {
  for (const [detailOpen, input, key] of [
    [true, "n", {}],
    [false, "", { escape: true }],
  ] as const) {
    const next = transitionApprovalInput(
      { requests: [first, second], detailOpen, detailOffset: 2 },
      input,
      key,
      { lineCount: 20, pageSize: 5 },
    );

    assert.deepEqual(next.response, { requestId: "approval-1", decision: "deny" });
    assert.deepEqual(next.requests, [second]);
    assert.equal(next.detailOpen, false);
    assert.equal(next.detailOffset, 0);
  }
});

test("expanded paging is clamped and unrelated keys stay consumed by the approval", () => {
  const paged = transitionApprovalInput(
    { requests: [first], detailOpen: true, detailOffset: 8 },
    "",
    { pageDown: true },
    { lineCount: 12, pageSize: 5 },
  );
  const ignored = transitionApprovalInput(
    paged,
    "x",
    {},
    { lineCount: 12, pageSize: 5 },
  );

  assert.equal(paged.detailOffset, 7);
  assert.equal(ignored.handled, true);
  assert.equal(ignored.response, undefined);
  assert.deepEqual(ignored.requests, [first]);
});
