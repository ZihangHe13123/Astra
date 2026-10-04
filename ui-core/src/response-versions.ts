import type { SessionState } from './session-state.js';
import { delegateActive } from './delegates.js';
export type ResponseSource = { index: number; digest: string };
export type ResponseVersion = { id: string; branch_id: string; status: string; number: number };
export type ResponseGroup = { id: string; selected_version: string; user_text: string; response_text: string; source_ref?: ResponseSource; versions: ResponseVersion[] };
export type ResponseVersions = { revision: number; active_branch: string; branch_id: string; choices: { group_id: string; version_id: string }[]; groups: ResponseGroup[]; targets: { source_ref: ResponseSource; group_id?: string; version_id?: string }[] };
export type ResponseOperation = { request_id: string; operation: 'response_regenerate' | 'response_select'; source_ref?: ResponseSource };
export type ResponseRegeneration = { request_id: string; source_ref: ResponseSource; status: 'running' | 'completed'; content: string };
export const sameResponseSource = (a?: ResponseSource, b?: ResponseSource) => !!a && !!b && a.index === b.index && a.digest === b.digest;
export const responseOperationActive = (state?: SessionState) => !!(state?.responseOperation || state?.regeneration);
/** Conservatively refuse a path switch while foreground or background work can append to it. */
export function responseControlsBusy(state?: SessionState): boolean {
  return !state || state.status !== 'ready' || state.busy || responseOperationActive(state)
    || state.messages.some(m => ['pending', 'unknown'].includes(m.submissionState || ''))
    || ['pending', 'running', 'cancelling'].includes(state.info.task_status?.task?.status)
    || ['scheduled', 'running', 'active'].includes(state.info.wakeup_status?.plan?.state)
    || !!state.preparation || !!state.approvals.length || !!state.questions.length
    || state.tools.some(t => t.status === 'running')
    || Object.values(state.delegates).some(delegateActive)
    || Object.values(state.processes).some(p => ['pending', 'queued', 'running', 'cancelling'].includes(p.status))
    || Object.values(state.teams).some(team => !['completed', 'cancelled', 'failed', 'stopped'].includes(team.status));
}
