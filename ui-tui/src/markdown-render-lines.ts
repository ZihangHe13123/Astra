import stringWidth from "string-width";
import { formatLatexForTerminal } from "./markdown-math.js";
import { renderMarkdownTable } from "./markdown-table.js";
import type { TimeRail } from "./message-time-rail.js";
import { terminalTokens } from "./terminal-text.js";
import {
  terminalHyperlinkDisplayText,
  terminalHyperlinkMode,
  type TerminalHyperlinkMode,
} from "./terminal-hyperlink.js";
import type { ChatMessage } from "./types.js";
import type { InlineSpan, MarkdownBlock } from "./terminal-markdown.js";

/** The colour a cell of a laid-out row takes from the theme. */
export type LineTone = "text" | "muted" | "subtle" | "accent" | "accentAlt" | "success" | "warning" | "danger";
/** `mark: "tool"` is drawn as the theme's own tool mark where that is one cell wide. */
export type LineCell = { text: string; tone?: LineTone; bold?: boolean; mark?: "tool" };

export type RenderLine = {
  key: string;
  role: ChatMessage["role"];
  text: string;
  spans?: InlineSpan[];
  prefix: string;
  // "cells" is a row laid out in advance (a tool call, a changed file): drawn as it is, never wrapped.
  kind: "text" | "header" | "list" | "quote" | "rule" | "code" | "table" | "math" | "cells";
  cells?: LineCell[];
  /** Beside the time rail: how far a wrapped list item or quote stands in from the text column. */
  hang?: number;
  /** The colour of the line's plain text, where it is not the speaker's own (a quiet notice, a warning). */
  tone?: LineTone;
  malformedMath?: boolean;
  timeRail?: TimeRail;
};

export type MarkdownLineOptions = {
  role: ChatMessage["role"];
  baseKey: string;
  bodyWidth: number;
  firstPrefix: string;
  continuationPrefix: string;
  timeRail?: TimeRail;
  startingLineIndex?: number;
};

export type MarkdownLineResult = {
  lines: RenderLine[];
  consumedLines: number;
};

export const INLINE_CODE_DELIMITER = "`";
export const MALFORMED_MATH_PREFIX = "⚠ ";

function sameSpanStyle(left: InlineSpan, right: InlineSpan): boolean {
  return left.kind === right.kind
    && left.href === right.href
    && left.malformed === right.malformed
    && left.provisional === right.provisional;
}

function appendSpan(line: InlineSpan[], span: InlineSpan): void {
  const previous = line.at(-1);
  if (previous && sameSpanStyle(previous, span)) {
    previous.text += span.text;
  } else {
    line.push({ ...span });
  }
}

function decorationWidth(span: InlineSpan): number {
  if (span.kind === "code") return stringWidth(INLINE_CODE_DELIMITER) * 2;
  if (span.kind === "math" && span.malformed) return stringWidth(MALFORMED_MATH_PREFIX);
  return 0;
}

function wrapWithWidths(
  spans: InlineSpan[],
  widthForLine: (lineIndex: number) => number,
  linkMode: TerminalHyperlinkMode = terminalHyperlinkMode(),
): InlineSpan[][] {
  const lines: InlineSpan[][] = [];
  let line: InlineSpan[] = [];
  let lineWidth = 0;

  const finishLine = () => {
    lines.push(line);
    line = [];
    lineWidth = 0;
  };

  for (const sourceSpan of spans) {
    const span = sourceSpan.kind === "link"
      ? {
          ...sourceSpan,
          text: terminalHyperlinkDisplayText(sourceSpan.text, sourceSpan.href, linkMode),
        }
      : sourceSpan;
    if (span.kind === "hard_break") {
      finishLine();
      continue;
    }
    for (const token of terminalTokens(span.text)) {
      const maxWidth = Math.max(1, widthForLine(lines.length));
      const mergesWithPrevious = Boolean(line.at(-1) && sameSpanStyle(line.at(-1)!, span));
      const addedWidth = token.width + (mergesWithPrevious ? 0 : decorationWidth(span));
      if (line.length > 0 && lineWidth + addedWidth > maxWidth) finishLine();
      const startsDecoratedSpan = !(line.at(-1) && sameSpanStyle(line.at(-1)!, span));
      appendSpan(line, { ...span, text: token.text });
      lineWidth += token.width + (startsDecoratedSpan ? decorationWidth(span) : 0);
    }
  }

  if (line.length > 0) lines.push(line);
  return lines;
}

export function wrapInlineSpans(
  spans: InlineSpan[],
  maxWidth: number,
  linkMode: TerminalHyperlinkMode = terminalHyperlinkMode(),
): InlineSpan[][] {
  return wrapWithWidths(spans, () => maxWidth, linkMode);
}

function textFor(spans: InlineSpan[]): string {
  return spans.map((span) => span.text).join("");
}

export function markdownBlocksToLines(
  blocks: MarkdownBlock[],
  options: MarkdownLineOptions,
): MarkdownLineResult {
  const lines: RenderLine[] = [];
  const startingLineIndex = options.startingLineIndex ?? 0;
  const railWidth = options.timeRail?.width ?? 0;

  const activityPrefix = () => lines.length === 0 && startingLineIndex === 0
    ? options.firstPrefix
    : options.continuationPrefix;
  // Beside the time rail every line starts in the text column, and a list or quote marker
  // stands in that column too, with its own text hanging after it. Without the rail a prefix
  // takes the room it has always taken.
  const isActivityPrefix = (prefix: string) => prefix === options.firstPrefix || prefix === options.continuationPrefix;
  const markerWidth = (prefix: string) => options.timeRail && !isActivityPrefix(prefix) ? stringWidth(prefix.trimStart()) : 0;
  const visiblePrefixWidth = (prefix: string, hang = 0) => options.timeRail
    ? options.timeRail.prefixWidth + (isActivityPrefix(prefix) ? hang : markerWidth(prefix))
    : stringWidth(prefix);
  const availableWidth = (prefix: string, hang = 0) => Math.max(1, options.bodyWidth - visiblePrefixWidth(prefix, hang) - railWidth);
  const continuationWidth = () => availableWidth(options.continuationPrefix);

  const append = (
    prefix: string,
    text: string,
    kind: RenderLine["kind"],
    extra: Pick<RenderLine, "spans" | "malformedMath" | "hang"> = {},
  ) => {
    const isFirstActivityLine = lines.length === 0 && startingLineIndex === 0;
    lines.push({
      key: `${options.baseKey}-${lines.length}`,
      role: options.role,
      prefix,
      text,
      kind,
      ...extra,
      timeRail: options.timeRail
        ? { ...options.timeRail, label: isFirstActivityLine ? options.timeRail.label : "" }
        : undefined,
    });
  };

  const emitStructured = (spans: InlineSpan[], kind: "text" | "header" | "list" | "quote", firstPrefix: string) => {
    const hang = markerWidth(firstPrefix);
    const wrapped = wrapWithWidths(spans, (lineIndex) => availableWidth(
      lineIndex === 0 ? firstPrefix : options.continuationPrefix,
      lineIndex === 0 ? 0 : hang,
    ));
    if (wrapped.length === 0) {
      append(firstPrefix, " ", kind, { spans: [] });
      return;
    }
    wrapped.forEach((line, index) => append(
      index === 0 ? firstPrefix : options.continuationPrefix,
      textFor(line),
      kind,
      index > 0 && hang ? { spans: line, hang } : { spans: line },
    ));
  };

  const emitPlain = (text: string, kind: "code" | "math" | "table", firstPrefix: string, malformedMath?: boolean) => {
    const rendererDecorationWidth = kind === "math" && malformedMath
      ? stringWidth(MALFORMED_MATH_PREFIX)
      : 0;
    const wrapped = wrapWithWidths([{ kind: "text", text: text || " " }], (lineIndex) => availableWidth(
      lineIndex === 0 ? firstPrefix : options.continuationPrefix,
    ) - rendererDecorationWidth);
    wrapped.forEach((line, index) => append(
      index === 0 ? firstPrefix : options.continuationPrefix,
      textFor(line),
      kind,
      malformedMath === undefined ? {} : { malformedMath },
    ));
  };

  for (const block of blocks) {
    if (block.kind === "paragraph") {
      emitStructured(block.spans, "text", activityPrefix());
      continue;
    }
    if (block.kind === "heading") {
      emitStructured(block.spans, "header", activityPrefix());
      continue;
    }
    if (block.kind === "quote") {
      emitStructured(block.spans, "quote", "  | ");
      continue;
    }
    if (block.kind === "list") {
      const prefix = block.ordered ? `${block.ordinal ?? 1}. `.padStart(4, " ") : "  - ";
      emitStructured(block.spans, "list", prefix);
      continue;
    }
    if (block.kind === "blank") {
      append(options.continuationPrefix, " ", "text");
      continue;
    }
    if (block.kind === "rule") {
      append(
        options.continuationPrefix,
        "-".repeat(Math.min(64, continuationWidth())),
        "rule",
      );
      continue;
    }
    if (block.kind === "code") {
      emitPlain(block.text, "code", activityPrefix());
      continue;
    }
    if (block.kind === "table") {
      const width = Math.min(availableWidth(activityPrefix()), continuationWidth());
      const table = width >= 24 ? renderMarkdownTable(block.source, 0, width) : null;
      const tableLines = table?.lines ?? block.source;
      tableLines.forEach((text, index) => emitPlain(
        text,
        "table",
        index === 0 ? activityPrefix() : options.continuationPrefix,
      ));
      continue;
    }

    if (block.kind === "math") {
      const formatted = formatLatexForTerminal(block.source);
      const mathLines = formatted.text.split(/\r?\n/).filter((line) => line.trim());
      (mathLines.length > 0 ? mathLines : [" "]).forEach((text, index) => {
        emitPlain(text, "math", index === 0 ? activityPrefix() : options.continuationPrefix, block.malformed || formatted.malformed);
      });
    }
  }

  return { lines, consumedLines: lines.length };
}
