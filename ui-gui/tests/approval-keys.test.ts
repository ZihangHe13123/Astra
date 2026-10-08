import { test } from "node:test";
import assert from "node:assert/strict";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { Approval } from "../src/renderer/controls.js";
import { approvalDecision, approvalKeyHint, typingIn, type ApprovalKeyPlace, type ApprovalKeyPress } from "../src/renderer/approval-keys.js";

const all = ["once", "session", "deny"];
const card: ApprovalKeyPlace = { in: "card", onButton: false };
const button: ApprovalKeyPlace = { in: "card", onButton: true };
const idle: ApprovalKeyPlace = { in: "window", typing: false };
const typing: ApprovalKeyPlace = { in: "window", typing: true };
const decide = (press: ApprovalKeyPress, place: ApprovalKeyPlace, choices = all) => approvalDecision(press, place, choices);

test("text typed outside the card never answers an approval", () => {
  for (const key of ["y", "Y", "a", "n", "Enter", "Escape", "Backspace", " "]) {
    assert.equal(decide({ key }, idle), undefined, key);
    assert.equal(decide({ key }, typing), undefined, key);
    assert.equal(decide({ key, shiftKey: true }, idle), undefined, key);
  }
});

test("Cmd or Ctrl with Enter or Backspace answers from anywhere except a field that holds text", () => {
  for (const command of [{ metaKey: true }, { ctrlKey: true }]) {
    assert.equal(decide({ key: "Enter", ...command }, idle), "once");
    assert.equal(decide({ key: "Enter", shiftKey: true, ...command }, idle), "session");
    assert.equal(decide({ key: "Backspace", ...command }, idle), "deny");
    assert.equal(decide({ key: "Enter", ...command }, card), "once");
    assert.equal(decide({ key: "Backspace", ...command }, button), "deny");
    // There the same keys send the draft and delete text.
    assert.equal(decide({ key: "Enter", ...command }, typing), undefined);
    assert.equal(decide({ key: "Enter", shiftKey: true, ...command }, typing), undefined);
    assert.equal(decide({ key: "Backspace", ...command }, typing), undefined);
    // Other shortcuts keep their meaning: select all, copy, the YOLO switch of the terminal.
    for (const key of ["a", "c", "y", "n", "Escape", "."]) assert.equal(decide({ key, ...command }, card), undefined, key);
    assert.equal(decide({ key: "Backspace", shiftKey: true, ...command }, idle), undefined);
  }
});

test("inside the card the terminal's keys decide, and Enter on a button presses that button", () => {
  assert.deepEqual(["y", "Y", "Enter", "a", "A", "n", "N", "Escape"].map(key => decide({ key }, card)),
    ["once", "once", "once", "session", "session", "deny", "deny", "deny"]);
  assert.deepEqual(["y", "a", "n", "Escape"].map(key => decide({ key }, button)), ["once", "session", "deny", "deny"]);
  assert.equal(decide({ key: "Enter" }, button), undefined);
  assert.equal(decide({ key: "Enter", shiftKey: true }, card), undefined);
  for (const key of ["x", "Tab", " ", "ArrowDown", "1"]) assert.equal(decide({ key }, card), undefined, key);
});

test("a held key, an input method and Alt combinations never decide", () => {
  for (const place of [card, idle]) {
    assert.equal(decide({ key: "Enter", metaKey: true, repeat: true }, place), undefined);
    assert.equal(decide({ key: "Enter", metaKey: true, isComposing: true }, place), undefined);
    assert.equal(decide({ key: "Enter", ctrlKey: true, altKey: true }, place), undefined);
  }
  assert.equal(decide({ key: "y", repeat: true }, card), undefined);
  assert.equal(decide({ key: "y", isComposing: true }, card), undefined);
  assert.equal(decide({ key: "n", altKey: true }, card), undefined);
});

test("only the choices a request offers can be given", () => {
  const takeover = ["once", "deny"];
  assert.equal(decide({ key: "a" }, card, takeover), undefined);
  assert.equal(decide({ key: "Enter", metaKey: true, shiftKey: true }, idle, takeover), undefined);
  assert.equal(decide({ key: "y" }, card, takeover), "once");
  assert.equal(decide({ key: "Backspace", ctrlKey: true }, idle, takeover), "deny");
  assert.equal(decide({ key: "y" }, card, ["deny"]), undefined);
});

test("a field counts as typing only while it holds text", () => {
  assert.equal(typingIn(null), false);
  assert.equal(typingIn({ value: "" }), false);
  assert.equal(typingIn({ value: "  \n" }), false);
  assert.equal(typingIn({ value: "草稿" }), true);
  assert.equal(typingIn({ textContent: "note" }), true);
  assert.equal(typingIn({ textContent: "" }), false);
});

test("the hint names the keys that work for this card, on this platform", () => {
  assert.equal(approvalKeyHint(all, true, true), "⌘ Enter 允许一次 · ⌘ Shift Enter 本会话允许 · ⌘ ⌫ 拒绝 · 点选卡片后可按 Y / A / N");
  assert.equal(approvalKeyHint(all, true, false), "Ctrl Enter 允许一次 · Ctrl Shift Enter 本会话允许 · Ctrl Backspace 拒绝 · 点选卡片后可按 Y / A / N");
  assert.equal(approvalKeyHint(["once", "deny"], true, false), "Ctrl Enter 允许一次 · Ctrl Backspace 拒绝 · 点选卡片后可按 Y / N");
  // A later request in the queue answers only to keys pressed inside it.
  assert.equal(approvalKeyHint(all, false, true), "点选卡片后可按 Y / A / N");
});

test("the card can take the focus and shows its keys; a later card does not claim the window's", () => {
  const event = { type: "tool_approval_request", request_id: "r1", tool_name: "write_file", reason: "写入文件", target: "/tmp/a.txt" };
  const render = (windowKeys: boolean) => renderToStaticMarkup(React.createElement(Approval, { event, send: async () => {}, windowKeys }));
  assert.match(render(true), /<section[^>]*tabindex="0"/);
  assert.match(render(true), /Enter 允许一次 · .*Shift Enter 本会话允许 · .*拒绝 · 点选卡片后可按 Y \/ A \/ N/);
  assert.match(render(false), /<p class="approval-keys[^"]*">点选卡片后可按 Y \/ A \/ N<\/p>/);
});
