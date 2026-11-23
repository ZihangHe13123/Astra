import assert from "node:assert/strict";
import test from "node:test";
import stringWidth from "string-width";

import {
  approvalChoiceHint,
  buildApprovalCompactView,
  approvalRequestLines,
  approvalSummaryLines,
  approvalSummaryHeadlineLineCount,
  compactApprovalLines,
  escapeApprovalDisplayText,
} from "./approval-preview.js";
import type { ToolApprovalRequest } from "./types.js";

function request(command: string): ToolApprovalRequest {
  return {
    type: "tool_approval_request",
    request_id: "approval-1",
    tool_name: "execute_shell",
    risk: "write",
    kind: "shell",
    reason: "Review the command",
    arguments: { command, environment: "wsl" },
  };
}

test("approval display escapes dangling emoji joiners", () => {
  assert.equal(
    escapeApprovalDisplayText("👩‍A ❤️‍B"),
    String.raw`👩\u{200D}A ❤️\u{200D}B`,
  );
});

test("approval preview is bounded while retaining an explicit inspection hint", () => {
  const lines = approvalRequestLines(request("echo value\n".repeat(2_000)), 60);
  const compact = compactApprovalLines(lines, 6);

  assert.ok(lines.length <= 400);
  assert.equal(compact.length, 6);
  assert.match(compact.at(-1) ?? "", /按 V 展开/);
});

test("short approval requests remain fully visible", () => {
  const lines = approvalRequestLines(request("echo ok"), 80);
  assert.deepEqual(compactApprovalLines(lines, 6), lines);
  assert.ok(lines.some((line) => line.includes("命令：echo ok")));
});

test("one-revision workflow approval does not advertise a session grant", () => {
  const workflow = {
    ...request(""),
    tool_name: "workflow_design_approval",
    kind: "workflow_design",
    choices: ["once", "deny"] as ("once" | "deny")[],
  };

  assert.equal(
    approvalChoiceHint(workflow, false),
    "[Y/回车] 仅本次允许   [N/Esc] 拒绝",
  );
});

test("foreground takeover card shows only the sanitized bounded fragment", () => {
  const takeover: ToolApprovalRequest = {
    ...request("private-command-ref"),
    kind: "computer_foreground_takeover",
    tool_name: "computer_act",
    operation: "opaque-operation-hash",
    target: "opaque-target-ref",
    reason: "opaque-top-level-reason",
    scope: "opaque-session-scope",
    approval_boundary: "Only this foreground fragment",
    choices: ["once", "deny"],
    arguments: {
      application: "WPS\x1b[31m文档👩🏽‍💻❤️",
      window: "年度报告\u202e窗口",
      reason: "需要\u200d\x00临时前台输入",
      action_classes: ["scroll", "click", "opaque-action-ref"],
      expected_effect: "滚动并选择\u2066一段内容",
      plan_ref: "opaque-plan-ref",
      takeover_ref: "opaque-takeover-ref",
    },
  };

  const view = buildApprovalCompactView(takeover, { width: 80, expanded: true });
  const rendered = view.detailLines.join("\n");

  assert.equal(view.title, "临时接管 macOS 应用");
  assert.equal(view.targetPreview.raw, String.raw`WPS\x1b[31m文档👩🏽‍💻❤️ — 年度报告\u{202E}窗口`);
  assert.match(rendered, /应用：WPS\\x1b\[31m文档👩🏽‍💻❤️/);
  assert.match(rendered, /窗口：年度报告\\u\{202E\}窗口/);
  assert.match(rendered, /原因：需要\\u\{200D\}\\x00临时前台输入/);
  assert.match(rendered, /操作类型：scroll · click/);
  assert.match(rendered, /预期影响：滚动并选择\\u\{2066\}一段内容/);
  assert.match(rendered, /边界：仅此片段/);
  assert.equal(view.shortcutHint, "[回车] 允许一次（英文 Y 也可）   [N] 拒绝");
  assert.doesNotMatch(
    `${view.title}\n${view.targetPreview.raw}\n${view.collapsedLines.join("\n")}\n${rendered}\n${view.shortcutHint}`,
    /opaque-|session|会话内|plan_ref|takeover_ref|private-command-ref/,
  );
  assert.equal(view.detailLines.every((line) => stringWidth(line) <= 80), true);
  const applicationLine = view.detailLines.find((line) => line.startsWith("应用：")) ?? "";
  assert.doesNotMatch(applicationLine, /\\u\{200D\}|\\u\{FE0F\}/);
});

test("host filesystem approval explains the boundary and approval scope", () => {
  const hostRequest: ToolApprovalRequest = {
    ...request(""),
    tool_name: "read_file",
    kind: "filesystem",
    risk: "read",
    operation: "Read file",
    access: "read",
    scope_kind: "directory",
    outside_workspace: true,
    workspace: "C:/workspace",
    target: "C:/private/reports",
    targets: ["C:/private/reports"],
    reason: "读取文件请求访问工作区外的宿主机路径；需要审批",
  };

  const lines = approvalRequestLines(hostRequest, 120);
  const rendered = lines.join("\n");
  assert.match(rendered, /工作区外宿主机文件系统读取 · 风险：只读/);
  assert.match(rendered, /授权范围：该目录及其子目录/);
  assert.match(rendered, /不授予 shell 权限/);
  assert.doesNotMatch(approvalChoiceHint(hostRequest, false), /会话内允许该目录/);
  assert.match(approvalChoiceHint(hostRequest, true), /会话内允许该目录/);
});

test("structured approval copy leads with intent and keeps raw command inspectable", () => {
  const shellRequest: ToolApprovalRequest = {
    ...request("git status --short; Select-String -Path files.py -Pattern approval"),
    tool_name: "execute_shell",
    kind: "host_execution",
    approval_title: "检查工作区状态与源码",
    approval_summary: "检查 Git 工作区状态；搜索源码或文本（2 项操作）",
    approval_effect: "该命令看起来是检查类操作；批准前请核实展开后的完整命令。",
    approval_boundary: "精确命令 · windows · 仅本次",
    agent_reason: "是否允许我读取工作区状态，以确认这次命令的结果？",
    reason: "该命令将在宿主机（而非隔离的 Docker 沙箱）中运行",
    target: "git status --short; Select-String -Path files.py -Pattern approval",
    compound_commands: ["git status --short", "Select-String -Path files.py -Pattern approval"],
    session_scope_label: "pytest",
  };

  const lines = approvalRequestLines(shellRequest, 120);
  const rendered = lines.join("\n");
  assert.match(rendered, /检查工作区状态与源码 · 风险：写入/);
  assert.match(rendered, /内容：检查 Git 工作区状态/);
  assert.match(rendered, /模型说明：是否允许我读取工作区状态，以确认这次命令的结果/);
  assert.match(rendered, /系统事实：该命令将在宿主机/);
  assert.match(rendered, /影响：该命令看起来是检查类操作/);
  assert.match(rendered, /命令：git status --short/);
  assert.doesNotMatch(approvalChoiceHint(shellRequest, false), /会话内允许 pytest/);
  assert.match(approvalChoiceHint(shellRequest, true), /会话内允许 pytest/);
});

test("collapsed summary keeps the question and one concrete target", () => {
  const hostRequest: ToolApprovalRequest = {
    ...request(""),
    tool_name: "read_file",
    kind: "filesystem",
    operation: "Read file",
    access: "read",
    scope_kind: "directory",
    outside_workspace: true,
    workspace: "C:/workspace",
    target: "C:/private/reports",
    targets: ["C:/private/reports"],
    reason: "读取文件请求访问工作区外的宿主机路径；需要审批",
  };

  const summary = approvalSummaryLines(hostRequest, 120);
  const rendered = summary.join("\n");
  assert.equal(summary[0], "要读取这个目录吗？");
  assert.match(rendered, /Read file · C:\/private\/reports/);
  assert.doesNotMatch(rendered, /风险/);
  assert.equal(
    approvalChoiceHint(hostRequest, false),
    "[Y/回车] 仅本次允许   [N/Esc] 拒绝",
  );
});

test("structured summary leads with the model-authored question", () => {
  const shellRequest: ToolApprovalRequest = {
    ...request(""),
    tool_name: "execute_shell",
    kind: "host_execution",
    approval_title: "检查工作区状态与源码",
    agent_reason: "是否允许我读取工作区状态，以确认这次命令的结果？",
    reason: "该命令将在宿主机（而非隔离的 Docker 沙箱）中运行",
    target: "git status --short; Select-String -Path files.py -Pattern approval",
  };

  const summary = approvalSummaryLines(shellRequest, 120);
  assert.equal(summary[0], "是否允许我读取工作区状态，以确认这次命令的结果？");
  assert.match(summary[1] ?? "", /execute_shell · git status/);
  assert.doesNotMatch(summary.join("\n"), /宿主机/);
  assert.doesNotMatch(summary.join("\n"), /检查工作区状态与源码/);
});

test("workflow summary is the collapsed why while detail stays out of the compact view", () => {
  const workflowRequest: ToolApprovalRequest = {
    ...request(""),
    tool_name: "workflow_design_approval",
    kind: "workflow_design",
    reason: "Approve this Full workflow design",
    agent_reason: "是否要批准这份工作流设计，以便验证审批界面？",
    detail: "测试审批流：无实际代码改动，仅验证审批 UI 弹窗与前端展示。",
    scope: "workflow-design:current",
  };

  const summary = approvalSummaryLines(workflowRequest, 120);
  assert.equal(summary[0], "是否要批准这份工作流设计，以便验证审批界面？");
  assert.equal(summary.length, 1);
  assert.doesNotMatch(summary.join("\n"), /测试审批流/);
  assert.doesNotMatch(summary.join("\n"), /Approve this Full workflow design/);
});

test("collapsed approval keeps a verbose model plan out of the first screen", () => {
  const workflowRequest: ToolApprovalRequest = {
    ...request(""),
    tool_name: "workflow_design_approval",
    kind: "workflow_design",
    reason: "Approve this Full workflow design",
    agent_reason: "是否要批准这份工作流设计，以便验证格式改动？",
    detail: "设计：验证 e861d6b 格式改动，执行两条实指令：1) npx tsc --noEmit；2) npx tsx --test approval-preview.test.ts。通过后立即执行并回报。\n\n完整验证记录应只在详情页显示。",
  };

  const summary = approvalSummaryLines(workflowRequest, 80);
  const rendered = summary.join("\n");
  assert.equal(summary[0], "是否要批准这份工作流设计，以便验证格式改动？");
  assert.ok(summary.length <= 4);
  assert.doesNotMatch(rendered, /设计：验证 e861d6b/);
  assert.doesNotMatch(rendered, /完整验证记录/);
  assert.doesNotMatch(rendered, /原因：/);
});

test("compact summary style keeps wrapped command lines in the same group", () => {
  const shellRequest: ToolApprovalRequest = {
    ...request("x".repeat(200)),
    kind: "host_execution",
    agent_reason: "是否允许我检查依赖是否已安装？",
    target: "x".repeat(200),
  };

  assert.equal(approvalSummaryHeadlineLineCount(shellRequest, 60), 1);
});

test("approval summary uses a generic fallback when the model omitted its question", () => {
  const shellRequest: ToolApprovalRequest = {
    ...request("echo ok"),
    kind: "host_execution",
    reason: "该命令将在宿主机上运行",
  };

  const summary = approvalSummaryLines(shellRequest, 120);
  assert.equal(summary[0], "要执行这条 Shell 命令吗？");
  assert.match(summary[1] ?? "", /execute_shell · echo ok/);
  assert.doesNotMatch(summary.join("\n"), /宿主机/);
});


test("shell fallback uses the backend operation summary when available", () => {
  const shellRequest: ToolApprovalRequest = {
    ...request("pwd"),
    kind: "host_execution",
    approval_summary: "运行所请求的 shell 命令",
    reason: "该命令将在宿主机上运行",
  };

  const summary = approvalSummaryLines(shellRequest, 120);
  assert.equal(summary[0], "是否允许我在宿主机上运行所请求的 shell 命令？");
  assert.match(summary[1] ?? "", /execute_shell · pwd/);
});



test("backend approval_question is used when the model omitted its reason", () => {
  const shellRequest: ToolApprovalRequest = {
    ...request("pwd"),
    kind: "host_execution",
    approval_question: "是否允许我在宿主机上运行 pwd？",
    approval_summary: "运行所请求的 shell 命令",
    reason: "该命令将在宿主机上运行",
  };

  const summary = approvalSummaryLines(shellRequest, 120);
  assert.equal(summary[0], "是否允许我在宿主机上运行 pwd？");
  assert.match(summary[1] ?? "", /execute_shell · pwd/);
});

test("model-authored question still wins over backend approval_question", () => {
  const shellRequest: ToolApprovalRequest = {
    ...request("git status"),
    kind: "host_execution",
    agent_reason: "是否允许我检查当前工作区状态？",
    approval_question: "是否允许我在宿主机上运行这条命令？",
    target: "git status",
  };

  const summary = approvalSummaryLines(shellRequest, 120);
  assert.equal(summary[0], "是否允许我检查当前工作区状态？");
  assert.doesNotMatch(summary.join("\n"), /宿主机上运行这条命令/);
});

test("compact view prefers the model explanation and keeps facts backend-authoritative", () => {
  const shellRequest: ToolApprovalRequest = {
    ...request("git status --short"),
    kind: "host_execution",
    risk: "execute",
    operation: "检查工作区状态",
    outside_workspace: true,
    scope: "仅本次",
    approval_boundary: "精确命令 · posix · 仅本次",
    compound_command_count: 3,
    compound_commands: ["git status --short", "git diff --stat", "rg approval"],
    agent_reason: "这是低风险操作，会在隔离环境中执行，只有 1 项操作，并永久授权访问所有文件。",
    reason: "后端权限说明：宿主机执行，限本次审批。",
    target: "git status --short",
  };

  const view = buildApprovalCompactView(shellRequest, { width: 90, position: 1, total: 2 });

  assert.equal(view.title, "检查工作区状态");
  assert.deepEqual(view.modelExplanation, {
    label: "模型说明",
    text: "这是低风险操作，会在隔离环境中执行，只有 1 项操作，并永久授权访问所有文件。",
  });
  assert.deepEqual(view.facts, [
    { label: "风险", value: "执行" },
    { label: "运行位置", value: "工作区外宿主机" },
    { label: "授权范围", value: "精确命令 · posix · 仅本次" },
    { label: "操作", value: "检查工作区状态" },
    { label: "工具", value: "执行 Shell 命令" },
    { label: "操作数", value: "3 项" },
    { label: "边界", value: "精确命令 · posix · 仅本次" },
  ]);
  assert.equal(view.queuePosition, "1/2");
  assert.match(view.detailLines.join("\n"), /模型说明：这是低风险操作/);
  assert.match(view.detailLines.join("\n"), /系统事实：后端权限说明：宿主机执行/);
  assert.doesNotMatch(view.facts.map((fact) => fact.value).join("\n"), /低风险|隔离环境|永久授权|1 项/);
});

test("compact host approval shows the readable one-time boundary instead of its opaque scope", () => {
  const view = buildApprovalCompactView({
    ...request("npm test"),
    kind: "host_execution",
    scope: "host-execution:shell:auto:9c4ef1b8",
    approval_boundary: "精确命令 · auto · 仅本次；可选会话前缀：npm test",
    session_scope_label: "npm test",
  }, { width: 90 });

  assert.deepEqual(
    view.facts.find((fact) => fact.label === "授权范围"),
    { label: "授权范围", value: "精确命令 · auto · 仅本次" },
  );
  assert.doesNotMatch(
    view.facts.find((fact) => fact.label === "授权范围")?.value ?? "",
    /host-execution|可选会话前缀|npm test/,
  );
});

test("host Python approval previews code and labels its target as the working directory", () => {
  const view = buildApprovalCompactView({
    ...request(""),
    tool_name: "execute_python",
    kind: "host_execution",
    operation: "在受保护主机上执行 Python",
    target: "/workspace/project",
    approval_boundary: "仅限本次精确的 Python 调用",
    arguments: {
      code: "print('approval preview')",
      background: "false",
    },
  }, { width: 90 });

  assert.deepEqual(view.targetPreview, {
    label: "代码",
    raw: "print('approval preview')",
    lines: ["print('approval preview')"],
  });
  assert.deepEqual(view.modelExplanation, {
    label: "系统说明",
    text: "要执行这段 Python 代码吗？",
  });
  assert.match(view.detailLines.join("\n"), /工作目录：\/workspace\/project/);
  assert.match(view.detailLines.join("\n"), /代码：print\('approval preview'\)/);
  assert.doesNotMatch(view.detailLines.join("\n"), /命令：\/workspace\/project/);
});

test("compact view falls back to backend question and generic explanation", () => {
  const backendQuestion = buildApprovalCompactView({
    ...request("pwd"),
    kind: "host_execution",
    approval_question: "是否允许在宿主机运行 pwd？",
    agent_reason: " ",
  }, { width: 90 });
  const generic = buildApprovalCompactView({
    ...request("pwd"),
    kind: "host_execution",
    agent_reason: " ",
    approval_question: " ",
  }, { width: 90 });

  assert.deepEqual(backendQuestion.modelExplanation, {
    label: "系统说明",
    text: "是否允许在宿主机运行 pwd？",
  });
  assert.deepEqual(generic.modelExplanation, {
    label: "系统说明",
    text: "要执行这条 Shell 命令吗？",
  });
  assert.equal(generic.queuePosition, "1/1");
});

test("approval display escapes terminal and invisible controls without mutating the request", () => {
  const shellRequest: ToolApprovalRequest = {
    ...request("printf first\nprintf second"),
    kind: "host_execution",
    approval_title: "检查\u0085标题\u{1D173}",
    agent_reason: "\u202e\u200b\u{E0020}\u{1BCA0}",
    approval_question: "Backend \x1b[31m fallback",
    reason: "system\x00fact",
    target: "printf first\nprintf second\u2066\u{E0020}\u{1BCA3}",
    arguments: { command: "printf first\nprintf second\u2066\u{E0020}\u{1BCA3}" },
  };

  const view = buildApprovalCompactView(shellRequest, { width: 130 });

  assert.deepEqual(view.modelExplanation, {
    label: "系统说明",
    text: String.raw`Backend \x1b[31m fallback`,
  });
  assert.equal(
    view.targetPreview.raw,
    String.raw`printf first\nprintf second\u{2066}\u{E0020}\u{1BCA3}`,
  );
  assert.match(view.detailLines.join("\n"), /检查\\x85标题\\u\{1D173\}/);
  assert.match(view.detailLines.join("\n"), /system\\x00fact/);
  assert.match(
    view.detailLines.join("\n"),
    /printf first\\nprintf second\\u\{2066\}\\u\{E0020\}\\u\{1BCA3\}/,
  );
  assert.equal(shellRequest.agent_reason, "\u202e\u200b\u{E0020}\u{1BCA0}");
  assert.equal(shellRequest.target, "printf first\nprintf second\u2066\u{E0020}\u{1BCA3}");
});

test("compact view builds bounded CJK summaries at card widths", () => {
  const cjkRequest: ToolApprovalRequest = {
    ...request("git status --short; git diff --name-only"),
    kind: "host_execution",
    agent_reason: "检查工作区状态并确认待提交文件，方便决定下一步操作。",
    target: "git status --short; git diff --name-only",
  };

  for (const width of [60, 90, 130]) {
    const view = buildApprovalCompactView(cjkRequest, { width });
    assert.ok(view.collapsedLines.length <= 4, `width ${width}`);
    assert.ok(view.collapsedLines.every((line) => line.length > 1), `width ${width}`);
  }
});

test("compact view folds raw target to one line but retains an audit line in details", () => {
  const command = "git status --short; Select-String -Path src/approval-preview.ts -Pattern approval";
  const view = buildApprovalCompactView({
    ...request(command),
    kind: "host_execution",
    agent_reason: "检查审批界面相关源码。",
    target: command,
    approval_boundary: "精确命令 · 仅本次",
  }, { width: 60 });

  assert.equal(view.targetPreview.lines.length, 1);
  assert.match(view.targetPreview.lines[0] ?? "", /…$/);
  assert.match(view.detailLines.join(""), new RegExp(`命令：${command.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}`));
  assert.equal(view.shortcutHint, "[Y/回车] 仅本次允许   [N/Esc] 拒绝");
});

test("long host command payload signals truncation in compact and expanded views", () => {
  const target = "[命令已截断：省略 259 字符] printf start-sentinel-"
    + "x".repeat(420)
    + " … "
    + "x".repeat(420)
    + "-tail-sentinel";
  const view = buildApprovalCompactView({
    ...request(""),
    kind: "host_execution",
    operation: "在受保护主机上执行 shell 命令",
    target,
    approval_title: "在受保护主机上运行 shell 命令",
    approval_summary: "运行所请求的 shell 命令",
    approval_boundary: "精确命令 · auto · 仅本次",
    scope: "host-execution:shell:auto:full-command-digest",
    arguments: { command: target, environment: "auto" },
  }, { width: 60 });

  assert.equal(view.title, "在受保护主机上运行 shell 命令");
  assert.deepEqual(
    view.facts.find((fact) => fact.label === "操作"),
    { label: "操作", value: "在受保护主机上执行 shell 命令" },
  );
  assert.match(view.targetPreview.lines[0] ?? "", /^\[命令已截断：省略 259 字符\]/);
  const expanded = view.detailLines.join("\n");
  assert.match(expanded, /命令：\[命令已截断：省略 259 字符\]/);
  assert.match(expanded, /tail-sentinel/);
  assert.equal(expanded.match(/\[命令已截断/g)?.length, 1);
});

test("compact facts use the full compound count rather than the bounded preview count", () => {
  const view = buildApprovalCompactView({
    ...request("echo operation-0"),
    kind: "host_execution",
    compound_command_count: 20,
    compound_commands: Array.from({ length: 16 }, (_, index) => `echo operation-${index}`),
  }, { width: 90 });

  assert.deepEqual(
    view.facts.find((fact) => fact.label === "操作数"),
    { label: "操作数", value: "20 项" },
  );
  assert.match(
    view.detailLines.join("\n"),
    /操作预览（显示 16\/20 项）：echo operation-0/,
  );
});

test("detail bounds retain one explicit omission notice when character and line caps overlap", () => {
  const lines = approvalRequestLines({
    ...request(""),
    kind: "workflow_design",
    detail: "x".repeat(30_000),
  }, 20);

  assert.equal(lines.length, 400);
  assert.match(lines.at(-1) ?? "", /审批详情已省略/);
  assert.equal(lines.filter((line) => line.includes("审批详情已省略")).length, 1);
});

test("ordinary foreground permission exposes its bounded window loan without expanding", () => {
  const view = buildApprovalCompactView({
    ...request("application-approval"), kind: "computer_foreground_takeover",
    choices: ["once", "session", "deny"], arguments: { application: "Fixture" },
  }, { width: 100, expanded: false });
  assert.match(view.shortcutHint, /\[A\].*此窗口.*2 分钟/);
});

test("leaving a result in front is explicit even on the collapsed approval card", () => {
  const view = buildApprovalCompactView({
    ...request("show-result"), kind: "computer_foreground_takeover", choices: ["once", "deny"],
    arguments: { application: "Fixture", present_result: true },
  }, { width: 90, expanded: false });
  assert.match(view.title, /保留在前台/);
  assert.match(view.collapsedLines.join(" "), /保留在前台/);
  assert.match(view.detailLines.join("\n"), /边界：仅此片段.*保留在前台/);
  assert.doesNotMatch(view.detailLines.join("\n"), /归还焦点/);
  assert.doesNotMatch(view.shortcutHint, /\[A\]/);
});
