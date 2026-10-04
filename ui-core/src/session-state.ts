import { reduceAgentTeamEvent, type AgentTeamView } from "./agent-team-state.js";
import type { ResponseVersions, ResponseOperation, ResponseRegeneration } from "./response-versions.js";
import type { PyEvent } from "./types.js";
import { delegateActive, reduceDelegateEvent, type DelegateView } from "./delegates.js";
import { reduceToolPreparation, type ToolPreparation } from "./tool-preparation.js";

/** Platform-independent projection. The backend remains the execution authority. */
export type UIEvent = { type: string; [key: string]: any };
export type Message = {
  id: string; role: string; content: string; timestamp?: number; pending?: boolean;
  reasoning_content?: string;
  stream_id?: string; source_ref?: { index: number; digest: string };
  submissionState?: "pending" | "accepted" | "rejected" | "unknown"; submissionError?: string;
};
export type SessionState = {
  id: string; session: string; workspace: string; mode: string; status: string; busy: boolean; isDraft: boolean;
  messages: Message[]; tools: UIEvent[]; approvals: UIEvent[]; questions: UIEvent[];
  processes: Record<string, UIEvent>; delegates: Record<string, DelegateView>; teams: Record<string, AgentTeamView>; info: Record<string, UIEvent>;
  notices: UIEvent[]; stream: string; serial: number; revision: number;
  preparation?: ToolPreparation;
  responseVersions?: ResponseVersions; responseOperation?: ResponseOperation; regeneration?: ResponseRegeneration;
  branchId?: string;
};
export function initialSession(id: string, workspace = ""): SessionState {
  return { id, workspace, session: "", mode: "work", status: "connecting", busy: false, isDraft: false,
    messages: [], tools: [], approvals: [], questions: [], processes: {}, delegates: {}, teams: {},
    info: {}, notices: [], stream: "", serial: 0, revision: 0 };
}
export function textContent(value: unknown): string {
  if (typeof value === "string") return value;
  if (Array.isArray(value)) return value.map(v => typeof v?.text === "string" ? v.text : "").filter(Boolean).join("\n");
  return value == null ? "" : JSON.stringify(value, null, 2);
}
export function plainText(text: string): string {
  return text.replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, "").replace(/\x1b\][^\x07]*(?:\x07|\x1b\\)/g, "");
}
export function projectEvent(previous: SessionState, event: UIEvent): SessionState {
  const state: SessionState = { ...previous, revision: previous.revision + 1 };
  // Unscoped side-command completion must not erase another running turn's preview.
  if (!(event.type === "done" && !event.request_id && ["pending", "running", "cancelling"].includes(previous.info.task_status?.task?.status))) {
    state.preparation = reduceToolPreparation(previous.preparation, event);
  }
  // Configuration and diagnostics do not turn a blank page into a conversation.
  if (["gui_user", "task_started", "chunk", "reasoning", "tool_calls"].includes(event.type)
      || event.type === "history" && event.messages?.length) state.isDraft = false;
  const append = (role: string, content: string, id?: string) => {
    if (!content) return;
    state.serial++;
    state.messages = [...state.messages, { id: id || `${state.id}:${state.serial}`, role, content,
      timestamp: typeof event.timestamp === "number" ? event.timestamp : Date.now() / 1000,
      ...(event.stream_id ? { stream_id: event.stream_id } : {}) }];
  };
  const notice = () => { state.notices = [...state.notices, event].slice(-200); };
  switch (event.type) {
    case "backend_hello": {
      const { restart_ready: _restart, ...info } = state.info;
      state.status = "loading"; state.info = { ...info, backend_hello: event }; break;
    }
    case "gui_ready": state.status = "ready"; state.workspace = event.workspace || state.workspace; break;
    case "gui_disconnected":
      state.status = "disconnected"; state.busy = false; state.stream = "";
      state.responseOperation = undefined; state.regeneration = undefined;
      state.delegates = Object.fromEntries(Object.entries(state.delegates).map(([id, d]) => [id, delegateActive(d) ? { ...d, status: "interrupted", current_tool: "" } : d]));
      state.tools = state.tools.map(t => t.status === "running" ? { ...t, status: "interrupted" } : t);
      state.messages = state.messages.map(m => m.submissionState === "pending" ? { ...m, submissionState: "unknown" } : m);
      if (state.info.connection_pending?.status === "pending") {
        state.info = { ...state.info,
          connection_pending: { ...state.info.connection_pending, status: "completed" },
          connection_result: { type: "connection_result", request_id: state.info.connection_pending.request_id, error: "连接已中断，请重新发起登录或连接。" } };
      }
      if (state.questions.length) append("system", "连接已中断，未完成的问题已失效；需要时请重新提问。");
      state.approvals = []; state.questions = []; notice(); break;
    case "gui_user":
      if (!event.submission_id || !state.messages.some(m => m.id === event.submission_id)) {
        append("user", event.text, event.submission_id);
        state.messages = state.messages.map(m => m.id === event.submission_id ? { ...m, pending: true, submissionState: "pending" } : m);
      }
      state.stream = ""; break;
    case "message_accepted":
    case "message_rejected":
    case "submission_status": {
      const status = event.type === "message_accepted" ? "accepted" : event.type === "message_rejected" ? "rejected" : event.status;
      if (!["accepted", "rejected", "unknown"].includes(status)) break;
      state.messages = state.messages.map(m => {
        if (m.id !== event.submission_id || status === "unknown" && ["accepted", "rejected"].includes(m.submissionState || "")) return m;
        return { ...m, pending: status === "unknown", submissionState: status,
          submissionError: status === "rejected" ? String(event.reason || event.error || event.message || "消息未被接收") : undefined };
      });
      if (status === "rejected" || status === "unknown") notice(); break;
    }
    case "connection_pending": {
      const { connection_auth: _auth, connection_result: _result, ...info } = state.info;
      state.info = { ...info, connection_pending: { ...event, status: "pending" } }; break;
    }
    case "connection_auth":
    case "connection_result": {
      const pending = state.info.connection_pending;
      // A cancelled login may finish after a newer login has started.
      if (pending && pending.request_id !== event.request_id) break;
      if (event.type === "connection_auth" && pending?.status === "completed") break;
      state.info = { ...state.info, [event.type]: event,
        connection_pending: { ...pending, type: "connection_pending", request_id: event.request_id,
          status: event.type === "connection_result" ? "completed" : "pending" } }; break;
    }
    case "response_versions":
      state.responseVersions = { revision: event.revision, active_branch: event.active_branch, branch_id: event.branch_id,
        choices: event.choices || [], groups: event.groups || [], targets: event.targets || [] };
      state.branchId = event.branch_id; break;
    case "response_operation_pending":
      state.responseOperation = { request_id: event.request_id, operation: event.operation, source_ref: event.source_ref };
      if (event.operation === "response_regenerate" && event.source_ref) state.regeneration = {
        request_id: event.request_id, source_ref: event.source_ref, status: "running", content: "" };
      break;
    case "response_regeneration": {
      if (state.responseOperation?.request_id !== event.request_id && state.regeneration?.request_id !== event.request_id) break;
      if (event.status === "failed" || event.status === "cancelled") state.regeneration = undefined;
      else if (event.status === "running" || event.status === "completed" && state.regeneration) state.regeneration = {
        request_id: event.request_id, source_ref: event.source_ref || state.regeneration?.source_ref,
        status: event.status, content: typeof event.content === "string" ? event.content : (state.regeneration?.content || "") + (event.delta || "") };
      break;
    }
    case "response_operation_result":
      if (state.responseOperation?.request_id !== event.request_id) break;
      state.responseOperation = undefined; state.regeneration = undefined;
      if (event.error) notice(); break;
    case "session_info":
      if (state.session !== event.name) { state.preparation = undefined; state.responseVersions = undefined; state.branchId = undefined; state.regeneration = undefined; }
      state.session = event.name; state.info = { ...state.info, session_info: event }; break;
    case "mode_info":
      if (event.mode !== state.mode) { state.isDraft = false; state.preparation = undefined; state.responseVersions = undefined; state.branchId = undefined; state.regeneration = undefined; }
      state.mode = event.mode;
      state.session = event.bar_session || event.minimal_session || event.local_session || state.session;
      state.info = { ...state.info, mode_info: event }; break;
    case "history":
      state.regeneration = undefined;
      if (typeof event.branch_id === "string") state.branchId = event.branch_id;
      state.messages = (event.messages || []).map((m: UIEvent, i: number) => ({
        id: m.id || `history:${event.session_id}:${i}`, role: m.role, content: textContent(m.content), timestamp: m.timestamp,
        ...(m.source_ref ? { source_ref: m.source_ref } : {}),
        ...(m.role === "assistant" && typeof m.reasoning_content === "string" && m.reasoning_content.trim()
          ? { reasoning_content: m.reasoning_content } : {}),
      }));
      state.tools = (event.tool_results || []).map((r: UIEvent, i: number) => ({ ...r, type: "tool_result", status: r.error ? "failed" : "completed", historical: true, result_index: i + 1, call_id: r.call_id || `history-tool:${i}` }));
      state.delegates = (event.delegates || []).reduce(reduceDelegateEvent, {});
      state.session = event.session_id || state.session; state.stream = ""; break;
    case "task_started": state.busy = true; state.stream = ""; state.info = { ...state.info, task_status: event }; break;
    case "task_status":
      state.info = { ...state.info, task_status: event };
      state.busy = ["pending", "running", "cancelling"].includes(event.task?.status);
      if (!state.busy && event.task?.id === previous.info.task_status?.task?.id) state.preparation = undefined;
      break;
    case "chunk":
    case "reasoning": {
      const role = event.type === "chunk" ? "assistant" : "reasoning";
      const last = state.messages[state.messages.length - 1];
      if (last && last.id === state.stream && last.role === role && last.stream_id === event.stream_id) {
        state.messages = [...state.messages.slice(0, -1), { ...last, content: last.content + event.content }];
      } else { append(role, event.content); state.stream = state.messages[state.messages.length - 1]?.id || ""; }
      state.busy = true; break;
    }
    case "tool_preparing": break;
    case "message_source": {
      const ref = event.source_ref;
      if (!ref || !Number.isSafeInteger(ref.index) || ref.index < 0 || !/^[a-f0-9]{64}$/.test(ref.digest)) break;
      state.messages = state.messages.map(message => (
        event.stream_id && message.stream_id === event.stream_id || event.submission_id && message.id === event.submission_id
      ) ? { ...message, source_ref: ref } : message);
      break;
    }
    case "tool_calls":
      state.stream = "";
      state.tools = [...state.tools, ...(event.calls || []).map((c: UIEvent) => ({ ...c, type: "tool_calls", call_id: c.id, status: "running" }))];
      state.busy = true; break;
    case "tool_progress":
      state.tools = state.tools.map(t => t.call_id === event.call_id ? { ...t, progress: event } : t); break;
    case "tool_result": {
      const match = event.call_id && state.tools.some(t => t.call_id === event.call_id);
      const existing = event.call_id ? state.tools.find(t => t.call_id === event.call_id) : undefined;
      const result_index = existing?.result_index || state.tools.reduce((max, t) => Math.max(max, Number(t.result_index) || 0), 0) + 1;
      const result = { ...event, result_index, status: event.error ? "failed" : "completed" };
      state.tools = match ? state.tools.map(t => t.call_id === event.call_id ? { ...t, ...result } : t) : [...state.tools, result];
      state.stream = "";
      if (!event.call_id) append(event.error ? "error" : "tool", plainText(event.error || event.output || ""));
      break;
    }
    case "tool_approval_request":
      state.approvals = [...state.approvals.filter(a => a.request_id !== event.request_id), event]; break;
    case "approval_resolved": state.approvals = state.approvals.filter(a => a.request_id !== event.request_id); break;
    case "user_question_request": state.questions = [...state.questions.filter(a => a.request_id !== event.request_id), event]; break;
    case "user_question_pending":
      state.questions = state.questions.map(a => a.request_id === event.request_id ? { ...a, state: "pending" } : a); break;
    case "user_question_resolved":
      state.questions = state.questions.filter(a => a.request_id !== event.request_id);
      if (event.state === "expired") append("system", event.reason || "问题已失效，请重新提问。"); break;
    case "approval_response_rejected":
      state.approvals = state.approvals.filter(a => a.request_id !== event.request_id);
      append("error", event.reason); notice(); break;
    case "user_question_response_rejected":
      state.questions = event.retryable ? state.questions.map(a => a.request_id === event.request_id ? { ...a, rejection: event } : a)
        : state.questions.filter(a => a.request_id !== event.request_id);
      append("error", event.reason); notice(); break;
    case "process_status": state.processes = { ...state.processes, [event.process_id]: event }; break;
    case "delegate_status":
      if (!event.session_id || !state.session || event.session_id === state.session) state.delegates = reduceDelegateEvent(state.delegates, event as Partial<DelegateView>);
      break;
    case "agent_team": {
      state.teams = Object.fromEntries(reduceAgentTeamEvent(Object.values(state.teams),
        event as Extract<PyEvent, { type: "agent_team" }>).map(team => [team.id, team])); break;
    }
    // A command's done/error can arrive while an independently running turn
    // still owns the backend. Only its task status can terminate that turn.
    case "done": state.busy = ["pending", "running", "cancelling"].includes(state.info.task_status?.task?.status); state.stream = ""; break;
    case "error": append("error", event.message || "请求失败"); state.busy = ["pending", "running", "cancelling"].includes(state.info.task_status?.task?.status); state.stream = ""; notice(); break;
    case "steering": break; // The submitted user message is already present.
    case "gui_notice": append("system", event.message); break;
    // Another Astra session on this computer: shown as a notice, never as the user's words.
    case "peer_message":
      append("system", `${event.direction === "in" ? "来自" : "发给"} ${event.peer}（${event.state}）：${event.text}`); break;
    default: state.info = { ...state.info, [event.type]: event }; break;
  }
  return state;
}
