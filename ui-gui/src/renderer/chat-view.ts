/** The personal thread read as a chat (specs/personal-thread.md D4): the same rows, laid out for talking. */
import type { UIEvent } from "@astra/ui-core/session-state";
import type { Row } from "./activity-state.js";

/** A pause this long between two messages starts a new time heading. */
export const CHAT_GAP_SECONDS = 10 * 60;

const pad = (value: number) => String(value).padStart(2, "0");

/** "08:30" today, "昨天 08:30", or "10月24日 08:30". */
export function chatTime(seconds: number, now = Date.now()): string {
  const when = new Date(seconds * 1000), today = new Date(now);
  const clock = `${pad(when.getHours())}:${pad(when.getMinutes())}`;
  const day = (date: Date) => new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
  const days = Math.round((day(today) - day(when)) / 86_400_000);
  return days === 0 ? clock : days === 1 ? `昨天 ${clock}` : `${when.getMonth() + 1}月${when.getDate()}日 ${clock}`;
}

/**
 * The rows a chat shows: a time heading where a pause begins, and no file-change notes (the thread has
 * no project; its changes stay in the details panel). Everything else keeps its place and identity.
 */
export function chatRows(rows: Row[], now = Date.now()): Row[] {
  const result: Row[] = [];
  let last: number | undefined;
  for (const row of rows) {
    if (row.role === "changes") continue;
    const spoken = row.role === "user" || row.role === "assistant";
    if (spoken && typeof row.timestamp === "number") {
      if (last === undefined || row.timestamp - last >= CHAT_GAP_SECONDS) {
        result.push({ id: `time:${row.id}`, role: "time", content: chatTime(row.timestamp, now) });
      }
      last = row.timestamp;
    }
    result.push(row);
  }
  return result;
}

/** A scheduled job's result, as the background host writes it into the thread: 【title】 then the body. */
export function jobResult(content: string): { title: string; body: string } | undefined {
  const match = /^【((?:定时任务|提醒)[^】\n]{0,200})】\n([\s\S]*)$/.exec(content);
  return match ? { title: match[1], body: match[2] } : undefined;
}

/** One line for the tool calls of a step: how many, and whether any is still running or failed. */
export function activitySummary(tools: UIEvent[]): string {
  const running = tools.filter(tool => tool.status === "running").length;
  const failed = tools.filter(tool => tool.status === "failed" || tool.error).length;
  if (running) return `正在使用工具（${tools.length - running}/${tools.length}）`;
  return `用了 ${tools.length} 个工具${failed ? `，${failed} 个出错` : ""}`;
}
