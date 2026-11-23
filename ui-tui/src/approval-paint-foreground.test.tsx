import assert from "node:assert/strict";
import childProcess from "node:child_process";
import { EventEmitter } from "node:events";
import { syncBuiltinESMExports } from "node:module";
import { PassThrough, Writable } from "node:stream";
import test from "node:test";
import React from "react";
import { render } from "ink";
import { approvalPaintOutput } from "./approval-paint.js";
import { approvalAwareStdin } from "./foreground-approval-input.js";

const probe = "\x1b[?1004$p";

/** The real App on a mocked terminal whose write callbacks can be held, so a frame stays unacknowledged. */
async function mountForegroundCard() {
  class Input extends PassThrough { isTTY = true; setRawMode() {} ref() { return this; } unref() { return this; } }
  class Child extends EventEmitter { stdin = new PassThrough(); stdout = new PassThrough(); stderr = new PassThrough(); kill() {} }
  const child = new Child(), spawn = childProcess.spawn;
  let started = false;
  childProcess.spawn = (() => { started = true; return child; }) as unknown as typeof spawn;
  syncBuiltinESMExports();
  const stdin = new Input(), delivered: string[] = [], answers: string[] = [];
  child.stdin.on("data", data => { const cmd = JSON.parse(String(data)); if (cmd.type === "tool_approval_response") answers.push(cmd.decision); });
  const state: { hold?: string; flush?: () => void } = { hold: "审批请求" };
  const sink = Object.assign(new Writable({ write(chunk, _, done) {
    const text = String(chunk), deliver = () => { delivered.push(text); done(); };
    if (state.hold && text.includes(state.hold)) state.flush = deliver;
    else deliver();
  } }), { isTTY: true, rows: 36, columns: 120 });
  const key = (bytes: string) => { stdin.write(bytes); stdin.emit("readable"); };
  const until = async (check: () => boolean) => {
    const limit = performance.now() + 2000;
    while (!check() && performance.now() < limit) await new Promise(resolve => setTimeout(resolve, 2));
    assert.ok(check(), "expected mocked terminal transition");
  };
  /** Acknowledge the held frame, holding the next frame that contains `next`, if any. */
  const release = (next?: string) => { const deliver = state.flush!; state.flush = undefined; state.hold = next; deliver(); };
  const { default: App } = await import("./app.js");
  const app = render(<App appshotClientFactory={() => Object.assign(new EventEmitter(), {
    start() {}, close() {}, recordInput() {}, updateDraft() {}, activityNS: 0n,
  }) as any} />, { stdin: approvalAwareStdin(stdin as any), stdout: approvalPaintOutput(sink as any), stderr: sink as any,
    debug: true, patchConsole: false, exitOnCtrlC: false });
  const unmount = () => {
    state.hold = undefined; state.flush?.(); app.unmount();
    childProcess.spawn = spawn; syncBuiltinESMExports();
  };
  try {
    await until(() => started);
    child.stdout.write(JSON.stringify({ type: "tool_approval_request", request_id: "F", kind: "computer_foreground_takeover",
      tool_name: "computer_act", risk: "write", reason: "fixture", choices: ["once", "deny"] }) + "\n");
    await until(() => !!state.flush);
  } catch (error) { unmount(); throw error; }
  return { key, until, release, answers, delivered, state, unmount };
}

test("a foreground press started before paint cannot grant on its post-paint release", async () => {
  const down = "\x1b[121u", up = "\x1b[121;1:3u";
  const t = await mountForegroundCard();
  try {
    t.key(down); assert.ok(!t.delivered.some(s => s.includes(probe)));
    t.release("按下并松开");
    await t.until(() => t.delivered.some(s => s.includes(probe)));
    t.key("\x1b[?1004;2$y\x1b[?0u\x1b[?11u");
    await t.until(() => !!t.state.flush);
    t.key(up); assert.deepEqual(t.answers, []);
    t.key(down); // Keyboard mode now negotiated, but the usable shortcuts frame is still blocked.
    t.release();
    await new Promise(resolve => setImmediate(resolve));
    t.key(up); assert.deepEqual(t.answers, []);
    t.key(down + up); await t.until(() => t.answers.length !== 0); assert.deepEqual(t.answers, ["once"]);
  } finally { t.unmount(); }
});

test("without key-event reporting, a key read before the card is painted cannot answer it", async () => {
  const t = await mountForegroundCard();
  try {
    for (const typed of ["y", "\r"]) t.key(typed); // the frame with the choice line is still unacknowledged
    await new Promise(resolve => setTimeout(resolve, 30));
    assert.deepEqual(t.answers, []); assert.ok(!t.delivered.some(s => s.includes(probe)));
    t.release();
    await t.until(() => t.delivered.some(s => s.includes(probe))); // painted: the card now owns raw input
    assert.deepEqual(t.answers, []);
    t.key("y"); await t.until(() => t.answers.length !== 0); assert.deepEqual(t.answers, ["once"]);
  } finally { t.unmount(); }
});
