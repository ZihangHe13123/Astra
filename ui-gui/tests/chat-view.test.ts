import { test } from "node:test";
import assert from "node:assert/strict";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { activitySummary, chatRows, chatTime, jobResult, managedBy, CHAT_GAP_SECONDS } from "../src/renderer/chat-view.js";
import { initialSession } from "@astra/ui-core/session-state";
import { MessageRow } from "../src/renderer/messages.js";
import type { Row } from "../src/renderer/activity-state.js";

const at = (text: string) => new Date(text).getTime() / 1000;
const now = new Date("2026-10-27T10:30:00").getTime();

test("a chat heads each pause with its time, today, yesterday or a date, and drops file-change notes", () => {
  assert.equal(chatTime(at("2026-10-27T08:05:00"), now), "08:05");
  assert.equal(chatTime(at("2026-10-26T20:00:00"), now), "昨天 20:00");
  assert.equal(chatTime(at("2026-10-24T09:00:00"), now), "10月24日 09:00");
  const rows: Row[] = [
    { id: "a", role: "assistant", content: "早报", timestamp: at("2026-10-27T08:30:00") },
    { id: "b", role: "user", content: "第二条呢", timestamp: at("2026-10-27T08:31:00") },
    { id: "activity:b", role: "activity", content: "", tools: [] },
    { id: "changes:r:1", role: "changes", content: "" },
    { id: "c", role: "assistant", content: "是这样", timestamp: at("2026-10-27T08:32:00") },
    { id: "d", role: "user", content: "好", timestamp: at("2026-10-27T08:32:00") + CHAT_GAP_SECONDS },
  ];
  assert.deepEqual(chatRows(rows, now).map(row => [row.id, row.role === "time" ? row.content : row.role]), [
    ["time:a", "08:30"], ["a", "assistant"], ["b", "user"], ["activity:b", "activity"], ["c", "assistant"],
    ["time:d", "08:42"], ["d", "user"]]);
});

test("a job's result is read back from the words the thread keeps, and nothing else is", () => {
  assert.deepEqual(jobResult("【定时任务：AI 晚报】\n三条新闻\n第二行"), { title: "定时任务：AI 晚报", body: "三条新闻\n第二行" });
  assert.deepEqual(jobResult("【提醒：喝水】\n该喝水了"), { title: "提醒：喝水", body: "该喝水了" });
  assert.equal(jobResult("【重点】\n这不是任务结果"), undefined);
  assert.equal(jobResult("定时任务：AI 晚报"), undefined);
});

test("a step's tool calls read as one line, saying when some still run or failed", () => {
  const tool = (status: string) => ({ type: "tool_result", status });
  assert.equal(activitySummary([tool("completed"), tool("completed")]), "用了 2 个工具");
  assert.equal(activitySummary([tool("completed"), tool("failed")]), "用了 2 个工具，1 个出错");
  assert.equal(activitySummary([tool("completed"), tool("running")]), "正在使用工具（1/2）");
});

test("in the chat a job result is a card, a step is one line, and Astra's words carry an avatar", () => {
  const render = (message: Row, chat = true) => renderToStaticMarkup(React.createElement(MessageRow, {
    message, timeline: true, reasoning: true, expanded: new Map(), fail: () => {}, chat }));
  const job = render({ id: "j", role: "assistant", content: "【定时任务：AI 晚报】\n" + "新闻。".repeat(200) });
  assert.match(job, /class="message assistant job-card"/);
  assert.match(job, /定时任务：AI 晚报/);
  assert.match(job, /class="message-body clamped"/);
  assert.match(job, /展开全文/);
  const step = render({ id: "activity:x", role: "activity", content: "", tools: [{ type: "tool_result", name: "search_web", status: "completed" }] });
  assert.match(step, /用了 1 个工具/);
  assert.doesNotMatch(step, /search_web/, "the calls themselves open on demand");
  assert.match(render({ id: "a", role: "assistant", content: "你好" }), /chat-avatar/);
  assert.match(render({ id: "t", role: "time", content: "08:30" }), /role="separator">08:30</);
  // Outside the chat the same rows look as before.
  assert.doesNotMatch(render({ id: "j", role: "assistant", content: "【定时任务：AI 晚报】\n新闻" }, false), /job-card|chat-avatar/);
});

test("a conversation knows the personal thread manages it while the task it was handed is open", () => {
  const state = initialSession("w");
  assert.equal(managedBy(state), undefined);
  const task = { task_id: "mac:t-1", title: "查一下为什么测试失败", state: "working", role: "assignee", with: "个人对话" };
  state.info = { peer_tasks: { type: "peer_tasks", tasks: [{ ...task, role: "requester", with: "别的会话" }, task] } };
  assert.deepEqual(managedBy(state), task);
  state.info = { peer_tasks: { type: "peer_tasks", tasks: [{ ...task, with: "做PPT" }] } };
  assert.equal(managedBy(state), undefined, "a task from another session is not the thread managing it");
});
