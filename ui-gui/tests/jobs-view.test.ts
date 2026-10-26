import { test } from "node:test";
import assert from "node:assert/strict";
import { dayLabel, groupByDay, jobState, resultLabel, resultText, sourceLabel, targetsLabel, timeLabel, type InboxRow, type JobRow }
  from "../src/renderer/jobs-view.js";

const noon = new Date(2026, 9, 26, 12, 0).getTime();
const at = (day: number, hour: number, minute = 0) => new Date(2026, 9, day, hour, minute).getTime() / 1000;

test("times read as today, yesterday, tomorrow or a date", () => {
  assert.equal(dayLabel(at(26, 8), noon), "今天");
  assert.equal(dayLabel(at(25, 23), noon), "昨天");
  assert.equal(dayLabel(at(27, 7, 30), noon), "明天");
  assert.equal(timeLabel(at(24, 9, 5), noon), "10月24日 09:05");
});

test("a job's state names why it was paused", () => {
  const job = (state: string, paused_reason = "") => ({ state, paused_reason } as JobRow);
  assert.deepEqual(jobState(job("enabled")), { label: "启用中", tone: "on" });
  assert.equal(jobState(job("paused", "failures")).label, "已暂停：连续 3 次没能完成");
  assert.equal(jobState(job("paused", "inactive")).label, "已暂停：7 天没有互动");
  assert.equal(jobState(job("paused")).label, "已暂停");
  assert.equal(jobState(job("completed")).label, "已完成");
});

test("results say what happened and where they went", () => {
  const row = (fields: Partial<InboxRow>) => ({ kind: "check", state: "completed", quiet: false, output: "两条新讨论",
    detail: "", skipped_count: 0, ...fields } as InboxRow);
  assert.equal(resultLabel(row({})), "结果");
  assert.equal(resultLabel(row({ quiet: true })), "没有新内容");
  assert.equal(resultLabel(row({ detail: "iteration_budget" })), "结果（轮数用完，可能不全）");
  assert.equal(resultText(row({ quiet: true })), "和上次相比没有变化，没有打扰你。");
  assert.equal(resultLabel(row({ kind: "reminder" })), "提醒");
  assert.equal(resultLabel(row({ state: "skipped", skipped_count: 3 })), "错过 3 次");
  assert.equal(resultLabel(row({ state: "failed", detail: "provider_error" })), "没能完成");
  assert.equal(resultText(row({ state: "failed", detail: "provider_error" })), "provider_error");
  assert.equal(targetsLabel(["desktop", "weixin"]), "发到桌面通知和微信");
  assert.equal(targetsLabel([]), "只进收件箱");
  assert.equal(sourceLabel("weixin"), "微信");
  assert.equal(sourceLabel(""), "对话");
});

test("activity groups by day, newest first", () => {
  const groups = groupByDay([{ time: at(25, 9) }, { time: at(26, 8) }, { time: at(26, 11) }, { time: at(24, 20) }], noon);
  assert.deepEqual(groups.map(group => [group.label, group.entries.length]), [["今天", 2], ["昨天", 1], ["10月24日", 1]]);
  assert.equal(groups[0].entries[0].time, at(26, 11));
});
