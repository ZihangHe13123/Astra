import { fromMarkdown } from "mdast-util-from-markdown";
import { gfmFromMarkdown } from "mdast-util-gfm";
import { gfm } from "micromark-extension-gfm";

/**
 * The person's comment on a passage of a document canvas, written as the doc_* tools read one:
 * `<!-- @astra: ... -->` (agent/runtime/tools/documents.py). The passage is what they selected in the
 * rendered document, and `occurrence` says which of the same text it was, counted in the text as rendered.
 * It is found in that text (the Markdown parsed, entities and escapes read as they show, emphasis not in the
 * way; not in a link's address or an HTML comment), and the comment goes
 *   - right after it in plain text;
 *   - after a code span, a link or an autolink it ends in, never inside one;
 *   - on the line after a code block (indented as the block) or a heading;
 * else in a line read without its inline Markdown, by the passage or its start, outside code blocks, at the
 * end of that line; and failing both at the end of the document, with the passage quoted.
 */
export function insertComment(source: string, passage: string, comment: string, occurrence = 0): string {
  // Two hyphens would end the HTML comment early.
  const text = comment.replace(/\s+/g, " ").replace(/-{2,}/g, "—").trim().slice(0, 500);
  if (!text) throw new Error("批注是空的。");
  const quote = squeeze(passage);
  if (!quote) throw new Error("没有选中要批注的文字。");
  const { pieces, rendered } = renderedText(source);
  // Blocks run together in the rendered text, as a selection's surroundings read; spaces are not counted.
  const pattern = new RegExp(quote.split(" ").map(word => word.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("\\s*"), "g");
  const match = [...rendered.matchAll(pattern)][occurrence];
  const piece = match && pieces.find(item => item.offset < match.index + match[0].length
    && match.index + match[0].length <= item.offset + item.value.length);
  if (match && piece) {
    // A pipe would split a table's row.
    const marker = `<!-- @astra: ${piece.table ? text.replace(/\|/g, "｜") : text} -->`;
    if (piece.kind === "line") {
      const lineEnd = source.indexOf("\n", piece.after);
      const at = lineEnd < 0 ? source.length : lineEnd;
      return `${source.slice(0, at)}\n${piece.indent}${marker}${source.slice(at)}`;
    }
    // Plain text read as written is placed exactly; anything else (an escape, an entity, code, a link) after it.
    const at = piece.kind === "text" && piece.exact ? piece.start + match.index + match[0].length - piece.offset : piece.after;
    return `${source.slice(0, at)} ${marker}${source.slice(at)}`;
  }
  const marker = `<!-- @astra: ${text} -->`;
  const lines = source.split("\n");
  const open = proseLines(lines, pieces);
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
 * A piece of the document's rendered text: its value, where it starts in the rendered text, and where a
 * comment about it goes in the source. `text` is placed right after the words (exactly when the source reads
 * as the value), `inline` after the code span or link, `line` on the line after a code block or a heading.
 */
type Piece = { value: string; offset: number; start: number; after: number; kind: "text" | "inline" | "line";
  exact: boolean; indent: string; table: boolean; block: boolean; end: number };
type MarkdownNode = { type: string; value?: string; position?: { start: { offset?: number }; end: { offset?: number } };
  children?: MarkdownNode[] };

function renderedText(source: string): { pieces: Piece[]; rendered: string } {
  const pieces: Piece[] = [];
  let rendered = "";
  const lineStart = (offset: number) => source.lastIndexOf("\n", offset - 1) + 1;
  const indentAt = (offset: number) => source.slice(lineStart(offset)).match(/^[ \t]*/)![0];
  const visit = (node: MarkdownNode, around: { heading?: MarkdownNode; link?: MarkdownNode; table: boolean }) => {
    const start = node.position?.start.offset, end = node.position?.end.offset;
    if (node.type === "html" || node.type === "image" || start === undefined || end === undefined) return;
    if (["text", "inlineCode", "code"].includes(node.type)) {
      const value = node.value ?? "";
      const base = { value, offset: rendered.length, start, end, table: around.table, block: node.type === "code" };
      if (node.type === "code") {
        // Indented code: one level less is out of the block.
        const fenced = /^[ \t]*(`{3,}|~{3,})/.test(source.slice(lineStart(start)));
        const indent = indentAt(start);
        pieces.push({ ...base, kind: "line", after: end, exact: false, indent: fenced ? indent : indent.replace(/^(?: {4}|\t)/, "") });
      } else if (around.heading) {
        pieces.push({ ...base, kind: "line", after: around.heading.position?.end.offset ?? end, exact: false,
          indent: indentAt(around.heading.position?.start.offset ?? start) });
      } else if (around.link || node.type === "inlineCode") {
        pieces.push({ ...base, kind: "inline", after: around.link?.position?.end.offset ?? end, exact: false, indent: "" });
      } else {
        pieces.push({ ...base, kind: "text", after: end, exact: source.slice(start, end) === value, indent: "" });
      }
      rendered += value;
      return;
    }
    const next = { heading: node.type === "heading" ? node : around.heading,
      link: ["link", "linkReference"].includes(node.type) ? node : around.link, table: around.table || node.type === "table" };
    for (const child of node.children ?? []) visit(child, next);
  };
  visit(fromMarkdown(source, { extensions: [gfm()], mdastExtensions: [gfmFromMarkdown()] }) as MarkdownNode, { table: false });
  return { pieces, rendered };
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
function proseLines(lines: string[], pieces: Piece[]): boolean[] {
  const starts: number[] = [];
  let offset = 0;
  for (const line of lines) { starts.push(offset); offset += line.length + 1; }
  return lines.map((line, index) => !pieces.some(piece => piece.block
    && piece.start < starts[index] + line.length + 1 && starts[index] <= piece.end));
}
