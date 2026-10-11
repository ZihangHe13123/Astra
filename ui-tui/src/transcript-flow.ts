import type { RenderLine } from "./markdown-render-lines.js";
import type { TimeRailLayout } from "./message-time-rail.js";

/**
 * What a piece of the transcript is, as far as the space around it goes. A "note" is a notice
 * of one line (a mode switched, a process finished); a "block" is anything longer that is not
 * part of the talk (a command's listing, a report).
 */
export type FlowItem = "user" | "assistant" | "reasoning" | "tool" | "changes" | "note" | "block";
/** What stands between two pieces: nothing, a line the time rail runs through, or a clear line. */
export type FlowGap = "none" | "rail" | "blank";

/** Rows that read as one list when they follow each other. */
const ROWS = new Set<FlowItem>(["tool", "note"]);

/**
 * The space between the pieces of a conversation. A piece leaves no blank line of its own; the
 * one that follows decides what goes between them, and brings it along: nothing between rows
 * (tool calls, one-line notices), a line the time rail runs through between anything else, and
 * a clear line before a new question.
 */
export class TranscriptFlow {
  private last: FlowItem | null = null;

  /** The piece whose space is still open, if any. */
  get previous(): FlowItem | null {
    return this.last;
  }

  before(next: FlowItem): FlowGap {
    const last = this.last;
    this.last = next;
    if (last === null) return "none";
    if (next === "user") return "blank";
    return ROWS.has(last) && ROWS.has(next) ? "none" : "rail";
  }

  reset(): void {
    this.last = null;
  }
}

/** What a message that is not part of the talk is: a one-line notice, or a block. */
export function noticeItem(content: string): "note" | "block" {
  return /\r?\n/.test(content.trim()) ? "block" : "note";
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
