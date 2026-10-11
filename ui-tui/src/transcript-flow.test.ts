import assert from "node:assert/strict";
import { test } from "node:test";
import { gapLine, TranscriptFlow, type FlowItem } from "./transcript-flow.js";

const gaps = (flow: TranscriptFlow, items: FlowItem[]) => items.map((item) => flow.before(item));

test("inside a turn the rail runs between pieces, and a run of tool calls has nothing between them", () => {
  const flow = new TranscriptFlow();
  assert.deepEqual(
    gaps(flow, ["user", "reasoning", "assistant", "tool", "tool", "tool", "assistant", "changes"]),
    ["none", "rail", "rail", "rail", "none", "none", "rail", "rail"],
  );
});

test("a new question starts on a clear line, whatever the turn before it ended with", () => {
  for (const last of ["assistant", "tool", "changes", "user"] as const) {
    const flow = new TranscriptFlow();
    gaps(flow, ["user", last]);
    assert.equal(flow.before("user"), "blank", last);
    assert.equal(flow.before("assistant"), "rail", last);
  }
});

test("a notice ends with its own blank line, so nothing is owed after it", () => {
  const flow = new TranscriptFlow();
  assert.equal(flow.before("other"), "none");
  assert.equal(flow.before("assistant"), "none");
  // Inside a turn a notice is set off by a clear line and leaves the next piece nothing to add.
  assert.equal(flow.before("other"), "blank");
  assert.equal(flow.previous, null);
  assert.equal(flow.before("tool"), "none");
  assert.equal(flow.before("user"), "blank");
});

test("a reset forgets what was owed", () => {
  const flow = new TranscriptFlow();
  gaps(flow, ["user", "tool"]);
  assert.equal(flow.previous, "tool");
  flow.reset();
  assert.equal(flow.previous, null);
  assert.equal(flow.before("tool"), "none");
});

test("an empty line carries the rail only when it is given one", () => {
  const plain = gapLine("a");
  assert.equal(plain.timeRail, undefined);
  assert.equal(plain.text.trim(), "");
  const railed = gapLine("b", { width: 7, prefixWidth: 7 });
  assert.deepEqual(railed.timeRail, { width: 7, prefixWidth: 7, label: "", kind: "relative" });
  assert.equal(railed.prefix, "");
});
