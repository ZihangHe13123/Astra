import assert from "node:assert/strict";
import { Writable } from "node:stream";
import React from "react";
import { render } from "ink";
import stringWidth from "string-width";
import stripAnsi from "strip-ansi";
import { ThemeProvider } from "../theme-context.js";
import { THEMES } from "../theme.js";
import { ActivityDock } from "./activity-dock.js";
import type { ComputerUiState } from "./status-bar.js";

type CaptureOptions = {
  status?: string;
  model?: string;
  lastTool?: { id: number; name: string; output: string; error: string; durationMs?: number };
  pendingApproval?: { queueIndex: number; queueTotal: number; toolName: string; status: "awaiting" };
  queuedApprovalCallIds?: Array<string | undefined>;
  tools?: Array<{ id: string; name: string; arguments?: string; startedAt: number }>;
  toolArguments?: string;
  expanded?: boolean;
  reasoningEffort?: "low" | "high" | "xhigh" | "max";
  codeMode?: "native" | "code" | "both";
  memory?: React.ComponentProps<typeof ActivityDock>["memory"];
  showReasoning?: boolean;
  generationStats?: React.ComponentProps<typeof ActivityDock>["generationStats"];
  computer?: ComputerUiState;
};

class CaptureStream extends Writable {
  rows = 8;
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

async function capture(
  columns: number,
  active: boolean,
  options: CaptureOptions = {},
): Promise<string> {
  const output = new CaptureStream(columns);
  const instance = render(
    <ThemeProvider theme={THEMES.glitchcity}>
      <ActivityDock
        expanded={options.expanded ?? false}
        tools={options.tools ?? (active ? [{ id: "1", name: "execute_shell", arguments: options.toolArguments, startedAt: 1_000 }] : [])}
        now={2_250}
        memory={options.memory ?? { steps: [{ text: "Verify", status: "in_progress" }] }}
        model={options.model ?? "Qwen3.6-35B-A3B"}
        totalTokens={0}
        promptTokens={0}
        contextPct={38.4}
        contextLimit={1_000_000}
        status={options.status ?? "ready"}
        pendingApproval={options.pendingApproval}
        queuedApprovalCallIds={options.queuedApprovalCallIds}
        showReasoning={options.showReasoning ?? true}
        reasoningEffort={options.reasoningEffort}
        codeMode={options.codeMode}
        generationStats={options.generationStats}
        lastTool={options.lastTool}
        sessionName="session_test"
        columns={columns}
        computer={options.computer}
      />
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
  return stripAnsi(output.chunks[0] ?? "").trimEnd();
}

const takeoverFrame = await capture(60, false, {
  computer: { active: true, handedOff: false, control: "foreground_takeover", application: "文档👩‍💻e\u0301" },
});
assert.match(takeoverFrame, /TAKEOVER/);
assert.equal(takeoverFrame.split(/\r?\n/).every((line) => stringWidth(line) <= 60), true);

for (const columns of [60, 90, 130]) {
  for (const active of [false, true]) {
    const frame = await capture(columns, active);
    assert.equal(
      frame.split(/\r?\n/).every((line) => stringWidth(line) <= columns),
      true,
      `${active ? "active" : "idle"} dock overflowed at ${columns} columns`,
    );
  }
}

for (const columns of [20, 28, 32]) {
  const frame = await capture(columns, false, {
    memory: {}, computer: { active: true, handedOff: true, control: "user_control", application: "WPS Office" },
  });
  assert.match(frame, /\^L/);
  assert.equal(frame.split(/\r?\n/).every((line) => stringWidth(line) <= columns), true);
  if (columns >= 28) assert.match(frame, /USER CONTROL/);
}

const fastRequest = await capture(130, false, {
  expanded: true, memory: {}, generationStats: { completion_tokens: 2, elapsed_seconds: 0.01, tokens_per_second: 200 },
});
assert.match(fastRequest, /2 tokens \/ 10ms/);
assert.doesNotMatch(fastRequest, /0\.0s/);

const screenshotFrame = await capture(165, false, {
  model: "deepseek-flash", reasoningEffort: "high", codeMode: "native", memory: {},
  lastTool: { id: 1, name: "context_open", output: "ok", error: "", durationMs: 18 },
});
assert.match(screenshotFrame, /READY │ EFFORT HIGH │ TOOLS NATIVE │ deepseek-flash │ CTX 38% │ R │ LAST ✓ context_open 18ms  \^L expand/);
assert.equal(screenshotFrame.split(/\r?\n/).length, 3);

for (const columns of [60, 90, 130, 165]) {
  for (const active of [false, true]) {
    const frame = await capture(columns, active, {
      expanded: true, model: "test-model", reasoningEffort: "high", codeMode: "native", memory: {},
    });
    assert.equal((frame.match(/test-model/g) ?? []).length, 1, `expanded model must appear once at ${columns} columns, active=${active}`);
    const detail = frame.split(/\r?\n/).find((line) => line.includes("STATUS")) ?? "";
    assert.equal(detail.includes("test-model"), false, "model stays in the summary while tools run");
    assert.match(detail, /ctx/);
    if (columns >= 130) assert.match(detail, /session session_test/);
    assert.doesNotMatch(detail, /STATUS │\s*│/);
    assert.doesNotMatch(frame, /No tool activity yet/);
  }
}

for (const columns of [60, 90, 130, 165]) {
  const frame = await capture(columns, true, {
    expanded: true, model: "test-model", reasoningEffort: "high", codeMode: "native", memory: {},
    generationStats: { completion_tokens: 240, elapsed_seconds: 6, tokens_per_second: 40 },
    lastTool: { id: 1, name: "context_open", output: "ok", error: "", durationMs: 18 },
  });
  const summary = frame.split(/\r?\n/)[1] ?? "";
  assert.match(summary, /test-model/);
  assert.match(summary, /(?:CTX |C)38%/);
  assert.match(summary, /40(?:\.0)?\s*(?:tok\/s|t\/s)/);
  assert.match(summary, /\^L/);
  assert.equal((frame.match(/LAST/g) ?? []).length, 1);
  assert.match(frame, /240 tokens \/ 6\.0s/);
  assert.equal(frame.split(/\r?\n/).every((line) => stringWidth(line) <= columns), true);
}

for (const columns of [60, 90, 130]) {
  const frame = await capture(columns, false, { reasoningEffort: "high", codeMode: "native" });
  assert.match(frame, /HIGH.*NATIVE/);
  assert.equal(frame.split(/\r?\n/).every((line) => stringWidth(line) <= columns), true);
}

const longIdleFrame = await capture(120, false, {
  status: "task C0CCDD · running",
  model: "Qwen3.8-27B-MLX-4bit",
  reasoningEffort: "max",
  showReasoning: false,
  lastTool: { id: 7, name: "web_extract", output: "", error: "failed", durationMs: 118 },
});
assert.equal(
  longIdleFrame.split(/\r?\n/).length,
  3,
  "the activity summary stays on one row when its labels are near the terminal width",
);

const approvalFrame = await capture(120, true, {
  pendingApproval: { queueIndex: 1, queueTotal: 2, toolName: "execute_shell", status: "awaiting" },
  toolArguments: "mkdir -p /private/sensitive && rm -rf /private/sensitive",
});
assert.match(approvalFrame, /APPROVAL 1\/2 · execute_shell · AWAITING/);
assert.doesNotMatch(approvalFrame, /\/private\/sensitive|rm -rf/);

const queuedTools = [
  { id: "approval-first", name: "execute_shell", arguments: "echo one-time-token-123", startedAt: 1_000 },
  { id: "approval-second", name: "execute_shell", arguments: "cat /private/second-approval-secret", startedAt: 1_000 },
  { id: "unrelated-read", name: "read_file", arguments: "docs/visible.md", startedAt: 1_000 },
];
const expandedMultiQueueFrame = await capture(180, false, {
  expanded: true,
  pendingApproval: { queueIndex: 1, queueTotal: 2, toolName: "execute_shell", status: "awaiting" },
  queuedApprovalCallIds: ["approval-first", "approval-second"],
  tools: queuedTools,
});
assert.match(expandedMultiQueueFrame, /READ.*docs\/visible\.md/);
assert.doesNotMatch(expandedMultiQueueFrame, /one-time-token-123|second-approval-secret/);

const missingIdApprovalFrame = await capture(180, false, {
  expanded: true,
  pendingApproval: { queueIndex: 1, queueTotal: 2, toolName: "execute_shell", status: "awaiting" },
  queuedApprovalCallIds: ["approval-first", undefined],
  tools: queuedTools,
});
assert.doesNotMatch(missingIdApprovalFrame, /one-time-token-123|second-approval-secret|docs\/visible\.md/);
