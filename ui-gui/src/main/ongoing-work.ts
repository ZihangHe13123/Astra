import type { SessionState } from "@astra/ui-core/session-state";
import { delegateActive } from "@astra/ui-core/delegates";

/** One lifecycle predicate for closing a session, deleting it and closing the window. */
export function hasOngoingWork(state: SessionState): boolean {
  if (state.status === "disconnected") return false;
  return state.busy || !!state.approvals.length || !!state.questions.length
    || ["pending", "running", "cancelling"].includes(state.info.task_status?.task?.status)
    || Object.values(state.processes).some(process => process.status === "running")
    || Object.values(state.delegates).some(delegateActive)
    || Object.values(state.teams).some(team => !["completed", "cancelled", "failed", "stopped"].includes(team.status))
    || ["scheduled", "running", "active"].includes(state.info.wakeup_status?.plan?.state);
}
