import assert from "node:assert/strict";
import { Writable } from "node:stream";
import React from "react";
import stringWidth from "string-width";
import stripAnsi from "strip-ansi";
import { THEMES, type ThemeName } from "./theme.js";
import { ThemeProvider } from "./theme-context.js";
import { markdownBlocksToLines } from "./markdown-render-lines.js";
import type { TimeRailLabelKind } from "./message-time-rail.js";
import type { RenderLine } from "./markdown-render-lines.js";
import { StreamingMarkdownCoordinator } from "./streaming-markdown.js";
import type { RuntimeMode } from "./types.js";

process.env.FORCE_COLOR = "3";
const { Box, render } = await import("ink");
const {
  MESSAGE_TIME_RAIL_WIDTH,
  MessageLine,
  messageRolePrefixWidth,
  messageToLines,
} = await import("./app.js");

class CaptureStream extends Writable {
  rows = 12;
  isTTY = true;
  chunks: string[] = [];

  constructor(public columns: number) {
    super();
  }

  getColorDepth(): number {
    return 24;
  }

  _write(chunk: Buffer | string, _encoding: BufferEncoding, callback: (error?: Error | null) => void) {
    this.chunks.push(String(chunk));
    callback();
  }
}

async function capture(
  themeName: ThemeName,
  runtimeMode: RuntimeMode,
  role: "user" | "reasoning" | "assistant" | "tool",
  label: string,
  kind: TimeRailLabelKind = "absolute",
  content = "",
  columns = 40,
): Promise<{ ansi: string; plain: string }> {
  const lines = messageToLines(
    { role, content },
    columns,
    true,
    runtimeMode,
    {
      label,
      kind,
      width: MESSAGE_TIME_RAIL_WIDTH,
      prefixWidth: messageRolePrefixWidth(role, runtimeMode),
    },
  );
  return captureLines(themeName, runtimeMode, lines, columns);
}

async function captureLines(
  themeName: ThemeName,
  runtimeMode: RuntimeMode,
  lines: RenderLine[],
  columns = 40,
): Promise<{ ansi: string; plain: string }> {
  const output = new CaptureStream(columns);
  const instance = render(
    <ThemeProvider theme={THEMES[themeName]}>
      <Box flexDirection="column" width={columns}>
        {lines.map((line) => <MessageLine key={line.key} line={line} runtimeMode={runtimeMode} />)}
      </Box>
    </ThemeProvider>,
    {
      stdout: output as unknown as NodeJS.WriteStream,
      debug: true,
      patchConsole: false,
      exitOnCtrlC: false,
    },
  );
  await new Promise((resolve) => setTimeout(resolve, 10));
  instance.unmount();
  const ansi = output.chunks[0] ?? "";
  return { ansi, plain: stripAnsi(ansi).trimEnd() };
}

assert.equal(MESSAGE_TIME_RAIL_WIDTH, 7);

const user = await capture("glitchcity", "work", "user", "23:06", "absolute", "哈喽");
// Beside the rail every speaker's text starts in one column: a shorter label is padded to the
// theme's widest ("THINK› " here).
assert.match(user.plain, /^23:06│YOU›   哈喽/u);
assert.match(user.ansi, /\x1b\[38;2;66;217;200m/u);
assert.match(user.ansi, /\x1b\[38;2;255;79;163m/u);
assert.match(user.ansi, /\x1b\[38;2;138;62;114m/u);

const wrappedUser = await capture(
  "glitchcity",
  "work",
  "user",
  "23:06",
  "absolute",
  "This user message is deliberately long enough to wrap onto another line.",
);
assert.match(wrappedUser.plain, /\n {5}│ {7}\S/u);

const reasoning = await capture(
  "glitchcity",
  "work",
  "reasoning",
  "",
  "absolute",
  "用户只是打了个招呼。后续推理仍需对齐。",
);
assert.match(reasoning.plain, /^ {5}│THINK› /u);
assert.match(reasoning.plain, /\n {5}│ {7}/u);
assert.match(reasoning.ansi, /\x1b\[38;2;255;184;77m/u);
assert.doesNotMatch(reasoning.ansi, /\x1b\[2m/u);

const assistant = await capture("glitchcity", "work", "assistant", "", "absolute", "晚上好呀。");
assert.match(assistant.plain, /^ {5}│LYRA›  晚上好呀。/u);

const handoffCoordinator = new StreamingMarkdownCoordinator();
const handoffRail = {
  label: "09:44",
  kind: "absolute" as const,
  width: MESSAGE_TIME_RAIL_WIDTH,
  prefixWidth: messageRolePrefixWidth("assistant", "work"),
};
handoffCoordinator.start("assistant", handoffRail);
const handoffPreviewUpdate = handoffCoordinator.push(
  "assistant",
  "system **trusts** `code` inside a fence:\n```ts\nconst answer = 42;",
);
const handoffCommitted = markdownBlocksToLines(handoffPreviewUpdate.committed, {
  role: "assistant",
  baseKey: "handoff-committed-1",
  bodyWidth: 72,
  firstPrefix: "lyra ",
  continuationPrefix: "    ",
  timeRail: handoffPreviewUpdate.timeRail,
  startingLineIndex: handoffPreviewUpdate.startingLineIndex,
});
const handoffPreview = markdownBlocksToLines(handoffPreviewUpdate.preview, {
  role: "assistant",
  baseKey: "handoff-preview",
  bodyWidth: 72,
  firstPrefix: "    ",
  continuationPrefix: "    ",
  timeRail: handoffPreviewUpdate.timeRail,
  startingLineIndex: handoffPreviewUpdate.startingLineIndex + handoffCommitted.consumedLines,
});
handoffCoordinator.advance("assistant", handoffCommitted.consumedLines);

const handoffOutput = new CaptureStream(80);
const handoffView = (lines: RenderLine[]) => (
  <ThemeProvider theme={THEMES.glitchcity}>
    <Box flexDirection="column" width={80}>
      {lines.map((line) => <MessageLine key={line.key} line={line} runtimeMode="work" />)}
    </Box>
  </ThemeProvider>
);
const handoffInstance = render(
  handoffView([...handoffCommitted.lines, ...handoffPreview.lines]),
  {
    stdout: handoffOutput as unknown as NodeJS.WriteStream,
    debug: true,
    patchConsole: false,
    exitOnCtrlC: false,
  },
);
await new Promise((resolve) => setTimeout(resolve, 10));

const handoffFinalUpdate = handoffCoordinator.push("assistant", "\n```\n");
const handoffFinalCommitted = markdownBlocksToLines(handoffFinalUpdate.committed, {
  role: "assistant",
  baseKey: "handoff-committed-2",
  bodyWidth: 72,
  firstPrefix: "    ",
  continuationPrefix: "    ",
  timeRail: handoffFinalUpdate.timeRail,
  startingLineIndex: handoffFinalUpdate.startingLineIndex,
});
handoffInstance.rerender(handoffView([
  ...handoffCommitted.lines,
  ...handoffFinalCommitted.lines,
]));
await new Promise((resolve) => setTimeout(resolve, 10));
const handoffPlain = stripAnsi(handoffOutput.chunks.at(-1) ?? "");
handoffInstance.unmount();
assert.equal(handoffPlain.match(/system trusts `code` inside a fence:/gu)?.length, 1);
assert.equal(handoffPlain.match(/const answer = 42;/gu)?.length, 1);
assert.doesNotMatch(handoffPlain, /\*\*|```/u);

const completeMarkdown = await capture(
  "glitchcity",
  "work",
  "assistant",
  "09:44",
  "absolute",
  [
    "# P21: Cascade 级联",
    "系统会自动**更信任平时表现好的推荐器**。",
    "```python",
    "CF coarse → DNN rerank → Top N",
    "```",
  ].join("\n"),
  44,
);
assert.match(completeMarkdown.plain, /^09:44│LYRA›  P21: Cascade 级联/u);
assert.doesNotMatch(completeMarkdown.plain, /\*\*|```|(?:^|\n)[^\n]*\b(?:code python|end code)\b/u);
assert.match(completeMarkdown.plain, /CF coarse → DNN rerank/u);
assert.match(completeMarkdown.plain, /→ Top N/u);

const codeBackground = await capture(
  "glitchcity",
  "work",
  "assistant",
  "09:44",
  "absolute",
  "```python\ndef demo():\n    return 1\n```",
  60,
);
assert.match(codeBackground.plain, /^09:44│LYRA›  def demo\(\):/u);
const firstCodeAnsiLine = codeBackground.ansi.split(/\r?\n/u)[0] ?? "";
const codeBackgroundEscape = "\x1b[48;2;16;21;42m";
const backgroundIndex = firstCodeAnsiLine.indexOf(codeBackgroundEscape);
const roleIndex = firstCodeAnsiLine.indexOf("LYRA");
assert.ok(backgroundIndex >= 0, "code payload uses the configured background");
assert.ok(backgroundIndex > roleIndex, "code background begins after the rail and role prefix");
const codePayloadLines = codeBackground.ansi
  .split(/\r?\n/u)
  .filter((line) => /def demo|return 1/u.test(stripAnsi(line)));
assert.equal(codePayloadLines.length, 2);
for (const line of codePayloadLines) {
  const lineBackground = line.indexOf(codeBackgroundEscape);
  const separator = line.indexOf("│");
  const reset = Math.max(line.lastIndexOf("\x1b[49m"), line.lastIndexOf("\x1b[0m"));
  assert.ok(separator >= 0 && lineBackground > separator, "each code-line lead remains background-free");
  assert.ok(reset > lineBackground, "each code payload resets its background before the line ends");
}

const tool = await capture("glitchcity", "work", "tool", "23:07", "absolute", "✓ shell 3ms\nremaining\n0");
assert.match(tool.plain, /^23:07│TOOL›  ✓ shell 3ms/u);
assert.match(tool.plain, /\n {5}│ {7}remaining/u);
assert.match(tool.ansi, /\x1b\[38;2;66;217;200m/u);
assert.match(tool.ansi, /\x1b\[38;2;138;62;114m/u);

const relativeTool = await capture(
  "glitchcity",
  "work",
  "tool",
  "+07s",
  "relative",
  "✓ read_file 1542 chars",
  48,
);
assert.match(relativeTool.plain, /^ \+07s│TOOL›  ✓ read_file 1542 chars/u);
assert.match(relativeTool.ansi, /\x1b\[2m/u);
assert.match(relativeTool.ansi, /\x1b\[38;2;66;217;200m/u);
assert.match(relativeTool.ansi, /\x1b\[38;2;66;217;200m\x1b\[2m \+07s/u);

const wrappedAssistant = await capture(
  "glitchcity",
  "work",
  "assistant",
  "",
  "absolute",
  "This assistant answer is deliberately long enough to wrap onto another line.",
);
assert.match(wrappedAssistant.plain, /\n {5}│ {7}\S/u);

const reasoningTable = await capture(
  "glitchcity",
  "work",
  "reasoning",
  "",
  "absolute",
  "| A | B |\n|---|---|\n| 1 | 2 |",
);
assert.equal(
  reasoningTable.plain.split(/\r?\n/).slice(1).every((line) => /^ {5}│ {7}\S/u.test(line)),
  true,
);

const minimal = await capture("glitchcity", "minimal", "assistant", "23:06", "absolute", "response");
assert.match(minimal.plain, /^23:06│ASSISTANT> response/u);

const gruvbox = await capture("gruvbox", "work", "assistant", "23:06", "absolute", "response");
assert.match(gruvbox.plain, /^23:06::CRT::  response/u);

for (const frame of [user.plain, reasoning.plain, assistant.plain, tool.plain, relativeTool.plain, minimal.plain, gruvbox.plain]) {
  assert.equal(frame.split(/\r?\n/).every((line) => stringWidth(line) <= 40), true);
}

// Beside the rail a list marker and a quote bar stand in the text column, and the lines an
// item wraps onto hang under its own text, not under its marker.
const listed = await capture(
  "glitchcity",
  "work",
  "assistant",
  "23:06",
  "absolute",
  "Two things:\n\n- the first item is long enough to wrap onto a second line here\n- short\n\n1. numbered and also long enough to need a second line\n\n> quoted words that run past the edge of this narrow frame",
  48,
);
const listedLines = listed.plain.split(/\r?\n/);
const startOf = (needle: string) => { const line = listedLines.find((item) => item.includes(needle))!; return line.indexOf(needle); };
const textColumn = startOf("Two things:");
assert.equal(textColumn, 6 + 7);
assert.equal(startOf("- the first item"), textColumn);
assert.equal(startOf("- short"), textColumn);
assert.equal(startOf("1. numbered"), textColumn);
assert.equal(startOf("| quoted"), textColumn);
const after = (needle: string) => listedLines[listedLines.findIndex((item) => item.includes(needle)) + 1];
assert.match(after("- the first item"), new RegExp(`^ {5}│ {${7 + 2}}\\S`), listed.plain);
assert.match(after("1. numbered"), new RegExp(`^ {5}│ {${7 + 3}}\\S`), listed.plain);
assert.match(after("| quoted"), new RegExp(`^ {5}│ {${7 + 2}}\\S`), listed.plain);
assert.equal(listedLines.every((line) => stringWidth(line) <= 48), true, listed.plain);
