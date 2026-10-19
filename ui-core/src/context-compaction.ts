import type { ContextCompactionEvent } from "./types.js";

export const COMPACTION_LABEL = "正在压缩上下文…";

function validCount(count: unknown): count is number {
  return typeof count === "number" && Number.isSafeInteger(count) && count >= 0;
}

function completionNotice(event: ContextCompactionEvent): string {
  const before = event.tokens_before;
  const after = event.tokens_after;
  if (!validCount(before) || before === 0 || !validCount(after)) {
    return `上下文压缩完成：${event.messages_before} → ${event.messages_after} 条消息。`;
  }
  const method = event.method === "cleanup" ? "轻量清理"
    : event.method === "summary" ? "历史压缩"
    : event.method === "truncate" ? "历史裁剪" : "压缩";
  const format = (value: number) => value.toLocaleString("en-US");
  const percent = (100 * Math.abs(before - after) / before).toFixed(1);
  const savings = after < before ? `释放 ${percent}%` : after > before ? `增加 ${percent}%` : "未减少";
  const overTarget = validCount(event.target_tokens) && event.target_tokens > 0 && after > event.target_tokens;
  const result = overTarget ? "结束" : "完成";
  const target = overTarget ? `仍高于目标 ${format(event.target_tokens!)} tokens。` : "";
  return `上下文${method}${result}：估算 ${format(before)} → ${format(after)} tokens，${savings}；消息 ${event.messages_before} → ${event.messages_after}。${target}`;
}

export function compactionNotice(event: ContextCompactionEvent): string | null {
  if (![event.messages_before, event.messages_after].every(validCount)) return null;
  switch (event.status) {
    case "started": return COMPACTION_LABEL;
    case "completed": return completionNotice(event);
    case "failed": return "上下文压缩未完成。";
    case "cancelled": return "上下文压缩已取消。";
    default: return null;
  }
}
