import type { PyEvent, UserQuestionRequest } from "./types.js";

export interface QuestionSubmissionRejection {
  requestId: string;
  reason: string;
}

export interface QuestionProtocolState {
  request: UserQuestionRequest | null;
  rejection: QuestionSubmissionRejection | null;
}

type QuestionProtocolEvent =
  | UserQuestionRequest
  | Extract<PyEvent, { type: "user_question_pending" }>
  | Extract<PyEvent, { type: "user_question_resolved" }>
  | Extract<PyEvent, { type: "user_question_response_rejected" }>;

export function reduceQuestionProtocolState(
  state: QuestionProtocolState,
  event: QuestionProtocolEvent,
): QuestionProtocolState {
  if (event.type === "user_question_request") {
    if (state.request?.request_id === event.request_id) return state;
    return { request: event, rejection: null };
  }
  if (state.request?.request_id !== event.request_id) return state;
  if (event.type === "user_question_pending") return { ...state, request: { ...state.request!, state: "pending" } };
  if (event.type === "user_question_resolved" || !event.retryable) {
    return { request: null, rejection: null };
  }
  return {
    request: state.request,
    rejection: { requestId: event.request_id, reason: event.reason },
  };
}
