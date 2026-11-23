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
  assert.match(approvalChoiceLabel("session", { kind: "computer_foreground_takeover" }), /此窗口.*2 分钟/);
  assert.equal(consequenceNote({}), "");
});

test("a draft grants only once; a withheld envelope never offers a positive choice, even on replay", () => {
  const stale = { choices: ["once", "session", "always", "deny"] };
  assert.deepEqual(approvalChoices({...stale, draft: {text: "message", cc: "copy", bcc: "blind"}}), ["once", "deny"]);
  for (const draft of [{text: "prefix", truncated: true}, {}, {text: "message", cc: null}, {text: "message", unshown: "field"}, null]) {
    assert.deepEqual(approvalChoices({...stale, draft} as any), ["deny"]);
  }
  for (const choices of [[], ["deny"], "deny"]) assert.deepEqual(approvalChoices({choices, draft: {text: "words"}} as any), ["deny"]);
});

test("non-mail destination bindings are an optional disclosed field, not another recipient or a new grant scope", () => {
  const card = { consequential: "send", choices: ["once", "session", "deny"] };
  const draft = { text: "Reply", via: "fixture/reply", destination: '{"comment_id":9876543210987654321,"owner":"team","repo":"notes"}' };
  assert.deepEqual(approvalChoices({ ...card, draft }), ["once", "deny"]);
  for (const destination of [{ repo: "notes" }, null, "", "repo:notes", "[]", "{}", '{"comment_id":{"id":4}}']) {
    assert.deepEqual(approvalChoices({ ...card, draft: { ...draft, destination } } as any), ["deny"]);
  }
  assert.deepEqual(approvalChoices({ ...card, draft: { text: "Mail", to: "person@example.invalid" } }), ["once", "deny"]);
  assert.deepEqual(approvalChoices(card).map(c => approvalChoiceLabel(c, card)), ["允许一次", "拒绝"]);
});

test("a background computer-write session is limited to this window and two minutes", () => {
  assert.equal(approvalChoiceLabel("session", { kind: "computer_write" }), "允许此窗口，最多 2 分钟");
  assert.equal(approvalChoiceLabel("session", { kind: "host_execution" }), "本会话允许");
});

test("a Save As intent is single-use and an incomplete disclosure cannot be approved", () => {
  const args = { source: "/local/Original.docx", directory: "/local", filename: "Copy.docx" };
  const card = { kind: "computer_foreground_takeover", choices: ["once", "session", "deny"] };
  assert.deepEqual(approvalChoices({ ...card, arguments: { save_as_intent: args } }), ["once", "deny"]);
  for (const value of [null, { ...args, filename: "" }, { ...args, truncated: true }, { filename: "Copy.docx" }]) {
    assert.deepEqual(approvalChoices({ ...card, arguments: { save_as_intent: value } }), ["deny"]);
  }
});
