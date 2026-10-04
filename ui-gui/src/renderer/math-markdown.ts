import { fromMarkdown } from "mdast-util-from-markdown";
import { gfmFromMarkdown } from "mdast-util-gfm";
import { gfm } from "micromark-extension-gfm";
import rehypeKatex from "rehype-katex";
import type { Root as MarkdownRoot, RootContent as MarkdownNode } from "mdast";
import type { Element, ElementContent, Root, RootContent } from "hast";
import type { VFile } from "vfile";

export const remarkMathOptions = { singleDollarTextMath: true } as const;
export const MAX_MATH_LENGTH = 16_384;

type Range = { start: number; end: number };
const protectedTypes = new Set(["code", "inlineCode", "html", "link", "image", "definition", "linkReference", "imageReference"]);
function protectedRanges(tree: MarkdownRoot, text: string): Range[] {
  const ranges: Array<Range & { soft?: "html" | "autolink" }> = [];
  const visit = (node: MarkdownRoot | MarkdownNode) => {
    if (protectedTypes.has(node.type)) {
      const start = node.position?.start.offset, end = node.position?.end.offset;
      if (start !== undefined && end !== undefined) ranges.push({ start, end, soft: node.type === "html" ? "html" : node.type === "link" && /^(?:https?:\/\/|www\.)/i.test(text.slice(start, end)) ? "autolink" : undefined });
    } else if ("children" in node) for (const child of node.children) visit(child as MarkdownNode);
  };
  visit(tree);
  ranges.sort((a, b) => a.start - b.start);
  // CommonMark can parse <b> inside $a<b>c$ as an HTML fragment. A complete
  // formula owns such inner fragments and URLs in TeX commands. An outer
  // tag, URL or attribute still owns itself (GFM URLs may swallow the closing $).
  const formulas: Range[] = [];
  let cursor = 0;
  const inspect = (end: number) => {
    const offset = cursor;
    normalizeText(text.slice(cursor, end), (start, finish) => formulas.push({ start: offset + start, end: offset + finish }));
  };
  for (const range of ranges) if (!range.soft) { inspect(range.start); cursor = range.end; }
  inspect(text.length);
  let formulaIndex = 0;
  return ranges.filter(range => {
    while (formulaIndex < formulas.length && formulas[formulaIndex].end <= range.start) formulaIndex++;
    const formula = formulas[formulaIndex];
    return !range.soft || !formula || formula.start >= range.start || (range.soft === "html" ? formula.end < range.end : formula.end <= range.start);
  });
}
function escaped(text: string, index: number): boolean {
  let count = 0;
  for (let i = index - 1; i >= 0 && text[i] === "\\"; i--) count++;
  return count % 2 === 1;
}
function closeDelimiter(text: string, delimiter: string, start: number): number {
  let index = text.indexOf(delimiter, start);
  while (index >= 0) {
    if (!escaped(text, index) && (delimiter[0] !== "$" || text[index - 1] !== "$" && text[index + delimiter.length] !== "$")) return index;
    index = text.indexOf(delimiter, index + delimiter.length);
  }
  return -1;
}
function dollarsAsText(text: string): string {
  return text.replace(/\$/g, (value, index: number) => escaped(text, index) ? value : "\\$");
}
/** Conservative ambiguity rule: bare amounts remain text, explicitly paired
 * numeric formulas such as $100$ or $2+3$ remain math. This is not TeX parsing. */
function looksLikeAmount(body: string, following: string): boolean {
  body = body.trim();
  if (!body) return true;
  if (!/^\d/.test(body)) return false;
  const explicitMath = /[\\^_{}=+*/<>]/.test(body);
  const currencyWords = /^\d[\d,.]*\s*(?:(?:USD|EUR|GBP|JPY|CNY|RMB|AUD|CAD|HKD|dollars?|euros?|pounds?|per|and|or|to)\b|a\s+(?:month|year|week|day)\b|美元|欧元|英镑|元|块)/i.test(body);
  const amountRange = /\d/.test(following) && /^\d[\d,.\s-]*$/.test(body);
  return !explicitMath && (currencyWords || amountRange);
}
// remark-math treats a single dollar as a delimiter even after a TeX escape.
// Preserve a literal TeX dollar without leaving a Markdown delimiter in its body.
function literalTeXDollars(body: string): string {
  return body.replace(/(\\+)\$/g, (match, slashes: string) => slashes.length % 2 ? slashes.slice(0, -1) + "\\text{\\char36}" : match);
}
function normalizeText(text: string, onFormula?: (start: number, end: number) => void): string {
  let result = "";
  const missingClosers = new Set<string>();
  for (let i = 0; i < text.length;) {
    // An unclosed inline-code span is ordinary text to CommonMark. While its
    // closing tick is still streaming, do not accidentally typeset its dollars.
    if (text[i] === "`" && !escaped(text, i)) {
      result += dollarsAsText(text.slice(i)); break;
    }
    if (text[i] === "\\" && !escaped(text, i) && ["(", "["].includes(text[i + 1])) {
      const endDelimiter = text[i + 1] === "(" ? "\\)" : "\\]";
      const end = missingClosers.has(endDelimiter) ? -1 : closeDelimiter(text, endDelimiter, i + 2);
      if (end >= 0) {
        const delimiter = text[i + 1] === "(" ? "$" : "$$";
        // Dollar signs inside bracket-delimited TeX belong to that formula;
        // retain literal dollars rather than creating new Markdown delimiters.
        const body = literalTeXDollars(dollarsAsText(text.slice(i + 2, end)));
        onFormula?.(i, end + 2);
        result += delimiter + body + delimiter; i = end + 2; continue;
      }
      missingClosers.add(endDelimiter);
      result += "\\" + text.slice(i, i + 2); i += 2; continue;
    }
    if (text[i] === "$" && !escaped(text, i)) {
      let count = 1;
      while (text[i + count] === "$") count++;
      if (count > 2) { result += "\\$".repeat(count); i += count; continue; }
      const delimiter = "$".repeat(count), end = closeDelimiter(text, delimiter, i + count);
      const body = end < 0 ? "" : text.slice(i + count, end);
      const complete = end >= 0 && (count === 2 || !/\n[\t ]*\n/.test(body));
      if (complete && (count === 2 || !looksLikeAmount(body, text[end + 1] || ""))) {
        onFormula?.(i, end + count);
        result += delimiter + literalTeXDollars(body) + delimiter; i = end + count; continue;
      }
      result += "\\$".repeat(count); i += count; continue;
    }
    result += text[i++];
  }
  return result;
}
/** Normalize only the rendering input. Keep the original message for copying,
 * storage and source inspection. Markdown code/link/HTML ranges are untouched. */
export function normalizeMathMarkdown(text: string): string {
  if (!/[\\$]/.test(text)) return text;
  const ranges = protectedRanges(fromMarkdown(text, { extensions: [gfm()], mdastExtensions: [gfmFromMarkdown()] }), text);
  let cursor = 0, result = "";
  for (const { start, end } of ranges) {
    result += normalizeText(text.slice(cursor, start)) + text.slice(start, end);
    cursor = end;
  }
  return result + normalizeText(text.slice(cursor));
}

function classNames(node: Element): string[] {
  return Array.isArray(node.properties.className) ? node.properties.className.map(String) : [];
}
function formula(node: RootContent | ElementContent): Element | undefined {
  if (node.type !== "element" || node.properties["data-astra-code"] || node.properties.dataAstraCode) return;
  if (node.tagName === "pre" && node.children.length === 1) return formula(node.children[0]);
  const names = classNames(node);
  if (node.tagName === "code" && (names.includes("math-inline") || names.includes("math-display"))) return node;
}
function textValue(node: RootContent | ElementContent): string {
  return node.type === "text" ? node.value : "children" in node ? node.children.map(child => textValue(child)).join("") : "";
}
function hasKatexError(nodes: RootContent[]): boolean {
  return nodes.some(node => node.type === "element" && (classNames(node).includes("katex-error") || hasKatexError(node.children)));
}
function fallback(value: string, display: boolean): Element {
  const delimiter = display ? "$$" : "$";
  return { type: "element", tagName: "span", properties: { className: ["math-fallback"], title: "公式未能排版，显示源码" }, children: [{ type: "text", value: delimiter + value + delimiter }] };
}
/** Render only remark-math nodes, never code fences named math/latex. A fresh
 * KaTeX options/macro table per formula prevents \gdef leaking across messages. */
export function rehypeSafeKatex() {
  return (tree: Root, file: VFile) => {
    const source = String(file.value);
    const walk = (parent: Root | Element) => {
      for (let i = 0; i < parent.children.length; i++) {
        const node = parent.children[i], math = formula(node);
        if (math) {
          const value = textValue(math), start = math.position?.start.offset ?? node.position?.start.offset;
          const display = classNames(math).includes("math-display") || start !== undefined && source.slice(start, start + 2) === "$$";
          if (display) math.properties.className = ["language-math", "math-display"];
          let replacement: RootContent[] = [fallback(value, display)];
          if (value.length <= MAX_MATH_LENGTH) {
            const fragment: Root = { type: "root", children: [node as RootContent] };
            try {
              const errorsBefore = file.messages.length;
              rehypeKatex({ trust: false, strict: "ignore", maxSize: 10, maxExpand: 1000, macros: {}, errorColor: "currentColor" })(fragment, file);
              if (file.messages.length === errorsBefore && !hasKatexError(fragment.children)) replacement = fragment.children;
            } catch { /* Bad or unfinished TeX remains readable; no renderer exception. */ }
          }
          parent.children.splice(i, 1, ...replacement as ElementContent[]);
          i += replacement.length - 1;
        } else if (node.type === "element" && node.tagName !== "pre" && node.tagName !== "code") walk(node);
      }
    };
    walk(tree);
  };
}
