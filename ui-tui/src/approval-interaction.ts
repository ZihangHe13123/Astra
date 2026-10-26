import type { ToolApprovalRequest } from "./types.js";
import { approvalChoices, type ApprovalChoice } from "@astra/ui-core/approval-choices";
import { clampToolDetailOffset } from "./tool-results.js";

export interface ApprovalInputKey {
  return?: boolean;
  escape?: boolean;
  upArrow?: boolean;
  downArrow?: boolean;
  pageUp?: boolean;
  pageDown?: boolean;
}

export interface ApprovalInteractionState {
  requests: ToolApprovalRequest[];
  detailOpen: boolean;
  detailOffset: number;
}

export interface ApprovalInteractionBounds {
  lineCount: number;
  pageSize: number;
}

export interface ApprovalInteractionTransition extends ApprovalInteractionState {
  handled: boolean;
  response?: {
    requestId: string;
    decision: ApprovalChoice;
  };
}

export function transitionApprovalInput(
  state: ApprovalInteractionState,
  input: string,
  key: ApprovalInputKey,
  bounds: ApprovalInteractionBounds,
): ApprovalInteractionTransition {
  const request = state.requests[0];
  if (!request) return { ...state, handled: false };

  const choices: readonly ApprovalChoice[] = !request.choices?.length && request.kind === "computer_foreground_takeover"
    ? ["once", "deny"]
    : approvalChoices(request);
  const normalizedInput = input.toLowerCase();

  const resolve = (
    decision: ApprovalChoice,
  ): ApprovalInteractionTransition => ({
    requests: state.requests.slice(1),
    detailOpen: false,
    detailOffset: 0,
    handled: true,
    response: { requestId: request.request_id, decision },
  });

  if ((key.return || normalizedInput === "y") && choices.includes("once")) {
    return resolve("once");
  }
  if (normalizedInput === "t" && choices.includes("task")) {
    return resolve("task");
  }
  if (normalizedInput === "a" && choices.includes("session")) {
    return resolve("session");
  }
  if (normalizedInput === "l" && choices.includes("always")) {
    return resolve("always");
  }
  if (normalizedInput === "v") {
    return {
      ...state,
      detailOpen: !state.detailOpen,
      detailOffset: 0,
      handled: true,
    };
  }
  if (state.detailOpen && key.upArrow) {
    return {
      ...state,
      detailOffset: clampToolDetailOffset(
        state.detailOffset - 1,
        bounds.lineCount,
        bounds.pageSize,
      ),
      handled: true,
    };
  }
  if (state.detailOpen && key.downArrow) {
    return {
      ...state,
      detailOffset: clampToolDetailOffset(
        state.detailOffset + 1,
        bounds.lineCount,
        bounds.pageSize,
      ),
      handled: true,
    };
  }
  if (state.detailOpen && key.pageUp) {
    return {
      ...state,
      detailOffset: clampToolDetailOffset(
        state.detailOffset - bounds.pageSize,
        bounds.lineCount,
        bounds.pageSize,
      ),
      handled: true,
    };
  }
  if (state.detailOpen && key.pageDown) {
    return {
      ...state,
      detailOffset: clampToolDetailOffset(
        state.detailOffset + bounds.pageSize,
        bounds.lineCount,
        bounds.pageSize,
      ),
      handled: true,
    };
  }
  if (state.detailOpen && key.escape) {
    return {
      ...state,
      detailOpen: false,
      detailOffset: 0,
      handled: true,
    };
  }
  if ((key.escape || normalizedInput === "n") && choices.includes("deny")) {
    return resolve("deny");
  }
  return { ...state, handled: true };
}
