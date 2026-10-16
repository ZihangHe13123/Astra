// Only the fields read here, so both a typed PyEvent and a reducer UIEvent fit.
type MemoryNotice = { type?: string; event?: string; memory_id?: string; summary?: string; scope?: string; count?: number; origin?: string };
type MemoryRecall = { type?: string; count?: number; items?: string[] };

const actions: Record<string, string> = { saved: "已记住", revised: "已更新记忆", forgotten: "已忘记" };

/** One line per saved-memory change, with the id the user can inspect or forget. */
export function memoryNoticeText(event: MemoryNotice): string {
  if (event.event === "proposed") {
    return `记忆整理：${event.count ?? 0} 条建议待你审核（/memory proposals）${event.summary ? `，如“${event.summary}”` : ""}`;
  }
  const scope = event.scope === "project" ? " · 本项目" : "";
  const id = String(event.memory_id ?? "").slice(0, 8);
  // A change applied by the background review, not by this turn.
  const origin = event.origin === "consolidation" ? "记忆整理 · " : "";
  return `${origin}${actions[event.event ?? ""] ?? "记忆变更"}：${event.summary ?? ""}（#${id}${scope}）`;
}

/** Which saved memories a turn brought back, shown before its answer. */
export function memoryRecallText(event: MemoryRecall): string {
  const shown = (event.items ?? []).slice(0, 3).join("；");
  return `回忆了 ${event.count ?? 0} 条记忆${shown ? `：${shown}` : ""}`;
}
