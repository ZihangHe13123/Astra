import test from "node:test";
import assert from "node:assert/strict";
import { compactionNotice } from "../src/context-compaction.js";
import type { ContextCompactionEvent } from "../src/types.js";

test("compaction has start, result, failure and cancellation notices", () => {
  const event: ContextCompactionEvent = { type: "context_compaction", status: "started", messages_before: 168, messages_after: 85 };
  assert.equal(compactionNotice(event), "正在压缩上下文…");
  assert.match(compactionNotice({ ...event, status: "completed" })!, /168 → 85/);
  assert.equal(compactionNotice({ ...event, status: "failed" }), "上下文压缩未完成。");
  assert.equal(compactionNotice({ ...event, status: "cancelled" }), "上下文压缩已取消。");
});

test("invalid compaction events cannot inject terminal text", () => {
  const event: ContextCompactionEvent = { type: "context_compaction", status: "completed", messages_before: 10, messages_after: 5 };
  assert.equal(compactionNotice({ ...event, status: "\x1b[2J" as any }), null);
  for (const count of [-1, 1.5, NaN, Infinity, "\x1b[2J"]) {
    assert.equal(compactionNotice({ ...event, messages_after: count as any }), null);
  }
});

test("cleanup reports token savings even when message counts barely change", () => {
  const event: ContextCompactionEvent = {
    type: "context_compaction", status: "completed", method: "cleanup",
    messages_before: 108, messages_after: 106, tokens_before: 468256, tokens_after: 445137, target_tokens: 450000,
  };
  assert.equal(compactionNotice(event), "上下文轻量清理完成：估算 468,256 → 445,137 tokens，释放 4.9%；消息 108 → 106。");
  assert.match(compactionNotice({ ...event, messages_after: 108 })!, /消息 108 → 108/);
});

test("history compression reports insufficient savings without claiming the target was met", () => {
  const event: ContextCompactionEvent = {
    type: "context_compaction", status: "completed", method: "summary",
    messages_before: 100, messages_after: 20, tokens_before: 10000, tokens_after: 9900, target_tokens: 9000,
  };
  assert.match(compactionNotice(event)!, /历史压缩.*仍高于目标 9,000/);
  assert.match(compactionNotice({ ...event, tokens_after: 10000 })!, /未减少/);
  assert.match(compactionNotice({ ...event, tokens_after: 11000 })!, /增加 10\.0%/);
});

test("invalid optional metrics fall back to counts without printing untrusted text", () => {
  const event: ContextCompactionEvent = { type: "context_compaction", status: "completed", messages_before: 10, messages_after: 5 };
  for (const invalid of [-1, NaN, Infinity, 1.5, "\x1b[2J"]) {
    assert.equal(compactionNotice({ ...event, tokens_before: invalid as any, tokens_after: 1, method: "cleanup" }), compactionNotice(event));
  }
  const text = compactionNotice({ ...event, tokens_before: 100, tokens_after: 50, method: "\x1b[2J" as any, target_tokens: "\x1b[2J" as any })!;
  assert.doesNotMatch(text, /\x1b/);
  assert.match(text, /100 → 50 tokens/);
});

test("a window shows compaction while it runs and keeps its result in the conversation", async () => {
  const { initialSession, projectEvent } = await import("../src/session-state.js");
  const started: ContextCompactionEvent = { type: "context_compaction", status: "started", messages_before: 40, messages_after: 40 };
  let state = projectEvent(initialSession("fixture"), started as never);
  assert.equal(state.info.context_compaction?.status, "started");
  assert.equal(state.messages.length, 0, "the running state is a status, not a message");
  state = projectEvent(state, { ...started, status: "completed", messages_after: 12, method: "summary",
    tokens_before: 90000, tokens_after: 20000 } as never);
  assert.equal(state.info.context_compaction?.status, "completed");
  assert.deepEqual(state.messages.map(message => [message.role, message.content]),
    [["system", "上下文历史压缩完成：估算 90,000 → 20,000 tokens，释放 77.8%；消息 40 → 12。"]]);
  state = projectEvent(state, { ...started, status: "failed" } as never);
  assert.equal(state.messages.at(-1)?.content, "上下文压缩未完成。");
});
