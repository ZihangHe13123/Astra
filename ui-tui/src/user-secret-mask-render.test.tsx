import assert from "node:assert/strict";
import { Writable } from "node:stream";
import React from "react";
import stripAnsi from "strip-ansi";
import { THEMES } from "./theme.js";
import { ThemeProvider } from "./theme-context.js";
import type { ChatMessage } from "./types.js";

const { Box, render } = await import("ink");
const { MessageLine, messageToLines } = await import("./app.js");

// Obviously fake fixtures of the published shapes, never real credentials.
const KEY = "sk-test" + "0".repeat(28);
const TOKEN = "ghp_" + "0".repeat(16) + "T".repeat(20);
const MASK = "•";
const masked = (value: string) => value.slice(0, 8) + MASK.repeat(value.length - 8);

class CaptureStream extends Writable {
  rows = 40;
  isTTY = true;
  chunks: string[] = [];
  constructor(public columns: number) { super(); }
  _write(chunk: Buffer | string, _encoding: BufferEncoding, callback: (error?: Error | null) => void) {
    this.chunks.push(String(chunk));
    callback();
  }
}

/** What the terminal shows for one message, drawn through the transcript's own row component. */
async function drawn(message: ChatMessage, columns = 120): Promise<string> {
  const output = new CaptureStream(columns);
  const lines = messageToLines(message, columns, true);
  const instance = render(
    <ThemeProvider theme={THEMES.glitchcity}>
      <Box flexDirection="column" width={columns}>
        {lines.map((line) => <MessageLine key={line.key} line={line} />)}
      </Box>
    </ThemeProvider>,
    { stdout: output as unknown as NodeJS.WriteStream, debug: true, patchConsole: false, exitOnCtrlC: false },
  );
  await new Promise((resolve) => setTimeout(resolve, 10));
  instance.unmount();
  return stripAnsi(output.chunks[0] ?? "");
}

const content = `用 ${KEY} 调用\n\n\`${TOKEN}\`\n\n\`\`\`\nexport DB=postgres://admin:hunter2-test@db.local/app\n\`\`\``;
const message: ChatMessage = { role: "user", content, timestamp: 1, id: 1 };
const user = await drawn(message);
assert.ok(user.includes(masked(KEY)), user);
assert.ok(user.includes(masked(TOKEN)), user);
assert.ok(user.includes(`postgres://admin:${MASK.repeat(12)}@db.local/app`), user);
assert.ok(!user.includes(KEY.slice(8)) && !user.includes(TOKEN.slice(8)) && !user.includes("hunter2-test"), user);
// Drawing masks the rows only; the message itself is kept as written.
assert.equal(message.content, content);

// The assistant's and the system's text are not masked in this change.
const assistant = await drawn({ role: "assistant", content: `用 ${KEY} 调用`, timestamp: 2, id: 2 });
assert.ok(assistant.includes(KEY), assistant);

// Ordinary text in a user's row reads exactly as before.
const plain = "pip install sk-learn，然后跑 task-1";
assert.deepEqual(
  messageToLines({ role: "user", content: plain, timestamp: 3, id: 3 }, 120, true).map((line) => line.text),
  messageToLines({ role: "assistant", content: plain, timestamp: 3, id: 3 }, 120, true).map((line) => line.text),
);
