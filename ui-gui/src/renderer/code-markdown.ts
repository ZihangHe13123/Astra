import type { Code, Root } from "mdast";
import type { Element, Root as HastRoot, RootContent } from "hast";
import rehypeHighlight from "rehype-highlight";

export const MAX_HIGHLIGHT_CHARS = 32_768;
const highlight = rehypeHighlight({ detect: false, plainText: ["mermaid", "math", "text", "txt", "plaintext"] });

/** Reuse registered language grammars across streamed Markdown revisions. */
export function rehypeCodeHighlight() { return highlight; }

/** The Markdown parser supplies code ranges, so quoted/nested fences remain code. */
export function remarkCodeMetadata() {
  return (tree: Root, file: { value: unknown }) => {
    const source = String(file.value);
    const walk = (node: Root | Root["children"][number] | { type: string; children?: unknown[] }) => {
      if (node.type === "code") {
        const code = node as Code;
        const raw = source.slice(code.position?.start.offset, code.position?.end.offset);
        const opening = /^(?: {0,3})?(`{3,}|~{3,})/.exec(raw);
        const last = raw.split(/\r?\n/).at(-1) || "";
        const complete = !!opening && raw.includes("\n") && new RegExp(`^[\\s>]*${opening[1][0]}{${opening[1].length},}\\s*$`).test(last);
        code.data ||= {};
        code.data.hProperties = { ...code.data.hProperties, "data-astra-code": "true", "data-fence-complete": String(complete) };
      }
      if ("children" in node && node.children) for (const child of node.children) walk(child as Parameters<typeof walk>[0]);
    };
    walk(tree);
  };
}

export function codeText(node: RootContent | Element): string {
  if (node.type === "text") return node.value;
  return "children" in node ? node.children.map(child => codeText(child)).join("") : "";
}

/** Avoid expensive guessing and large-input tokenization on every stream delta. */
export function rehypeCodeBudget() {
  return (tree: HastRoot) => {
    const walk = (node: HastRoot | RootContent) => {
      if (node.type === "element" && node.tagName === "code") {
        const classes = Array.isArray(node.properties.className) ? node.properties.className : [];
        if (classes.includes("language-mermaid") || codeText(node).length > MAX_HIGHLIGHT_CHARS) {
          node.properties.className = [...classes, "no-highlight"];
        }
      }
      if ("children" in node) for (const child of node.children) walk(child);
    };
    walk(tree);
  };
}
