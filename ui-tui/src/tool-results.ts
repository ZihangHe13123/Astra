import stringWidth from "string-width";
import { imageSearchSummary } from "./image-gallery.js";
import { documentSummary, type DocumentLine } from "./document-view.js";

export interface ToolResultRecord {
  id: number;
  name: string;
  output: string;
  error: string;
  durationMs?: number;
  artifactPath?: string;
  outputTruncated?: boolean;
}

const ANSI_PATTERN = /\x1b\[[0-9;]*m/g;
const GRAPHEME_SEGMENTER = typeof Intl.Segmenter === "function"
  ? new Intl.Segmenter(undefined, { granularity: "grapheme" })
  : undefined;

function graphemeSegments(text: string): string[] {
  return GRAPHEME_SEGMENTER
    ? Array.from(GRAPHEME_SEGMENTER.segment(text), ({ segment }) => segment)
    : Array.from(text);
}

export function toolResultBody(result: ToolResultRecord): string {
  // Documents show one line in the transcript; Ctrl+O shows the document itself.
  const document = documentSummary(result);
  if (document) return document;
  return imageSearchSummary(result) ?? (result.output || result.error || "(done)");
}

export function shouldCollapseToolResult(
  content: string,
  maxChars = 700,
  maxLines = 6,
): boolean {
  if (content.length > maxChars) return true;
  return content.split(/\r?\n/).length > maxLines;
}

export function summarizeToolResult(content: string, maxChars = 120): string {
  const firstUsefulLine = content
    .replace(ANSI_PATTERN, "")
    .split(/\r?\n/)
    .map((line) => line.trim())
    .find(Boolean) ?? "(empty result)";
  if (stringWidth(firstUsefulLine) <= maxChars) return firstUsefulLine;
  const budget = Math.max(0, maxChars - stringWidth("…"));
  let summary = "";
  for (const segment of graphemeSegments(firstUsefulLine)) {
    if (stringWidth(summary) + stringWidth(segment) > budget) break;
    summary += segment;
  }
  return summary.trimEnd() + "…";
}

export function wrapToolResult(content: string, width: number): string[] {
  const safeWidth = Math.max(1, width);
  const wrapped: string[] = [];
  for (const sourceLine of content.replace(/\r\n/g, "\n").split("\n")) {
    if (!sourceLine) {
      wrapped.push(" ");
      continue;
    }
    let line = "";
    let lineWidth = 0;
    for (const segment of graphemeSegments(sourceLine)) {
      const segmentWidth = stringWidth(segment);
      if (line && lineWidth + segmentWidth > safeWidth) {
        wrapped.push(line);
        line = "";
        lineWidth = 0;
      }
      line += segment;
      lineWidth += segmentWidth;
      if (lineWidth >= safeWidth) {
        wrapped.push(line);
        line = "";
        lineWidth = 0;
      }
    }
    if (line) wrapped.push(line);
  }
  return wrapped.length ? wrapped : ["(empty result)"];
}

/** Wrap styled lines, repeating each line's style on its continuation rows. */
export function wrapStyledLines(lines: DocumentLine[], width: number): DocumentLine[] {
  return lines.flatMap((line) => wrapToolResult(line.text, width).map((text) => ({ text, style: line.style })));
}

export function clampToolDetailOffset(offset: number, lineCount: number, pageSize: number): number {
  const maxOffset = Math.max(0, lineCount - Math.max(1, pageSize));
  return Math.max(0, Math.min(offset, maxOffset));
}
