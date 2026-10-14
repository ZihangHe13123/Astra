import { escapeApprovalDisplayText } from "@astra/ui-core/approval-display";
import type { UIEvent } from "@astra/ui-core/session-state";

const MAX_PREVIEW = 24_000;
/** Preserve code layout; make ANSI, bidi and other hidden controls visible like the TUI. */
export function approvalText(value: unknown): string {
  const raw = typeof value === "string" ? value : value == null ? "" : JSON.stringify(value, null, 2);
  const bounded = raw.length > MAX_PREVIEW ? `${raw.slice(0, MAX_PREVIEW)}\n… 审批预览已截断；省略 ${raw.length - MAX_PREVIEW} 字符` : raw;
  return bounded.split("\n").map(escapeApprovalDisplayText).join("\n");
}

export function approvalPreview(event: UIEvent) {
  const args = event.arguments && typeof event.arguments === "object" ? event.arguments : {};
  const code = typeof args.code === "string" ? args.code : "";
  const command = typeof args.command === "string" ? args.command : "";
  const target = code || command || (event.targets?.length ? event.targets.join("\n") : event.target) || args;
  const facts = [
    ["风险", ({ read: "只读", write: "写入", execute: "执行", network: "网络访问", destructive: "高风险操作" } as Record<string, string>)[event.risk] || event.risk],
    ["运行位置", event.outside_workspace ? "工作区外宿主机" : event.kind === "host_execution" ? "宿主机" : event.workspace],
    ["工作目录", code ? event.target : event.workspace],
    ["影响", event.approval_effect], ["授权边界", event.approval_boundary],
    ["会话授权前缀", event.session_scope_label],
    ["操作数", event.compound_command_count ? `${event.compound_command_count} 项` : undefined],
    ["范围", event.scope],
  ].filter((entry): entry is [string, string] => typeof entry[1] === "string" && !!entry[1]);
  return {
    title: approvalText(event.approval_title || event.tool_name),
    explanation: approvalText(event.agent_reason || event.approval_question || event.reason),
    explanationLabel: event.agent_reason ? "模型说明" : "系统说明",
    summary: approvalText(event.approval_summary),
    targetLabel: code ? "代码预览" : command || event.kind === "host_execution" ? "命令预览" : "目标与参数预览",
    target: approvalText(target),
    facts: facts.map(([label, value]) => ({ label, value: approvalText(value) })),
    details: approvalText({ arguments: event.arguments, targets: event.targets, compound_commands: event.compound_commands,
      detail: event.detail, preview: event.preview, scopes: event.scopes, change_summary: event.change_summary }),
  };
}
