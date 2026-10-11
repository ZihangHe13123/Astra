import type { RenderLine } from "./markdown-render-lines.js";
import type { TimeRailLayout } from "./message-time-rail.js";

/** What a piece of the transcript is, as far as the space around it goes. */
export type FlowItem = "user" | "assistant" | "reasoning" | "tool" | "changes" | "other";
/** What stands between two pieces: nothing, a line the time rail runs through, or a clear line. */
export type FlowGap = "none" | "rail" | "blank";

const IN_TURN = new Set<FlowItem>(["assistant", "reasoning", "tool", "changes"]);

/**
 * The space between the pieces of a conversation. A piece leaves no blank line of its own; the
 * one that follows decides what goes between them, and brings it along: nothing inside a run of
 * tool calls, a line the time rail runs through inside a turn, and a clear line before a new
 * question. A piece that is not part of a turn ("other": a notice, a command's answer) keeps
 * the blank line it has always ended with, so nothing is owed after it.
 */
export class TranscriptFlow {
  private last: FlowItem | null = null;

  /** The piece whose space is still open, if any. */
  get previous(): FlowItem | null {
    return this.last;
  }

  before(next: FlowItem): FlowGap {
    const last = this.last;
    this.last = next === "other" ? null : next;
    if (last === null) return "none";
    if (last === "tool" && next === "tool") return "none";
    return IN_TURN.has(next) ? "rail" : "blank";
  }

  reset(): void {
    this.last = null;
  }
}

/** One empty line of the transcript, with the time rail running through it when `rail` is given. */
export function gapLine(key: string, rail?: TimeRailLayout): RenderLine {
  return {
    key,
    role: "system",
    prefix: rail ? "" : "    ",
    text: " ",
    kind: "text",
    spans: [],
    timeRail: rail ? { ...rail, label: "", kind: "relative" } : undefined,
  };
}
