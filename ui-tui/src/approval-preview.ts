import stringWidth from "string-width";
import { approvalChoices, type ApprovalChoice } from "@astra/ui-core/approval-choices";
import type { ToolApprovalRequest } from "./types.js";
import { summarizeToolResult, wrapToolResult } from "./tool-results.js";

const MAX_DETAIL_CHARS = 24_000;
const MAX_DETAIL_LINES = 400;

const RISK_LABELS: Record<string, string> = {
  read: "只读",
  write: "写入",
  execute: "执行",
  network: "网络访问",
  destructive: "高风险操作",
};

const TOOL_LABELS: Record<string, string> = {
  workflow_design_approval: "工作流设计审批",
  execute_shell: "执行 Shell 命令",
  execute_python: "执行 Python",
  read_file: "读取文件",
  write_file: "写入文件",
  edit_file: "编辑文件",
  apply_patch: "应用补丁",
};

const ARGUMENT_LABELS: Record<string, string> = {
  command: "命令",
  code: "代码",
  environment: "环境",
  path: "路径",
  summary: "摘要",
};

export interface ApprovalFactItem {
  label: string;
  value: string;
}

export interface ApprovalExplanation {
  label: "模型说明" | "系统说明";
  text: string;
}

export interface ApprovalTargetPreview {
  label: "代码" | "命令" | "目标" | "路径";
  raw: string;
  lines: string[];
}

/**
 * A React-independent representation of an approval card. Model text is kept
 * separate from backend-authoritative facts so a generated explanation cannot
 * alter the request's risk, location, scope, or target.
 */
export interface ApprovalCompactView {
  title: string;
  modelExplanation: ApprovalExplanation;
  facts: ApprovalFactItem[];
  targetPreview: ApprovalTargetPreview;
  collapsedLines: string[];
  detailLines: string[];
  shortcutHint: string;
  queuePosition: string;
}

export interface ApprovalCompactViewOptions {
  width: number;
  position?: number;
  total?: number;
  expanded?: boolean;
}

import { escapeApprovalDisplayText, isUnsafeDisplayCodePoint } from "@astra/ui-core/approval-display";
export { escapeApprovalDisplayText } from "@astra/ui-core/approval-display";

function visibleApprovalText(value: string | undefined): string {
  const candidate = value?.trim() ?? "";
  const visible = Array.from(candidate)
    .filter((character) => !isUnsafeDisplayCodePoint(character.codePointAt(0) ?? 0))
    .join("");
  if (!visible || stringWidth(visible) === 0) return "";
  return escapeApprovalDisplayText(candidate);
}

function displayText(value: string | undefined): string {
  return escapeApprovalDisplayText(value ?? "");
}

function valueText(value: unknown): string {
  if (typeof value === "string") return displayText(value);
  try {
    return displayText(JSON.stringify(value));
  } catch {
    return displayText(String(value));
  }
}

function isComputerTakeoverRequest(request: ToolApprovalRequest): boolean {
  return request.kind === "computer_foreground_takeover";
}

function takeoverArgument(request: ToolApprovalRequest, key: string): string {
  const value = request.arguments?.[key];
  return typeof value === "string" ? displayText(value) : "";
}

function stringArgument(request: ToolApprovalRequest, key: string): string {
  const value = request.arguments?.[key];
  return typeof value === "string" ? value : "";
}

function takeoverActionClasses(request: ToolApprovalRequest): string[] {
  const value = request.arguments?.action_classes;
  if (!Array.isArray(value)) return [];
  const allowed = new Set(["press", "text", "click", "double_click", "scroll", "drag"]);
  return value
    .filter((item): item is string => typeof item === "string" && allowed.has(item))
    .slice(0, 6)
    .map(displayText);
}

function takeoverFacts(request: ToolApprovalRequest): ApprovalFactItem[] {
  const application = takeoverArgument(request, "application") || "Selected application";
  const window = takeoverArgument(request, "window") || "Selected window";
  const reason = takeoverArgument(request, "reason") || "Background control is insufficient";
  const actionClasses = takeoverActionClasses(request);
  const expectedEffect = takeoverArgument(request, "expected_effect") || "Temporarily control the selected window";
  return [
    { label: "应用", value: application },
    { label: "窗口", value: window },
    { label: "原因", value: reason },
    { label: "操作类型", value: actionClasses.join(" · ") || "application interaction" },
    { label: "预期影响", value: expectedEffect },
    { label: "边界", value: request.choices?.includes("session")
      ? "本次 CU 会话内此应用的普通操作；高影响操作另行确认。连续操作时应用保持在前台。"
      : "仅此片段" },
  ];
}

function isFilesystemRequest(request: ToolApprovalRequest): boolean {
  return request.kind === "filesystem" || request.kind === "filesystem_patch";
}

function filesystemAccessLabel(request: ToolApprovalRequest): string {
  return request.access === "write" ? "写入" : "读取";
}

function filesystemScopeLabel(request: ToolApprovalRequest): string {
  switch (request.scope_kind) {
    case "directory":
      return "该目录及其子目录";
    case "batch":
      return "本批次路径";
    case "file":
      return "该文件";
    default:
      return "该路径";
  }
}

function riskLabel(risk: string): string {
  return RISK_LABELS[risk.toLowerCase()] ?? displayText(risk);
}

function toolLabel(toolName: string): string {
  return TOOL_LABELS[toolName] ?? displayText(toolName);
}

function firstTarget(request: ToolApprovalRequest): string {
  if (isComputerTakeoverRequest(request)) {
    return [takeoverArgument(request, "application"), takeoverArgument(request, "window")]
      .filter(Boolean)
      .join(" — ");
  }
  const code = stringArgument(request, "code");
  if (request.tool_name === "execute_python" && code) {
    return displayText(code);
  }
  const targets = request.targets?.filter(Boolean) ?? [];
  const target = targets.length > 0
    ? targets.join(", ")
    : request.target ?? stringArgument(request, "command");
  return displayText(target);
}

function targetLabel(request: ToolApprovalRequest): ApprovalTargetPreview["label"] {
  if (request.tool_name === "execute_python") return "代码";
  if (request.kind === "host_execution") return "命令";
  if (request.targets?.length) return "路径";
  return "目标";
}

function genericApprovalExplanation(request: ToolApprovalRequest): string {
  if (
    request.kind === "workflow_design"
    || request.tool_name === "workflow_design_approval"
  ) {
    return "要批准这份工作流设计吗？";
  }
  if (request.tool_name === "execute_python") {
    const summary = visibleApprovalText(request.approval_summary);
    return summary
      ? `是否允许我在宿主机上${summary}？`
      : "要执行这段 Python 代码吗？";
  }
  if (
    request.kind === "host_execution"
    || request.tool_name === "execute_shell"
    || request.tool_name === "bash"
  ) {
    const summary = visibleApprovalText(request.approval_summary);
    return summary
      ? `是否允许我在宿主机上${summary}？`
      : "要执行这条 Shell 命令吗？";
  }
  if (isFilesystemRequest(request)) {
    const access = filesystemAccessLabel(request);
    const scope = request.scope_kind === "directory"
      ? "这个目录"
      : request.scope_kind === "batch"
        ? "这批路径"
        : request.scope_kind === "file"
          ? "这个文件"
          : "这个路径";
    return `要${access}${scope}吗？`;
  }
  return `要允许${approvalRequestTitle(request)}吗？`;
}

function approvalExplanation(request: ToolApprovalRequest): ApprovalExplanation {
  if (isComputerTakeoverRequest(request)) {
    return { label: "系统说明", text: takeoverArgument(request, "reason") || "需要临时前台接管" };
  }
  const agentReason = visibleApprovalText(request.agent_reason);
  if (agentReason) return { label: "模型说明", text: agentReason };
  const backendQuestion = visibleApprovalText(request.approval_question);
  if (backendQuestion) return { label: "系统说明", text: backendQuestion };
  return { label: "系统说明", text: genericApprovalExplanation(request) };
}

function approvalFacts(request: ToolApprovalRequest): ApprovalFactItem[] {
  if (isComputerTakeoverRequest(request)) return takeoverFacts(request);
  const filesystem = isFilesystemRequest(request);
  const location = request.outside_workspace
    ? "工作区外宿主机"
    : filesystem ? "工作区文件系统"
    : request.kind === "host_execution" ? "宿主机"
      : "当前运行环境";
  const scope = filesystem && request.scope_kind
    ? filesystemScopeLabel(request)
    : request.kind === "host_execution" && request.approval_boundary
      ? displayText(request.approval_boundary.split("；可选会话前缀：", 1)[0].trim())
      : displayText(request.scope);
  const operation = displayText(request.operation ?? request.tool_name);
  const compoundCommandCount = request.compound_command_count
    ?? request.compound_commands?.length;
  const facts: ApprovalFactItem[] = [
    { label: "风险", value: riskLabel(request.risk) },
    { label: "运行位置", value: location },
    ...(scope ? [{ label: "授权范围", value: scope }] : []),
    { label: "操作", value: operation },
    { label: "工具", value: toolLabel(request.tool_name) },
    ...(compoundCommandCount
      ? [{ label: "操作数", value: `${compoundCommandCount} 项` }]
      : []),
    ...(request.approval_boundary
      ? [{ label: "边界", value: displayText(request.approval_boundary) }]
      : []),
  ];
  return facts;
}

export function approvalRequestTitle(request: ToolApprovalRequest): string {
  if (isComputerTakeoverRequest(request)) return "临时接管 macOS 应用";
  if (request.approval_title) return displayText(request.approval_title);
  if (isFilesystemRequest(request)) {
    if (request.scope_kind === "batch") return "宿主机文件系统批量修改";
    return request.outside_workspace
      ? `工作区外宿主机文件系统${filesystemAccessLabel(request)}`
      : `文件系统${filesystemAccessLabel(request)}`;
  }
  return toolLabel(request.tool_name);
}

const MAX_SUMMARY_LINES = 4;
const MAX_SUMMARY_REASON_CHARS = 120;
const MAX_SUMMARY_TARGET_CHARS = 160;

/**
 * Collapsed first-screen lines follow the small approval prompt used by dsh:
 * one question and one concrete target. The complete request remains
 * available in {@link approvalRequestLines}.
 * @param request - the pending approval request.
 * @param width - the render width to wrap against.
 * @returns at most four rendered lines.
 */
export function approvalSummaryLines(
  request: ToolApprovalRequest,
  width: number,
): string[] {
  if (isComputerTakeoverRequest(request)) {
    const facts = takeoverFacts(request);
    const application = facts[0]?.value ?? "Selected application";
    const window = facts[1]?.value ?? "Selected window";
    const reason = facts[2]?.value ?? "";
    return wrapToolResult(
      [`临时接管 ${application} · ${window}`, reason].filter(Boolean).join("\n"),
      Math.max(20, width),
    ).slice(0, MAX_SUMMARY_LINES);
  }
  const operation = displayText(request.operation ?? request.tool_name);
  const targets = request.targets?.filter(Boolean) ?? [];
  const target = targets.length > 0
    ? displayText(targets.join(", "))
    : displayText(request.target ?? stringArgument(request, "command"));
  const lines = [approvalSummaryQuestion(request)];
  if (target) {
    const targetLine = target === operation ? target : `${operation} · ${target}`;
    lines.push(summarizeToolResult(targetLine, MAX_SUMMARY_TARGET_CHARS));
  }
  const wrapped = wrapToolResult(lines.join("\n"), Math.max(20, width));
  if (wrapped.length <= MAX_SUMMARY_LINES) return wrapped;
  return [
    ...wrapped.slice(0, MAX_SUMMARY_LINES - 1),
    `… ${wrapped.length - MAX_SUMMARY_LINES + 1} 更多行`,
  ];
}

function approvalSummaryQuestion(request: ToolApprovalRequest): string {
  return summarizeToolResult(
    approvalExplanation(request).text,
    MAX_SUMMARY_REASON_CHARS,
  );
}

export function approvalSummaryHeadlineLineCount(
  request: ToolApprovalRequest,
  width: number,
): number {
  return wrapToolResult(
    approvalSummaryQuestion(request),
    Math.max(20, width),
  ).length;
}

export function buildApprovalCompactView(
  request: ToolApprovalRequest,
  options: ApprovalCompactViewOptions,
): ApprovalCompactView {
  const width = Math.max(20, options.width);
  const rawTarget = firstTarget(request);
  const targetPreview: ApprovalTargetPreview = {
    label: targetLabel(request),
    raw: rawTarget,
    lines: rawTarget
      ? [summarizeToolResult(rawTarget, Math.max(12, width - 12))]
      : [],
  };
  const position = Math.max(1, options.position ?? 1);
  const total = Math.max(position, options.total ?? 1);
  return {
    title: isComputerTakeoverRequest(request)
      ? approvalRequestTitle(request)
      : displayText(request.approval_title ?? request.operation) || approvalRequestTitle(request),
    modelExplanation: approvalExplanation(request),
    facts: approvalFacts(request),
    targetPreview,
    collapsedLines: approvalSummaryLines(request, width),
    detailLines: approvalRequestLines(request, width),
    shortcutHint: approvalChoiceHint(request, options.expanded ?? false),
    queuePosition: `${position}/${total}`,
  };
}

export function approvalRequestLines(
  request: ToolApprovalRequest,
  width: number,
): string[] {
  if (isComputerTakeoverRequest(request)) {
    return wrapToolResult(
      [approvalRequestTitle(request), ...takeoverFacts(request).map((fact) => `${fact.label}：${fact.value}`)].join("\n"),
      Math.max(20, width),
    ).slice(0, MAX_DETAIL_LINES);
  }
  const filesystem = isFilesystemRequest(request);
  const targets = request.targets?.filter(Boolean) ?? [];
  const explanation = approvalExplanation(request);
  const hasExplicitExplanation = Boolean(
    visibleApprovalText(request.agent_reason) || visibleApprovalText(request.approval_question),
  );
  const detailFacts = approvalFacts(request).filter((fact) =>
    !["风险", "运行位置", "操作", "工具"].includes(fact.label),
  );
  const presentationTitle = displayText(request.approval_title)
    || (filesystem
      ? approvalRequestTitle(request)
      : displayText(request.operation) || toolLabel(request.tool_name));
  const argumentLines = Object.entries(request.arguments ?? {})
    .filter(([key]) =>
      (key !== "path" || !request.target)
      && (key !== "command" || request.kind !== "host_execution")
    )
    .filter(([key, value]) =>
      !(key === "summary" && request.detail && valueText(value) === request.detail)
    )
    .map(([key, value]) => `${ARGUMENT_LABELS[key] ?? displayText(key)}：${valueText(value)}`);
  const compoundCommands = request.compound_commands?.map(displayText) ?? [];
  const compoundCommandTotal = request.compound_command_count ?? compoundCommands.length;
  const compoundCommandLabel = compoundCommandTotal > compoundCommands.length
    ? `操作预览（显示 ${compoundCommands.length}/${compoundCommandTotal} 项）`
    : "操作";
  const sources = [
    `${presentationTitle} · 风险：${riskLabel(request.risk)}`,
    hasExplicitExplanation ? `${explanation.label}：${explanation.text}` : "",
    request.reason ? `系统事实：${displayText(request.reason)}` : "",
    ...detailFacts.map((fact) => `${fact.label}：${fact.value}`),
    request.approval_summary ? `内容：${displayText(request.approval_summary)}` : "",
    request.approval_effect ? `影响：${displayText(request.approval_effect)}` : "",
    filesystem && request.outside_workspace && !request.approval_effect
      ? "边界：仅在你批准后 agent 才能访问该宿主机路径；不授予 shell 权限"
      : "",
    filesystem && request.workspace ? `工作区：${displayText(request.workspace)}` : "",
    targets.length > 0
      ? `路径（${targets.length}）：${displayText(targets.join(", "))}`
      : request.target
        ? request.tool_name === "execute_python"
          ? `工作目录：${displayText(request.target)}`
          : request.kind === "host_execution"
          ? `命令：${displayText(request.target)}`
          : `目标：${displayText(request.target)}`
        : "",
    compoundCommands.length
      ? `${compoundCommandLabel}：${compoundCommands.join(" · ")}`
      : "",
    ...argumentLines,
    request.change_summary
      ? `变更：${[
          displayText(request.change_summary.kind),
          request.change_summary.files !== undefined ? `${request.change_summary.files} 个文件` : "",
          `+${request.change_summary.additions ?? 0}`,
          `-${request.change_summary.deletions ?? 0}`,
        ].filter(Boolean).join(" · ")}`
      : "",
    request.scopes?.length
      ? `范围：${displayText(request.scopes.join(", "))}`
      : request.scope ? `范围：${displayText(request.scope)}` : "",
    request.verifier ? `校验：${displayText(request.verifier)}` : "",
    request.detail ? `详情：${displayText(request.detail)}` : "",
    displayText(request.preview),
  ].filter(Boolean);

  const raw = sources.join("\n");
  const bounded = raw.length > MAX_DETAIL_CHARS
    ? `${raw.slice(0, MAX_DETAIL_CHARS)}\n… 审批详情过长，已截断至 ${MAX_DETAIL_CHARS.toLocaleString()} 字符`
    : raw;
  const lines = wrapToolResult(bounded, Math.max(20, width));
  if (lines.length <= MAX_DETAIL_LINES) return lines;
  return [
    ...lines.slice(0, MAX_DETAIL_LINES - 1),
    "… 审批详情已省略",
  ];
}

export function compactApprovalLines(lines: string[], maxLines: number): string[] {
  const safeMax = Math.max(2, maxLines);
  if (lines.length <= safeMax) return lines;
  return [
    ...lines.slice(0, safeMax - 1),
    `… ${lines.length - safeMax + 1} 更多行 · 按 V 展开`,
  ];
}

/**
 * The keyboard-choice row: collapsed shows only allow-once and deny; the
 * session grant appears once the detail view is expanded.
 * @param request - the pending approval request.
 * @param expanded - whether the detail view is open.
 * @returns the human-readable key hints, joined.
 */
export function approvalChoiceHint(
  request: ToolApprovalRequest,
  expanded: boolean,
): string {
  const choices: readonly ApprovalChoice[] = !request.choices?.length && isComputerTakeoverRequest(request)
    ? ["once", "deny"]
    : approvalChoices(request);
  const draft = Boolean(request.draft?.text);
  const sessionLabel = isComputerTakeoverRequest(request)
    ? "本次 CU 会话内允许此应用的普通操作"
    : request.scope_kind === "directory"
    ? "会话内允许该目录"
    : request.scope_kind === "batch"
      ? "会话内允许本批次"
      : request.scope_kind === "file"
        ? "会话内允许该文件"
      : request.session_scope_label
        ? `会话内允许 ${displayText(request.session_scope_label)}`
      : "会话内允许";
  return [
    choices.includes("once") ? (draft ? "[Y/回车] 发送" : "[Y/回车] 仅本次允许") : "",
    expanded && choices.includes("task") ? "[T] 本任务允许" : "",
    (expanded || isComputerTakeoverRequest(request)) && choices.includes("session") ? `[A] ${sessionLabel}` : "",
    expanded && choices.includes("always") ? "[L] 以后都允许" : "",
    choices.includes("deny") ? (draft ? (expanded ? "[N] 丢弃" : "[N/Esc] 丢弃") : expanded ? "[N] 拒绝" : "[N/Esc] 拒绝") : "",
  ].filter(Boolean).join("   ");
}
