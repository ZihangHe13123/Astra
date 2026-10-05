import assert from "node:assert/strict";
import test from "node:test";
import childProcess from "node:child_process";
import { syncBuiltinESMExports } from "node:module";
import { EventEmitter } from "node:events";
import { PassThrough, Writable } from "node:stream";
import React from "react";
import { render } from "ink";
import stripAnsi from "strip-ansi";
import { StreamingMarkdownCoordinator } from "./streaming-markdown.js";
import { TuiLifecycle } from "./tui-lifecycle.js";

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
const localDefinition = {
  command: "/fixture", label: "Fixture", description: "local fixture",
  ui: { brand: "FIXTURE MODE", exitLabel: "leave fixture", headerTag: "PRIVATE", idle: "FIXTURE OPEN", busy: "FIXTURE…", placeholder: "local input" },
};
const { default: App } = await import("./app.js");
class Input extends PassThrough { isTTY = true; setRawMode() {} ref() { return this; } unref() { return this; } }
class Output extends Writable {
  columns = 90; rows = 30; isTTY = true; chunks: string[] = [];
  _write(data: Buffer, _: BufferEncoding, done: () => void) { this.chunks.push(String(data)); done(); }
}
const settle = () => new Promise((resolve) => setTimeout(resolve, 60));
async function setup(columns = 90, rows = 30, appshotClientFactory?: (consumer:any) => any, appshotManifestReader?: (path:string)=>string, lifecycle?: TuiLifecycle) {
  const stdin = new Input(); const stdout = new Output();
  stdout.columns = columns; stdout.rows = rows;
  const app = render(<App appshotClientFactory={appshotClientFactory} appshotManifestReader={appshotManifestReader} lifecycle={lifecycle} />, { stdin: stdin as any, stdout: stdout as any, stderr: stdout as any, debug: true, patchConsole: false, exitOnCtrlC: false });
  await settle();
  return { app, stdin, stdout, child: children.at(-1)!, frame: () => stdout.chunks.map(stripAnsi).filter((chunk) => chunk.trim()).at(-1) ?? "", async key(value: string) { stdin.write(value); await settle(); }, async submit(value: string) { stdin.write(value); await settle(); stdin.write("\r"); await settle(); } };
}
async function tool(h: Awaited<ReturnType<typeof setup>>) { h.child.event({ type: "tool_result", name: "read_file", output: "detail line\n".repeat(20), error: "" }); await settle(); await h.key("\x0f"); assert.match(h.frame(), /Tool #1/); }

test("tool parameter preparation is visible before execution and clears on cancellation", async () => {
  const h = await setup(160, 40);
  try {
    await h.submit("prepare a file");
    h.child.event({ type: "tool_preparing", attempt_id: "p", state: "preparing", calls: [
      { index: 0, call_id: "c", name: "write_file", summary: "report.md", argument_chars: 1024 },
    ] });
    await settle();
    assert.match(h.frame(), /准备 WRITE_FILE/);
    assert.match(h.frame(), /1,024/);
    assert.doesNotMatch(h.frame(), /RUN WRITE|LAST.*WRITE|Tool #1/);
    h.child.event({ type: "done" });
    await settle();
    assert.doesNotMatch(h.frame(), /准备 WRITE_FILE/);
  } finally { h.app.unmount(); }
});

test("idle teammate stops activity and a finished parent accepts a fresh turn", async () => {
  const h = await setup(143, 40);
  const dock = () => h.frame().split("\n").filter((line) => /\bCTX \d+%/.test(line)).join("\n");
  try {
    h.child.event({ type: "model_info", model: "test", total_tokens: 0, prompt_tokens: 0, completion_tokens: 0, context_pct: 0 });
    await settle();
    await h.submit("run a retained teammate");
    h.child.event({ type: "tool_progress", call_id: "spawn", name: "team_spawn", stage: "assistant_turn", status: "running", current: 2, total: 6 });
    h.child.event({ type: "process_status", process_id: "retained", kind: "subagent", status: "running", label: "member", started_at: Date.now() / 1000, output_chars: 25, duration_ms: 10 });
    await settle();
    assert.match(dock(), /RUN team_spawn/);
    h.child.event({ type: "tool_progress", call_id: "spawn", name: "team_spawn", stage: "delegate_idle", status: "idle" });
    h.child.event({ type: "agent_team", kind: "agent_team", event: "team_agent_idle", team_id: "team", agent_id: "member", process_id: "retained", status: "idle" });
    await settle();
    assert.doesNotMatch(dock(), /RUN|ASSISTANT TURN/);
    // Terminal progress must remove the synthetic parent join as well.
    h.child.event({ type: "tool_progress", call_id: "join", name: "delegate_task", stage: "joining", status: "running" });
    await settle();
    assert.match(dock(), /RUN/);
    h.child.event({ type: "tool_progress", call_id: "join", name: "delegate_task", stage: "joined", status: "completed" });
    await settle();
    assert.doesNotMatch(dock(), /RUN/);
    h.child.event({ type: "done", content: "ready for the next turn" });
    await settle();
    assert.match(dock(), /READY/);
    await h.submit("next assignment");
    assert.deepEqual(h.child.commands.at(-1), { type: "message", text: "next assignment" });
    assert.doesNotMatch(h.frame(), /steering.*next assignment/);
    h.child.event({ type: "tool_progress", call_id: "spawn", name: "team_spawn", stage: "delegate_awakened", status: "running" });
    await settle();
    assert.match(dock(), /RUN team_spawn/);
  } finally { h.app.unmount(); }
});

for (const outcome of ["completed", "failed", "cancelled", "disconnect"] as const) {
  test(`compaction stays visible while pending and clears on ${outcome}`, async () => {
    const h = await setup(143, 30);
    const event = { type: "context_compaction", messages_before: 168, messages_after: 85 };
    const dock = () => h.frame().split("\n").filter((line) => /\bCTX \d+%/.test(line)).join("\n");
    try {
      await h.submit("continue the task");
      h.child.event({ type: "tool_calls", calls: [{ id: "parallel-work", name: "execute_shell", arguments: {} }] });
      h.child.event({ ...event, status: "started", messages_after: 168 });
      await settle();
      assert.match(dock(), /正在压缩上下文…/, h.frame());
      assert.doesNotMatch(dock(), /READY/);
      h.child.event({ type: "tool_result", name: "execute_shell", output: "Finished", error: "" });
      await h.key("draft to retain");
      if (outcome === "disconnect") {
        h.child.emit("exit", 1);
      } else {
        h.child.event({ ...event, status: outcome });
      }
      await settle();
      assert.doesNotMatch(dock(), /正在压缩上下文…/);
      assert.match(h.frame(), /draft to retain/);
      if (outcome === "completed") {
        assert.match(h.stdout.chunks.map(stripAnsi).join(""), /上下文压缩完成：168 → 85 条消息。/);
        h.child.event({ type: "generation_progress", phase: "requesting", elapsed_seconds: 0, idle_seconds: 0 });
        await settle();
        assert.match(dock(), /MODEL WAIT/);
      } else if (outcome !== "disconnect") {
        assert.match(h.stdout.chunks.map(stripAnsi).join(""), outcome === "failed" ? /上下文压缩未完成/ : /上下文压缩已取消/);
      }
    } finally { h.app.unmount(); }
  });
}

test("compaction token feedback is rendered in the conversation", async () => {
  const h = await setup(143, 30);
  try {
    h.child.event({ type: "context_compaction", status: "completed", method: "cleanup", messages_before: 108, messages_after: 106, tokens_before: 468256, tokens_after: 445137, target_tokens: 450000 });
    await settle();
    const rendered = h.stdout.chunks.map(stripAnsi).join("");
    assert.match(rendered, /上下文轻量清理完成/);
    assert.match(rendered, /468,256 → 445,137 tokens，释放 4.9%/);
    assert.match(rendered, /消息 108 → 106/);
  } finally { h.app.unmount(); }
});

test("terminal shutdown drains late events without restart acknowledgements, UI updates or premature kill", async () => {
  let finished = false;
  const lifecycle = new TuiLifecycle(async () => { finished = true; });
  const h = await setup(90, 30, undefined, undefined, lifecycle);
  try {
    await h.submit("first");
    h.child.event({ type: "restart_status", state: "draining", request_id: "a".repeat(32), message: "Waiting" });
    await settle();
    const before = children.length;
    const shutdown = lifecycle.requestExit("terminal_output_failure");
    const commands = h.child.commands.slice();
    h.child.event({ type: "reasoning", content: "LATE OUTPUT ".repeat(8192) });
    h.child.event({ type: "restart_ready", request_id: "a".repeat(32), session: "same_session" });
    h.child.event({ type: "wakeup_status", plan: { state: "cancelled" }, message: "LATE WAKEUP" });
    h.child.stderr.write("LATE ERROR\n");
    await settle();
    assert.deepEqual(h.child.commands, commands);
    assert.doesNotMatch(h.stdout.chunks.join(""), /LATE OUTPUT|LATE WAKEUP|LATE ERROR/);
    assert.equal(h.child.stdout.readableLength, 0, "backend output stays drained during persistence");
    h.child.stdin.emit("error", new Error("EPIPE"));
    assert.equal(h.child.kills, 0, "a closed command pipe must not interrupt saving");
    h.child.exitCode = 0;
    h.child.emit("exit", 0);
    await shutdown;
    assert.equal(finished, true);
    assert.equal(children.length, before);
  } finally {
    h.child.exitCode = 0; h.child.emit("exit", 0);
    h.app.unmount();
    await lifecycle.requestExit("ui_exit");
  }
});

test("hidden composer never submits detail navigation input", async () => {
  const h = await setup(); try {
    await tool(h); await h.key("hidden draft"); await h.key("\r");
    assert.equal(h.child.commands.filter((c) => c.type === "message").length, 0);
    await h.key("q"); await h.submit("visible draft");
    assert.equal(h.child.commands.at(-1)?.text, "visible draft");
  } finally { h.app.unmount(); }
});

for (const command of ["/retry", "/fixture retry"]) {
  test(`${command} blocks conflicting commands from submission through completion`, async () => {
    const h = await setup();
    try {
      h.child.event({ type: "local_mode_info", definition: localDefinition, sessions: [] });
      await settle();
      await h.submit(command);
      await h.submit("/reset");
      assert.equal(h.child.commands.filter((c) => c.cmd === "/reset").length, 0);
      h.child.event({ type: "history", messages: [] });
      h.child.event({ type: "tool_result", name: "retry", output: "Retrying last request", error: "" });
      h.child.event({ type: "task_started", task: { id: "retry-task", status: "running" } });
      h.child.event({ type: "chunk", content: "RETRY STREAM" });
      await settle();
      await h.submit("/reset");
      await h.submit("/yolo off");
      assert.equal(h.child.commands.filter((c) => c.cmd === "/reset").length, 0);
      assert.equal(h.child.commands.at(-1)?.cmd, "/yolo off");
      assert.match(h.frame(), /RETRY STREAM/);
      await h.submit("keep the answer short");
      assert.deepEqual(h.child.commands.at(-1), { type: "message", text: "keep the answer short" });
      h.child.event({ type: "done" });
      await settle();
      await h.submit("/reset");
      assert.equal(h.child.commands.at(-1)?.cmd, "/reset");
    } finally { h.app.unmount(); }
  });

  test(`${command} rejection and disconnect release the composer`, async () => {
    const h = await setup();
    try {
      h.child.event({ type: "local_mode_info", definition: localDefinition, sessions: [] });
      await settle();
      await h.submit(command);
      h.child.event({ type: "tool_result", name: "retry", output: "", error: "No request to retry." });
      h.child.event({ type: "done" });
      await settle();
      await h.submit("/reset");
      assert.equal(h.child.commands.at(-1)?.cmd, "/reset");
      h.child.event({ type: "local_mode_info", definition: localDefinition, sessions: [] });
      await settle();
      await h.submit(command);
      h.child.emit("exit", 1);
      await settle();
      await h.submit("/reconnect");
      assert.notEqual(children.at(-1), h.child);
    } finally { h.app.unmount(); }
  });
}

test("backend-started retry enters busy even after a legacy command acknowledgement", async () => {
  const h = await setup();
  let exits = 0;
  const originalExit = process.exit;
  process.exit = (() => { exits += 1; }) as typeof process.exit;
  try {
    h.child.event({ type: "done" });
    h.child.event({ type: "task_started", task: { id: "retry-task", status: "running" } });
    h.child.event({ type: "chunk", content: "still streaming" });
    await settle();
    await h.submit("/reset");
    assert.equal(h.child.commands.filter((c) => c.cmd === "/reset").length, 0);
    await h.key("\x03");
    assert.equal(exits, 0);
    assert.equal(h.child.commands.at(-1)?.cmd, "/cancel");
    h.child.event({ type: "task_status", task: { id: "retry-task", status: "cancelled" } });
    await settle();
    await h.submit("/reset");
    assert.equal(h.child.commands.at(-1)?.cmd, "/reset");
  } finally { process.exit = originalExit; h.app.unmount(); }
});

test("replayed task starts do not lock an idle composer", async () => {
  const h = await setup();
  try {
    h.child.event({ type: "task_started", task: { id: "old-task", status: "running" }, replayed: true });
    await settle();
    await h.submit("/reset");
    assert.equal(h.child.commands.at(-1)?.cmd, "/reset");
  } finally { h.app.unmount(); }
});

for (const command of ["/retry", "/fixture retry"]) {
  test(`${command} write failure leaves reconnect available`, async () => {
    const h = await setup();
    try {
      h.child.stdin.write = (() => { throw new Error("EPIPE"); }) as typeof h.child.stdin.write;
      h.child.event({ type: "local_mode_info", definition: localDefinition, sessions: [] });
      await settle();
      await h.submit(command);
      await h.submit("/reconnect");
      const reconnected = children.at(-1)!;
      assert.notEqual(reconnected, h.child);
      await h.submit("/reset");
      assert.equal(reconnected.commands.at(-1)?.cmd, "/reset");
    } finally { h.app.unmount(); }
  });
}

for (const [path, prompt] of [["/tmp/photo.PNG", "哈"], ["C:\\cards\\photo.PNG", "哈"], ["/tmp/photo.PNG", ""]]) {
  test(`image path ${path} with prompt ${JSON.stringify(prompt)} reaches the message channel`, async () => {
    const h = await setup();
    try {
      if (prompt) await h.key(prompt);
      await h.key(path);
      await h.key("\r");
      assert.deepEqual(h.child.commands.at(-1), { type: "message", text: `${path} ${prompt}`.trim() });
      h.child.event({ type: "done" });
      await settle();
      await h.submit("/reset");
      assert.deepEqual(h.child.commands.at(-1), { type: "command", cmd: "/reset" });
    } finally {
      h.app.unmount();
    }
  });
}

test("new approval leaves tool details before accepting a decision", async () => {
  const h = await setup(); try {
    await tool(h);
    h.child.event({ type: "tool_approval_request", request_id: "approval-1", tool_name: "execute_shell", risk: "execute", reason: "VISIBLE APPROVAL", target: "echo test", choices: ["once", "deny"] }); await settle();
    assert.match(h.frame(), /echo test/);
    await h.key("y"); assert.equal(h.child.commands.at(-1)?.type, "tool_approval_response");
  } finally { h.app.unmount(); }
});
test("new question leaves tool details and is visible", async () => {
  const h = await setup(); try { await tool(h);
    h.child.event({ type: "user_question_request", request_id: "q1", questions: [{ id: "choice", question: "VISIBLE QUESTION", multi_select: false }] }); await settle();
    assert.match(h.frame(), /VISIBLE QUESTION/);
  } finally { h.app.unmount(); }
});
test("disconnected message does not lock reconnect behind busy", async () => {
  const h = await setup(); try {
    h.child.emit("exit", 1); await settle();
    await h.submit("hello offline"); const before = children.length;
    await h.submit("/reconnect"); assert.equal(children.length, before + 1);
    await h.submit("hello online"); assert.equal(children.at(-1)?.commands.at(-1)?.text, "hello online");
  } finally { h.app.unmount(); }
});
test("spawn error and subsequent exit tear down once and allow reconnect", async () => {
  const h = await setup(); try {
    assert.doesNotThrow(() => h.child.emit("error", new Error("spawn ENOENT")));
    await settle();
    const beforeExit = h.stdout.chunks.join("");
    h.child.emit("exit", -2); await settle();
    assert.equal(h.stdout.chunks.join(""), beforeExit, "exit after error must not repeat teardown/render");
    const before = children.length; await h.submit("/reconnect"); assert.equal(children.length, before + 1);
  } finally { h.app.unmount(); }
});

test("detail wrapping is shared and cached across scrolling and clock ticks", async () => {
  const h = await setup();
  const body = "cached unique tool output ".repeat(150);
  const originalSegment = Intl.Segmenter.prototype.segment;
  let wraps = 0;
  Intl.Segmenter.prototype.segment = function (input: string) {
    if (input === body) wraps++;
    return originalSegment.call(this, input);
  };
  try {
    h.child.event({ type: "tool_result", name: "read_file", output: body, error: "" }); await settle();
    wraps = 0; await h.key("\x0f");
    assert.equal(wraps, 1, "panel and scrolling bounds share one wrap");
    wraps = 0; await h.key("\x1b[6~");
    h.child.event({ type: "tool_calls", calls: [{ id: "clock-tool", name: "waiting", arguments: "" }] });
    await new Promise((resolve) => setTimeout(resolve, 450));
    assert.equal(wraps, 0, "neither page movement nor unrelated clock updates rewrap");
    h.stdout.columns = 70; h.stdout.emit("resize"); await new Promise((resolve) => setTimeout(resolve, 250));
    assert.equal(wraps, 1, "width change invalidates once");
  } finally { Intl.Segmenter.prototype.segment = originalSegment; h.app.unmount(); }
});

test("destroyed backend input releases the process so reconnect works", async () => {
  const h = await setup(); try {
    h.child.stdin.destroy(); await h.submit("broken pipe");
    const before = children.length; await h.submit("/reconnect"); assert.equal(children.length, before + 1);
  } finally { h.app.unmount(); }
});
test("synchronous write failure does not enter busy and allows reconnect", async () => {
  const h = await setup(); try {
    h.child.stdin.write = (() => { throw new Error("write EPIPE"); }) as typeof h.child.stdin.write;
    await h.submit("failed write"); const before = children.length;
    await h.submit("/reconnect"); assert.equal(children.length, before + 1);
  } finally { h.app.unmount(); }
});
test("backpressure still sends and subsequent text uses steering", async () => {
  const h = await setup(); try {
    const write = h.child.stdin.write.bind(h.child.stdin);
    h.child.stdin.write = ((data: string) => { write(data); return false; }) as typeof h.child.stdin.write;
    await h.submit("first"); await h.submit("second");
    assert.deepEqual(h.child.commands.filter((c) => c.type === "message").map((c) => c.text), ["first", "second"]);
    assert.match(h.stdout.chunks.map(stripAnsi).join(""), /⟡ second/);
  } finally { h.app.unmount(); }
});

test("yolo commands preserve a live reply and its busy state", async () => {
  const h = await setup();
  try {
    await h.submit("first");
    h.child.event({ type: "chunk", content: "LIVE YOLO REPLY" });
    await settle();
    for (const cmd of ["/yolo", "/yolo off", "/yolo status"]) {
      await h.submit(cmd);
      assert.deepEqual(h.child.commands.at(-1), { type: "command", cmd });
      h.child.event({ type: "yolo_status", yolo: cmd === "/yolo" });
      await settle();
      assert.match(h.frame(), /LIVE YOLO REPLY/, "control commands must keep streaming content");
    }
    await h.submit("second");
    assert.match(h.stdout.chunks.map(stripAnsi).join(""), /⟡ second/, "YOLO acknowledgement must not finish the turn");
  } finally { h.app.unmount(); }
});

test("restart drains a live reply, acknowledges delivery, and restores once", async () => {
  const h = await setup();
  try {
    await h.submit("first");
    h.child.event({ type: "chunk", content: "FINAL BEFORE RESTART" });
    await settle();
    await h.submit("/restart");
    assert.deepEqual(h.child.commands.at(-1), { type: "command", cmd: "/restart" });
    assert.match(h.frame(), /FINAL BEFORE RESTART/);
    h.child.event({ type: "restart_status", state: "draining", request_id: "a".repeat(32), message: "Waiting for completion" });
    await settle();
    await h.submit("new work");
    assert.equal(h.child.commands.filter(c => c.text === "new work").length, 0);
    h.child.event({ type: "done" });
    h.child.event({ type: "restart_ready", request_id: "a".repeat(32), session: "same_session" });
    await settle();
    assert.deepEqual(h.child.commands.at(-1), { type: "restart_ack", request_id: "a".repeat(32) });
    const before = children.length;
    h.child.emit("exit", 42);
    await settle();
    assert.equal(children.length, before + 1);
    const next = children.at(-1)!;
    next.event({ type: "history", session_id: "same_session", messages: [] });
    await settle();
    assert.match(h.frame(), /session has been restored/);
    h.child.emit("exit", 42);
    await settle();
    assert.equal(children.length, before + 1);
  } finally { h.app.unmount(); }
});

test("wakeup status and cancellation work while busy without clearing the reply", async () => {
  const h = await setup();
  try {
    await h.submit("first");
    h.child.event({ type: "chunk", content: "KEEP THIS REPLY" });
    await settle();
    for (const cmd of ["/wakeup", "/wakeup cancel"]) {
      await h.submit(cmd);
      assert.deepEqual(h.child.commands.at(-1), { type: "command", cmd });
      h.child.event({ type: "wakeup_status", plan: { state: "cancelled" }, message: "Wakeup stopped" });
      await settle();
      assert.match(h.frame(), /KEEP THIS REPLY/);
    }
  } finally { h.app.unmount(); }
});

test("a missing display update cancels controlled restart without acknowledging it", async () => {
  const h = await setup();
  try {
    h.child.stdout.write("{broken frame\n");
    h.child.event({ type: "restart_ready", request_id: "a".repeat(32), session: "same_session" });
    await settle();
    assert.equal(h.child.commands.filter(c => c.type === "restart_ack").length, 0);
    assert.deepEqual(h.child.commands.at(-1), { type: "command", cmd: "/restart cancel" });
    const before = children.length;
    h.child.emit("exit", 42);
    await settle();
    assert.equal(children.length, before);
  } finally { h.app.unmount(); }
});

test("Ctrl+Y toggles yolo through approvals without approving a request or losing the draft", async () => {
  const h = await setup();
  try {
    await h.key("unsent draft");
    h.child.event({ type: "tool_approval_request", request_id: "yolo-approval", tool_name: "execute_shell", risk: "execute", reason: "pending", target: "echo pending", choices: ["once", "deny"] });
    await settle();
    await h.key("\x19");
    assert.deepEqual(h.child.commands.at(-1), { type: "command", cmd: "/yolo" });
    assert.equal(h.child.commands.filter((c) => c.type === "tool_approval_response").length, 0);
    h.child.event({ type: "yolo_status", yolo: true });
    await settle();
    assert.match(h.frame(), /echo pending/, "backend owns resolving eligible approvals");
    await h.key("\x19");
    assert.equal(h.child.commands.filter((c) => c.cmd === "/yolo").length, 2);
    h.child.event({ type: "approval_resolved", request_id: "yolo-approval", state: "approved", decision: "once" });
    await settle();
    await h.key("\r");
    assert.equal(h.child.commands.at(-1)?.text, "unsent draft");
  } finally { h.app.unmount(); }
});

test("busy unsupported commands explain why they did not run", async () => {
  const h = await setup();
  try {
    await h.submit("first");
    await h.submit("/reset");
    assert.equal(h.child.commands.filter((c) => c.cmd === "/reset").length, 0);
    assert.match(h.stdout.chunks.map(stripAnsi).join(""), /Finish or cancel the current reply/);
  } finally { h.app.unmount(); }
});

test("yolo badge resets when its backend exits and stays hidden in isolated chat modes", async () => {
  const h = await setup();
  try {
    h.child.event({ type: "yolo_status", yolo: true });
    await settle();
    assert.match(h.frame(), /│ YOLO /);
    h.child.event({ type: "mode_info", mode: "bar", bar_session: "test" });
    await settle();
    assert.doesNotMatch(h.frame(), /│ YOLO /);
    h.child.event({ type: "mode_info", mode: "work" });
    await settle();
    assert.match(h.frame(), /│ YOLO /);
    h.child.emit("exit", 1);
    await settle();
    assert.doesNotMatch(h.frame(), /│ YOLO /);
    await h.submit("/reconnect");
    assert.doesNotMatch(h.frame(), /│ YOLO /);
  } finally { h.app.unmount(); }
});

test("short terminal reserves the visible frame for question navigation", async () => {
  const h = await setup(40, 12); try {
    h.child.event({ type: "user_question_request", request_id: "short-q", questions: [{ id: "choice", question: "Choose one", multi_select: false, options: [{label: "First option"}, {label: "Second option"}] }] }); await settle();
    const frame = h.frame().trimEnd();
    assert.match(frame, /Choose one/);
    assert.ok(frame.split("\n").length <= 11, frame);
  } finally { h.app.unmount(); }
});

for (const failure of ["disconnected", "EPIPE"] as const) {
  test(`/compress ${failure} leaves ordinary local commands available`, async () => {
    const h = await setup(); try {
      if (failure === "disconnected") { h.child.emit("exit", 1); await settle(); }
      else h.child.stdin.write = (() => { throw new Error("write EPIPE"); }) as typeof h.child.stdin.write;
      await h.submit("/compress");
      await h.submit("/help");
      assert.match(h.stdout.chunks.map(stripAnsi).join(""), /Shortcuts: Ctrl\+L/, "failed compression must not busy-lock local help");
    } finally { h.app.unmount(); }
  });
}

test("failed approval write cannot resurrect queued approvals cleared by teardown", async () => {
  const h = await setup(); try {
    for (let index = 1; index <= 2; index++) {
      h.child.event({ type: "tool_approval_request", request_id: `queued-${index}`, tool_name: "execute_shell", risk: "execute", reason: "approval test", target: `echo queued-${index}`, choices: ["once", "deny"] });
    }
    await settle(); assert.match(h.frame(), /queued-1/);
    h.child.stdin.write = (() => { throw new Error("write EPIPE"); }) as typeof h.child.stdin.write;
    await h.key("y");
    assert.doesNotMatch(h.frame(), /echo queued-2/, "teardown owns clearing the entire stale approval queue");
    const before = children.length; await h.submit("/reconnect");
    assert.equal(children.length, before + 1, "composer must be enabled after failed approval transport");
  } finally { h.app.unmount(); }
});

test("composer frame spans the available width while empty, typing and after resize",async()=>{
 const h=await setup(100,30);try{
  for(const [width,text] of [[100,""],[100,"hello"],[70,"!"]] as const){
   h.stdout.columns=width;h.stdout.emit("resize");h.child.event({type:"model_info",model:"test",total_tokens:0,prompt_tokens:0,completion_tokens:0,context_pct:0,context_limit:10000});if(text)await h.key(text);else await settle();
   const frame=h.frame();const borders=frame.split("\n").filter(line=>/[┌╭╔]/.test(line)&&/[┐╮╗]/.test(line));
   const inputBorder=borders.at(-1)?.trim();assert.equal(inputBorder?.length,width-1,`composer border: ${inputBorder}`);
  }
 }finally{h.app.unmount();}
});
test("model download stderr progress is hidden while real stderr stays error",async()=>{
 const h=await setup();try{
  h.child.stderr.write("Fetching 9 files: 100%|████| 9/9 [00:00<00:00, 2948.20it/s]\n");await settle();
  const output=h.stdout.chunks.map(stripAnsi).join("\n");
  assert.doesNotMatch(output,/Fetching 9 files/);
  h.child.stderr.write("OSError: model weights could not be opened\n");await settle();
  assert.match(h.stdout.chunks.map(stripAnsi).join("\n"),/err\s+\[backend\] OSError/);
 }finally{h.app.unmount();}
});

test("Appshot commands stay local while busy and input activity follows Ink only", async () => {
  let starts=0, closes=0, inputs=0; const calls:any[]=[];
  const client:any=new EventEmitter();
  Object.assign(client,{activityNS:0n,updateDraft:()=>{},release:()=>{},state:{enabled:true,chord:"ctrl+shift+s",registration:"registered",connectedTuis:1,permission:"ready"}, start:async()=>{starts++},close:async()=>{closes++},recordInput:()=>{inputs++},command:async(...args:any[])=>{calls.push(args);return {ok:true,code:"ok"}}});
  const h=await setup(90,30,()=>client);
  try {assert.equal(starts,1);assert.equal(inputs,0);h.child.event({type:"text_delta",text:"MODEL OUTPUT"});await settle();assert.equal(inputs,0);
    await h.submit("ordinary prompt");const messages=h.child.commands.length;
    await h.submit("/appshot status");assert.deepEqual(calls,[["status",""]]);assert.equal(h.child.commands.length,messages);assert.ok(inputs>0);
  } finally {h.app.unmount(); await settle();} assert.equal(closes,1);
});

const appshotNotices = (h: Awaited<ReturnType<typeof setup>>) =>
  h.frame().split("\n").filter((line) => /Appshot[：:]/.test(line)).map((line) => line.slice(line.indexOf("Appshot")).trim());

test("typing without the optional Appshot helper installed prints nothing about Appshot", async () => {
  const { mkdtempSync, realpathSync, rmSync } = await import("node:fs");
  const { tmpdir } = await import("node:os");
  const { join } = await import("node:path");
  const { AppshotClient } = await import("./appshot-client.js");
  const { productionWindowsAppshotDependencies } = await import("./appshot-windows.js");
  // A source installation whose helper bundle was never built; the real Windows resolution runs.
  const root = mkdtempSync(join(realpathSync(tmpdir()), "appshot-no-helper-"));
  const h = await setup(90, 30, (consumer) => new AppshotClient({ platform: "win32", consumer,
    windowsDeps: productionWindowsAppshotDependencies({ LOCALAPPDATA: root, AGENT_PROJECT_ROOT: root }) }));
  try {
    for (const key of "0123456789") await h.key(key);
    assert.match(h.frame(), /0123456789/);
    assert.deepEqual(appshotNotices(h), []);
  } finally { h.app.unmount(); await settle(); rmSync(root, { recursive: true, force: true }); }
});

test("typing retries an unreachable Appshot broker without repeating its retry notice", async () => {
  const { AppshotClient } = await import("./appshot-client.js");
  let launches = 0;
  const deps: any = { currentUID: () => 501, now: () => process.hrtime.bigint(),
    identity: async () => ({ pid: process.pid, uid: 501, process_start: "1", monotonic_ns: "100" }),
    discover: async () => { throw new Error("no broker"); }, connect: async () => { throw new Error("no broker"); },
    launch: async () => { launches++; } };
  const h = await setup(90, 30, (consumer) => new AppshotClient({ platform: "darwin", deps, consumer, retryMS: 1 }));
  try {
    assert.equal(launches, 1);
    for (const key of "0123456789") await h.key(key);
    // A key that lands while a retry is still in flight joins it, so the count is only bounded below.
    assert.ok(launches > 1, "typing retried the broker");
    assert.deepEqual(appshotNotices(h), ["Appshot：连接未恢复，已暂停自动重试；在本窗口按键可重试。"]);
  } finally { h.app.unmount(); await settle(); }
});


test("Appshot busy submission has typed admission, matching replay settlement and restart status without resend", async()=>{
 const {readFileSync}=await import('node:fs');
 const m=JSON.parse(readFileSync(new URL('../../native/macos-computer-helper/Tests/Fixtures/appshot_manifest_v1.json',import.meta.url),'utf8'));
 let consumer:any;const releases:string[]=[];
 class Client extends EventEmitter {activityNS=0n;state={connection:'connected'};recipientBinding=m.broker;async start(){this.emit('change',this.state);}async close(){}recordInput(){}updateDraft(){}release(id:string){releases.push(id);}async command(){return {ok:true};}}
 const h=await setup(140,40,c=>{consumer=c;return new Client();},()=>JSON.stringify(m));
 const capture=(id:string)=>{const o={type:'attach_offer',version:1,request_id:id,broker_id:m.broker.instance_id,session_id:m.broker.session_id,manifest_path:`/fixture/appshot-${m.token}.manifest.json`};assert.equal(consumer.stage(o,m.broker),true);consumer.commit({...o,type:'attach_commit'});};
 try{
 await h.submit('ordinary start');capture('a');await settle();assert.equal(h.child.commands.filter(c=>c.appshots).length,0);assert.match(h.frame(),/Appshot #1/);await h.key('\r');
 const sent=h.child.commands.filter(c=>c.appshots);assert.equal(sent.length,1);assert.ok(sent[0].submission_id);assert.equal(sent[0].appshot_session_id,m.broker.session_id);
 assert.ok(!h.frame().includes('⟡ [Appshot'));
 await h.key('new text');h.child.event({type:'message_rejected',submission_id:sent[0].submission_id,code:'backend_busy',retryable:true});await settle();
 assert.match(h.frame(),/new text/);assert.match(h.frame(),/Appshot #1/);assert.match(h.frame(),/backend_busy/);
 for(const code of ['context_budget_unavailable','context_budget_exceeded','appshot_vision_unavailable','evil\u001b[31m\nINJECTED']){
 await h.key('\r');const pending=h.child.commands.filter(c=>c.appshots).at(-1);
 h.child.event({type:'submission_status',submission_id:pending.submission_id,status:'rejected',code,retryable:true});await settle();
 assert.match(h.frame(),/new text/);assert.match(h.frame(),/Appshot #1/);assert.deepEqual(releases,[]);
 if(code.startsWith('evil'))assert.ok(!h.frame().includes('INJECTED'));else assert.ok(h.frame().includes(code));
 }

 await h.key('\r');const second=h.child.commands.filter(c=>c.appshots).at(-1);assert.notEqual(second.submission_id,sent[0].submission_id);
 h.child.event({type:'message_accepted',submission_id:sent[0].submission_id,replayed:true});await settle();assert.deepEqual(releases,[]);
 h.child.emit('exit',1);await settle();await h.submit('/reconnect');
 const restarted=children.at(-1)!;assert.equal(restarted.commands.filter(c=>c.appshots).length,0);assert.ok(restarted.commands.some(c=>c.type==='submission_status'&&c.submission_id===second.submission_id));
 restarted.event({type:'submission_status',submission_id:second.submission_id,status:'unknown'});await settle();assert.match(h.frame(),/unknown/);
 await h.submit('/appshot pending discard');assert.deepEqual(releases,['a']);
 }finally{h.app.unmount();}
});

test("image search completion displays a gallery command before the turn completes", async () => {
  const h = await setup();
  try {
    await h.submit("find garden images");
    h.child.event({ type: "tool_result", name: "search_images", call_id: "image-call", error: "", output: JSON.stringify({
      type: "image_search", success: true,
      gallery_path: "/tmp/image-galleries/images-0123456789abcdef0123456789abcdef.html",
      images: [{ id: "img_a", title: "Garden", source_url: "https://source.example/", image_url: "https://img.example/a.jpg" }],
    }) });
    await settle();
    assert.match(h.stdout.chunks.map(stripAnsi).join(""), /\/gallery 1/);
    // A local gallery command must not be sent into the running agent's queue.
    const before = h.child.commands.length;
    await h.submit("/gallery 999");
    assert.equal(h.child.commands.length, before);
    assert.match(h.stdout.chunks.map(stripAnsi).join(""), /没有可打开的图片图库/);
  } finally { h.app.unmount(); }
});

test("Ctrl+L opens status details with no tool activity after startup", async () => {
  const h = await setup(150, 32);
  try {
    h.child.event({ type: "model_info", model: "deepseek-flash", reasoning_effort: "high", total_tokens: 4200, prompt_tokens: 4000, completion_tokens: 200, context_pct: 5, context_used: 4000, context_limit: 1000000, show_reasoning: true });
    h.child.event({ type: "working_memory", memory: {} });
    h.child.event({ type: "history", messages: [{ role: "user", content: "restored conversation" }, { role: "assistant", content: "restored answer" }] });
    await settle();
    assert.match(h.frame(), /EFFORT HIGH/);
    assert.match(h.frame(), /\^L expand/);
    await h.key("\x0c");
    assert.match(h.frame(), /\^L collapse/);
    assert.match(h.frame(), /STATUS/);
    assert.match(h.frame(), /deepseek-flash/);
    assert.equal((h.frame().match(/deepseek-flash/g) ?? []).length, 1, "expanding the dock must not repeat the model");
    assert.doesNotMatch(h.frame(), /No tool activity yet/);
    assert.doesNotMatch(h.frame(), /LAST [✓✕]/, "startup must not invent a previous tool result");
    await h.key("\x0c");
    assert.match(h.frame(), /\^L expand/);
    assert.doesNotMatch(h.frame(), /No tool activity yet/);
  } finally { h.app.unmount(); }
});

test("effort updates preserve LAST and Ctrl+L activity details", async () => {
  const h = await setup(150, 32);
  try {
    h.child.event({ type: "tool_result", name: "context_open", output: "context ready", error: "", duration_ms: 18 });
    h.child.event({ type: "model_info", model: "deepseek-flash", reasoning_effort: "high", total_tokens: 4200, prompt_tokens: 4000, completion_tokens: 200, context_pct: 5, context_limit: 1000000, show_reasoning: true });
    await settle();
    assert.match(h.frame(), /LAST ✓ context_open 18ms/);
    assert.match(h.frame(), /EFFORT HIGH/);
    await h.key("\x0c");
    assert.match(h.frame(), /STATUS/);
    assert.match(h.frame(), /LAST ✓ context_open/);
    h.child.event({ type: "model_info", model: "deepseek-flash", reasoning_effort: "max", total_tokens: 4200, prompt_tokens: 4000, completion_tokens: 200, context_pct: 5, context_limit: 1000000, show_reasoning: true });
    await settle();
    assert.match(h.frame(), /EFFORT MAX/);
    assert.match(h.frame(), /LAST ✓ context_open/);
    assert.match(h.frame(), /\^L collapse/);
    await h.key("\x0c");
    assert.match(h.frame(), /\^L expand/);
    assert.doesNotMatch(h.frame(), /STATUS/);
  } finally { h.app.unmount(); }
});

test("restored tool activity preserves the original status order and keyboard details", async () => {
  const h = await setup(165, 32);
  try {
    h.child.event({ type: "model_info", model: "deepseek-flash", reasoning_effort: "high", code_mode: "native", total_tokens: 4200, prompt_tokens: 4000, completion_tokens: 200, context_pct: 11, context_limit: 500000, show_reasoning: true });
    h.child.event({ type: "history", messages: [{ role: "assistant", content: "restored answer" }], tool_results: [{ name: "context_open", output: "saved context detail", error: "", duration_ms: 18 }] });
    await settle();
    assert.match(h.frame(), /READY.*EFFORT HIGH.*TOOLS NATIVE.*deepseek-flash.*CTX 11%.*R.*LAST ✓ context_open 18ms/);
    assert.match(h.frame(), /\^L expand/);
    await h.key("\x0c");
    assert.match(h.frame(), /LAST ✓ context_open/);
    assert.match(h.frame(), /STATUS/);
    assert.doesNotMatch(h.frame(), /No tool activity yet/);
    await h.key("\x0f");
    assert.match(h.frame(), /saved context detail/);
    h.child.event({ type: "history", messages: [], tool_results: [] });
    await settle();
    assert.doesNotMatch(h.frame(), /saved context detail|LAST ✓ context_open|Tool #/);
    assert.match(h.frame(), /TOOLS NATIVE.*deepseek-flash/);
    h.child.event({ type: "tool_result", name: "read_file", output: "new session detail", error: "", duration_ms: 42 });
    await settle();
    await h.key("\x0f");
    assert.match(h.frame(), /new session detail/);
    assert.doesNotMatch(h.frame(), /saved context detail/);
  } finally { h.app.unmount(); }
});

test("request speed updates without moving the model and resets on model or session changes", async () => {
  const h = await setup(165, 32);
  const modelInfo = { type: "model_info", model: "deepseek-flash", model_key: "deepseek/flash", reasoning_effort: "high", code_mode: "native", total_tokens: 4200, prompt_tokens: 4000, completion_tokens: 200, context_pct: 5, context_limit: 500000, show_reasoning: true };
  try {
    h.child.event(modelInfo);
    h.child.event({ type: "history", session_id: "speed-session", messages: [{ role: "assistant", content: "ready" }], tool_results: [] });
    h.child.event({ type: "generation_stats", completion_tokens: 240, elapsed_seconds: 6, tokens_per_second: 40 });
    await settle();
    assert.match(h.frame(), /40\.0 tok\/s/);
    await h.key("\x0c");
    assert.equal((h.frame().match(/deepseek-flash/g) ?? []).length, 1);
    assert.match(h.frame(), /240 tokens \/ 6\.0s incl\. wait/);
    h.child.event({ type: "tool_calls", calls: [{ id: "active-call", name: "read_file", arguments: "{}" }] });
    await settle();
    const summary = h.frame().split("\n").find((line) => line.includes("EFFORT HIGH")) ?? "";
    assert.match(summary, /RUN READ.*TOOLS NATIVE.*deepseek-flash.*CTX 5%.*40\.0 tok\/s/);
    assert.equal((h.frame().match(/deepseek-flash/g) ?? []).length, 1);
    h.child.event({ type: "tool_result", call_id: "active-call", name: "read_file", output: "ok", error: "", duration_ms: 1000 });
    h.child.event({ type: "generation_stats", completion_tokens: 120, elapsed_seconds: 6, tokens_per_second: 20 });
    h.child.event(modelInfo);
    await settle();
    assert.match(h.frame(), /20\.0 tok\/s/);
    h.child.event({ type: "history", session_id: "speed-session", messages: [{ role: "assistant", content: "compressed history" }], tool_results: [] });
    await settle();
    assert.match(h.frame(), /20\.0 tok\/s/, "same-session compression preserves the last measured speed");
    h.child.event({ type: "generation_stats", completion_tokens: 0, elapsed_seconds: 0, tokens_per_second: null });
    await settle();
    assert.match(h.frame(), /20\.0 tok\/s/);
    h.child.event({ ...modelInfo, model_key: "another/flash" });
    await settle();
    assert.doesNotMatch(h.frame(), /tok\/s|last request:/);
    h.child.event({ type: "generation_stats", completion_tokens: 90, elapsed_seconds: 3, tokens_per_second: 30 });
    await settle();
    assert.match(h.frame(), /30\.0 tok\/s/);
    h.child.event({ type: "history", session_id: "new-speed-session", messages: [], tool_results: [] });
    await settle();
    assert.doesNotMatch(h.frame(), /tok\/s|last request:/);
  } finally { h.app.unmount(); }
});

for (const terminalStatus of ["completed", "failed", "cancelled", "done_verified", "interrupted"]) {
test(`matching ${terminalStatus} task status unlocks the composer when done is missing`, async () => {
  const h = await setup(130, 32);
  try {
    await h.submit("first request");
    h.child.event({ type: "task_started", task: { id: "current-task", status: "running" } });
    h.child.event({ type: "chunk", content: "final response without a newline" });
    h.child.event({ type: "task_status", task: { id: "old-task", status: "completed" } });
    await settle();
    assert.match(h.frame(), /TASK CURREN.*RUNNING/);
    h.child.event({ type: "task_status", task: { id: "current-task", status: terminalStatus } });
    await settle();
    assert.doesNotMatch(h.frame(), /TASK.*RUNNING/);
    assert.match(h.stdout.chunks.map(stripAnsi).join(""), /final response without a newline/);
    await h.submit("/help");
    assert.match(h.stdout.chunks.map(stripAnsi).join(""), /Shortcuts: Ctrl\+L/);
  } finally { h.app.unmount(); }
});
}

for (const endEvent of ["done", "error", "exit"]) {
test(`${endEvent} rendering failures are visible and cannot retain busy state`, async () => {
  const h = await setup(130, 32);
  const finish = StreamingMarkdownCoordinator.prototype.finish;
  try {
    await h.submit("first request");
    h.child.event({ type: "task_started", task: { id: "failed-render", status: "running" } });
    h.child.event({ type: "reasoning", content: "pending text" });
    h.child.event({ type: "user_question_request", request_id: "pending-question", questions: [{ id: "choice", question: "Pending decision", multi_select: false }] });
    await settle();
    StreamingMarkdownCoordinator.prototype.finish = function () { throw new RangeError("SENSITIVE_SENTINEL"); };
    assert.doesNotThrow(() => endEvent === "exit" ? h.child.emit("exit", 1)
      : h.child.event({ type: endEvent, message: "Request failed" }));
    StreamingMarkdownCoordinator.prototype.finish = finish;
    await settle();
    assert.doesNotMatch(h.frame(), /TASK.*RUNNING/);
    assert.doesNotMatch(h.frame(), /Pending decision/);
    const output = h.stdout.chunks.map(stripAnsi).join("");
    assert.match(output, /A response update could not be displayed/);
    assert.doesNotMatch(output, /SENSITIVE_SENTINEL/);
    await h.submit("/help");
    assert.match(h.stdout.chunks.map(stripAnsi).join(""), /Shortcuts: Ctrl\+L/);
  } finally { StreamingMarkdownCoordinator.prototype.finish = finish; h.app.unmount(); }
});
}

test("current request silence replaces old speed and recovers on output and completion", async () => {
  const h = await setup(140, 32);
  try {
    await h.submit("wait for a response");
    h.child.event({ type: "generation_stats", completion_tokens: 100, elapsed_seconds: 1, tokens_per_second: 100 });
    await settle();
    assert.match(h.frame(), /100\.0 tok\/s/);
    h.child.event({ type: "generation_progress", phase: "requesting", elapsed_seconds: 5, idle_seconds: 5 });
    await settle();
    assert.match(h.frame(), /MODEL WAIT 5S/);
    assert.doesNotMatch(h.frame(), /tok\/s|t\/s/);
    h.child.event({ type: "generation_progress", phase: "waiting", elapsed_seconds: 40, idle_seconds: 35 });
    await settle();
    assert.match(h.frame(), /NO OUTPUT 35S/);
    h.child.event({ type: "computer_state", active: false, permission: "degraded" });
    h.stdout.columns = 60; h.stdout.emit("resize");
    await settle();
    assert.match(h.frame(), /(?:NO OUTPUT|WAIT) 35S/);
    await h.key("\x03");
    assert.deepEqual(h.child.commands.at(-1), { type: "command", cmd: "/cancel" });
    h.child.event({ type: "generation_progress", phase: "streaming", elapsed_seconds: 41, idle_seconds: 0 });
    await settle();
    assert.match(h.frame(), /(?:GENERATING|GEN) 41S/);
    h.child.event({ type: "done" });
    await settle();
    assert.doesNotMatch(h.frame(), /NO OUTPUT|GENERATING|MODEL WAIT/);
  } finally { h.app.unmount(); }
});

test("an alias shows the version it resolved to once an answer has named it", async () => {
  // Live Astra 2026-09-29: an older Claude Code CLI kept resolving `sonnet` to Sonnet 5 after 5.5 was
  // out, and the status line only said `sonnet`.
  const h = await setup(150, 32);
  try {
    const info = { type: "model_info", model: "sonnet", reasoning_effort: "high", total_tokens: 4200, prompt_tokens: 4000, completion_tokens: 200, context_pct: 5, context_limit: 1000000 };
    h.child.event(info);
    h.child.event({ type: "history", messages: [{ role: "assistant", content: "restored answer" }] });
    await settle();
    assert.match(h.frame(), /READY.*sonnet.*CTX 5%/);
    assert.doesNotMatch(h.frame(), /sonnet-5/);
    h.child.event({ ...info, served_model: "claude-sonnet-5" });
    await settle();
    assert.match(h.frame(), /READY.*sonnet-5 .*CTX 5%/);
    assert.doesNotMatch(h.frame(), /claude-sonnet/);
    h.child.event({ ...info, served_model: "claude-sonnet-5-5" });
    await settle();
    assert.match(h.frame(), /READY.*sonnet-5-5.*CTX 5%/);
    // Switching models starts over: nothing is known until the next answer.
    h.child.event(info);
    await settle();
    assert.doesNotMatch(h.frame(), /sonnet-5/);
  } finally { h.app.unmount(); }
});

test('timed question survives done and Tab returns to composer while retaining the card',async()=>{
  const h=await setup(130,40);
  try {
    h.child.event({type:'user_question_request',request_id:'timed-q',mode:'timed',state:'waiting',questions:[{id:'language',question:'Optional language preference',multi_select:false}]});
    h.child.event({type:'user_question_pending',request_id:'timed-q',state:'pending'});
    h.child.event({type:'done'});
    await settle();
    assert.match(h.frame(),/Optional language preference/);
    await h.key('\t');
    await h.submit('continue researching');
    assert.deepEqual(h.child.commands.at(-1),{type:'message',text:'continue researching'});
    assert.match(h.frame(),/Optional language preference/);
  } finally {h.app.unmount();}
});
