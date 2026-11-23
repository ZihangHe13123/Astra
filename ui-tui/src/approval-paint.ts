import { randomBytes } from "node:crypto";
import { useLayoutEffect, useMemo, useRef, useSyncExternalStore } from "react";
import stringWidth from "string-width";
import { draftFingerprint, draftLogicalLines, MAX_DRAFT_ROWS, type DraftRow } from "./approval-draft.js";

// Ink preserves OSC 8 styles on cells surviving Yoga layout, wrapping and clipping.
// Only the trusted card styles these two literal glyphs. The nonce never reaches the terminal.
const start = "\x1b]8;;astra-paint-";
const end = "\x1b]8;;\x07";
const marks = /\x1b\]8;;astra-paint-([0-9a-f]{32})-([br])-([ht])\x07/g;
export function approvalPaintGlyph(epoch: string | undefined, ready: boolean, edge: "h" | "t", glyph = edge === "h" ? "◆" : "O") {
  return epoch ? `${start}${epoch}-${ready ? "r" : "b"}-${edge}\x07${glyph}${end}` : glyph;
}

const draftMarks = /\x1b\]8;;astra-paint-([0-9a-f]{32})-d(\d{1,3})([lc])\x07/g;
export function approvalDraftRow(epoch: string | undefined, i: number, row: DraftRow) {
  const cells = `${row.newLine ? "▏" : "↳"}${row.text}▕`;
  return epoch ? `${start}${epoch}-d${i}${row.newLine ? "l" : "c"}\x07${cells}${end}` : cells;
}

/** Read the actual post-layout cells, not a summary/row count reported by the component. */
function paintedDraft(frameBetweenEdges: string, epoch: string): string | undefined {
  if (!frameBetweenEdges.includes(`astra-paint-${epoch}-d`)) return;
  const logical: string[] = [];
  let index = 0, units = 0, finished = false;
  for (const line of frameBetweenEdges.split("\n")) {
    const [mark, extra] = line.matchAll(draftMarks);
    if (!mark) { if (index) finished = true; continue; }
    if (extra || finished || index >= MAX_DRAFT_ROWS || mark[1] !== epoch || mark[2] !== String(index++)) return;
    // Our draft rows only use foreground color. In particular, don't hash through
    // concealment, cursor movement, or a foreign OSC as though the text were visible.
    const safeStyles = /\x1b\[(?:0|3[0-79]|9[0-7]|38;5;\d{1,3}|38;2;\d{1,3};\d{1,3};\d{1,3})m/g;
    if (/[\x00-\x1f\x7f-\x9f]/.test(line.replace(draftMarks, "").replaceAll(end, "").replace(safeStyles, ""))) return;
    const from = mark.index! + mark[0].length, to = line.indexOf(end, from);
    if (to < 0) return;
    const cells = line.slice(from, to).replace(/\x1b\[[\d;]*m/g, "");
    if (!cells.startsWith(mark[3] === "l" ? "▏" : "↳") || !cells.endsWith("▕") || (units += cells.length) > 128_000) return;
    if (mark[3] === "l") logical.push(cells.slice(1, -1));
    else if (logical.length) logical[logical.length - 1] += cells.slice(1, -1);
    else return;
  }
  return logical.length ? draftFingerprint(logical) : undefined;
}

type Receipt = { epoch: string; ready: boolean; draftDigest?: string };
type Observer = { read(): Receipt | undefined; subscribe(cb: () => void): () => void };
const observers = new WeakMap<NodeJS.WriteStream, Observer>();
const absent = () => undefined;
const unsubscribed = () => () => {};
const protocolOnly = /^(?:\x1b\[(?:\?1004\$p|\?u|>11u|<u|\?(?:1004|25)[hl]))+$/;

function visibleCard(frame: string, rows: number, columns: number): Receipt | undefined {
  const [head, tail, extra] = frame.matchAll(marks);
  if (!head || !tail || extra || head[3] !== "h" || tail[3] !== "t" ||
      head[1] !== tail[1] || head[2] !== tail[2]) return;
  const cell = (mark: RegExpMatchArray) => frame.slice(mark.index! + mark[0].length).match(/^(?:\x1b\[[\d;]*m)*([^\x1b])/u)?.[1];
  if (cell(head) !== "◆" || !["O", "·", "e", "n"].includes(cell(tail) ?? "")) return;

  // A full clear (and debug mode) can replay Static history. From the marked
  // header to the final newline, require visible rows, no wrapping, and only
  // cell styling, not cursor motion that could erase or move these glyphs.
  const from = frame.lastIndexOf("\n", head.index!) + 1;
  const lines = frame.slice(from).replace(marks, "").replace(draftMarks, "").replaceAll(end, "").replace(/\x1b\[[\d;]*m/g, "").split("\n");
  if (Number.isSafeInteger(rows) && rows > 0 && Number.isSafeInteger(columns) && columns > 0 && lines.length <= rows && lines.every(line =>
    !/[\x00-\x1f\x7f-\x9f]/.test(line) && stringWidth(line) < columns)) {
    return { epoch: head[1]!, ready: head[2] === "r", draftDigest: paintedDraft(frame.slice(head.index, tail.index), head[1]!) };
  }
}

/** Ink's output only. Install over TerminalOutput so write callbacks acknowledge the actual bytes. */
export function approvalPaintOutput(target: NodeJS.WriteStream): NodeJS.WriteStream {
  let receipt: Receipt | undefined, revision = 0;
  const listeners = new Set<() => void>();
  const publish = (next?: Receipt) => {
    if (receipt?.epoch === next?.epoch && receipt?.ready === next?.ready && receipt?.draftDigest === next?.draftDigest) return;
    receipt = next;
    // A synchronous sink must not reenter Ink while it is committing its tree.
    queueMicrotask(() => listeners.forEach(notify => notify()));
  };
  const write = (chunk: string | Uint8Array, encoding?: BufferEncoding | ((err?: Error | null) => void),
    callback?: (err?: Error | null) => void) => {
    const cb = typeof encoding === "function" ? encoding : callback;
    const enc = typeof encoding === "string" ? encoding : "utf8";
    if (typeof chunk === "string" && (!chunk || protocolOnly.test(chunk))) return target.write(chunk, enc, cb);
    const serial = ++revision, columns = target.columns, rows = target.rows;
    let candidate: Receipt | undefined;
    if (typeof chunk === "string" && chunk.includes(start)) {
      if (target.isTTY) candidate = visibleCard(chunk, rows, columns);
      // Ink can emit a redundant link terminator at line end. Drop our links
      // and their terminators, without altering actual hyperlinks in the frame.
      let privateLink = false;
      chunk = chunk.replace(/\x1b\]8;;([^\x07\x1b\n]*)\x07/g, (code: string, url: string) => {
        if (url) privateLink = /^astra-paint-[0-9a-f]{32}-(?:[br]-[ht]|d\d{1,3}[lc])$/.test(url);
        return privateLink ? "" : code;
      });
    }
    if (receipt?.epoch !== candidate?.epoch || receipt?.ready !== candidate?.ready || receipt?.draftDigest !== candidate?.draftDigest) publish();
    let settled = false;
    try {
      return target.write(chunk, enc, error => {
        if (settled) return;
        settled = true;
        if (serial === revision) publish(!error && target.rows === rows && target.columns === columns ? candidate : undefined);
        cb?.(error);
      });
    } catch (error) {
      settled = true;
      if (serial === revision) publish();
      throw error;
    }
  };
  const stream = new Proxy(target, { get(source, prop) {
    if (prop === "write") return write;
    const value = Reflect.get(source, prop, source);
    return typeof value === "function" ? value.bind(source) : value;
  } });
  observers.set(stream, { read: () => receipt, subscribe(cb) { listeners.add(cb); return () => listeners.delete(cb); } });
  return stream;
}

export function useApprovalPaint(stdout: NodeJS.WriteStream, head: object | undefined, rows: number, columns: number,
  expanded: boolean, inputFenced = false, loanSelected = false) {
  const observer = observers.get(stdout);
  const epoch = useMemo(() => head && observer ? randomBytes(16).toString("hex") : undefined,
    [head, observer, rows, columns, expanded, inputFenced, loanSelected]);
  const expectedDraft = useMemo(() => {
    const lines = head && "draft" in head ? draftLogicalLines(head.draft) : undefined;
    return lines && draftFingerprint(lines);
  }, [head]);
  const latest = useSyncExternalStore(observer?.subscribe ?? unsubscribed, observer?.read ?? absent);
  const receipt = epoch && latest?.epoch === epoch ? latest : undefined;
  // Ink replaces its useInput listener in a passive effect. A fresh key may reach
  // the preceding listener after this same epoch already committed its painted
  // input owner. Never borrow another epoch's ACK or a merely queued receipt.
  const committed = useRef<Receipt>();
  useLayoutEffect(() => {
    committed.current = receipt;
    return () => { committed.current = undefined; };
  }, [receipt]);
  return { epoch, receipt, current: (ready: boolean, requireCompleteDraft = false) => {
    const proof = committed.current;
    return !!proof && proof.epoch === epoch && proof.ready === ready && proof === observer?.read() &&
      (!requireCompleteDraft || (!!expectedDraft && proof.draftDigest === expectedDraft)) &&
      (!receipt || proof === receipt) && stdout.isTTY && !stdout.destroyed && !stdout.writableEnded &&
      stdout.rows === rows && stdout.columns === columns;
  } };
}
