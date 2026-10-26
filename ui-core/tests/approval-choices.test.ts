import { test } from "node:test";
import assert from "node:assert/strict";
import { approvalChoiceLabel, approvalChoices, consequenceNote } from "../src/approval-choices.js";

test("a card shows what it offers, and an old request keeps its three answers", () => {
  assert.deepEqual(approvalChoices({ choices: ["once", "task", "session", "always", "deny"] }), ["once", "task", "session", "always", "deny"]);
  assert.deepEqual(approvalChoices({}), ["once", "session", "deny"]);
  assert.deepEqual(approvalChoices({ choices: ["once", "bogus", "deny"] }), ["once", "deny"]);
});

test("a consequential request is decided per call, and a draft reads as send or discard", () => {
  const send = { choices: ["once", "session", "deny"], consequential: "send", draft: { to: "张三", text: "明天见" } };
  assert.deepEqual(approvalChoices(send), ["once", "deny"]);
  assert.deepEqual(approvalChoices(send).map(choice => approvalChoiceLabel(choice, send)), ["发送", "丢弃"]);
  assert.equal(consequenceNote(send), "发送类操作：每次都会单独问你，之前给过的授权不覆盖它。");
  assert.equal(approvalChoiceLabel("task", {}), "本任务允许");
  assert.equal(consequenceNote({}), "");
});
