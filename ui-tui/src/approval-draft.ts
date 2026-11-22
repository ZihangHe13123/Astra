import { createHash } from "node:crypto";
import { escapeApprovalDisplayText } from "@astra/ui-core/approval-display";
import { wrapToolResult } from "./tool-results.js";

// An oversized/malformed payload is not an approvable excerpt. Bound both render work
// and proof verification; never put message content in persistent state or diagnostics.
export const MAX_DRAFT_ROWS = 200;
const maxUnits = 8192;
const fieldLabels = { to: "发给", subject: "主题", via: "通过" } as const;
const fields = new Set(["text", "truncated", ...Object.keys(fieldLabels)]);

function inert(value: string): string {
  // Make a real control and a literal string that spells its escape distinguishable.
  return escapeApprovalDisplayText(value.replaceAll("\\", "\\\\"))
    .replaceAll("\u2028", "\\u{2028}").replaceAll("\u2029", "\\u{2029}");
}

export function draftLogicalLines(value: unknown): string[] | undefined {
  if (!value || typeof value !== "object" || Array.isArray(value)) return;
  const draft = value as Record<string, unknown>;
  if (Object.keys(draft).some(k => !fields.has(k)) || ("truncated" in draft && draft.truncated !== false) ||
      typeof draft.text !== "string" || !draft.text.length) return;
  let size = 0;
  for (const [name, field] of Object.entries(draft)) {
    if (name === "truncated") continue;
    if (typeof field !== "string" || (size += field.length) > maxUnits || /[\ud800-\udfff]/u.test(field)) return;
  }
  const lines: string[] = [];
  for (const [name, label] of Object.entries(fieldLabels)) {
    if (typeof draft[name] === "string") lines.push(`${label}：${inert(draft[name])}`);
  }
  lines.push("正文：", ...draft.text.split("\n").map(inert)); // retain blank, repeated and final empty lines
  return lines.length <= MAX_DRAFT_ROWS ? lines : undefined;
}

export function draftFingerprint(lines: string[]): string {
  return createHash("sha256").update(JSON.stringify(lines)).digest("hex");
}

export type DraftRow = { text: string; newLine: boolean };
export function wrapDraft(value: unknown, contentWidth: number): DraftRow[] | undefined {
  const lines = draftLogicalLines(value);
  if (!lines) return;
  const rows: DraftRow[] = [];
  for (const line of lines) {
    // Two visible rail cells enclose even leading/trailing spaces or an entirely blank row.
    const parts = line ? wrapToolResult(line, contentWidth - 2) : [""];
    if (rows.length + parts.length > MAX_DRAFT_ROWS) return;
    parts.forEach((text, i) => rows.push({ text, newLine: i === 0 }));
  }
  return rows;
}
