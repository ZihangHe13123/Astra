import assert from "node:assert/strict";
import test from "node:test";
import childProcess from "node:child_process";
import { syncBuiltinESMExports } from "node:module";
import { EventEmitter } from "node:events";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { PassThrough, Writable } from "node:stream";
import React from "react";
import stripAnsi from "strip-ansi";

class FakeChild extends EventEmitter {
  exitCode: number | null = null;
  signalCode: string | null = null;
  stdin = new PassThrough();
  stdout = new PassThrough();
  stderr = new PassThrough();
  constructor() { super(); this.stdin.resume(); }
  kill() { return true; }
  event(event: object) { this.stdout.write(JSON.stringify(event) + "\n"); }
}
const children: FakeChild[] = [];
childProcess.spawn = (() => { const child = new FakeChild(); children.push(child); return child; }) as unknown as typeof childProcess.spawn;
childProcess.execFile = (() => { throw new Error("render tests must never execute an installed Appshot helper"); }) as unknown as typeof childProcess.execFile;
syncBuiltinESMExports();
process.env.TUI_STARTUP_ANIMATION = "0";
process.env.ASTRA_HOME = mkdtempSync(join(tmpdir(), "astra-tool-viewer-"));
// On a CI machine Ink writes no live frame at all, only static output, and these tests are
// about the live frame. Ink reads the environment once, when it is loaded, so it is told
// before then that this is an interactive terminal.
process.env.CI = "false";
const { render } = await import("ink");
const { default: App } = await import("./app.js");

class Input extends PassThrough { isTTY = true; setRawMode() {} ref() { return this; } unref() { return this; } }
class Output extends Writable {
  isTTY = true; chunks: string[] = [];
  constructor(public columns: number, public rows: number, private readonly sink?: (data: string) => void) { super(); }
  _write(data: Buffer, _: BufferEncoding, done: () => void) { this.chunks.push(String(data)); this.sink?.(String(data)); done(); }
}
const wait = (ms = 80) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * As much of a terminal as these tests need: a main and an alternate screen, a cursor, the
 * erasing and cursor moves Ink writes, and scrolling. Enough to ask what a person would see.
 */
class Terminal {
  private readonly screens = { main: [] as string[], alt: [] as string[] };
  private readonly cursors = { main: { row: 0, col: 0 }, alt: { row: 0, col: 0 } };
  active: "main" | "alt" = "main";
  constructor(private readonly rows: number) {
    for (const lines of Object.values(this.screens)) for (let row = 0; row < rows; row += 1) lines.push("");
  }
  get lines(): string[] { return this.screens[this.active].map((line) => line.trimEnd()); }
  get text(): string { return this.lines.join("\n"); }
  rowOf(needle: string | RegExp): number {
    return this.lines.findIndex((line) => typeof needle === "string" ? line.includes(needle) : needle.test(line));
  }
  write(data: string): void {
    const pattern = /\x1b\[(\??)([0-9;]*)([A-Za-z])|\x1b\][^\x07]*\x07|\r|\n|[^\x1b\r\n]+/gu;
    for (const [token, mark, numbers, final] of data.matchAll(pattern)) {
      const lines = this.screens[this.active];
      const cursor = this.cursors[this.active];
      const count = Number((numbers ?? "").split(";")[0] || 1);
      if (token === "\n") {
        cursor.col = 0;
        if (cursor.row === this.rows - 1) { lines.shift(); lines.push(""); } else cursor.row += 1;
      } else if (token === "\r") cursor.col = 0;
      else if (final === undefined) {
        if (!token.startsWith("\x1b")) {
          lines[cursor.row] = lines[cursor.row].padEnd(cursor.col).slice(0, cursor.col) + token + lines[cursor.row].slice(cursor.col + token.length);
          cursor.col += token.length;
        }
      } else if (mark === "?" && numbers === "1049") {
        this.active = final === "h" ? "alt" : "main";
        if (final === "h") { this.screens.alt.fill(""); this.cursors.alt = { row: 0, col: 0 }; }
      } else if (mark === "?") { /* cursor visibility, paste mode */ }
      else if (final === "A") cursor.row = Math.max(0, cursor.row - count);
      else if (final === "G") cursor.col = Math.max(0, count - 1);
      else if (final === "H" || final === "f") { cursor.row = 0; cursor.col = 0; }
      else if (final === "K") lines[cursor.row] = numbers === "2" ? "" : lines[cursor.row].slice(0, cursor.col);
      else if (final === "J") lines.fill("");
    }
  }
}

/** The app as it really draws: Ink erasing and rewriting its frame, into a terminal that keeps score. */
async function real(columns = 100, rows = 30) {
  const terminal = new Terminal(rows);
  const stdin = new Input();
  const stdout = new Output(columns, rows, (data) => terminal.write(stripColour(data)));
  let instance: ReturnType<typeof render> | undefined;
  instance = render(<App clearLiveOutput={() => instance?.clear()} />, { stdin: stdin as any, stdout: stdout as any, stderr: stdout as any, debug: false, patchConsole: false, exitOnCtrlC: false });
  await wait(150);
  const child = children.at(-1)!;
  return {
    terminal,
    child,
    async key(value: string, settle = 150) { stdin.write(value); await wait(settle); },
    async events(...events: object[]) { for (const event of events) child.event(event); await wait(150); },
    async close() { instance?.unmount(); await wait(30); },
  };
}
const stripColour = (data: string) => data.replace(/\x1b\[[0-9;]*m/gu, "");

const results = [
  { type: "tool_calls", calls: [
    { id: "a", name: "read_file", arguments: JSON.stringify({ path: "notes/first.md" }) },
    { id: "b", name: "execute_shell", arguments: JSON.stringify({ command: "pytest tests -q" }) },
    { id: "c", name: "search_text", arguments: JSON.stringify({ pattern: "needle", path: "src" }) },
  ] },
  { type: "tool_result", call_id: "a", name: "read_file", output: Array.from({ length: 40 }, (_, index) => `FIRST-${index + 1}`).join("\n"), error: "", duration_ms: 12 },
  { type: "tool_result", call_id: "b", name: "execute_shell", output: "SECOND-OUTPUT\n3 passed", error: "", duration_ms: 900 },
  { type: "tool_result", call_id: "c", name: "search_text", output: "THIRD-HIT-1\nTHIRD-HIT-2", error: "", duration_ms: 30 },
];

// A person opens the viewer at any moment: while the app is still, or just as something else
// is being drawn. Either way the viewer's frame must land on the screen it switched to.
for (const moment of ["the app is still", "another update has just been drawn"] as const) {
  test(`Ctrl+O shows the output, not an empty screen, when ${moment}`, async () => {
    const h = await real();
    try {
      await h.events({ type: "session_info", name: "s", messages: 1 }, ...results);
      await wait(250);
      const before = h.terminal.lines;
      const dock = h.terminal.rowOf(/CTX \d+%/);
      assert.ok(dock > 0 && h.terminal.rowOf("needle") >= 0, h.terminal.text);

      if (moment !== "the app is still") h.child.event({ type: "model_info", model: "m", total_tokens: 1, prompt_tokens: 1, completion_tokens: 0, context_pct: 1 });
      await h.key("\x0f");
      assert.equal(h.terminal.active, "alt");
      assert.match(h.terminal.text, /Tool #3 \(3\/3\)/, `the viewer is drawn on the screen it opened\n${h.terminal.text}`);
      assert.match(h.terminal.text, /THIRD-HIT-1[^\n]*\n[^\n]*THIRD-HIT-2/);

      // Closing gives the conversation back as it was: nothing of the viewer left on it,
      // nothing of the conversation erased, the status line where it stood.
      await h.key("\x1b");
      assert.equal(h.terminal.active, "main");
      assert.doesNotMatch(h.terminal.text, /THIRD-HIT|Tool #3 \(/);
      assert.equal(h.terminal.rowOf(/CTX \d+%/), dock, h.terminal.text);
      assert.deepEqual(h.terminal.lines.slice(0, dock - 1), before.slice(0, dock - 1), "the conversation above the status line is untouched");
      assert.equal(h.terminal.lines.filter((line) => /CTX \d+%/.test(line)).length, 1);
    } finally { await h.close(); }
  });
}

test("in the viewer the arrows step through the kept results without leaving it", async () => {
  const h = await real();
  try {
    await h.events({ type: "session_info", name: "s", messages: 1 }, ...results);
    await wait(250);
    const dock = h.terminal.rowOf(/CTX \d+%/);
    await h.key("\x0f");
    const shown = () => h.terminal.text;
    assert.match(shown(), /Tool #3 \(3\/3\) · lines 1-2\/2 · SEARCH needle  in src/);
    assert.match(shown(), /←\/→ other results/);

    await h.key("\x1b[D");
    assert.equal(h.terminal.active, "alt", "stepping stays in the viewer");
    assert.match(shown(), /Tool #2 \(2\/3\) · lines 1-2\/2 · RUN pytest tests -q/);
    assert.match(shown(), /SECOND-OUTPUT/);
    assert.doesNotMatch(shown(), /THIRD-HIT/, "nothing of the result before is left on screen");

    // A long result is scrolled; stepping away and back starts it from its top again.
    await h.key("\x1b[D");
    assert.match(shown(), /Tool #1 \(1\/3\) · lines 1-25\/40 · READ notes\/first\.md/);
    await h.key("\x1b[6~");
    assert.match(shown(), /lines 16-40\/40/);
    assert.match(shown(), /FIRST-40/);
    await h.key("\x1b[D");
    assert.match(shown(), /Tool #1 \(1\/3\) · lines 16-40\/40/, "there is nothing before the first result");
    await h.key("\x1b[C");
    await h.key("\x1b[D");
    assert.match(shown(), /Tool #1 \(1\/3\) · lines 1-25\/40/);

    await h.key("\x1b[C");
    await h.key("\x1b[C");
    await h.key("\x1b[C");
    assert.match(shown(), /Tool #3 \(3\/3\)/, "nor anything after the last");

    // A result that arrives while the viewer is open is one more to step to, and waits its
    // turn in the conversation until the viewer is closed.
    await h.events(
      { type: "tool_calls", calls: [{ id: "d", name: "read_file", arguments: JSON.stringify({ path: "notes/fourth.md" }) }] },
      { type: "tool_result", call_id: "d", name: "read_file", output: "FOURTH", error: "", duration_ms: 5 },
    );
    assert.match(shown(), /Tool #3 \(3\/4\)/);
    await h.key("\x1b[C");
    assert.match(shown(), /Tool #4 \(4\/4\) · lines 1-1\/1 · READ notes\/fourth\.md/);

    await h.key("q");
    assert.equal(h.terminal.active, "main");
    assert.doesNotMatch(h.terminal.text, /FOURTH$|Tool #4 \(/m);
    assert.ok(h.terminal.rowOf("notes/fourth.md") >= 0, h.terminal.text);
    assert.equal(h.terminal.lines.filter((line) => /CTX \d+%/.test(line)).length, 1);
    assert.equal(h.terminal.rowOf(/CTX \d+%/), dock + 1, "one row was added to the conversation while the viewer was open");
  } finally { await h.close(); }
});
