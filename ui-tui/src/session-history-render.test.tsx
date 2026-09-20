import assert from "node:assert/strict";
import test from "node:test";
import childProcess from "node:child_process";
import { syncBuiltinESMExports } from "node:module";
import { EventEmitter } from "node:events";
import { PassThrough, Writable } from "node:stream";
import React from "react";
import { render } from "ink";
import stripAnsi from "strip-ansi";

class FakeBackend extends EventEmitter {
  exitCode = null;
  signalCode = null;
  stdin = new PassThrough();
  stdout = new PassThrough();
  stderr = new PassThrough();
  commands: { type: string; cmd?: string; text?: string }[] = [];
  constructor() {
    super();
    this.stdin.on("data", data => this.commands.push(JSON.parse(String(data))));
  }
  kill() { return true; }
  event(event: object) { this.stdout.write(JSON.stringify(event) + "\n"); }
}
let backend: FakeBackend;
childProcess.spawn = ((_command: string, args: string[]) => {
  assert.deepEqual(args, ["-m", "agent.cli.backend"]);
  backend = new FakeBackend();
  return backend;
}) as unknown as typeof childProcess.spawn;
childProcess.execFile = (() => { throw new Error("History tests must not run Appshot"); }) as unknown as typeof childProcess.execFile;
syncBuiltinESMExports();
process.env.TUI_STARTUP_ANIMATION = "0";
const { default: App } = await import("./app.js");

class Input extends PassThrough {
  isTTY = true;
  setRawMode() {}
  ref() { return this; }
  unref() { return this; }
}
class Output extends Writable {
  columns = 143;
  rows = 40;
  isTTY = true;
  chunks: string[] = [];
  _write(data: Buffer, _: BufferEncoding, done: () => void) {
    this.chunks.push(String(data));
    done();
  }
}
function appshot() {
  return Object.assign(new EventEmitter(), {
    state: { enabled: false, registration: "disabled", connectedTuis: 0, permission: "ready" },
    start: async () => {}, close: async () => {}, recordInput: () => {}, updateDraft: () => {}, release: () => {},
  }) as any;
}
const settle = () => new Promise(resolve => setTimeout(resolve, 60));

test("session replacement prints each restored message once, in order", async () => {
  const stdin = new Input();
  const stdout = new Output();
  // Debug mode reprints entire frames, hiding irreversible Static writes.
  const app = render(<App appshotClientFactory={appshot} />, {
    stdin: stdin as any, stdout: stdout as any, stderr: stdout as any,
    debug: false, patchConsole: false, exitOnCtrlC: false,
  });
  const captured = () => stripAnsi(stdout.chunks.join(""));
  const submit = async (text: string) => {
    stdin.write(text); await settle(); stdin.write("\r"); await settle();
  };
  const expectOnceInOrder = (markers: string[]) => {
    const output = captured();
    let previous = -1;
    for (const marker of markers.flatMap(content => content.split(/\n+/u))) {
      assert.equal(output.split(marker).length - 1, 1, `${marker} must print once`);
      const index = output.indexOf(marker);
      assert.ok(index > previous, `${marker} must follow the previous message`);
      previous = index;
    }
  };
  const restore = async (session: string, markers: string[]) => {
    await submit(`/session ${session}`);
    assert.deepEqual(backend.commands.at(-1), { type: "command", cmd: `/session ${session}` });
    stdout.chunks = [];
    backend.event({ type: "history", session_id: session, messages: markers.map((content, index) => ({
      role: index % 2 === 0 ? "user" : "assistant", content,
      timestamp: 1_789_876_091 + index * 32,
    })) });
    await settle();
    backend.event({ type: "tool_result", name: "session", output: `Switched to session '${session}'`, error: "" });
    backend.event({ type: "done" });
    await settle();
    expectOnceInOrder(markers);
  };
  try {
    await settle();
    backend.event({ type: "model_info", model: "test", total_tokens: 0, prompt_tokens: 0, completion_tokens: 0, context_pct: 0 });
    await settle();
    await restore("greeting", ["USER_GREETING", "ASSISTANT_GREETING\n\nSECOND_PARAGRAPH"]);
    await restore("short", ["SHORT_USER"]);
    await restore("long", ["FIRST_USER", "FIRST_REPLY", "SECOND_USER", "SECOND_REPLY", "THIRD_USER", "THIRD_REPLY"]);
    await restore("empty", []);
    await restore("greeting", ["USER_GREETING", "ASSISTANT_GREETING\n\nSECOND_PARAGRAPH"]);

    await submit("/reset");
    assert.deepEqual(backend.commands.at(-1), { type: "command", cmd: "/reset" });
    backend.event({ type: "done" });
    await settle();
    stdout.chunks = [];
    backend.event({ type: "chunk", content: "FRESH_REPLY_AFTER_RESET" });
    backend.event({ type: "done", content: "FRESH_REPLY_AFTER_RESET" });
    await settle();
    expectOnceInOrder(["FRESH_REPLY_AFTER_RESET"]);
    assert.doesNotMatch(captured(), /USER_GREETING|ASSISTANT_GREETING|THIRD_REPLY/);
  } finally { app.unmount(); }
});
