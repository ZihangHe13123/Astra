import assert from "node:assert/strict";
import childProcess from "node:child_process";
import { EventEmitter } from "node:events";
import { syncBuiltinESMExports } from "node:module";
import { PassThrough, Writable } from "node:stream";
import test from "node:test";
import React from "react";
import { Box, Text, render } from "ink";
import stringWidth from "string-width";
import stripAnsi from "strip-ansi";
import { approvalPaintOutput } from "./approval-paint.js";
import { approvalAwareStdin } from "./foreground-approval-input.js";

const sample = { to: "recipient@example.invalid", cc: "copy@example.invalid", bcc: "blind@example.invalid",
  subject: "Review this message", via: "inert mail fixture",
  text: Array.from({ length: 6 }, (_, i) => `distinct-body-line-${i + 1}`).join("\n") };

async function withApproval(columns: number, rows: number, request: Record<string, unknown>, exercise: (h: {
  frame(): string; key(bytes: string): Promise<void>; answers: string[]; commands: string[];
  overwrite(text: string): Promise<void>; resize(columns: number, rows: number): Promise<void>;
  restoreWithBlockedWriter(): Promise<void>; ack(): Promise<void>;
}) => Promise<void>) {
  class Input extends PassThrough { isTTY = true; setRawMode() {} ref() { return this; } unref() { return this; } }
  class Child extends EventEmitter { stdin = new PassThrough(); stdout = new PassThrough(); stderr = new PassThrough(); kill() {} }
  const child = new Child(), spawn = childProcess.spawn;
  childProcess.spawn = (() => child) as unknown as typeof spawn;
  syncBuiltinESMExports();
  let screen = "";
  let hold = false, flush: (() => void) | undefined;
  const answers: string[] = [], commands: string[] = [], stdin = new Input();
  child.stdin.on("data", data => { const cmd = JSON.parse(String(data));
    if (cmd.type === "tool_approval_response") answers.push(cmd.decision); else if (cmd.type === "command") commands.push(cmd.cmd); });
  const output = Object.assign(new Writable({ write(chunk, _, done) {
    const deliver = () => { if (String(chunk).includes("\n")) screen = stripAnsi(String(chunk)); done(); };
    if (hold) flush = deliver; else deliver();
  } }), { columns, rows, isTTY: true });
  const settle = () => new Promise<void>(resolve => setTimeout(resolve, 50));
  let app: ReturnType<typeof render> | undefined;
  try {
    const { default: App } = await import("./app.js");
    const inertApp = <App appshotClientFactory={() => Object.assign(new EventEmitter(), {
      start() {}, close() {}, recordInput() {}, updateDraft() {}, activityNS: 0n,
    }) as any} />;
    let overlay: { x: number; y: number } | undefined;
    const scene = () => <Box flexDirection="column">{inertApp}{overlay &&
      <Box position="absolute" marginLeft={overlay.x} marginTop={overlay.y}><Text>×</Text></Box>}</Box>;
    app = render(scene(), { stdin: approvalAwareStdin(stdin as any), stdout: approvalPaintOutput(output as any), stderr: output as any,
      debug: true, patchConsole: false, exitOnCtrlC: false });
    await settle();
    child.stdout.write(JSON.stringify({ type: "tool_approval_request", request_id: "fixture", risk: "write", reason: "fixture", ...request }) + "\n");
    await settle(); assert.match(screen, /审批请求/);
    await exercise({ answers, commands, frame: () => screen, key: async bytes => { stdin.write(bytes); stdin.emit("readable"); await settle(); },
      resize: async (columns, rows) => { Object.assign(output, { columns, rows }); output.emit("resize"); await settle(); },
      restoreWithBlockedWriter: async () => { hold = true; overlay = undefined; app!.rerender(scene()); await settle(); assert.ok(flush); },
      ack: async () => { hold = false; flush!(); flush = undefined; await settle(); },
      overwrite: async text => {
        const lines = screen.split("\n"), y = lines.findIndex(line => line.includes(text)); assert.notEqual(y, -1);
        const line = lines[y]!; overlay = { x: stringWidth(line.slice(0, line.indexOf(text))) + 2, y };
        app!.rerender(scene()); await settle(); assert.ok(screen.includes("×") && !screen.includes(text));
      } });
  } finally {
    hold = false; flush?.(); app?.unmount(); childProcess.spawn = spawn; syncBuiltinESMExports();
  }
}

function withDraft(columns: number, rows: number, draft: Record<string, unknown>, exercise: Parameters<typeof withApproval>[3]) {
  return withApproval(columns, rows, { tool_name: "send_fixture", consequential: "send",
    kind: "tool_execution", choices: ["once", "deny"], draft }, exercise);
}

test("Tab loan confirmation requires every disclosed scope cell even if both card edges survive", async () => {
  await withApproval(120, 36, { kind: "computer_foreground_takeover", tool_name: "computer_act",
    choices: ["once", "session", "deny"] }, async h => {
    await h.overwrite("最多 2 分钟");
    await h.key("A"); assert.deepEqual(h.answers, [], "direct borrowing needs the same scope disclosure");
    await h.restoreWithBlockedWriter(); await h.ack();
    await h.key("\t");
    assert.match(h.frame(), /确认借用此窗口，最多 2 分钟/);
    await h.overwrite("最多 2 分钟"); // an Ink overlay inside the trusted hint
    assert.match(h.frame(), /审批请求/);
    assert.match(h.frame(), /YOLO/);
    await h.key("\r");
    assert.deepEqual(h.answers, []);
    await h.restoreWithBlockedWriter(); await h.key("\r"); assert.deepEqual(h.answers, []);
    await h.ack(); assert.match(h.frame(), /确认借用此窗口，最多 2 分钟/);
    await h.key("\r"); assert.deepEqual(h.answers, ["session"]);
  });
});

test("a clipped loan cannot be confirmed until its wrapped hint is visible in the viewport", async () => {
  await withApproval(120, 36, { kind: "computer_foreground_takeover", tool_name: "computer_act",
    choices: ["once", "session", "deny"] }, async h => {
    await h.key("\t");
    await h.resize(24, 5); await h.key("\r"); assert.deepEqual(h.answers, []);
    await h.resize(41, 24);
    const cells = h.frame().replaceAll("│", "").replace(/\s/g, "");
    assert.ok(cells.includes("[回车]确认借用此窗口，最多2分钟"), h.frame());
    await h.key("\r"); assert.deepEqual(h.answers, ["session"]);
  });
});

test("a 50x14 send card must not grant from its painted header and footer alone", async () => {
  await withDraft(50, 14, sample, async h => {
    await h.key("y"); assert.deepEqual(h.answers, []);
    assert.match(h.frame(), /放大|调整终端|扩大/);
    await h.key("n"); assert.deepEqual(h.answers, ["deny"]);
  });
});

test("the same draft fully visible at 120x36 can grant", async () => {
  await withDraft(120, 36, sample, async h => {
    for (const text of [sample.to, `抄送：${sample.cc}`, `密送：${sample.bcc}`, sample.subject, sample.via,
      ...sample.text.split("\n")]) assert.ok(h.frame().includes(text), text);
    await h.key("\r"); assert.deepEqual(h.answers, ["once"]);
  });
});

for (const [label, width, height, draft] of [
  ["short", 50, 14, sample], ["truncated", 120, 36, { ...sample, truncated: true }], ["complete", 120, 36, sample],
] as const) test(`Ctrl+Y is not a mode switch on a ${label} send card`, async () => {
  await withDraft(width, height, draft, async h => {
    await h.key("\x19"); assert.ok(!h.commands.includes("/yolo")); assert.deepEqual(h.answers, []);
    assert.doesNotMatch(h.frame(), /\[Ctrl\+Y\]/);
    await h.key("n"); assert.deepEqual(h.answers, ["deny"]);
  });
});

for (const [label, field] of Object.entries({ recipient: sample.to, cc: sample.cc, bcc: sample.bcc,
  subject: sample.subject, route: sample.via, body: "distinct-body-line-3" })) {
  test(`overpainting an interior ${label} cell revokes the full-draft receipt`, async () => {
    await withDraft(120, 36, sample, async h => {
      const height = h.frame().split("\n").length;
      await h.overwrite(field); assert.equal(h.frame().split("\n").length, height);
      assert.match(h.frame(), /审批请求/); assert.match(h.frame(), /丢弃/);
      await h.key("y"); assert.deepEqual(h.answers, []);
      await h.key("n"); assert.deepEqual(h.answers, ["deny"]);
    });
  });
}

test("a reply requires the whole destination in the acknowledged Ink frame, not just the body or tool name", async () => {
  const destination = '{"comment_id":9876543210987654321,"owner":"team","repo":"notes"}';
  const draft = { destination, via: "fixture/reply", text: sample.text };
  await withDraft(50, 14, draft, async h => {
    await h.key("y"); assert.deepEqual(h.answers, []);
    await h.resize(120, 36); assert.ok(h.frame().includes(`目标位置：${destination}`));
    assert.ok(h.frame().includes(`通过：${draft.via}`)); assert.ok(h.frame().includes("distinct-body-line-6"));
    await h.overwrite("9876543210987654321"); await h.key("\r"); assert.deepEqual(h.answers, []);
    await h.restoreWithBlockedWriter(); await h.key("y"); assert.deepEqual(h.answers, []);
    await h.ack(); assert.ok(h.frame().includes(`目标位置：${destination}`)); assert.deepEqual(h.answers, []);
    await h.key("\r"); assert.deepEqual(h.answers, ["once"]);
  });
});

test("resizing reveals the full draft without replaying an earlier attempted grant", async () => {
  await withDraft(50, 14, sample, async h => {
    await h.key("y"); assert.deepEqual(h.answers, []);
    await h.resize(120, 36); assert.ok(h.frame().includes("distinct-body-line-6")); assert.deepEqual(h.answers, []);
    await h.key("y"); assert.deepEqual(h.answers, ["once"]);
  });
});

test("restoring an overwritten draft requires a new complete writer ACK and a fresh send decision", async () => {
  await withDraft(120, 36, sample, async h => {
    await h.overwrite(sample.subject); await h.restoreWithBlockedWriter();
    await h.key("\r"); assert.deepEqual(h.answers, []);
    await h.ack(); assert.ok(h.frame().includes(sample.subject)); assert.deepEqual(h.answers, []);
    await h.key("\r"); assert.deepEqual(h.answers, ["once"]);
  });
});

test("wrapped addresses, subjects and the whole multiline message survive compact and expanded review", async () => {
  const draft = { ...sample, to: "r".repeat(125) + "@wrapped.invalid", subject: "s".repeat(135) + " SUBJECT-END",
    text: ["leading  and trailing  ", "repeat", "", "repeat", "safe\x1b[2J and\\x1b", "tab\there", "seven", "FINAL", ""].join("\n") };
  await withDraft(120, 36, draft, async h => {
    for (const view of ["collapsed", "expanded"]) {
      if (view === "expanded") await h.key("v");
      const frame = h.frame();
      for (const part of ["@wrapped.invalid", "SUBJECT-END", sample.via, "▏leading  and trailing  ▕", "▏FINAL▕",
        String.raw`safe\x1b[2J and\\x1b`, String.raw`tab\there`, "↳"]) assert.ok(frame.includes(part), part);
      assert.equal(frame.split("▏repeat▕").length - 1, 2);
      assert.equal(frame.split("▏▕").length - 1, 2);
    }
    await h.key("y"); assert.deepEqual(h.answers, ["once"]);
  });
});

for (const [name, draft] of Object.entries({
  oversized: { ...sample, text: "x".repeat(8193) }, missingBody: { to: sample.to, subject: sample.subject },
  opaqueDestination: { text: "reply", destination: { repo: "notes" } }, nonBindingDestination: { text: "reply", destination: '[]' },
  alreadyTruncated: { ...sample, truncated: true }, missingMetadataValue: { ...sample, subject: null },
})) test(`${name} cannot authorize an excerpt; denial still works`, async () => {
  await withDraft(120, 36, draft, async h => {
    assert.match(h.frame(), /不能.*发送/);
    await h.key("y"); assert.deepEqual(h.answers, []);
    await h.key("n"); assert.deepEqual(h.answers, ["deny"]);
  });
});
