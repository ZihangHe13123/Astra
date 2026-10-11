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
import { render } from "ink";
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
// One test switches the timeline off, which is saved: keep these tests' settings to themselves.
process.env.ASTRA_HOME = mkdtempSync(join(tmpdir(), "astra-conversation-flow-"));
const { default: App } = await import("./app.js");

class Input extends PassThrough { isTTY = true; setRawMode() {} ref() { return this; } unref() { return this; } }
class Output extends Writable {
  columns = 100; rows = 44; isTTY = true; chunks: string[] = [];
  _write(data: Buffer, _: BufferEncoding, done: () => void) { this.chunks.push(String(data)); done(); }
}
const settle = () => new Promise((resolve) => setTimeout(resolve, 60));

async function setup(columns = 100, rows = 44) {
  const stdin = new Input(); const stdout = new Output();
  stdout.columns = columns; stdout.rows = rows;
  const app = render(<App />, { stdin: stdin as any, stdout: stdout as any, stderr: stdout as any, debug: true, patchConsole: false, exitOnCtrlC: false });
  await settle();
  const child = children.at(-1)!;
  return {
    app,
    child,
    frame: () => stdout.chunks.map(stripAnsi).filter((chunk) => chunk.trim()).at(-1) ?? "",
    async submit(value: string) { stdin.write(value); await settle(); stdin.write("\r"); await settle(); },
    async events(...events: object[]) { for (const event of events) child.event(event); await settle(); },
  };
}

/** The conversation's own lines of a frame: everything above the status line's rule. */
function conversation(frame: string): string[] {
  const lines = frame.split("\n");
  const rule = lines.findIndex((line) => /^─{20,}/.test(line));
  return (rule < 0 ? lines : lines.slice(0, rule)).map((line) => line.trimEnd());
}
// A time or its place, then the rail itself, whatever the theme draws it with.
const RAIL = /^(?: {5}|\d\d:\d\d| \+\d\ds)\S/;

async function turn(h: Awaited<ReturnType<typeof setup>>) {
  await h.submit("why is the reverted file listed");
  await h.events({ type: "chunk", content: "Reading how the ledger opens a file." });
  await h.events({ type: "tool_calls", calls: [
    { id: "c1", name: "read_file", arguments: JSON.stringify({ path: "agent/runtime/turn_change_store.py" }) },
    { id: "c2", name: "search_text", arguments: JSON.stringify({ pattern: "os.open(", path: "agent/runtime" }) },
  ] });
  await h.events(
    { type: "tool_result", call_id: "c1", name: "read_file", output: "line\n".repeat(61).trimEnd(), error: "", duration_ms: 96 },
    { type: "tool_result", call_id: "c2", name: "search_text", output: "hit\n".repeat(14).trimEnd(), error: "", duration_ms: 312 },
  );
  await h.events({ type: "tool_calls", calls: [{ id: "c3", name: "execute_shell", arguments: JSON.stringify({ command: "pytest tests/test_turn_change_store.py -q" }) }] });
  await h.events({ type: "tool_result", call_id: "c3", name: "execute_shell", output: "collected 18 items\n\n18 passed in 4.21s", error: "", duration_ms: 4210 });
  await h.events({ type: "chunk", content: "The ledger read the file in text mode.\n\n- it opens with `O_BINARY` now\n- one test was added" });
}

test("a turn reads as one piece: tool calls as rows, and the rail unbroken from question to change report", async () => {
  const h = await setup(120);
  try {
    await h.events({ type: "session_info", name: "sess-a", messages: 1 });
    await turn(h);
    await h.events({
      type: "turn_changes", session_id: "sess-a", request_id: "r1", turn_seq: 1, unknown_count: 0,
      totals: { files: 2, added: 9, removed: 1 },
      files: [
        { path: "agent/runtime/turn_change_store.py", state: "modified", added: 3, removed: 1, compare: "ok", reason: "" },
        { path: "tests/test_turn_change_store.py", state: "added", added: 6, removed: 0, compare: "ok", reason: "" },
      ],
    });
    await h.events({ type: "done" });
    const lines = conversation(h.frame());
    const text = lines.join("\n");

    // One row per call: what was done, to what, what came of it, how long, how it ended, its number.
    assert.match(text, /\S READ\s+agent\/runtime\/turn_change_store\.py\s+61 lines\s+0\.1s ✓\s+#1$/m, text);
    assert.match(text, /\S SEARCH\s+os\.open\(  in agent\/runtime\s+14 lines\s+0\.3s ✓\s+#2$/m, text);
    assert.match(text, /\S RUN\s+pytest tests\/test_turn_change_store\.py -q\s+18 passed in 4\.21s\s+4\.2s ✓\s+#3$/m, text);
    // The output itself stays out of the conversation.
    assert.doesNotMatch(text, /collected 18 items|collapsed/);
    // A run of calls is one block: the speaker's label on its first row only, no line between
    // the rows, and every column in the same place.
    const rows = lines.filter((line) => /\S (READ|SEARCH|RUN) /.test(line));
    assert.equal(rows.length, 3, text);
    assert.equal(lines.indexOf(rows[2]) - lines.indexOf(rows[0]), 2);
    const label = (row: string) => row.slice(6, row.search(/\S (READ|SEARCH|RUN) /)).trim();
    assert.notEqual(label(rows[0]), "");
    assert.deepEqual([label(rows[1]), label(rows[2])], ["", ""]);
    for (const mark of ["✓", "#"]) assert.equal(new Set(rows.map((row) => row.indexOf(mark))).size, 1, text);

    // What the turn changed is a small table, not a sentence.
    assert.match(text, /✎ 本轮改动 2 个文件  \+9 −1\s+\/changes 查看$/m, text);
    assert.match(text, /├ M agent\/runtime\/turn_change_store\.py\s+\+3 −1$/m, text);
    assert.match(text, /└ A tests\/test_turn_change_store\.py\s+\+6 −0$/m, text);

    // From the question to the change report the time rail runs through every line.
    const first = lines.findIndex((line) => line.includes("why is the reverted file listed"));
    const last = lines.findIndex((line) => line.includes("└ A tests/"));
    assert.ok(first >= 0 && last > first, text);
    for (const line of lines.slice(first, last + 1)) assert.match(line, RAIL, `the rail breaks at: ${JSON.stringify(line)}`);
    // One line, no more, stands between any two pieces of the turn.
    for (let index = first; index < last; index += 1) {
      assert.ok(/\S/.test(lines[index].slice(6)) || /\S/.test(lines[index + 1].slice(6)), `two empty lines at ${index}\n${text}`);
    }

    // Everything said starts in one column, and a list marker and a tool row stand in it.
    const column = (needle: string | RegExp) => {
      const line = lines.find((item) => typeof needle === "string" ? item.includes(needle) : needle.test(item))!;
      return typeof needle === "string" ? line.indexOf(needle) : line.search(needle);
    };
    const said = column("why is the reverted");
    for (const needle of ["Reading how the ledger", "The ledger read", "- it opens with", "✎ 本轮改动", /\S READ /]) {
      assert.equal(column(needle), said, `${String(needle)} starts where the question does`);
    }
  } finally { h.app.unmount(); }
});

test("a failed call says why under its row, and the next question starts a new block", async () => {
  const h = await setup(120);
  try {
    await h.events({ type: "session_info", name: "sess-a", messages: 1 });
    await h.submit("run the tests");
    await h.events({ type: "tool_calls", calls: [{ id: "c1", name: "execute_shell", arguments: JSON.stringify({ command: "pytest tests -q -x" }) }] });
    await h.events({ type: "tool_result", call_id: "c1", name: "execute_shell", output: "…\n1 failed, 212 passed", error: "Command exited with code 1\nTraceback follows", duration_ms: 98_000 });
    await h.events({ type: "chunk", content: "One test failed." }, { type: "done" });
    await h.submit("which one");
    await h.events({ type: "chunk", content: "The rename test." }, { type: "done" });
    const lines = conversation(h.frame());
    const text = lines.join("\n");

    assert.match(text, /\S RUN\s+pytest tests -q -x\s+1 failed, 212 passed\s+1:38 ✕\s+#1$/m, text);
    const row = lines.findIndex((line) => /\S RUN /.test(line));
    // Only the first line of the reason, right under the row and under the rail.
    assert.match(lines[row + 1], /^ {5}\S\s+Command exited with code 1$/, text);
    assert.doesNotMatch(text, /Traceback follows/);

    // Between the two turns stands one line the rail does not run through; inside each the
    // rail is unbroken.
    const first = lines.findIndex((line) => line.includes("run the tests"));
    const answer = lines.findIndex((line) => line.includes("One test failed."));
    const second = lines.findIndex((line) => line.includes("which one"));
    const last = lines.findIndex((line) => line.includes("The rename test."));
    assert.ok(first >= 0 && answer > first && second === answer + 2 && last > second, text);
    assert.equal(lines[answer + 1], "");
    for (const line of [...lines.slice(first, answer + 1), ...lines.slice(second, last + 1)]) assert.match(line, RAIL, JSON.stringify(line));
  } finally { h.app.unmount(); }
});

test("with the timeline off the same pieces stand without a rail", async () => {
  const h = await setup(120);
  try {
    await h.events({ type: "session_info", name: "sess-a", messages: 1 });
    await h.submit("/timeline off");
    await h.submit("look it up");
    await h.events({ type: "tool_calls", calls: [
      { id: "c1", name: "read_file", arguments: JSON.stringify({ path: "README.md" }) },
      { id: "c2", name: "read_file", arguments: JSON.stringify({ path: "docs/usage.md" }) },
    ] });
    await h.events(
      { type: "tool_result", call_id: "c1", name: "read_file", output: "a\nb", error: "", duration_ms: 12 },
      { type: "tool_result", call_id: "c2", name: "read_file", output: "a\nb\nc", error: "", duration_ms: 30 },
    );
    await h.events({ type: "chunk", content: "Found it." }, { type: "done" });
    const lines = conversation(h.frame());
    const text = lines.join("\n");
    const rows = lines.filter((line) => /\S READ /.test(line));
    assert.equal(rows.length, 2, text);
    assert.match(rows[0], /^tool \S READ\s+README\.md\s+2 lines\s+12ms ✓\s+#1$/, text);
    assert.match(rows[1], /^ {5}\S READ\s+docs\/usage\.md\s+3 lines\s+30ms ✓\s+#2$/, text);
    assert.equal(lines.indexOf(rows[1]) - lines.indexOf(rows[0]), 1);
    // One clear line before the run and one after it, as between any two pieces.
    assert.equal(lines[lines.indexOf(rows[0]) - 1].trim(), "");
    assert.equal(lines[lines.indexOf(rows[1]) + 1].trim(), "");
    assert.match(lines[lines.indexOf(rows[1]) + 2], /Found it\./);
    await h.submit("/timeline on");
  } finally { h.app.unmount(); }
});

test("a command and its answer stand apart from the question that follows, one clear line each", async () => {
  const h = await setup(120);
  try {
    await h.events({ type: "session_info", name: "sess-a", messages: 1 });
    await h.submit("/timeline on");
    await h.submit("hello");
    await h.events({ type: "chunk", content: "Hi." }, { type: "done" });
    const lines = conversation(h.frame());
    const text = lines.join("\n");
    const echo = lines.findIndex((line) => line.includes("/timeline on"));
    const answer = lines.findIndex((line) => line.includes("Timeline display: ON"));
    const hello = lines.findIndex((line) => line.includes("hello"));
    const reply = lines.findIndex((line) => line.includes("Hi."));
    assert.deepEqual([answer - echo, hello - answer, reply - hello], [2, 2, 2], text);
    assert.equal(lines[echo + 1].trim(), "", text);
    assert.equal(lines[answer + 1].trim(), "", text);
    // The rail joins the question to its reply, and nothing else here.
    assert.match(lines[hello + 1], /^ {5}\S$/, text);
  } finally { h.app.unmount(); }
});

test("a restored conversation is spaced like a live one", async () => {
  const h = await setup(120);
  try {
    await h.events({ type: "history", session_id: "sess-a", messages: [
      { role: "user", content: "first question", timestamp: 1_800_000_000 },
      { role: "assistant", content: "first answer", timestamp: 1_800_000_005 },
      { role: "user", content: "second question", timestamp: 1_800_000_300 },
      { role: "assistant", content: "second answer", timestamp: 1_800_000_304 },
    ] });
    const lines = conversation(h.frame());
    const text = lines.join("\n");
    const at = (needle: string) => lines.findIndex((line) => line.includes(needle));
    assert.deepEqual([at("first answer") - at("first question"), at("second question") - at("first answer"), at("second answer") - at("second question")], [2, 2, 2], text);
    // Inside a turn the rail runs through the line between question and answer; between turns it does not.
    assert.match(lines[at("first question") + 1], /^ {5}\S$/, text);
    assert.equal(lines[at("first answer") + 1], "", text);
    assert.match(lines[at("second question") + 1], /^ {5}\S$/, text);
    // What is typed next follows the restored turns as it would have followed them live.
    await h.submit("third question");
    const after = conversation(h.frame());
    const third = after.findIndex((line) => line.includes("third question"));
    assert.equal(after[third - 1], "", after.join("\n"));
    assert.match(after[third - 2], /second answer/, after.join("\n"));
  } finally { h.app.unmount(); }
});
