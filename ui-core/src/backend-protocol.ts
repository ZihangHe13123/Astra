import type { PyEvent } from "./types.js";

export type ProtocolDiagnostic = {
  phase: "parse" | "handle";
  event_type: string;
  error_type: string;
};

const diagnosticEvents = new Set([
  "chunk", "reasoning", "done", "error", "history", "tool_result", "tool_calls",
  "task_started", "task_status", "generation_stats", "generation_progress", "context_compaction", "model_info",
  "computer_state", "process_status", "user_question_request", "user_question_pending", "user_question_resolved", "tool_approval_request",
]);
const errorTypes = new Set(["Error", "TypeError", "RangeError", "SyntaxError", "ReferenceError", "URIError", "EvalError"]);

type Envelope = {
  type?: string; event_id?: string; cursor?: number; replayable?: boolean; replayed?: boolean; sequence?: number; generation?: string;
  request_id?: unknown; retryable?: unknown;
};

/** The approval or question a request opens. */
const controlRequests: Record<string, string> = { tool_approval_request: "approval", user_question_request: "question" };
/** The approval or question an event ends for good; a retryable question rejection leaves it open. */
function settledControl(event: Envelope): string | undefined {
  const kind = { approval_resolved: "approval", approval_response_rejected: "approval", user_question_resolved: "question" }[event.type ?? ""]
    ?? (event.type === "user_question_response_rejected" && !event.retryable ? "question" : undefined);
  return kind && typeof event.request_id === "string" && event.request_id ? `${kind}:${event.request_id}` : undefined;
}

export class EventDeliveryState {
  private seen = new Set<string>();
  /** Approvals and questions already ended: a lost live request replayed after its resolution is stale. */
  private settled = new Set<string>();
  private failed = new Set<number>();
  private highWater = 0;
  private generation = "";
  private sequence = 0;
  private completedThrough = 0;
  private gapFloor: number | undefined;

  get cursor(): number {
    return Math.min(this.highWater, this.gapFloor ?? this.highWater,
      ...[...this.failed].map(value => value - 1));
  }
  get hasPendingDelivery(): boolean {
    return this.failed.size > 0 || this.gapFloor !== undefined;
  }
  duplicate(event: Envelope): boolean {
    const control = controlRequests[event.type ?? ""];
    return Boolean(event.event_id && this.seen.has(event.event_id))
      || Boolean(event.replayed && typeof event.cursor === "number"
        && (event.cursor <= this.cursor || event.cursor < this.completedThrough))
      || Boolean(control && typeof event.request_id === "string" && this.settled.has(`${control}:${event.request_id}`));
  }
  gap(event: Envelope): boolean {
    if (event.replayed || !event.generation || !Number.isSafeInteger(event.sequence)) return false;
    const gap = event.generation === this.generation && event.sequence! > this.sequence + 1;
    // Newer live events may arrive before their recovery page. Keep the
    // applied cursor before the gap so those missing events are not deduped.
    if (gap && this.gapFloor === undefined) this.gapFloor = this.cursor;
    if (this.generation !== event.generation) this.sequence = 0;
    this.generation = event.generation;
    this.sequence = Math.max(this.sequence, event.sequence!);
    return gap;
  }
  completeReplay(): void {
    this.gapFloor = undefined;
  }
  acknowledge(event: Envelope, success: boolean): void {
    if (success && event.event_id) {
      this.seen.add(event.event_id);
      if (this.seen.size > 4096) this.seen.delete(this.seen.values().next().value!);
    }
    const settled = success ? settledControl(event) : undefined;
    if (settled) {
      this.settled.add(settled);
      if (this.settled.size > 1024) this.settled.delete(this.settled.values().next().value!);
    }
    // Volatile text frames carry the latest journal cursor too. They cannot
    // acknowledge lifecycle events that the renderer has never applied.
    if (event.replayable && Number.isSafeInteger(event.cursor) && event.cursor! > 0) {
      if (success) this.failed.delete(event.cursor!);
      else if (this.failed.size < 4096) this.failed.add(event.cursor!);
      this.highWater = Math.max(this.highWater, event.cursor!);
      if (success && event.type === "done") this.completedThrough = Math.max(this.completedThrough, event.cursor!);
    }
  }
}

export function createBackendEventReceiver(
  handle: (event: PyEvent) => void,
  report: (diagnostic: ProtocolDiagnostic) => void,
  options: { delivery?: EventDeliveryState; onGap?: (afterCursor: number) => void;
    onAccepted?: (event: PyEvent, parseMs: number) => void } = {},
): (line: string) => void {
  const delivery = options.delivery ?? new EventDeliveryState();
  let replayPending = false;
  const reported = new Set<string>();
  const failed = (phase: ProtocolDiagnostic["phase"], eventType: unknown, error: unknown) => {
    const diagnostic = {
      phase,
      event_type: typeof eventType === "string" && diagnosticEvents.has(eventType) ? eventType : "unknown",
      error_type: error instanceof Error && errorTypes.has(error.name) ? error.name : "Error",
    };
    const key = JSON.stringify(diagnostic);
    if (reported.has(key) || reported.size >= 32) return;
    reported.add(key);
    try { report(diagnostic); } catch { /* A diagnostic sink cannot break event delivery. */ }
  };
  return (line) => {
    const parseStarted = options.onAccepted ? performance.now() : 0;
    let event: PyEvent;
    try {
      event = JSON.parse(line);
      if (!event || typeof event !== "object" || Array.isArray(event) || typeof event.type !== "string") {
        throw new SyntaxError("Invalid event envelope");
      }
    } catch (error) {
      failed("parse", undefined, error);
      return;
    }
    const envelope = event as PyEvent & Envelope;
    if (delivery.duplicate(envelope)) {
      // A completed turn supersedes its replayed transient controls, and a
      // resolution supersedes its request. Retire their failed delivery slots
      // without resurrecting tools, approvals or questions.
      delivery.acknowledge(envelope, true);
      return;
    }
    try { options.onAccepted?.(event, performance.now() - parseStarted); } catch { /* Timing is optional. */ }
    if (delivery.gap(envelope) && !replayPending && options.onGap) {
      replayPending = true;
      try { options.onGap(delivery.cursor); } catch (error) {
        replayPending = false;
        failed("handle", "event_replay", error);
      }
    }
    try {
      handle(event);
      delivery.acknowledge(envelope, true);
      if (event.type === "event_replay_complete" && !event.has_more) {
        delivery.completeReplay();
        replayPending = false;
      }
    } catch (error) {
      delivery.acknowledge(envelope, false);
      failed("handle", event.type, error);
    }
  };
}
