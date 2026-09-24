import assert from "node:assert/strict";
import { PassThrough, Writable } from "node:stream";
import React from "react";
import { render } from "ink";
import stripAnsi from "strip-ansi";
import stringWidth from "string-width";
import { THEMES } from "../theme.js";
import { ThemeProvider } from "../theme-context.js";
import { ActivityDock, activityVisualState, buildActivitySummary, formatToolElapsed, formatToolName, formatToolProgress } from "./activity-dock.js";

// --- helpers (unchanged) ---

assert.equal(formatToolElapsed(1_000, 2_250), "1.2s");
assert.equal(formatToolName("read_file"), "READ");
assert.equal(formatToolName("stat_file"), "STAT");
assert.equal(formatToolName("apply_patch"), "PATCH");
assert.equal(formatToolName("execute_shell"), "execute_shell");
assert.equal(formatToolProgress({ stage: "running", status: "running" }), "RUNNING");
assert.equal(formatToolProgress({ stage: "retrying", status: "running", current: 2, total: 3 }), "RETRYING · 2/3");
assert.equal(formatToolProgress({ stage: "downloading", status: "running", percent: 42.4 }), "DOWNLOADING · 42%");
assert.equal(activityVisualState("ready", 0), "idle");
assert.equal(activityVisualState("thinking", 0), "busy");
assert.equal(activityVisualState("正在压缩上下文…", 0), "busy");
assert.equal(activityVisualState("bar · talking", 0), "busy");
assert.equal(activityVisualState("bar · private shift", 0), "idle");
assert.equal(activityVisualState("task FD6661 · running", 0), "busy");
assert.equal(activityVisualState("tool execute_shell · 1.2s", 1), "busy");
assert.equal(activityVisualState("ready", 0, true), "busy");
assert.equal(activityVisualState("disconnected", 0), "error");
assert.equal(activityVisualState("task FD6661 · failed", 0), "error");

class TestStdin extends PassThrough {
  isTTY = true;
  setRawMode() {}
  ref() { return this; }
  unref() { return this; }
}

class TestStdout extends Writable {
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

async function renderActivityDock({ columns = 80, ...props }: Partial<React.ComponentProps<typeof ActivityDock>> & { columns?: number } = {}) {
  const stdin = new TestStdin();
  const stdout = new TestStdout(columns);
  const instance = render(
    React.createElement(
      ThemeProvider,
      {
        theme: THEMES.hermes,
        children: React.createElement(ActivityDock, {
          expanded: false,
          tools: [],
          now: 1_000,
          memory: {},
          model: "Qwen3.6-35B-A3B",
          totalTokens: 0,
          promptTokens: 0,
          contextPct: 0,
          status: "ready",
          showReasoning: false,
          sessionName: "",
          columns,
          ...props,
        }),
      },
    ),
    {
      stdin: stdin as unknown as NodeJS.ReadStream,
      stdout: stdout as unknown as NodeJS.WriteStream,
      stderr: stdout as unknown as NodeJS.WriteStream,
      patchConsole: false,
      exitOnCtrlC: false,
    },
  );
  await new Promise((resolve) => setTimeout(resolve, 80));
  instance.unmount();
  return stripAnsi(stdout.chunks.join(""));
}

const collapsedActive = await renderActivityDock({
  computer: { active: true, handedOff: false, control: "background", application: "WPS Office" },
});
assert.match(collapsedActive, /COOP · WPS Office/);
assert.doesNotMatch(collapsedActive, /STATUS/);

const collapsedHandoff = await renderActivityDock({
  computer: { active: true, handedOff: true, application: "Finder" },
});
assert.match(collapsedHandoff, /USER CONTROL · Finder/);

const expandedWithoutDetails = await renderActivityDock({
  expanded: true,
  computer: { active: true, handedOff: true, application: "Finder" },
});
assert.match(expandedWithoutDetails, /USER CONTROL · Finder/);
assert.match(expandedWithoutDetails, /STATUS/);
assert.doesNotMatch(expandedWithoutDetails, /No tool activity yet/);

const expandedWithDetails = await renderActivityDock({
  expanded: true,
  memory: { goal: "verify one indicator" },
  computer: { active: true, handedOff: false, control: "foreground_takeover", application: "WPS Office" },
});
assert.equal((expandedWithDetails.match(/TAKEOVER/g) ?? []).length, 1);

const pausedComputer = await renderActivityDock({
  computer: { active: true, handedOff: false, control: "paused", application: "WPS Office" },
});
assert.match(pausedComputer, /COMPUTER PAUSED/);

const narrowComputer = await renderActivityDock({
  columns: 32,
  computer: { active: true, handedOff: true, application: "文档👩‍💻e\u0301" },
});
assert.match(narrowComputer, /USER CONTROL/);
assert.equal(narrowComputer.split(/\r?\n/).every((line) => stringWidth(line) <= 32), true);

const inactiveComputer = await renderActivityDock({
  computer: { active: false, handedOff: false, permission: "available" },
});
assert.doesNotMatch(inactiveComputer, /COMPUTER (?:ACTIVE|DEGRADED|UNSUPPORTED)|USER CONTROL/);

const degradedComputer = await renderActivityDock({
  computer: { active: false, handedOff: false, permission: "degraded" },
});
assert.match(degradedComputer, /COMPUTER DEGRADED/);

const unsupportedComputer = await renderActivityDock({
  computer: { active: false, handedOff: false, permission: "unsupported" },
});
assert.match(unsupportedComputer, /COMPUTER UNSUPPORTED/);

// --- existing baseline (no mode, unchanged) ---

const base = {
  status: "ready",
  tools: [],
  now: 2_250,
  memory: { steps: [
    { text: "Inspect", status: "completed" as const },
    { text: "Implement", status: "in_progress" as const },
  ] },
  contextPct: 38.4,
  columns: 90,
  model: "Qwen3.6-35B-A3B",
  showReasoning: true,
};

assert.deepEqual(buildActivitySummary(base), ["READY", "Qwen3.6-35B-A3B", "PLAN 1/2", "CTX 38%", "R"]);
assert.deepEqual(buildActivitySummary({
  ...base,
  columns: 60,
  tools: [{ id: "1", name: "very_long_shell_command_name", startedAt: 1_000 }],
}), ["RUN very_long… 1.2s", "Qwen3.6-35B-A3B", "P1/2", "C38%", "R"]);
assert.deepEqual(buildActivitySummary({
  ...base,
  columns: 130,
  lastTool: { id: 3, name: "read_file", output: "ok", error: "", durationMs: 42 },
}), ["READY", "Qwen3.6-35B-A3B", "PLAN 1/2", "CTX 38%", "R", "LAST ✓ READ 42ms"]);

assert.deepEqual(buildActivitySummary({
  ...base,
  pendingApproval: {
    queueIndex: 1,
    queueTotal: 2,
    toolName: "execute_shell",
    status: "awaiting",
  },
}), ["APPROVAL 1/2 · execute_shell · AWAITING", "Qwen3.6-35B-A3B", "PLAN 1/2", "CTX 38%", "R"]);

// --- reasoningEffort display (idle) ---

const baseWithMode = { ...base, reasoningEffort: "max" as const };

// Wide: EFFORT MAX
assert.deepEqual(buildActivitySummary({ ...baseWithMode, columns: 130 }),
  ["READY", "EFFORT MAX", "Qwen3.6-35B-A3B", "PLAN 1/2", "CTX 38%", "R"]);
assert.deepEqual(buildActivitySummary({ ...baseWithMode, columns: 130, reasoningEffort: "high" }),
  ["READY", "EFFORT HIGH", "Qwen3.6-35B-A3B", "PLAN 1/2", "CTX 38%", "R"]);
assert.deepEqual(buildActivitySummary({ ...baseWithMode, columns: 130, reasoningEffort: "xhigh" }),
  ["READY", "EFFORT XHIGH", "Qwen3.6-35B-A3B", "PLAN 1/2", "CTX 38%", "R"]);

// Standard: MAX / HIGH
assert.deepEqual(buildActivitySummary({ ...baseWithMode, columns: 90 }),
  ["READY", "MAX", "Qwen3.6-35B-A3B", "PLAN 1/2", "CTX 38%", "R"]);
assert.deepEqual(buildActivitySummary({ ...baseWithMode, columns: 90, reasoningEffort: "high" }),
  ["READY", "HIGH", "Qwen3.6-35B-A3B", "PLAN 1/2", "CTX 38%", "R"]);

// Compact (60 cols): CODE / HIGH, model shortened to fit
assert.deepEqual(buildActivitySummary({ ...baseWithMode, columns: 60 }),
  ["READY", "MAX", "Qwen3.6-35B-A3B", "P1/2", "C38%", "R"]);
assert.deepEqual(buildActivitySummary({ ...baseWithMode, columns: 60, reasoningEffort: "high" }),
  ["READY", "HIGH", "Qwen3.6-35B-A3B", "P1/2", "C38%", "R"]);
assert.deepEqual(buildActivitySummary({ ...baseWithMode, columns: 60, reasoningEffort: "xhigh" }),
  ["READY", "XHIGH", "Qwen3.6-35B-A3B", "P1/2", "C38%", "R"]);

// --- reasoningEffort display during active tool ---

const activeTool = { id: "1", name: "read_file", startedAt: 1_000 };
const baseActive = { ...baseWithMode, columns: 90, tools: [activeTool] };

assert.deepEqual(buildActivitySummary(baseActive),
  ["RUN READ · 1.2s", "MAX", "Qwen3.6-35B-A3B", "PLAN 1/2", "CTX 38%", "R"]);
assert.deepEqual(buildActivitySummary({ ...baseActive, columns: 130 }),
  ["RUN READ · 1.2s", "EFFORT MAX", "Qwen3.6-35B-A3B", "PLAN 1/2", "CTX 38%", "R"]);
assert.deepEqual(buildActivitySummary({ ...baseActive, columns: 60 }),
  ["RUN READ 1.2s", "MAX", "Qwen3.6-35B-A3B", "P1/2", "C38%", "R"]);

// Undefined reasoningEffort: no mode tag inserted at any width
function assertNoModeTag(segments: string[]) {
  const nonTrivial = segments.filter(s => s === "MAX" || s === "HIGH" || s === "EFFORT MAX" || s === "EFFORT HIGH" || s === "MAX");
  assert.equal(nonTrivial.length, 0, `unexpected mode tag: ${JSON.stringify(segments)}`);
}
assertNoModeTag(buildActivitySummary({ ...base, columns: 130, reasoningEffort: undefined }));
assertNoModeTag(buildActivitySummary({ ...base, columns: 90, reasoningEffort: undefined }));
assertNoModeTag(buildActivitySummary({ ...base, columns: 60, reasoningEffort: undefined }));
