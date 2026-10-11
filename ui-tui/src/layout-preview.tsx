import React from "react";
import { Writable } from "node:stream";
import { Box, Text, render } from "ink";
import stringWidth from "string-width";
import stripAnsi from "strip-ansi";
import { ActivityDock } from "./components/activity-dock.js";
import { AppHeader } from "./components/app-header.js";
import { BarStatusDock } from "./components/bar-chrome.js";
import { BarShelf } from "./components/bar-shelf.js";
import { ThemeProvider, useTheme } from "./theme-context.js";
import { THEMES, type ThemeName } from "./theme.js";

class CaptureStream extends Writable {
  columns: number;
  rows = 32;
  isTTY = true;
  chunks: string[] = [];

  constructor(columns: number) {
    super();
    this.columns = columns;
  }

  _write(chunk: Buffer | string, _encoding: BufferEncoding, callback: (error?: Error | null) => void) {
    this.chunks.push(String(chunk));
    callback();
  }
}

function PreviewInput() {
  const theme = useTheme();
  return (
    <Box borderStyle={theme.chrome?.inputFrameStyle ?? theme.chrome?.frameStyle ?? "single"} borderColor={theme.accentAlt} paddingX={1}>
      <Text bold color={theme.accent}>{theme.chrome?.promptLabel ?? "you"} </Text>
      <Text color={theme.muted}>type a message</Text>
    </Box>
  );
}

function Fixture({ columns, active = true }: { columns: number; active?: boolean }) {
  const now = 12_500;
  return (
    <Box flexDirection="column" width={columns}>
      <AppHeader columns={columns} busy />
      <Text color="gray">LYRA › Checking the current workspace and preparing the next action.</Text>
      <ActivityDock
        expanded={active && columns >= 90}
        tools={active ? [{
          id: "preview-1",
          name: "read_image",
          arguments: "{ path: screenshot.png, detail: original }",
          startedAt: 1_000,
          progress: { stage: "reading", status: "running", current: 11, unit: "s", message: "screenshot.png" },
        }] : []}
        now={now}
        memory={active ? {
          goal: "Inspect a screenshot and explain the visible result",
          progress: "Screenshot located; reading image",
          steps: [
            { text: "Locate screenshot", status: "completed" },
            { text: "Read image", status: "in_progress" },
            { text: "Explain result", status: "pending" },
          ],
        } : {}}
        model="Qwen3.6-35B-A3B"
        totalTokens={18_400}
        promptTokens={16_200}
        contextUsed={16_200}
        contextPct={38}
        contextLimit={196_608}
        status={active ? "tool read_image" : "ready"}
        showReasoning
        sessionName="layout_preview"
        columns={columns}
      />
      <PreviewInput />
    </Box>
  );
}

function BarFixture({ columns }: { columns: number }) {
  return (
    <Box flexDirection="column" width={columns}>
      <Text color="#D8D4C8">LYRA › The rain has settled in. Your glass is waiting under the neon.</Text>
      <BarStatusDock
        session="bar_20260717_230700"
        busy={false}
        disconnected={false}
        ambiance={{ weather: "downpour", power: "flicker", music: "old_radio", radio: "local_news" }}
        shift={{ turn_count: 6, phase: "deep" }}
        lyraGlass={{ active: true, name: "夜班清水", note: "真柠檬", fill: 2 }}
        outputMode="atomic"
        columns={columns}
      />
      <BarShelf
        drink={{ active: true, name: "夜航灯", note: "接骨木、冷萃与细小水珠", tone: "cyan", temperature: "cold", fill: 2 }}
        columns={columns}
      />
      <Box borderStyle="double" borderColor="#42D9C8" paddingX={1}>
        <Text bold color="#FF4FA3">say </Text><Text color="#8A91A8">talk over the rain...</Text>
      </Box>
    </Box>
  );
}

async function capture(themeName: ThemeName, columns: number, active = true): Promise<string> {
  const stdout = new CaptureStream(columns);
  const instance = render(
    <ThemeProvider theme={THEMES[themeName]}><Fixture columns={columns} active={active} /></ThemeProvider>,
    { stdout: stdout as unknown as NodeJS.WriteStream, debug: true, patchConsole: false, exitOnCtrlC: false },
  );
  await new Promise((resolve) => setTimeout(resolve, 20));
  instance.unmount();
  const frame = stripAnsi(stdout.chunks[0] ?? "").trimEnd();
  const tooWide = frame.split(/\r?\n/).find((line) => stringWidth(line) > columns);
  if (tooWide) throw new Error(`${themeName} ${columns}-column preview overflowed: ${tooWide}`);
  return frame;
}

async function captureBar(columns: number): Promise<string> {
  const stdout = new CaptureStream(columns);
  const instance = render(
    <ThemeProvider theme={THEMES.glitchcity}><BarFixture columns={columns} /></ThemeProvider>,
    { stdout: stdout as unknown as NodeJS.WriteStream, debug: true, patchConsole: false, exitOnCtrlC: false },
  );
  await new Promise((resolve) => setTimeout(resolve, 20));
  instance.unmount();
  const frame = stripAnsi(stdout.chunks[0] ?? "").trimEnd();
  const tooWide = frame.split(/\r?\n/).find((line) => stringWidth(line) > columns);
  if (tooWide) throw new Error(`bar ${columns}-column preview overflowed: ${tooWide}`);
  return frame;
}

const idleFrame = await capture("glitchcity", 130, false);
process.stdout.write(`\n=== glitchcity · 130 columns · idle ===\n${idleFrame}\n`);

for (const themeName of ["classic", "glitchcity"] as const) {
  for (const columns of [60, 90, 130]) {
    const frame = await capture(themeName, columns);
    process.stdout.write(`\n=== ${themeName} · ${columns} columns ===\n${frame}\n`);
  }
}

for (const columns of [60, 90, 130]) {
  const frame = await captureBar(columns);
  process.stdout.write(`\n=== bar · ${columns} columns ===\n${frame}\n`);
}
