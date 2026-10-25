import { test } from "node:test";
import assert from "node:assert/strict";
import { Attention, LONG_TURN_MS } from "../src/main/attention.js";

function fixture() {
  let now = 0;
  let focused = false;
  const attention = new Attention({ focused: () => focused, now: () => now });
  return { attention, later: (ms: number) => { now += ms; }, focus: (value: boolean) => { focused = value; } };
}

test("a long reply that finishes while the window is in the background is announced once", () => {
  const { attention, later } = fixture();
  assert.equal(attention.session("a", "", true, { type: "task_started" }), undefined);
  later(LONG_TURN_MS);
  assert.equal(attention.session("a", "", true, { type: "chunk" }), undefined, "still working");
  assert.deepEqual(attention.session("a", "", false, { type: "done" }),
    { title: "Astra 回复好了", body: "回到对话看看吧", runtime: "a" });
  assert.equal(attention.session("a", "", false, { type: "done" }), undefined, "only once");
});

test("a short reply, or any reply while the window is in front, is not announced", () => {
  const { attention, later, focus } = fixture();
  attention.session("a", "", true, { type: "task_started" });
  later(LONG_TURN_MS - 1);
  assert.equal(attention.session("a", "", false, { type: "done" }), undefined);
  focus(true);
  attention.session("a", "", true, { type: "task_started" });
  later(LONG_TURN_MS * 3);
  assert.equal(attention.session("a", "", false, { type: "done" }), undefined);
  assert.equal(attention.session("a", "", true, { type: "tool_approval_request", approval_title: "x" }), undefined);
});

test("approvals and questions in the window ask for the person; a WeChat approval does not", () => {
  const { attention } = fixture();
  assert.deepEqual(attention.session("a", "", true, { type: "tool_approval_request", approval_title: "新建定时任务：喝水" }),
    { title: "Astra 需要你批准", body: "新建定时任务：喝水", runtime: "a" });
  assert.equal(attention.session("a", "", true, { type: "tool_approval_request", approval_title: "x", channel: "weixin" }), undefined);
  assert.deepEqual(attention.session("a", "", true, { type: "user_question_request", questions: [{ question: "用哪个模型？" }] }),
    { title: "Astra 有问题想问你", body: "用哪个模型？", runtime: "a" });
});

test("the background host's job results become notifications, clipped", () => {
  const { attention } = fixture();
  assert.deepEqual(attention.host({ type: "job_notification", title: "定时任务：Rust 早报", body: "两条新讨论" }),
    { title: "定时任务：Rust 早报", body: "两条新讨论" });
  assert.equal(attention.host({ type: "weixin_status" }), undefined);
  const long = attention.host({ type: "job_notification", title: "t", body: "字".repeat(500) });
  assert.ok(long && long.body.length === 160 && long.body.endsWith("…"));
});
