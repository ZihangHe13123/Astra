import { randomBytes } from "node:crypto";
import { useLayoutEffect, useMemo, useRef, useSyncExternalStore } from "react";
import stringWidth from "string-width";

// Ink preserves OSC 8 styles on cells surviving Yoga layout, wrapping and clipping.
// Only the trusted card styles these two literal glyphs. The nonce never reaches the terminal.
const start = "\x1b]8;;astra-paint-";
const end = "\x1b]8;;\x07";
const marks = /\x1b\]8;;astra-paint-([0-9a-f]{32})-([br])-([ht])\x07/g;
export function approvalPaintGlyph(epoch: string | undefined, ready: boolean, edge: "h" | "t", glyph = edge === "h" ? "◆" : "O") {
  return epoch ? `${start}${epoch}-${ready ? "r" : "b"}-${edge}\x07${glyph}${end}` : glyph;
}

type Receipt = { epoch: string; ready: boolean };
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
  if (cell(head) !== "◆" || !["O", "e", "n"].includes(cell(tail) ?? "")) return;

  // A full clear (and debug mode) can replay Static history. From the marked
  // header to the final newline, require visible rows, no wrapping, and only
  // cell styling, not cursor motion that could erase or move these glyphs.
  const from = frame.lastIndexOf("\n", head.index!) + 1;
  const lines = frame.slice(from).replace(marks, "").replaceAll(end, "").replace(/\x1b\[[\d;]*m/g, "").split("\n");
  if (Number.isSafeInteger(rows) && rows > 0 && Number.isSafeInteger(columns) && columns > 0 && lines.length <= rows && lines.every(line =>
    !/[\x00-\x1f\x7f-\x9f]/.test(line) && stringWidth(line) < columns)) {
    return { epoch: head[1]!, ready: head[2] === "r" };
  }
}

/** Ink's output only. Install over TerminalOutput so write callbacks acknowledge the actual bytes. */
export function approvalPaintOutput(target: NodeJS.WriteStream): NodeJS.WriteStream {
  let receipt: Receipt | undefined, revision = 0;
  const listeners = new Set<() => void>();
  const publish = (next?: Receipt) => {
    if (receipt?.epoch === next?.epoch && receipt?.ready === next?.ready) return;
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
        if (url) privateLink = /^astra-paint-[0-9a-f]{32}-[br]-[ht]$/.test(url);
        return privateLink ? "" : code;
      });
    }
    if (receipt?.epoch !== candidate?.epoch || receipt?.ready !== candidate?.ready) publish();
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
  expanded: boolean) {
  const observer = observers.get(stdout);
  const epoch = useMemo(() => head && observer ? randomBytes(16).toString("hex") : undefined,
    [head, observer, rows, columns, expanded]);
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
  return { epoch, receipt, current: (ready: boolean) => {
    const proof = committed.current;
    return !!proof && proof.epoch === epoch && proof.ready === ready && proof === observer?.read() &&
      (!receipt || proof === receipt) && stdout.isTTY && !stdout.destroyed && !stdout.writableEnded &&
      stdout.rows === rows && stdout.columns === columns;
  } };
}
