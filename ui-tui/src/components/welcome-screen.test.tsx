import assert from "node:assert/strict";
import { PassThrough, Writable } from "node:stream";
import React from "react";
import { Box, render } from "ink";
import stringWidth from "string-width";
import stripAnsi from "strip-ansi";
import { ThemeProvider } from "../theme-context.js";
import { THEMES } from "../theme.js";
import { ActivityDock } from "./activity-dock.js";
import { AppHeader } from "./app-header.js";
import { InputBar } from "./input-bar.js";
import { WelcomeScreen } from "./welcome-screen.js";

class CaptureStream extends Writable {
  rows = 32;
  isTTY = true;
  chunks: string[] = [];

  constructor(public columns: number) {
    super();
  }

  _write(chunk: Buffer | string, _encoding: BufferEncoding, callback: (error?: Error | null) => void) {
    this.chunks.push(String(chunk));
    callback();
  }
}

class TestStdin extends PassThrough {
  isTTY = true;
  setRawMode() {}
  ref() { return this; }
  unref() { return this; }
}

const info = {
  skills: 15,
  tools: 72,
  model: "deepseek-v4-pro",
  learning: { mode: "review" as const, auto: false, pending: 0, learned: 2 },
  mcp: [{ name: "local", state: "ready" }],
};

for (const columns of [60, 90, 130]) {
  const output = new CaptureStream(columns);
  const instance = render(
    <ThemeProvider theme={THEMES.glitchcity}>
      <WelcomeScreen columns={columns} rows={30} info={info} model={info.model} sessionName="session_test" />
    </ThemeProvider>,
    { stdout: output as unknown as NodeJS.WriteStream, debug: true, patchConsole: false, exitOnCtrlC: false },
  );
  await new Promise((resolve) => setTimeout(resolve, 20));
  instance.unmount();
  const frame = stripAnsi(output.chunks[0] ?? "").trimEnd();

  assert.match(frame, /ASTRA \/\/ OPERATOR CONSOLE/);
  assert.match(frame, /TOOLS\s+72\s+───\s+ONLINE/);
  assert.match(frame, /MCP\s+1\/1\s+───\s+LINKED/);
  assert.match(frame, /LEARN\s+2\s+───\s+MANUAL REVIEW/);
  assert.equal(frame.split(/\r?\n/).every((line) => stringWidth(line) <= columns), true);
  if (process.env.SHOW_WELCOME_PREVIEW === "1") {
    process.stdout.write(`\n=== welcome · ${columns} columns ===\n${frame}\n`);
  }
}

const shellColumns = 130;
const shellRows = 30;
const shellOutput = new CaptureStream(shellColumns);
shellOutput.rows = shellRows;
const shellInput = new TestStdin();
const shell = render(
  <ThemeProvider theme={THEMES.glitchcity}>
    <Box flexDirection="column" height={shellRows - 1}>
      <AppHeader columns={shellColumns} busy={false} />
      <WelcomeScreen columns={shellColumns} rows={shellRows} info={info} model={info.model} sessionName="session_test" />
      <ActivityDock
        expanded={false}
        tools={[]}
        now={Date.now()}
        memory={{}}
        model={info.model}
        totalTokens={0}
        promptTokens={0}
        contextPct={1}
        contextLimit={1_000_000}
        status="ready"
        showReasoning
        sessionName="session_test"
        columns={shellColumns}
      />
      <InputBar
        onSubmit={() => {}}
        disabled={false}
        sessionList={[]}
        modelList={[]}
        columns={shellColumns}
      />
    </Box>
  </ThemeProvider>,
  {
    stdin: shellInput as unknown as NodeJS.ReadStream,
    stdout: shellOutput as unknown as NodeJS.WriteStream,
    stderr: shellOutput as unknown as NodeJS.WriteStream,
    debug: true,
    patchConsole: false,
    exitOnCtrlC: false,
  },
);
await new Promise((resolve) => setTimeout(resolve, 20));
shell.unmount();
const shellFrame = stripAnsi(shellOutput.chunks[0] ?? "").trimEnd();
assert.match(shellFrame, /GLITCH CITY \/\/ AGENT BAR/);
assert.match(shellFrame, /● READY/);
assert.match(shellFrame, /order type your order/);
assert.match(shellFrame, /[╔┌]/);
assert.equal(shellFrame.split(/\r?\n/).every((line) => stringWidth(line) <= shellColumns), true);
if (process.env.SHOW_WELCOME_PREVIEW === "1") {
  process.stdout.write(`\n=== welcome shell · ${shellColumns}x${shellRows} ===\n${shellFrame}\n`);
}
