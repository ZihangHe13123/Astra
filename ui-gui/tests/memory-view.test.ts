import { test } from "node:test";
import assert from "node:assert/strict";
import { coreRoom, filterMemories, kindLabel, proposalLabel, statusLabel, usedLabel, type CoreFile, type SavedMemory }
  from "../src/renderer/memory-view.js";

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
