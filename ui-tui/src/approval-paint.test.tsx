import assert from "node:assert/strict";
import { PassThrough, Writable } from "node:stream";
import test from "node:test";
import React from "react";
import { Box, render, Text, useStdout } from "ink";
import { approvalPaintGlyph, approvalPaintOutput, useApprovalPaint } from "./approval-paint.js";

const settle = () => new Promise<void>(resolve => setImmediate(resolve));
function fixture(adapt = true) {
  const frames: Array<{ text: string; ack: (error?: Error) => void }> = [];
  const sink = Object.assign(new Writable(), { rows: 8, columns: 60, isTTY: true });
  sink.write = ((chunk: string, enc?: BufferEncoding | (() => void), callback?: (error?: Error) => void) => {
    frames.push({ text: String(chunk), ack: typeof enc === "function" ? enc : callback ?? (() => {}) });
    return false; // Backpressure until the test delivers this particular frame.
  }) as typeof sink.write;
  const target = sink as unknown as NodeJS.WriteStream;
  const stdout = adapt ? approvalPaintOutput(target) : target;
  let canAnswer = () => false;
  function Card({ head, tail = true, extraRows = 0 }: { head: object; tail?: boolean; extraRows?: number }) {
    const paint = useApprovalPaint(useStdout().stdout, head, sink.rows, sink.columns, false);
    canAnswer = () => paint.current(false);
    return <Box flexDirection="column">
      <Text>{approvalPaintGlyph(paint.epoch, false, "h")} Same request</Text>
      <Text>{tail ? approvalPaintGlyph(paint.epoch, false, "t") : "clipped"}</Text>
      {Array.from({ length: extraRows }, (_, i) => <Text key={i}>row</Text>)}
    </Box>;
  }
  const headA = {}, headB = {};
  const app = render(<Card head={headA} />, { stdout, stderr: stdout, stdin: new PassThrough() as any,
    debug: true, patchConsole: false, exitOnCtrlC: false });
  return { app, frames, stdout, canAnswer: () => canAnswer(),
    drawB: (tail = true, extraRows = 0) => app.rerender(<Card head={headB} tail={tail} extraRows={extraRows} />) };
}

test("visually identical successive cards require their own completed write; terminal bytes are not receipts", async () => {
  const h = fixture();
  try {
    const a = h.frames.find(frame => frame.text.includes("Same request"))!;
    h.drawB(); const b = h.frames.at(-1)!;
    assert.equal(a.text, b.text); assert.ok(!a.text.includes("astra-paint"));
    a.ack(); await settle(); assert.equal(h.canAnswer(), false);
    b.ack(); await settle(); assert.equal(h.canAnswer(), true);
    h.stdout.write(b.text); const echo = h.frames.at(-1)!;
    echo.ack(); b.ack(); await settle(); assert.equal(h.canAnswer(), false);
  } finally { h.app.unmount(); }
});

test("clipped cells, scrollback-only cards and failed writes do not arm a decision", async () => {
  const h = fixture();
  try {
    h.drawB(false); h.frames.at(-1)!.ack(); await settle(); assert.equal(h.canAnswer(), false);
    h.drawB(true, 10); h.frames.at(-1)!.ack(); await settle(); assert.equal(h.canAnswer(), false);
    h.drawB(); const failed = h.frames.at(-1)!;
    failed.ack(new Error("writer failed")); failed.ack(); await settle(); assert.equal(h.canAnswer(), false);
  } finally { h.app.unmount(); }
});

test("an embedding without the paint adapter cannot approve even after render", async () => {
  const h = fixture(false);
  try { h.frames.at(-1)!.ack(); await settle(); assert.equal(h.canAnswer(), false); }
  finally { h.app.unmount(); }
});

test("cursor visibility cannot supersede a pending frame ACK; cursor movement can", async () => {
  const h = fixture();
  try {
    const card = h.frames.find(frame => frame.text.includes("Same request"))!;
    h.stdout.write("\x1b[?25h\x1b[?25l");
    assert.equal(h.canAnswer(), false);
    card.ack(); await settle(); assert.equal(h.canAnswer(), true);
    h.stdout.write("\x1b[1A");
    card.ack(); await settle(); assert.equal(h.canAnswer(), false);
  } finally { h.app.unmount(); }
});
