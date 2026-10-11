import stringWidth from "string-width";
import { describeTool, span } from "@astra/ui-core/tool-activity";
import type { LineCell } from "./markdown-render-lines.js";

export type ToolRowInput = {
  /** The number `/tool <id>` opens. */
  id: number;
  name: string;
  arguments?: string;
  output: string;
  error: string;
  durationMs?: number;
  /** One line the call wants said under its row: where a document stands, how to open a gallery. */
  note?: string;
};

const VERB_WIDTH = 9;
const TIME_WIDTH = 6;

function clip(value: string, width: number): string {
  if (stringWidth(value) <= width) return value;
  if (width <= 1) return width === 1 ? "…" : "";
  let result = "";
  for (const char of value) {
    if (stringWidth(result + char) >= width) break;
    result += char;
  }
  return `${result}…`;
}

const right = (value: string, width: number) => " ".repeat(Math.max(0, width - stringWidth(value))) + value;
const firstLine = (value: string) => value.split(/\r?\n/).map((line) => line.trim()).find(Boolean) ?? "";

export type RowInput = {
  verb: string;
  target: string;
  /** A second, quieter part of the target, shown when there is room ("in agent/runtime"). */
  where?: string;
  result?: string;
  time?: string;
  failed?: boolean;
  /** The number `/tool <id>` opens; a row with none keeps the column empty. */
  id?: number;
};

/**
 * One row of `width` cells: what was done, to what, what came of it, how long it took and how
 * it ended. Columns sit at the same place in every row of the same width, so rows that follow
 * each other read as a table.
 */
export function rowCells(input: RowInput, width: number, icon = "◆"): LineCell[] {
  const failed = Boolean(input.failed);
  const tone = failed ? "danger" : "accentAlt";
  const id = input.id === undefined ? "" : `#${input.id}`;
  const idWidth = Math.max(4, stringWidth(id) + 1);
  const timeWidth = width >= 36 ? TIME_WIDTH : 0;
  // The target and the result share what is left: the result ends where the time begins, in
  // every row, and the target has all the room the result does not need.
  const shared = Math.max(0, width - (stringWidth(icon) + 1 + VERB_WIDTH + timeWidth + 2 + idWidth));
  const result = shared >= 28 ? clip(input.result ?? "", Math.min(24, Math.floor(shared / 2))) : "";
  const targetWidth = shared - (result ? stringWidth(result) + 2 : 0);
  const where = input.where ?? "";
  const whereText = where && stringWidth(input.target) + 2 + stringWidth(where) <= targetWidth ? `  ${where}` : "";
  const target = clip(input.target, targetWidth - stringWidth(whereText));
  const time = timeWidth ? input.time ?? "" : "";
  // What is left between the target and the first thing on the right is bridged by a faint
  // line of dots, so the eye can cross a wide row. Without a result the time stands right
  // after the dots; with one, the time keeps its own column.
  const open = targetWidth - stringWidth(target) - stringWidth(whereText)
    + (result ? 2 : timeWidth - stringWidth(time));
  return [
    { text: `${icon} `, tone, mark: "tool" },
    { text: input.verb.padEnd(VERB_WIDTH), tone, bold: true },
    { text: target, tone: "text" },
    ...(whereText ? [{ text: whereText, tone: "muted" } as LineCell] : []),
    { text: leader(open), tone: "subtle" },
    ...(result ? [{ text: result, tone: failed ? "danger" : "muted" } as LineCell] : []),
    ...(timeWidth ? [{ text: result ? right(time, timeWidth) : time, tone: "muted" } as LineCell] : []),
    { text: failed ? " ✕" : " ✓", tone: failed ? "danger" : "success" },
    { text: right(id, idWidth), tone: "muted" },
  ];
}

/** `width` cells between two things on a row: dots with a space at each end, or spaces when too short to read as a line. */
function leader(width: number): string {
  return width >= 6 ? ` ${"·".repeat(width - 2)} ` : " ".repeat(Math.max(0, width));
}

/** A line under a row: a note, or why a call failed. Cut to the row, never wrapped. */
export function belowRowCells(text: string, width: number, tone: LineCell["tone"]): LineCell[] {
  return [{ text: "  " }, { text: clip(firstLine(text), Math.max(1, width - 2)), tone }];
}

// Under a tenth of a second the shared format would say "0.0s"; milliseconds say more.
const took = (durationMs: number | undefined) => typeof durationMs !== "number" ? ""
  : durationMs < 95 ? `${Math.max(0, Math.round(durationMs))}ms`
  : span(durationMs / 1000);

/**
 * A finished tool call as one row, with the number that opens its output. A call with a note
 * gets it on a row below, and a failed call the first line of what went wrong.
 */
export function toolRowCells(tool: ToolRowInput, width: number, icon = "◆"): LineCell[][] {
  const failed = Boolean(tool.error);
  // What the call was and what it left behind are described as for a call that ended well; that
  // it failed is told by the colour, the mark and the row below.
  const step = describeTool({ ...tool, error: "", status: "completed" });
  const [what, where = ""] = step.verb === "SEARCH" ? step.target.split(/  (?=in )/u) : [step.target];
  const note = firstLine(tool.note ?? "");
  return [
    rowCells({ verb: step.verb, target: what, where, result: step.result, time: took(tool.durationMs), failed, id: tool.id }, width, icon),
    ...(note ? [belowRowCells(note, width, "muted")] : []),
    ...(failed ? [belowRowCells(firstLine(tool.error) || firstLine(tool.output) || "failed", width, "danger")] : []),
  ];
}

export type ProcessRowInput = { kind: string; label?: string; status: string; durationMs?: number; outputChars?: number; processId: string };

/**
 * A background process that has ended, as a row like a tool call's: what ran, how much it
 * wrote, how long it ran. One that did not complete says how it ended where the size would be.
 */
export function processRowCells(process: ProcessRowInput, width: number, icon = "◆"): LineCell[] {
  const completed = process.status === "completed";
  const what = firstLine(process.label ?? "") || process.processId.slice(0, 8);
  const chars = typeof process.outputChars === "number" ? `${process.outputChars.toLocaleString("en-US")} chars` : "";
  return rowCells({
    verb: "RUN",
    target: `background ${process.kind} · ${what}`,
    result: completed ? chars : process.status.replaceAll("_", " "),
    time: took(process.durationMs),
    failed: !completed,
  }, width, icon);
}
