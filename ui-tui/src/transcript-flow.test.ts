import assert from "node:assert/strict";
import { test } from "node:test";
import { gapLine, noticeItem, TranscriptFlow, type FlowItem } from "./transcript-flow.js";

const gaps = (flow: TranscriptFlow, items: FlowItem[]) => items.map((item) => flow.before(item));

test("inside a turn the rail runs between pieces, and a run of tool calls has nothing between them", () => {
  const flow = new TranscriptFlow();
  assert.deepEqual(
    gaps(flow, ["user", "reasoning", "assistant", "tool", "tool", "tool", "assistant", "changes"]),
    ["none", "rail", "rail", "rail", "none", "none", "rail", "rail"],
  );
});

test("a new question starts on a clear line, whatever came before it", () => {
  for (const last of ["assistant", "tool", "changes", "user", "note", "block"] as const) {
    const flow = new TranscriptFlow();
    gaps(flow, ["user", last]);
    assert.equal(flow.before("user"), "blank", last);
    assert.equal(flow.before("assistant"), "rail", last);
  }
});

test("a one-line notice is a row among rows, and stands off from what is said", () => {
  const flow = new TranscriptFlow();
  // Between tool calls it takes one line and no more.
  assert.deepEqual(gaps(flow, ["user", "tool", "note", "tool", "note", "note"]), ["none", "rail", "none", "none", "none", "none"]);
  // The rail runs between a notice and what is said before or after it.
  assert.deepEqual(gaps(flow, ["assistant", "note", "assistant"]), ["rail", "rail", "rail"]);
  // A command and its answer are one exchange.
  const command = new TranscriptFlow();
  assert.deepEqual(gaps(command, ["user", "note", "user", "block", "note"]), ["none", "rail", "blank", "rail", "rail"]);
});

test("a message that is not part of the talk is a notice when it is one line, a block otherwise", () => {
  assert.equal(noticeItem("YOLO mode ON"), "note");
  assert.equal(noticeItem("  trailing newline does not make a block\n"), "note");
  assert.equal(noticeItem("Current theme: a\nAvailable: a, b"), "block");
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
