import { test } from "node:test";
import assert from "node:assert/strict";
import { coreRoom, filterMemories, kindLabel, proposalLabel, sizeLabel, statusLabel, usedLabel, wipeSummary, type CoreFile,
  type DataPlan, type SavedMemory } from "../src/renderer/memory-view.js";

const memory = (fields: Partial<SavedMemory>) => ({ id: "m", summary: "喜欢简短的回答", content: "回答尽量简短", kind: "preference",
  status: "active", scope: "global", saved_by_astra: true, created_at: "2026-10-25T08:00:00", valid_until: "", supersedes: "",
  source_session: "", uses: 0, last_used_at: "", ...fields } as SavedMemory);

test("a core file's room warns past nine tenths", () => {
  const file = (chars: number) => ({ scope: "user", file: "USER.md", entries: [], usage: { chars, limit: 1375, available_chars: 0, over_limit: false } } as CoreFile);
  assert.deepEqual(coreRoom(file(1346)), { percent: 98, label: "1346 / 1375 字", nearlyFull: true });
  assert.equal(coreRoom(file(600)).nearlyFull, false);
  assert.equal(coreRoom({ ...file(0), usage: null }).label, "还没有内容");
});

test("saved memories read as the person would name them", () => {
  assert.equal(kindLabel("user_fact"), "关于你");
  assert.equal(kindLabel("preference"), "偏好");
  assert.equal(statusLabel("active"), "");
  assert.equal(statusLabel("superseded"), "已被新版本替换");
  assert.equal(usedLabel(memory({})), "还没用到过");
  assert.equal(usedLabel(memory({ uses: 3, last_used_at: "2026-10-24T10:00:00" })), "用到 3 次，最近 2026-10-24");
  assert.equal(proposalLabel({ action: "revise" } as never), "更新");
});

test("the filter matches summaries, full text and kinds", () => {
  const memories = [memory({ id: "a" }), memory({ id: "b", summary: "住在吉隆坡", content: "搬家了", kind: "user_fact" })];
  assert.deepEqual(filterMemories(memories, "简短").map(item => item.id), ["a"]);
  assert.deepEqual(filterMemories(memories, "关于你").map(item => item.id), ["b"]);
  assert.equal(filterMemories(memories, "  ").length, 2);
});

test("sizes read in the unit that fits", () => {
  assert.deepEqual([0, 900, 2048, 5 * 1024 * 1024 + 1, 300 * 1024 * 1024, 3 * 1024 ** 3].map(sizeLabel),
    ["0 B", "900 B", "2.0 KB", "5.0 MB", "300 MB", "3.0 GB"]);
});

test("a wipe lists what is there once each, and browser logins only when chosen", () => {
  const item = (key: string, label: string, bytes: number, fields = {}) =>
    ({ key, label, path: `/x/${key}`, present: true, bytes, export: true, browser: false, ...fields });
  const plan: DataPlan = { confirmation: "清除", kept: [], not_exported: [], items: [
    item("memory", "已保存的记忆", 1024), item("vectors:a", "对话的向量索引", 512), item("vectors:b", "对话的向量索引", 512),
    item("tasks", "任务和步骤记录", 0, { present: false }), item("browser-profiles", "Astra 浏览器的登录和 Cookie", 1024, { browser: true }),
  ] };
  assert.deepEqual(wipeSummary(plan, false), { labels: ["已保存的记忆", "对话的向量索引"], size: "2.0 KB" });
  assert.deepEqual(wipeSummary(plan, true).labels.at(-1), "Astra 浏览器的登录和 Cookie");
});
