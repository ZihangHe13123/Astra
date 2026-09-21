import type { UIEvent } from "@astra/ui-core/session-state";

export function toolStatus(tool: UIEvent): string {
  return tool.error ? "failed" : tool.status || (tool.type === "tool_result" ? "completed" : "running");
}
export function toolEmptyOutput(tool: UIEvent): string {
  if (tool.output || tool.error) return "";
  return toolStatus(tool) === "running" ? "等待结果…" : toolStatus(tool) === "completed" ? "已完成，无文本输出。" : "执行已结束，无结果可显示。";
}
export function toolExpanded(tool: UIEvent): boolean {
  return toolStatus(tool) === "running" || !!tool.error && !tool.historical;
}
