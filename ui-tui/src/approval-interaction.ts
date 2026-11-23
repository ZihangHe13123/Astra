import type { ToolApprovalRequest } from "./types.js";
import { approvalChoices, type ApprovalChoice } from "@astra/ui-core/approval-choices";
import { clampToolDetailOffset } from "./tool-results.js";

export interface ApprovalInputKey {
  return?: boolean;
  escape?: boolean;
  ctrl?: boolean;
  meta?: boolean;
  shift?: boolean;
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

/** A foreground answer from the raw terminal parser, bound to the head that was on screen. */
export type ForegroundChoice = { requestId: string; decision: "once" | "session" };

/** Live FIFO; React renders its snapshots, but input and backend events need not wait for a repaint. */
export class ApprovalQueue {
  private pending: ToolApprovalRequest[] = [];
  private writing = false;

  get requests() { return this.pending; }
  enqueue(request: ToolApprovalRequest) {
    if (!this.pending.some(item => item.request_id === request.request_id)) this.pending = [...this.pending, request];
    return this.pending;
  }
  remove(id: string) { return this.pending = this.pending.filter(item => item.request_id !== id); }
  clear() { return this.pending = []; }

  respond(displayed: ToolApprovalRequest | null | undefined, write: () => boolean): boolean {
    if (!displayed || displayed !== this.pending[0] || this.writing) return false;
    this.writing = true;
    try {
      if (!write()) return false;
      // write can synchronously deliver a resolution or a disconnect. Never pop its replacement.
      if (this.pending[0] === displayed) this.pending = this.pending.slice(1);
      return true;
    } finally {
      this.writing = false;
    }
  }
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
  chosen?: ForegroundChoice,
): ApprovalInteractionTransition {
  const request = state.requests[0];
  if (!request) return { ...state, handled: false };

  // Ink strips Alt's ESC prefix from letters; it also reports meta on *bare* Esc
  // (empty input), and shift on uppercase letters. Don't infer intent from the letter alone.
  const escape = key.escape && input === "" && !key.return && !key.ctrl && !key.shift;
  if (key.ctrl || (key.meta && !escape) || (key.shift && !/^[A-Z]$/.test(input))) {
    return { ...state, handled: true };
  }
  const choices: readonly ApprovalChoice[] = !("draft" in request) && !request.choices?.length && request.kind === "computer_foreground_takeover"
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

  if (request.kind === "computer_foreground_takeover") {
    // Only the raw parser answers: it binds the key to this head and refuses Escape suffixes, string
    // payloads and pastes. Ink's keypress stream cannot tell those from a typed Y or Enter.
    if (chosen?.requestId === request.request_id && choices.includes(chosen.decision)) return resolve(chosen.decision);
  } else if (((key.return && (input === "" || input === "\r")) || normalizedInput === "y") && choices.includes("once")) {
    return resolve("once");
  } else if (normalizedInput === "t" && choices.includes("task")) {
    return resolve("task");
  } else if (normalizedInput === "a" && choices.includes("session")) {
    return resolve("session");
  } else if (normalizedInput === "l" && choices.includes("always")) {
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
  if (state.detailOpen && escape) {
    return {
      ...state,
      detailOpen: false,
      detailOffset: 0,
      handled: true,
    };
  }
  if ((escape || normalizedInput === "n") && choices.includes("deny")) {
    return resolve("deny");
  }
  return { ...state, handled: true };
}
