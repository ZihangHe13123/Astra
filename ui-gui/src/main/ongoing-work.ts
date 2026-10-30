import type { SessionState } from "@astra/ui-core/session-state";
import { delegateActive } from "@astra/ui-core/delegates";
import { teamWorking } from "@astra/ui-core/agent-team-state";

/** One lifecycle predicate for closing a session, deleting it and closing the window. */
export function hasOngoingWork(state: SessionState): boolean {
  if (state.status === "disconnected") return false;
  return state.status !== "ready" || state.messages.some(message => ["pending", "unknown"].includes(message.submissionState || ""))
    || !!state.responseOperation || !!state.regeneration || state.busy || !!state.approvals.length || !!state.questions.length
    || ["pending", "running", "cancelling"].includes(state.info.task_status?.task?.status)
    || Object.values(state.processes).some(process => process.status === "running")
    || Object.values(state.delegates).some(delegateActive)
    || Object.values(state.teams).some(teamWorking)
    || ["scheduled", "running", "active"].includes(state.info.wakeup_status?.plan?.state);
}

/**
 * Whether quitting or wiping the data must wait for this session. The personal thread is the background
 * host's, which starts and restarts it on its own: while it is only starting it holds nothing up, though
 * work of its own (a reply, an approval waiting) still does.
 */
export function holdsUp(state: SessionState): boolean {
  if (state.personal && state.status !== "ready" && state.status !== "disconnected") return false;
  return hasOngoingWork(state);
}
