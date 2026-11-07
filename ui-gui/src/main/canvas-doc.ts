import { fromMarkdown } from "mdast-util-from-markdown";
import { gfmFromMarkdown } from "mdast-util-gfm";
import { gfm } from "micromark-extension-gfm";

/**
 * The person's comment on a passage of a document canvas, written as the doc_* tools read one:
 * `<!-- @astra: ... -->` (agent/runtime/tools/documents.py). The passage is what they selected in the
 * rendered document, and `occurrence` says which of the same text it was, counted as rendered. It is found
 *   1. as written, in text that renders (not in a link's address or an HTML comment), the comment going right
 *      after it; after a code span or a code block it is in, never inside one; on the line below a heading;
 *   2. in a line read without its inline Markdown (emphasis, code, links), by the passage or its start,
 *      outside code blocks, and the comment goes at the end of that line;
 * and failing both at the end of the document, with the passage quoted.
 */
export function insertComment(source: string, passage: string, comment: string, occurrence = 0): string {
  // Two hyphens would end the HTML comment early.
  const text = comment.replace(/\s+/g, " ").replace(/-{2,}/g, "—").trim().slice(0, 500);
  if (!text) throw new Error("批注是空的。");
  const quote = squeeze(passage);
  if (!quote) throw new Error("没有选中要批注的文字。");
  const marker = `<!-- @astra: ${text} -->`;
  const spans = renderedSpans(source);
  const pattern = new RegExp(quote.split(" ").map(word => word.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("\\s+"), "g");
  const found = [...source.matchAll(pattern)].flatMap(match => {
    const start = match.index, end = start + match[0].length;
    const span = spans.find(item => item.start <= start && end <= item.end);
    return span ? [{ end, span }] : [];
  });
  const chosen = found[occurrence];
  if (chosen) {
    const { end, span } = chosen;
    if (span.kind === "text") return `${source.slice(0, end)} ${marker}${source.slice(end)}`;
    if (span.kind === "inline") return `${source.slice(0, span.end)} ${marker}${source.slice(span.end)}`;
    const lineEnd = source.indexOf("\n", span.end);
    const at = lineEnd < 0 ? source.length : lineEnd;
    return `${source.slice(0, at)}\n${span.indent}${marker}${source.slice(at)}`;
  }
  const lines = source.split("\n");
  const open = proseLines(lines, spans);
  const plainQuote = plain(quote), start = plainQuote.slice(0, 24);
  for (const needle of [plainQuote, start]) {
    if (!needle) continue;
    const index = lines.flatMap((line, at) => open[at] && plain(line).includes(needle) ? [at] : [])[occurrence];
    if (index === undefined) continue;
    const line = lines[index];
    if (/^ {0,3}#{1,6}\s/.test(line)) lines.splice(index + 1, 0, marker);
    else lines[index] = `${line.replace(/\s*$/, "")} ${marker}`;
    return lines.join("\n");
  }
  const about = `${quote.slice(0, 40)}${quote.length > 40 ? "…" : ""}`.replace(/-{2,}/g, "—");
  return `${source.replace(/\s*$/, "")}\n\n<!-- @astra: ${text}（关于：「${about}」） -->\n`;
}

/**
 * Where the document's rendered text is in its source: plain text (in a heading or not), code spans and code
 * blocks. A comment after a heading or a code block goes on a line of its own, indented as the block is, so
 * it stays in the list item it belongs to and does not itself become code.
 */
type Span = { start: number; end: number; kind: "text" | "inline" | "heading" | "block"; indent: string };
type MarkdownNode = { type: string; position?: { start: { offset?: number }; end: { offset?: number } }; children?: MarkdownNode[] };

function renderedSpans(source: string): Span[] {
  const spans: Span[] = [];
  const indentAt = (offset: number) => source.slice(source.lastIndexOf("\n", offset - 1) + 1).match(/^[ \t]*/)![0];
  const visit = (node: MarkdownNode, heading?: MarkdownNode) => {
    const start = node.position?.start.offset, end = node.position?.end.offset;
    if (node.type === "html" || start === undefined || end === undefined) return;
    if (node.type === "code") {
      const indent = indentAt(start);
      // Indented code: one level less is out of the block.
      const fenced = /^[ \t]*(`{3,}|~{3,})/.test(source.slice(source.lastIndexOf("\n", start - 1) + 1));
      spans.push({ start, end, kind: "block", indent: fenced ? indent : indent.replace(/^(?: {4}|\t)/, "") });
    } else if (node.type === "inlineCode") spans.push({ start, end, kind: "inline", indent: "" });
    else if (node.type === "text" && heading?.position?.end.offset !== undefined)
      spans.push({ start, end: heading.position.end.offset, kind: "heading", indent: indentAt(heading.position.start.offset ?? start) });
    else if (node.type === "text") spans.push({ start, end, kind: "text", indent: "" });
    for (const child of node.children ?? []) visit(child, node.type === "heading" ? node : heading);
  };
  visit(fromMarkdown(source, { extensions: [gfm()], mdastExtensions: [gfmFromMarkdown()] }) as MarkdownNode);
  return spans;
}

const squeeze = (value: string) => value.replace(/\s+/g, " ").trim();

/** A line as it reads once rendered: no inline Markdown, no list or quote marks, spaces collapsed. */
function plain(line: string): string {
  return squeeze(line
    .replace(/^ {0,3}(?:#{1,6}\s+|>\s*|[-*+]\s+|\d+[.)]\s+)/, "")
    .replace(/!?\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/<!--[\s\S]*?-->|<[^>]+>/g, "")
    .replace(/(\*{1,3}|_{1,3}|`+|~~)/g, ""));
}

/** Which lines are prose: none of a code block, fenced or indented, nor its fences. */
function proseLines(lines: string[], spans: Span[]): boolean[] {
  const starts: number[] = [];
  let offset = 0;
  for (const line of lines) { starts.push(offset); offset += line.length + 1; }
  return lines.map((line, index) => !spans.some(span => span.kind === "block"
    && span.start < starts[index] + line.length + 1 && starts[index] <= span.end));
}
