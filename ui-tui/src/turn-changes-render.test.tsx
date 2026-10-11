import assert from "node:assert/strict";
import test from "node:test";
import childProcess from "node:child_process";
import { syncBuiltinESMExports } from "node:module";
import { EventEmitter } from "node:events";
import { PassThrough, Writable } from "node:stream";
import React from "react";
import { render } from "ink";
import stripAnsi from "strip-ansi";

class FakeChild extends EventEmitter {
  exitCode: number | null = null;
  signalCode: string | null = null;
  kills = 0;
  stdin = new PassThrough();
  stdout = new PassThrough();
  stderr = new PassThrough();
  commands: any[] = [];
  constructor() { super(); this.stdin.on("data", (data) => this.commands.push(JSON.parse(String(data)))); }
  kill() { this.kills++; return true; }
  event(event: object) { this.stdout.write(JSON.stringify(event) + "\n"); }
}
const children: FakeChild[] = [];
childProcess.spawn = (() => { const child = new FakeChild(); children.push(child); return child; }) as unknown as typeof childProcess.spawn;
childProcess.execFile = (() => { throw new Error("render tests must never execute an installed Appshot helper"); }) as unknown as typeof childProcess.execFile;
syncBuiltinESMExports();
process.env.TUI_STARTUP_ANIMATION = "0";
const { default: App } = await import("./app.js");

class Input extends PassThrough { isTTY = true; setRawMode() {} ref() { return this; } unref() { return this; } }
class Output extends Writable {
  columns = 90; rows = 30; isTTY = true; chunks: string[] = [];
  _write(data: Buffer, _: BufferEncoding, done: () => void) { this.chunks.push(String(data)); done(); }
}
const settle = () => new Promise((resolve) => setTimeout(resolve, 60));

async function setup(columns = 90, rows = 30) {
  const stdin = new Input(); const stdout = new Output();
  stdout.columns = columns; stdout.rows = rows;
  const app = render(<App />, { stdin: stdin as any, stdout: stdout as any, stderr: stdout as any, debug: true, patchConsole: false, exitOnCtrlC: false });
  await settle();
  return {
    app,
    stdin,
    stdout,
    child: children.at(-1)!,
    display: () => stdout.chunks.map(stripAnsi).join(""),
    latestFrame: () => stdout.chunks.map(stripAnsi).filter((chunk) => chunk.trim()).at(-1) ?? "",
  };
}

const file = (path: string, reason = "") => ({ path, state: "modified", added: 4, removed: 1, compare: "ok", reason });
const occurrences = (haystack: string, needle: string) => haystack.split(needle).length - 1;
const turnChanges = (overrides: Record<string, unknown> = {}) => ({
  type: "turn_changes",
  session_id: "sess-a",
  request_id: "req-1",
  turn_seq: 3,
  files: [file("src/a.ts"), file("src/b.ts"), file("src/c.ts")],
  unknown_count: 0,
  totals: { files: 3, added: 42, removed: 7 },
  ...overrides,
});
const SESSION_INFO = { type: "session_info", name: "sess-a", messages: 1 };

test("turn_changes renders one summary line with totals", async () => {
  const h = await setup();
  try {
    h.child.event(SESSION_INFO);
    await settle();
    h.child.event(turnChanges());
    await settle();
    h.child.event({ type: "done" });
    await settle();
    const display = h.display();
    assert.match(display, /✎ 本轮改动 3 个文件  \+42 −7\s+\/changes 查看/, display);
    assert.equal(occurrences(h.latestFrame(), "✎ 本轮"), 1, h.latestFrame());
    // Each file has its own row under the totals.
    for (const [mark, path] of [["├", "src/a.ts"], ["├", "src/b.ts"], ["└", "src/c.ts"]]) {
      assert.match(h.latestFrame(), new RegExp(`${mark} M ${path.replace("/", "\\/").replace(".", "\\.")}\\s+\\+4 −1`), h.latestFrame());
    }
  } finally { h.app.unmount(); }
});

test("turn_changes with no files and no unknown renders nothing", async () => {
  const h = await setup();
  try {
    h.child.event(SESSION_INFO);
    await settle();
    h.child.event(turnChanges({ files: [], unknown_count: 0, totals: { files: 0, added: 0, removed: 0 } }));
    await settle();
    h.child.event({ type: "done" });
    await settle();
    const display = h.display();
    assert.ok(!display.includes("✎"), display);
  } finally { h.app.unmount(); }
});

test("turn_changes with unknown only renders the confirmation hint", async () => {
  const h = await setup();
  try {
    h.child.event(SESSION_INFO);
    await settle();
    h.child.event(turnChanges({ files: [], unknown_count: 2, totals: { files: 0, added: 0, removed: 0 } }));
    await settle();
    h.child.event({ type: "done" });
    await settle();
    const display = h.display();
    assert.match(display, /✎ 另有 2 个路径未能确认\s+\/changes 查看/, display);
    assert.equal(occurrences(h.latestFrame(), "✎"), 1, h.latestFrame());
  } finally { h.app.unmount(); }
});

test("cancelled turn still renders files and unknown hint", async () => {
  const h = await setup();
  try {
    h.child.event(SESSION_INFO);
    await settle();
    h.child.event(turnChanges({
      files: [file("src/a.ts", "cancelled"), file("src/b.ts", "cancelled")],
      unknown_count: 1,
      totals: { files: 2, added: 42, removed: 7 },
    }));
    await settle();
    h.child.event({ type: "done" });
    await settle();
    const display = h.display();
    assert.match(display, /✎ 本轮改动 2 个文件  \+42 −7 · 另有 1 个路径未能确认\s+\/changes 查看/, display);
    assert.equal(occurrences(h.latestFrame(), "✎ 本轮"), 1, h.latestFrame());
  } finally { h.app.unmount(); }
});

test("duplicate turn_changes renders once, a new turn_seq renders again", async () => {
  const h = await setup();
  try {
    h.child.event(SESSION_INFO);
    await settle();
    const payload = turnChanges();
    h.child.event(payload);
    await settle();
    h.child.event(payload);
    await settle();
    assert.equal(occurrences(h.latestFrame(), "✎ 本轮"), 1, h.latestFrame());
    h.child.event(turnChanges({ turn_seq: 4, request_id: "req-2" }));
    await settle();
    assert.equal(occurrences(h.latestFrame(), "✎ 本轮"), 2, h.latestFrame());
  } finally { h.app.unmount(); }
});

test("turn_changes from another session is ignored until the current session reports", async () => {
  const h = await setup();
  try {
    h.child.event(SESSION_INFO);
    await settle();
    h.child.event(turnChanges({ session_id: "sess-b", request_id: "req-b", turn_seq: 9 }));
    await settle();
    assert.ok(!h.display().includes("✎"), h.display());
    h.child.event(turnChanges());
    await settle();
    const display = h.display();
    assert.match(display, /✎ 本轮改动 3 个文件  \+42 −7\s+\/changes 查看/, display);
    assert.equal(occurrences(h.latestFrame(), "✎ 本轮"), 1, h.latestFrame());
  } finally { h.app.unmount(); }
});
