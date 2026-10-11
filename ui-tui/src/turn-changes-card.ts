import stringWidth from "string-width";
import type { LineCell } from "./markdown-render-lines.js";
import type { TurnChangesEventData, TurnChangesFile } from "./turn-changes.js";

/** U+2212 MINUS SIGN, as in the one-line summary this card replaces. */
const MINUS = "\u2212";
const HINT = "/changes 查看";
const LETTERS: Record<string, string> = { modified: "M", added: "A", deleted: "D" };

const count = (value: unknown) => typeof value === "number" && Number.isFinite(value) ? value : 0;
const right = (value: string, width: number) => " ".repeat(Math.max(0, width - stringWidth(value))) + value;

/** A path that does not fit keeps its end: the file's name says more than where the tree begins. */
function clipStart(value: string, width: number): string {
  if (stringWidth(value) <= width) return value;
  if (width <= 1) return width === 1 ? "…" : "";
  const chars = [...value];
  let result = "";
  for (let index = chars.length - 1; index >= 0; index -= 1) {
    if (stringWidth(chars[index] + result) >= width) break;
    result = chars[index] + result;
  }
  return `…${result}`;
}

/**
 * What a turn changed, as rows of `width` cells: a head with the totals and the command that
 * shows the diff, then one row per file, up to `maxFiles` of them. Nothing when the turn changed
 * no file and left no path unconfirmed. A malformed report is shown as far as it can be read.
 */
export function turnChangesCard(event: TurnChangesEventData, width: number, maxFiles = 6): LineCell[][] | null {
  if (event === null || typeof event !== "object") return null;
  const files = (Array.isArray(event.files) ? event.files : []).filter(
    (file): file is TurnChangesFile => file !== null && typeof file === "object" && typeof file.path === "string",
  );
  const unknown = count(event.unknown_count);
  if (files.length === 0 && unknown === 0) return null;

  const totals = event.totals !== null && typeof event.totals === "object" ? event.totals : {};
  const unconfirmed = unknown > 0 ? `另有 ${unknown} 个路径未能确认` : "";
  const head: LineCell[] = files.length
    ? [
        { text: `✎ 本轮改动 ${files.length} 个文件`, tone: "warning", bold: true },
        { text: "  " },
        { text: `+${count(totals.added)}`, tone: "success" },
        { text: " " },
        { text: `${MINUS}${count(totals.removed)}`, tone: "danger" },
        ...(unconfirmed ? [{ text: ` · ${unconfirmed}`, tone: "muted" } as LineCell] : []),
      ]
    : [{ text: `✎ ${unconfirmed}`, tone: "warning", bold: true }];
  const used = head.reduce((sum, cell) => sum + stringWidth(cell.text), 0);
  // The command stays whole or is left out; half of it is no help.
  if (used + 2 + stringWidth(HINT) <= width) {
    head.push({ text: " ".repeat(width - used - stringWidth(HINT)) }, { text: HINT, tone: "muted" });
  }

  const shown = files.slice(0, files.length > maxFiles ? maxFiles - 1 : maxFiles);
  const hidden = files.length - shown.length;
  const counted = shown.map((file) => file.added === null && file.removed === null
    ? null
    : { added: `+${count(file.added)}`, removed: `${MINUS}${count(file.removed)}` });
  const addedWidth = Math.max(0, ...counted.map((entry) => entry ? stringWidth(entry.added) : 0));
  const pathWidth = Math.max(8, Math.min(
    Math.max(...shown.map((file) => stringWidth(file.path)), 0),
    width - 4 - 2 - addedWidth - 1 - 5,
  ));
  const rows = shown.map((file, index): LineCell[] => {
    const last = index === shown.length - 1 && hidden === 0;
    const path = clipStart(file.path, pathWidth);
    const entry = counted[index];
    return [
      { text: last ? "└ " : "├ ", tone: "muted" },
      { text: `${LETTERS[file.state] ?? "·"} `, tone: file.state === "added" ? "success" : file.state === "deleted" ? "danger" : "warning" },
      { text: path + " ".repeat(pathWidth - stringWidth(path)), tone: "text" },
      ...(entry
        ? [
            { text: "  " + right(entry.added, addedWidth), tone: "success" } as LineCell,
            { text: ` ${entry.removed}`, tone: "danger" } as LineCell,
          ]
        : [{ text: `  ${file.state}`, tone: "muted" } as LineCell]),
    ];
  });
  if (hidden > 0) rows.push([{ text: "└ ", tone: "muted" }, { text: `还有 ${hidden} 个文件`, tone: "muted" }]);
  return [head, ...rows];
}
