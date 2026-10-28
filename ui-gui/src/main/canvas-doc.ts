/**
 * The person's comment on a passage of a document canvas, written as the doc_* tools read one:
 * `<!-- @astra: ... -->` (agent/runtime/tools/documents.py). The passage is what they selected in the
 * rendered document, so its Markdown may be spelled differently; outside code blocks it is found
 *   1. as written, and the comment goes right after it;
 *   2. in a line read without its inline Markdown (emphasis, code, links), by the passage or its start,
 *      and the comment goes at the end of that line;
 * and failing both at the end of the document, with the passage quoted. A comment never goes inside a
 * heading: it goes on the line below it.
 */
export function insertComment(source: string, passage: string, comment: string): string {
  // Two hyphens would end the HTML comment early.
  const text = comment.replace(/\s+/g, " ").replace(/-{2,}/g, "—").trim().slice(0, 500);
  if (!text) throw new Error("批注是空的。");
  const quote = squeeze(passage);
  if (!quote) throw new Error("没有选中要批注的文字。");
  const marker = `<!-- @astra: ${text} -->`;
  const lines = source.split("\n");
  const open = proseLines(lines);
  const place = (index: number, after?: number) => {
    const line = lines[index];
    if (/^ {0,3}#{1,6}\s/.test(line)) lines.splice(index + 1, 0, marker);
    else lines[index] = after === undefined ? `${line.replace(/\s*$/, "")} ${marker}` : `${line.slice(0, after)} ${marker}${line.slice(after)}`;
    return lines.join("\n");
  };
  for (let index = 0; index < lines.length; index++) {
    if (!open[index]) continue;
    const end = endOf(lines[index], quote);
    if (end !== undefined) return place(index, end);
  }
  const plainQuote = plain(quote), start = plainQuote.slice(0, 24);
  for (const needle of [plainQuote, start]) {
    if (!needle) continue;
    for (let index = 0; index < lines.length; index++) if (open[index] && plain(lines[index]).includes(needle)) return place(index);
  }
  const about = `${quote.slice(0, 40)}${quote.length > 40 ? "…" : ""}`.replace(/-{2,}/g, "—");
  return `${source.replace(/\s*$/, "")}\n\n<!-- @astra: ${text}（关于：「${about}」） -->\n`;
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

/** Where `quote` ends in `line` as written, its runs of spaces read as one; undefined when it is not there. */
function endOf(line: string, quote: string): number | undefined {
  const at = squeeze(line).indexOf(quote);
  if (at < 0) return undefined;
  const leading = line.length - line.trimStart().length;
  let seen = 0, end = leading;
  while (end < line.length && seen < at + quote.length) {
    if (!(/\s/.test(line[end]) && /\s/.test(line[end - 1] ?? ""))) seen++;
    end++;
  }
  return end;
}

/** Which lines are prose: not inside a fenced code block, nor its fences. */
function proseLines(lines: string[]): boolean[] {
  let fence = "";
  return lines.map(line => {
    const mark = line.match(/^ {0,3}(`{3,}|~{3,})/);
    if (fence) { if (mark && mark[1][0] === fence[0] && mark[1].length >= fence.length) fence = ""; return false; }
    if (mark) { fence = mark[1]; return false; }
    return true;
  });
}
