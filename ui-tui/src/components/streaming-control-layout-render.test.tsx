import assert from "node:assert/strict";
import { PassThrough, Writable } from "node:stream";
import React from "react";
import stripAnsi from "strip-ansi";
import type { RenderLine } from "../markdown-render-lines.js";
import type { RuntimeMode } from "../types.js";
import { THEMES } from "../theme.js";
import { ThemeProvider } from "../theme-context.js";
import { ActivityDock } from "./activity-dock.js";
import { AppHeader } from "./app-header.js";
import { BarStatusDock } from "./bar-chrome.js";
import { BarShelf } from "./bar-shelf.js";
import { InputBar } from "./input-bar.js";
import { MinimalStatusDock } from "./minimal-chrome.js";
import { LocalStatusDock } from "./local-chrome.js";
import { StreamingControlLayout } from "./streaming-control-layout.js";

process.env.FORCE_COLOR = "3";
const { Box, render } = await import("ink");
const { MessageLine } = await import("../app.js");

class TestStdin extends PassThrough {
  isTTY = true;
  setRawMode() {}
  ref() { return this; }
  unref() { return this; }
}

class CaptureStream extends Writable {
  rows = 40;
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

const COLUMNS = 120;
const modeMarkers: Record<RuntimeMode, { header: string; status: string; input: string; auxiliary?: string }> = {
  work: {
    header: "GLITCH CITY // AGENT BAR",
    status: "THINKING",
    input: "order waiting for model...",
  },
  minimal: {
    header: "MINIMAL MODE",
    status: "RUNNING",
    input: "you waiting for model...",
  },
  local: {
    header: "LOCAL MODE",
    status: "WORKING…",
    input: "you waiting for model...",
  },
  bar: {
    header: "GLITCH CITY // LYRA'S BAR",
    status: "LYRA IS MIXING",
    auxiliary: "BAR SHELF //",
    input: "say waiting for model...",
  },
};

function dynamicLine(mode: RuntimeMode, text: string, index: number): React.ReactNode {
  const line: RenderLine = {
    key: `${mode}-${index}-${text}`,
    role: "assistant",
    text,
    prefix: "lyra ",
    kind: "text",
    spans: [{ kind: "text", text }],
  };
  return <MessageLine key={line.key} line={line} runtimeMode={mode} />;
}

function controls(mode: RuntimeMode): {
  header: React.ReactNode;
  status: React.ReactNode;
  auxiliary?: React.ReactNode;
} {
  if (mode === "bar") {
    return {
      header: null,
      status: <BarStatusDock
        session="layout_bar"
        busy
        disconnected={false}
        ambiance={{ weather: "rain", power: "stable", music: "low_synth", radio: "static" }}
        shift={{ turn_count: 1, phase: "early" }}
        lyraGlass={{ active: false, name: "", note: "", fill: 0 }}
        outputMode="stream"
        columns={COLUMNS}
      />,
      auxiliary: <BarShelf
        drink={{ active: false, name: "", note: "", tone: "clear", temperature: "room", fill: 0 }}
        columns={COLUMNS}
      />,
    };
  }
  if (mode === "minimal") {
    return {
      header: <AppHeader columns={COLUMNS} busy mode="minimal" />,
      status: <MinimalStatusDock
        session="layout_minimal"
        busy
        disconnected={false}
        tools={[]}
        now={2_000}
        columns={COLUMNS}
      />,
    };
  }
  if (mode === "local") {
    return {
      header: <AppHeader columns={COLUMNS} busy mode="local" />,
      status: <LocalStatusDock
        session="layout_local"
        busy
        disconnected={false}
        columns={COLUMNS}
      />,
    };
  }
  return {
    header: <AppHeader columns={COLUMNS} busy mode="work" />,
    status: <ActivityDock
      expanded={false}
      tools={[]}
      now={2_000}
      memory={{}}
      model="layout-model"
      totalTokens={0}
      promptTokens={0}
      contextPct={10}
      status="thinking"
      showReasoning
      sessionName="layout_work"
      columns={COLUMNS}
    />,
  };
}

function view(mode: RuntimeMode, preview: string[]): React.ReactElement {
  const chrome = controls(mode);
  return (
    <ThemeProvider theme={THEMES.glitchcity}>
      <Box flexDirection="column" width={COLUMNS}>
        <StreamingControlLayout
          dynamic={preview.map((text, index) => dynamicLine(mode, text, index))}
          header={chrome.header}
          status={chrome.status}
          auxiliary={chrome.auxiliary}
          input={<InputBar
            onSubmit={() => {}}
            disabled
            sessionList={[]}
            modelList={[]}
            currentTheme="glitchcity"
            runtimeMode={mode}
            columns={COLUMNS}
          />}
        />
      </Box>
    </ThemeProvider>
  );
}

function latestFrame(output: CaptureStream): string {
  const rendered = [...output.chunks].reverse().find((chunk) => stripAnsi(chunk).includes("PREVIEW-"));
  return stripAnsi(rendered ?? "").trimEnd();
}

function assertControlOrder(mode: RuntimeMode, frame: string, visiblePreview: string[]): void {
  const markers = modeMarkers[mode];
  const headerIndex = frame.indexOf(markers.header);
  const statusIndex = frame.indexOf(markers.status);
  const auxiliaryIndex = markers.auxiliary ? frame.indexOf(markers.auxiliary) : -1;
  const inputIndex = frame.indexOf(markers.input);
  assert.ok(headerIndex >= 0, `${mode} header missing\n${frame}`);
  assert.ok(statusIndex > headerIndex, `${mode} status is not immediately below its header\n${frame}`);
  if (markers.auxiliary) {
    assert.ok(auxiliaryIndex > statusIndex, `${mode} auxiliary control is not below status\n${frame}`);
    assert.ok(inputIndex > auxiliaryIndex, `${mode} input is not below auxiliary control\n${frame}`);
  } else {
    assert.ok(inputIndex > statusIndex, `${mode} input is not below status\n${frame}`);
  }
  for (const marker of visiblePreview) {
    const previewIndex = frame.indexOf(marker);
    assert.ok(previewIndex >= 0, `${mode} preview ${marker} missing\n${frame}`);
    assert.ok(previewIndex < headerIndex, `${mode} preview ${marker} leaked into the control region\n${frame}`);
  }
  assert.equal(
    frame.slice(headerIndex).includes("PREVIEW-"),
    false,
    `${mode} rendered dynamic content between its contiguous controls\n${frame}`,
  );
}

for (const mode of ["work", "minimal", "bar", "local"] as const) {
  const stdin = new TestStdin();
  const stdout = new CaptureStream(COLUMNS);
  const instance = render(view(mode, ["PREVIEW-ONE"]), {
    stdin: stdin as unknown as NodeJS.ReadStream,
    stdout: stdout as unknown as NodeJS.WriteStream,
    stderr: stdout as unknown as NodeJS.WriteStream,
    debug: true,
    patchConsole: false,
    exitOnCtrlC: false,
  });

  await new Promise((resolve) => setTimeout(resolve, 10));
  assertControlOrder(mode, latestFrame(stdout), ["PREVIEW-ONE"]);

  instance.rerender(view(mode, ["PREVIEW-ONE", "PREVIEW-TWO"]));
  await new Promise((resolve) => setTimeout(resolve, 10));
  assertControlOrder(mode, latestFrame(stdout), ["PREVIEW-ONE", "PREVIEW-TWO"]);

  instance.rerender(view(mode, ["PREVIEW-FINAL"]));
  await new Promise((resolve) => setTimeout(resolve, 10));
  const replacedFrame = latestFrame(stdout);
  assertControlOrder(mode, replacedFrame, ["PREVIEW-FINAL"]);
  assert.doesNotMatch(replacedFrame, /PREVIEW-(?:ONE|TWO)/u);
  instance.unmount();
}

console.log("streaming control layout Ink tests passed");
