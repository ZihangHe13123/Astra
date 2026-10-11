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

/**
 * A finished tool call as one row of `width` cells: what was done, to what, what came of it,
 * how long it took, how it ended, and the number that opens its output. Columns sit at the same
 * place in every row of the same width, so a run of calls reads as a table. A call with a note
 * gets it on a row below, and a failed call the first line of what went wrong.
 */
export function toolRowCells(tool: ToolRowInput, width: number, icon = "◆"): LineCell[][] {
  const failed = Boolean(tool.error);
  // What the call was and what it left behind are described as for a call that ended well; that
  // it failed is told by the colour, the mark and the row below.
  const step = describeTool({ ...tool, error: "", status: "completed" });
  const tone = failed ? "danger" : "accentAlt";
  const id = `#${tool.id}`;
  const idWidth = Math.max(4, stringWidth(id) + 1);
  const timeWidth = width >= 36 ? TIME_WIDTH : 0;
  // The target and the result share what is left: the result ends where the time begins, in
  // every row, and the target has all the room the result does not need.
  const shared = Math.max(1, width - (stringWidth(icon) + 1 + VERB_WIDTH + timeWidth + 2 + idWidth));
  const result = shared >= 28 ? clip(step.result, Math.min(24, Math.floor(shared / 2))) : "";
  const targetWidth = shared - (result ? stringWidth(result) + 2 : 0);

  const [what, where = ""] = step.verb === "SEARCH" ? step.target.split(/  (?=in )/u) : [step.target];
  const whereText = where && stringWidth(what) + 2 + stringWidth(where) <= targetWidth ? `  ${where}` : "";
  const target = clip(what, targetWidth - stringWidth(whereText));
  const padding = " ".repeat(Math.max(0, targetWidth - stringWidth(target) - stringWidth(whereText)));
  // Under a tenth of a second the shared format would say "0.0s"; milliseconds say more.
  const time = typeof tool.durationMs !== "number" ? ""
    : tool.durationMs < 95 ? `${Math.max(0, Math.round(tool.durationMs))}ms`
    : span(tool.durationMs / 1000);

  const row: LineCell[] = [
    { text: `${icon} `, tone, mark: "tool" },
    { text: step.verb.padEnd(VERB_WIDTH), tone, bold: true },
    { text: target, tone: "text" },
    { text: whereText + padding, tone: "muted" },
    ...(result ? [{ text: `  ${result}`, tone: failed ? "danger" : "muted" } as LineCell] : []),
    ...(timeWidth ? [{ text: right(time, timeWidth), tone: "muted" } as LineCell] : []),
    { text: failed ? " ✕" : " ✓", tone: failed ? "danger" : "success" },
    { text: right(id, idWidth), tone: "muted" },
  ];
  const below = (text: string, lineTone: LineCell["tone"]): LineCell[] =>
    [{ text: "  " }, { text: clip(text, Math.max(1, width - 2)), tone: lineTone }];
  const note = firstLine(tool.note ?? "");
  return [
    row,
    ...(note ? [below(note, "muted")] : []),
    ...(failed ? [below(firstLine(tool.error) || firstLine(tool.output) || "failed", "danger")] : []),
  ];
}
