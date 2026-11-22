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

test("a foreground press started before paint cannot grant on its post-paint release", async () => {
  class Input extends PassThrough { isTTY = true; setRawMode() {} ref() { return this; } unref() { return this; } }
  class Child extends EventEmitter { stdin = new PassThrough(); stdout = new PassThrough(); stderr = new PassThrough(); kill() {} }
  const child = new Child(), spawn = childProcess.spawn;
  let started = false;
  childProcess.spawn = (() => { started = true; return child; }) as unknown as typeof spawn;
  syncBuiltinESMExports();
  const down = "\x1b[121u", up = "\x1b[121;1:3u", probe = "\x1b[?1004$p";
  const stdin = new Input(), delivered: string[] = [], answers: string[] = [];
  child.stdin.on("data", data => { const cmd = JSON.parse(String(data)); if (cmd.type === "tool_approval_response") answers.push(cmd.decision); });
  let hold: "first" | "shortcuts" | undefined = "first", flush: (() => void) | undefined;
  const sink = Object.assign(new Writable({ write(chunk, _, done) {
    const text = String(chunk), deliver = () => { delivered.push(text); done(); };
    if (hold === "first" && text.includes("审批请求") || hold === "shortcuts" && text.includes("按下并松开")) flush = deliver;
    else deliver();
  } }), { isTTY: true, rows: 36, columns: 120 });
  const key = (bytes: string) => { stdin.write(bytes); stdin.emit("readable"); };
  const until = async (check: () => boolean) => {
    const limit = performance.now() + 2000;
    while (!check() && performance.now() < limit) await new Promise(resolve => setTimeout(resolve, 2));
    assert.ok(check(), "expected mocked terminal transition");
  };
  let app: ReturnType<typeof render> | undefined;
  try {
    const { default: App } = await import("./app.js");
    app = render(<App appshotClientFactory={() => Object.assign(new EventEmitter(), {
      start() {}, close() {}, recordInput() {}, updateDraft() {}, activityNS: 0n,
    }) as any} />, { stdin: approvalAwareStdin(stdin as any), stdout: approvalPaintOutput(sink as any), stderr: sink as any,
      debug: true, patchConsole: false, exitOnCtrlC: false });
    await until(() => started);
    child.stdout.write(JSON.stringify({ type: "tool_approval_request", request_id: "F", kind: "computer_foreground_takeover",
      tool_name: "computer_act", risk: "write", reason: "fixture", choices: ["once", "deny"] }) + "\n");
    await until(() => !!flush);
    key(down); assert.ok(!delivered.some(s => s.includes(probe)));
    let deliver = flush!; flush = undefined; hold = "shortcuts"; deliver();
    await until(() => delivered.some(s => s.includes(probe)));
    key("\x1b[?1004;2$y\x1b[?0u\x1b[?11u");
    await until(() => !!flush);
    key(up); assert.deepEqual(answers, []);
    key(down); // Keyboard mode now negotiated, but the usable shortcuts frame is still blocked.
    deliver = flush!; flush = undefined; hold = undefined; deliver();
    await new Promise(resolve => setImmediate(resolve));
    key(up); assert.deepEqual(answers, []);
    key(down + up); await until(() => answers.length !== 0); assert.deepEqual(answers, ["once"]);
  } finally {
    hold = undefined; flush?.(); app?.unmount();
    childProcess.spawn = spawn; syncBuiltinESMExports();
  }
});
