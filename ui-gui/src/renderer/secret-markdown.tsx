import React, { createContext, useContext, useMemo, useState } from "react";
import type { Element, ElementContent, Root, Text } from "hast";
import { findSecrets, maskSecretSlice } from "@astra/ui-core/secret-mask";

/** Element a masked credential piece is drawn as; its children keep the original text. */
export const SECRET_TAG = "astra-secret";

/** Elements whose text reads on with their neighbours'. Any other element (block, `code`, table cell) is scanned on its own. */
const INLINE = new Set(["a", "abbr", "b", "bdi", "bdo", "br", "cite", "del", "dfn", "em", "i", "ins", "kbd", "mark", "q", "s", "samp", "small", "span", "strong", "sub", "sup", "u", "var", "wbr"]);

const classes = (node: Element) => Array.isArray(node.properties.className) ? node.properties.className.map(String) : [];

/**
 * Masks recognised credentials in the rendered tree of a sent user message. It runs after parsing,
 * highlighting and formulas, so the Markdown structure, link targets and code (and its copy button)
 * come from the original text exactly as before; only drawn text changes. Text is scanned per block,
 * joined across inline elements and highlight tokens, so a value split by emphasis or a token is
 * still one value: each piece becomes an `astra-secret` element holding its original text, its
 * masked form and the value's key, and every piece of one value is revealed together.
 * Typeset formulas are left alone; their text is TeX, not what is shown.
 */
export function rehypeMaskSecrets() {
  return (tree: Root) => {
    let unit: { node: Text; parent: Root | Element }[] = [];
    let units = 0;
    const replaced = new Map<Text, ElementContent[]>();
    const parents = new Set<Root | Element>();
    const flush = () => {
      if (!unit.length) return;
      const text = unit.map(entry => entry.node.value).join("");
      const spans = findSecrets(text);
      const id = units++;
      let offset = 0, first = 0;
      for (const { node, parent } of unit) {
        const start = offset, end = offset + node.value.length;
        offset = end;
        while (first < spans.length && spans[first].end <= start) first++;
        if (first >= spans.length || spans[first].start >= end) continue;
        const pieces: ElementContent[] = [];
        let cursor = start;
        for (let index = first; index < spans.length && spans[index].start < end; index++) {
          const span = spans[index];
          const from = Math.max(start, span.start), to = Math.min(end, span.end);
          if (from > cursor) pieces.push({ type: "text", value: text.slice(cursor, from) });
          pieces.push({ type: "element", tagName: SECRET_TAG, properties: { dataSecretKey: `${id}:${span.start}`, dataMasked: maskSecretSlice(text, span, from, to) }, children: [{ type: "text", value: text.slice(from, to) }] });
          cursor = to;
        }
        if (cursor < end) pieces.push({ type: "text", value: text.slice(cursor, end) });
        replaced.set(node, pieces);
        parents.add(parent);
      }
      unit = [];
    };
    const walk = (node: Root | Element) => {
      for (const child of node.children) {
        if (child.type === "text") unit.push({ node: child, parent: node });
        else if (child.type === "element") {
          if (classes(child).some(name => name === "katex" || name === "katex-display")) { flush(); continue; }
          const inline = INLINE.has(child.tagName);
          if (!inline) flush();
          walk(child);
          if (!inline) flush();
        }
      }
    };
    walk(tree);
    flush();
    for (const parent of parents) parent.children = parent.children.flatMap(child => child.type === "text" && replaced.get(child) || [child]) as typeof parent.children;
  };
}

type Reveal = { shown: (key: string) => boolean; toggle: (key: string) => void };
export const SecretRevealContext = createContext<Reveal | undefined>(undefined);
const NONE: ReadonlySet<string> = new Set();

/** The values shown in one message view: keys are only meaningful for the text they were found in. */
export type RevealState = { text: string; keys: ReadonlySet<string> };
export const revealedKeys = (state: RevealState, text: string): ReadonlySet<string> => state.text === text ? state.keys : NONE;
/** Show or hide one value, leaving the others as they are; a changed text starts with all hidden. */
export function toggleRevealed(state: RevealState, text: string, key: string): RevealState {
  const keys = new Set(revealedKeys(state, text));
  if (!keys.delete(key)) keys.add(key);
  return { text, keys };
}

/** Which values of one message view are shown. Nothing is saved: a remounted row starts with every value hidden. */
export function SecretReveal({ text, children }: { text: string; children: React.ReactNode }) {
  const [state, setState] = useState<RevealState>({ text, keys: NONE });
  const keys = revealedKeys(state, text);
  const reveal = useMemo<Reveal>(() => ({
    shown: key => keys.has(key),
    toggle: key => setState(old => toggleRevealed(old, text, key)),
  }), [keys, text]);
  return <SecretRevealContext.Provider value={reveal}>{children}</SecretRevealContext.Provider>;
}

/** One masked piece. Clicking it shows or hides that value only; inside a link it never follows the link. */
export function SecretValue({ node, children }: { node?: Element; children?: React.ReactNode }) {
  const reveal = useContext(SecretRevealContext);
  const key = String(node?.properties.dataSecretKey ?? "");
  const shown = !!reveal?.shown(key);
  const toggle = (event: React.SyntheticEvent) => { event.preventDefault(); event.stopPropagation(); reveal?.toggle(key); };
  return <span className={`secret-value${shown ? " shown" : ""}`} role="button" tabIndex={0} aria-pressed={shown}
    aria-label={shown ? undefined : "已遮挡的密钥，点按显示"} title={shown ? "点按隐藏" : "点按显示这个值"} data-secret-key={key}
    onClick={toggle} onKeyDown={event => { if (event.key === "Enter" || event.key === " ") toggle(event); }}>
    {shown ? children : String(node?.properties.dataMasked ?? "")}
  </span>;
}
