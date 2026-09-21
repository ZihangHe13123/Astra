// Protocol types for Python ↔ TUI communication

import type { TurnChangesEventData } from "./turn-changes.js";

export type RuntimeMode = "work" | "bar" | "minimal" | "local";

export type LocalModeDefinition = {
  command: string;
  label: string;
  description: string;
  ui: { brand: string; exitLabel: string; headerTag: string; idle: string; busy: string; placeholder: string };
};

export type GenerationStats = {
  completion_tokens: number;
  elapsed_seconds: number;
  tokens_per_second: number;
};

export type GenerationProgress = {
  phase: "requesting" | "streaming" | "waiting" | "finished";
  elapsed_seconds: number;
  idle_seconds: number;
};

export type ContextCompactionEvent = {
  type: "context_compaction";
  status: "started" | "completed" | "failed" | "cancelled";
  messages_before: number;
  messages_after: number;
  tokens_before?: number;
  tokens_after?: number;
  target_tokens?: number;
  method?: "none" | "cleanup" | "summary" | "truncate";
  dropped_steps?: number;
  cleared_results?: number;
};

export interface UserQuestionOption {
  label: string;
  description?: string;
}

export interface UserQuestion {
  id: string;
  question: string;
  header?: string;
  options?: UserQuestionOption[];
  multi_select: boolean;
}

export interface UserQuestionAnswer {
  id: string;
  selected: string[];
  custom?: string;
}

export type UserQuestionRequest = {
  type: "user_question_request";
  request_id: string;
  questions: UserQuestion[];
};

export type VisionPreprocessEvent = {
  type: "vision_preprocess";
  message: string;
  protected: boolean;
  protected_local_images: number;
  unprotected_external_images: number;
};

export type ComputerControl = "inactive" | "background" | "foreground_takeover" | "user_control" | "paused";

export type ComputerStateEvent = {
  type: "computer_state";
  active: boolean;
  handed_off: boolean;
  control?: ComputerControl;
  application?: string;
  permission?: "available" | "degraded" | "unsupported";
};

// Events from Python (via stdin)
export type AppshotAdmissionEvent =
  | {type:'message_accepted'; submission_id:string}
  | {type:'message_rejected'; submission_id:string; code:string; retryable:boolean}
  | {type:'submission_status'; submission_id:string; status:'accepted'|'rejected'|'pending'|'unknown'; code?:string; retryable?:boolean};
export type PyEvent =
  | AppshotAdmissionEvent
  | ContextCompactionEvent
  | { type: "chunk"; content: string }
  | { type: "reasoning"; content: string }
  | {
      type: "tool_result";
      name: string;
      call_id?: string;
      output: string;
      error: string;
      code: string;
      duration_ms?: number;
      output_truncated?: boolean;
      artifact_path?: string;
      error_type?: string;
      recoverable?: boolean;
      retryable?: boolean;
      recovery_hint?: string;
      partial?: boolean;
      details?: Record<string, unknown>;
      artifact_ref?: string;
    }
  | { type: "tool_calls"; calls?: ToolCallInfo[] }
  | {
      type: "tool_progress";
      call_id: string;
      name: string;
      stage: string;
      status: string;
      message?: string;
      current?: number;
      total?: number;
      percent?: number;
      unit?: string;
    }
  | VisionPreprocessEvent
  | ComputerStateEvent
  | {
      type: "process_status";
      event: "process_started" | "process_output" | "process_completed" | "process_failed" | "process_cancelled";
      process_id: string;
      kind: string;
      label: string;
      task_id?: string;
      status: "running" | "completed" | "failed" | "cancelled" | "interrupted";
      started_at: number;
      completed_at?: number | null;
      duration_ms: number;
      exit_code?: number | null;
      output_chars: number;
      stdout_chars: number;
      stderr_chars: number;
      artifact_path?: string;
    }
  | {
      type: "agent_team";
      event:
        | "team_created"
        | "team_resumed"
        | "team_agent_created"
        | "team_agent_started"
        | "team_agent_idle"
        | "team_agent_awakened"
        | "team_agent_terminal"
        | "team_agent_cancelled"
        | "team_message"
        | "team_inbox_read"
        | "team_task_created"
        | "team_task_claimed"
        | "team_task_updated"
        | "team_stopped";
      kind: "agent_team";
      team_id: string;
      name?: string;
      goal?: string;
      status?: string;
      lead_agent_id?: string;
      agent_id?: string;
      parent_agent_id?: string;
      role?: string;
      process_id?: string;
      sender_agent_id?: string;
      recipient_agent_ids?: string[];
      message_kind?: string;
      message_count?: number;
      acknowledged?: boolean;
      team_task_id?: string;
      title?: string;
      owner_agent_id?: string;
    }
  | {
      type: "tool_approval_request";
      request_id: string;
      call_id?: string;
      tool_name: string;
      risk: string;
      kind: string;
      reason: string;
      agent_reason?: string;
      reason_source?: "agent" | "backend" | string;
      detail?: string;
      target?: string;
      operation?: string;
      access?: string;
      approval_title?: string;
      approval_summary?: string;
      approval_effect?: string;
      approval_boundary?: string;
      approval_question?: string;
      scope_kind?: "file" | "directory" | "batch" | string;
      scope?: string;
      scopes?: string[];
      targets?: string[];
      compound_commands?: string[];
      compound_command_count?: number;
      session_scope_label?: string;
      workspace?: string;
      outside_workspace?: boolean;
      verifier?: string;
      change_summary?: {
        kind?: string;
        files?: number;
        additions?: number;
        deletions?: number;
      };
      preview?: string;
      arguments?: Record<string, unknown>;
      choices?: ("once" | "session" | "deny")[];
      surface?: "tui" | "gui" | "channel";
      channel?: string;
      state?: "pending";
    }
  | {
      type: "approval_inbox_snapshot";
      orphaned: ({
        request_id: string;
        tool_name?: string;
        target?: string;
        channel?: string;
        created_at: string;
      })[];
    }
  | {
      type: "approval_resolved";
      request_id: string;
      state: "approved" | "denied" | "cancelled" | "orphaned";
      decision?: "once" | "session" | "deny" | "";
    }
  | {
      type: "approval_response_rejected";
      request_id: string;
      reason: string;
    }
  | UserQuestionRequest
  | {
      type: "user_question_resolved";
      request_id: string;
      state: "answered" | "cancelled";
    }
  | {
      type: "user_question_response_rejected";
      request_id: string;
      reason: string;
      retryable: boolean;
    }
  | {
      type: "event_replay_complete";
      after_cursor: number;
      next_cursor: number;
      cursor: number;
      count: number;
      has_more: boolean;
    }
  | { type: "connection_result"; request_id: string; provider_id?: string; error: string; notice?: string }
  | { type: "connection_auth"; request_id: string; verification_uri: string; user_code: string; expires_in: number }
  | ({ type: "turn_changes" } & TurnChangesEventData)
  | { type: "done" }
  | { type: "backend_hello"; protocol_version: number }
  | { type: "restart_ready"; request_id: string; session: string; replayed?: boolean }
  | { type: "restart_status"; request_id: string; state: string; message: string }
  | { type: "wakeup_status"; plan: Record<string, unknown>; message: string }
  | {
      type: "error";
      message: string;
      recoverable?: boolean;
      code?: string;
      retryable?: boolean;
      recovery_hint?: string;
      tool_name?: string;
      call_id?: string;
      partial?: boolean;
      duration_ms?: number;
      details?: Record<string, unknown>;
      artifact_ref?: string;
    }
  | { type: "model_info"; model: string; model_key?: string; reasoning_effort?: "low" | "high" | "max" | null; code_mode?: "native" | "code" | "both"; personas?: { name: string; description: string }[]; models?: (string | ModelInfo)[]; providers?: ProviderInfo[]; connection_routes?: ConnectionRoute[]; recent_models?: string[]; provider_errors?: Record<string, string>; total_tokens: number; prompt_tokens: number; completion_tokens: number; cache_hit_tokens?: number; cache_miss_tokens?: number; context_pct: number; context_used?: number; context_limit: number; show_reasoning?: boolean }
  | { type: "session_info"; name: string; messages: number }
    | { type: "cache_status"; cache_hit_tokens: number; cache_miss_tokens: number }

  | { type: "mode_info"; mode: RuntimeMode; ephemeral: boolean; bar_session?: string; minimal_session?: string; local_session?: string; memory_enabled: boolean; tools_enabled: boolean; learning_enabled: boolean }
  | { type: "bar_session_list"; sessions: { name: string; messages: number; current: boolean }[] }
  | { type: "minimal_session_list"; sessions: { name: string; messages: number; current: boolean }[] }
  | { type: "local_mode_info"; definition: LocalModeDefinition | null; sessions: { name: string; messages: number; current: boolean }[] }
  | {
      type: "bar_state";
      session: string;
      drink: BarDrinkState;
      ambiance: BarAmbianceState;
      lyra_glass: LyraGlassState;
      shift: BarShiftState;
      output_mode: "atomic" | "stream";
      notice?: string;
    }
  | { type: "session_list"; sessions: { name: string; messages: number; current: boolean }[] }
  | { type: "history"; messages: { role: string; content: string; timestamp?: number }[]; session_id?: string; tool_results?: { name: string; output: string; error: string; duration_ms?: number; artifact_path?: string; output_truncated?: boolean }[] }
  | { type: "working_memory"; session_id: string; memory: WorkingMemory }
  | ({ type: "generation_stats" } & GenerationStats)
  | ({ type: "generation_progress" } & GenerationProgress)
  | {
      type: "memory_retention";
      decision: "retained" | "confirmed" | "superseded";
      mode: "conservative" | "evolving";
      retained_ids: string[];
      confirmed_ids: string[];
      superseded_ids: string[];
    }
  | { type: "learning_review"; message: string; proposal_ids: string[] }
  | { type: "steering"; message: string; text: string }
  | { type: "learning_review_status"; status: "queued" | "running" | "cancelled" | "idle" }
  | { type: "task_started"; task: TaskInfo }
  | { type: "task_status"; task: TaskInfo | null }
  | { type: "yolo_status"; yolo: boolean; error?: string }
  | ({ type: "startup_banner" } & StartupInfo);

export type StartupInfo = {
  skills: number;
  mcp: { name: string; state: string; tools?: number; error?: string }[];
  learning: { mode: "off" | "review"; auto: boolean; pending: number; learned?: number | null; legacy_pending?: number; error?: string };
  tools: number;
  model: string;
};

export interface TaskInfo {
  id: string;
  status: string;
  input_text?: string;
  step_count?: number;
}

export interface BarDrinkState {
  active: boolean;
  name: string;
  note: string;
  tone: "amber" | "cyan" | "pink" | "clear";
  temperature: "hot" | "cold" | "room";
  fill: number;
}

export interface BarAmbianceState {
  weather: "drizzle" | "rain" | "downpour" | "clearing";
  power: "stable" | "flicker" | "brownout";
  music: "silent" | "low_synth" | "old_radio" | "jukebox";
  radio: "static" | "local_news" | "weather" | "emergency";
}

export interface LyraGlassState {
  active: boolean;
  name: string;
  note: string;
  fill: number;
}

export interface BarShiftState {
  turn_count: number;
  phase: "early" | "deep" | "late" | "last_call";
}

export interface ToolCallInfo {
  id: string;
  name: string;
  arguments?: string;
  progress?: ToolProgressInfo;
}

export interface ToolProgressInfo {
  stage: string;
  status: string;
  message?: string;
  current?: number;
  total?: number;
  percent?: number;
  unit?: string;
}

export interface ProviderInfo {
  id: string; label: string; endpoint: string; connected: boolean;
  source: string; error: string; count: number;
}
export interface ConnectionRoute {
  id: string; provider: string; label: string; base_url: string;
  api_key_env: string; key_available: boolean;
  auth_mode?: "api-key" | "oauth";
}
export interface ModelInfo {
  source?: string;
  metadata_known?: boolean;
  capabilities?: string[];
  fetched_at?: number;
  key: string;
  name: string;
  provider: string;
  provider_id: string;
  endpoint: string;
  context_limit: number;
  current?: boolean;
}

export type WorkingStepStatus = "pending" | "in_progress" | "completed";

export interface WorkingStep {
  text: string;
  status: WorkingStepStatus;
}

export interface WorkingMemory {
  goal?: string;
  plan?: string;
  progress?: string;
  constraints?: string;
  open_items?: string;
  artifacts?: string;
  notes?: string;
  steps?: WorkingStep[];
}

// Commands from TUI (via stdout)
export type TuiCommand =
  | { type: "restart_ack"; request_id: string }
  | { type: "message"; text: string; appshots?:never }
  | { type: "message"; text: string; appshots:Array<{label:string;manifest_path:string}>; submission_id:string; appshot_session_id:string; appshot_broker_id:string }
  | {type:'submission_status'; submission_id:string}
  | { type: "image"; path: string; prompt: string }
  | { type: "command"; cmd: string }
  | { type: "refresh_models"; provider_id?: string; force?: boolean }
  | { type: "connect_provider"; request_id: string; route_id: string; base_url: string; api_key: string; api_key_env: string }
  | { type: "cancel_connection"; request_id: string }
  | { type: "event_replay"; after_cursor: number; limit?: number }
  | { type: "tool_approval_response"; request_id: string; decision: "once" | "session" | "deny" }
  | { type: "user_question_response"; request_id: string; answers: UserQuestionAnswer[] }
  | { type: "user_question_cancel"; request_id: string }
  | { type: "exit" };

export type ToolApprovalRequest = Extract<PyEvent, { type: "tool_approval_request" }>;

export interface ChatMessage {
  role: "user" | "assistant" | "system" | "tool" | "reasoning" | "error";
  content: string;
  timestamp?: number;
  id?: number | string;  // <-- added for <Static> keying
}

export type InputSubmission = {
  text: string;
  appshots: Array<{label:string; manifest_path:string}>;
  submissionId?: string;
  appshotSessionId?: string;
  appshotBrokerId?: string;
};
