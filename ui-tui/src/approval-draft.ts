import { createHash } from "node:crypto";
import { escapeApprovalDisplayText } from "@astra/ui-core/approval-display";
import { completeApprovalDraft, draftLabels, saveAsIntentFacts } from "@astra/ui-core/approval-choices";
import { wrapToolResult } from "./tool-results.js";

// An oversized/malformed payload is not an approvable excerpt. Bound both render work
// and proof verification; never put message content in persistent state or diagnostics.
export const MAX_DRAFT_ROWS = 200;
const maxUnits = 8192;


function inert(value: string): string {
  // Make a real control and a literal string that spells its escape distinguishable.
  return escapeApprovalDisplayText(value.replaceAll("\\", "\\\\"))
    .replaceAll("\u2028", "\\u{2028}").replaceAll("\u2029", "\\u{2029}");
}

export function draftLogicalLines(draft: unknown): string[] | undefined {
  if (!completeApprovalDraft(draft, maxUnits)) return;
  const lines: string[] = [];
  for (const name of Object.keys(draftLabels) as (keyof typeof draftLabels)[]) {
    if (draft[name] !== undefined) lines.push(`${draftLabels[name]}：${inert(draft[name])}`);
  }
  lines.push("正文：", ...draft.text.split("\n").map(inert)); // retain blank, repeated and final empty lines
  return lines.length <= MAX_DRAFT_ROWS ? lines : undefined;
}

/** Both disclosures use the same checked screen rows before a positive answer. */
export function approvalDisclosureLines(card: object): string[] | undefined {
  const facts = saveAsIntentFacts(card);
  if (facts) {
    if (!facts.length) return;
    const lines = facts.map(({label, value}) => `${label}：${value}`);
    if ("approval_boundary" in card && typeof card.approval_boundary === "string")
      lines.push(`边界：${inert(card.approval_boundary)}`);
    return lines;
  }
  return "draft" in card ? draftLogicalLines(card.draft) : undefined;
}

export function draftFingerprint(lines: string[]): string {
  return createHash("sha256").update(JSON.stringify(lines)).digest("hex");
}

export type DraftRow = { text: string; newLine: boolean };
export function wrapDisclosure(lines: string[] | undefined, contentWidth: number): DraftRow[] | undefined {
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
