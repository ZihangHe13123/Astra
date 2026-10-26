import { test } from "node:test";
import assert from "node:assert/strict";
import { tokenLabel, usageStatus } from "../src/renderer/usage-view.js";

test("today's usage shows only against a budget, in its own unit", () => {
  const base = { tokens: 620_000, cost: 3.1, currency: "¥", over_budget: false };
  assert.equal(usageStatus({ ...base, budget: {} }), undefined);
  assert.deepEqual(usageStatus({ ...base, budget: { daily_tokens: 1_000_000 } }), { text: "今日 620k / 1.0M token", over: false });
  assert.deepEqual(usageStatus({ ...base, budget: { daily_cost: 5 } }), { text: "今日 ¥3.10 / ¥5", over: false });
  assert.deepEqual(usageStatus({ ...base, cost: null, budget: { daily_cost: 5 }, over_budget: true }), { text: "今日 ¥0.00 / ¥5", over: true });
  assert.equal(tokenLabel(950), "950");
});
