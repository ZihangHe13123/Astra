// Entry point — render Ink App to stderr. Backend IPC uses a child-process pipe,
// so the Node process stdout is not part of the JSON protocol.
import React from "react";
import { render } from "ink";
import App from "./app.js";
import { installTerminalOutput } from "./terminal-output.js";
import { TuiLifecycle } from "./tui-lifecycle.js";
import { appendFile, mkdir } from "node:fs/promises";
import { join } from "node:path";

const outputStream = process.stderr;
const bracketedPasteOn = "\u001b[?2004h";
const bracketedPasteOff = "\u001b[?2004l";
const { output } = installTerminalOutput(outputStream, {}, process.stdout.isTTY ? [process.stdout] : []);

// Ensure the output stream reports terminal size. Ink/Yoga rely on these values,
// and Windows can leave stderr without columns/rows even when stdout has them.
const fallbackSize = !outputStream.columns || !outputStream.rows;
if (!outputStream.columns && process.stdout.columns) {
  Object.defineProperty(outputStream, "columns", {
    get: () => process.stdout.columns,
    configurable: true,
  });
}
if (!outputStream.rows && process.stdout.rows) {
  Object.defineProperty(outputStream, "rows", {
    get: () => process.stdout.rows,
    configurable: true,
  });
}

if (fallbackSize) process.stdout.on("resize", () => outputStream.emit("resize"));

// Tell Windows Terminal that this full-screen input safely handles multiline
// paste as one value. The terminal then avoids the shell-oriented warning and
// wraps pasted data in markers that paste-command.ts strips before storage.
if (process.stdin.isTTY && outputStream.isTTY) {
  outputStream.write(bracketedPasteOn);
}

let instance: ReturnType<typeof render> | undefined;
const lifecycle = new TuiLifecycle(async (reason, result) => {
  instance?.unmount();
  if (process.stdin.isTTY && outputStream.isTTY) {
    outputStream.write(bracketedPasteOff + "\u001b[?1049l\u001b[?25h");
  }
  const drained = await output.close();
  const directory = join(process.env.AGENT_PROJECT_ROOT || process.cwd(), ".logs");
  // No user text or raw terminal data enters the diagnostic.
  const diagnostic = (async () => {
    await mkdir(directory, { recursive: true });
    await appendFile(join(directory, "tui-terminal.jsonl"), JSON.stringify({
      time: new Date().toISOString(), pid: process.pid, reason, ...result, drained,
      code: output.fault?.code ?? null, ...output.metrics,
    }) + "\n");
  })().catch(() => {});
  await Promise.race([diagnostic, new Promise(resolve => setTimeout(resolve, 500))]);
  process.exit(reason === "terminal_output_failure" || !drained ? 1 : 0);
});
output.on("fault", () => { void lifecycle.requestExit("terminal_output_failure"); });
process.on("SIGHUP", () => { void lifecycle.requestExit("terminal_output_failure"); });
process.on("SIGTERM", () => { void lifecycle.requestExit("signal"); });
process.on("SIGINT", () => { void lifecycle.requestExit("signal"); });

instance = render(<App lifecycle={lifecycle} clearLiveOutput={() => instance?.clear()} />, {
  stdout: outputStream as any,
  stderr: outputStream as any,
  patchConsole: false,
  // App owns Ctrl+C: first press cancels an active durable task, a second
  // press (or an idle press) exits. Ink's default would consume it first.
  exitOnCtrlC: false,
});

// Keep alive
instance.waitUntilExit().then(() => lifecycle.requestExit("ui_exit"), () => lifecycle.requestExit("ui_exit"));
