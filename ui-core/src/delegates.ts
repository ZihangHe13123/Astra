/** Display-only child execution state. The backend owns scheduling and cancellation. */
export type DelegateStatus = "queued" | "running" | "idle" | "completed" | "failed" | "cancelled" | "timed_out" | "partial" | "interrupted";
export type DelegateView = {
  process_id: string; task_id?: string; session_id?: string; goal: string; worker_type?: string;
  status: DelegateStatus; started_at?: number; updated_at?: number; completed_at?: number;
  duration_ms?: number; current_tool?: string; turns_used?: number; max_turns?: number;
  result?: string; error?: string; partial?: boolean; goal_truncated?: boolean; result_truncated?: boolean;
  error_truncated?: boolean; history_truncated?: boolean; artifact_path?: string; team_id?: string; agent_id?: string;
};
export const delegateActive = (d: Pick<DelegateView, "status">): boolean => ["queued", "running", "idle"].includes(d.status);
export function reduceDelegateEvent(previous: Record<string, DelegateView>, event: Partial<DelegateView>): Record<string, DelegateView> {
  if (!event.process_id || !event.status || !["queued", "running", "idle", "completed", "failed", "cancelled", "timed_out", "partial", "interrupted"].includes(event.status)) return previous;
  const old = previous[event.process_id];
  if (old && ((event.updated_at || 0) < (old.updated_at || 0)
      || !delegateActive(old) && old.status !== "interrupted" && delegateActive({ status: event.status }))) return previous;
  const next = { ...previous, [event.process_id]: { ...old, ...event, goal: event.goal || old?.goal || "委派任务" } as DelegateView };
  if (["queued", "running"].includes(event.status)) {
    delete next[event.process_id].result; delete next[event.process_id].result_truncated;
  }
  // Preserve ongoing children even in long sessions; bound finished display records.
  const finished = Object.values(next).filter(d => !delegateActive(d)).sort((a, b) => (b.updated_at || 0) - (a.updated_at || 0));
  for (const d of finished.slice(200)) delete next[d.process_id];
  return next;
}
export function orderedDelegates(delegates: Record<string, DelegateView>): DelegateView[] {
  return Object.values(delegates).sort((a, b) => Number(delegateActive(b)) - Number(delegateActive(a))
    || (b.started_at || b.updated_at || 0) - (a.started_at || a.updated_at || 0) || a.process_id.localeCompare(b.process_id));
}
export function delegateStatusLabel(status: DelegateStatus): string {
  return { queued: "排队中", running: "运行中", idle: "待命", completed: "已完成", failed: "失败", cancelled: "已取消", timed_out: "已超时", partial: "部分完成", interrupted: "已中断" }[status];
}
