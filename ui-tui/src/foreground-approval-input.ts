import { useLayoutEffect, useRef, useState } from "react";
import { useStdin, useStdout, type Key } from "ink";
import type { ForegroundRelease } from "./approval-interaction.js";

const ESC = "\x1b", CSI = ESC + "[", pasteEnd = CSI + "201~";
// Kitty keyboard protocol: disambiguate + report event types + encode even ordinary letters/Enter.
const flags = 1 | 2 | 8;
type Handler = (id: string, input: string, key: Partial<Key>, released?: ForegroundRelease) => boolean | void;

type Route = { filter?: (data: string) => string };
const routes = new WeakMap<NodeJS.ReadStream, Route>();

/** Ink 5 can throw while *parsing* CSI-u, even for an inactive composer. Keep its normal raw-mode
 * and readable ownership, filtering the same TTY at the read boundary, before key normalization. */
export function approvalAwareStdin(source: NodeJS.ReadStream): NodeJS.ReadStream {
  const route: Route = {};
  const read = (size?: number) => {
    for (;;) {
      const data = source.read(size);
      if (data === null || !route.filter) return data;
      const normal = route.filter(String(data));
      if (normal) return normal;
    }
  };
  const input = new Proxy(source, { get(stream, key) {
    if (key === "read") return read;
    const value = Reflect.get(stream, key, stream);
    return typeof value === "function" ? value.bind(stream) : value;
  } });
  routes.set(input, route);
  return input;
}

/** Only the raw, acknowledged terminal protocol can produce a foreground release. Never log stdin. */
export class ForegroundTerminalInput {
  private id?: string;
  private ready = false;
  private done = false;
  private armed?: number;
  private focused = true;
  private enabledFocus = false;
  private focusWasReset = false;
  private stage: "idle" | "probe" | "confirm" = "idle";
  private pushed = false;
  private protocolFrame = false;
  // Replies are ordered but have no request IDs. Ignore replies to older heads, even across an idle interval.
  private focusQueries = 0;
  private flagQueries = 0;
  private sequence = "";
  private holdPrefix = false;
  private sequenceOwner?: string;
  private overflow = false;
  private paste = false;
  private quarantinePaste = false;
  private pasteMatch = 0;
  private controlString?: "osc" | "st";
  private quarantineString = false;
  private stringEscape = false;

  constructor(private write: ((s: string) => void) | undefined, private handle: Handler,
    private reportReady: (id?: string) => void) {}

  private restore(query: boolean) {
    if (this.pushed) {
      if (query) this.flagQueries++;
      this.write?.((this.enabledFocus ? CSI + "?1004l" : "") + CSI + "<u" + (query ? CSI + "?u" : ""));
      this.pushed = false;
    }
  }

  close() { this.restore(false); this.id = undefined; this.armed = undefined; }

  head(id?: string) {
    if (id === this.id) return;
    this.restore(true);
    this.id = id; this.armed = undefined; this.ready = this.done = this.enabledFocus = false; this.stage = "idle";
    if (id && this.paste) this.quarantinePaste = true;
    if (id && this.controlString) this.quarantineString = true;
    this.reportReady();
    if (id && this.write) {
      this.focusQueries++;
      this.write(CSI + "?1004$p"); // ask DECRQM before changing focus reporting
    }
  }

  private send(input: string, key: Partial<Key>, decision?: ForegroundRelease["decision"]) {
    const id = this.id;
    if (!id || this.done) return;
    if (this.handle(id, input, key, decision ? { requestId: id, decision } : undefined) && this.id === id) this.done = true;
  }

  private frame(csi: string, owner?: string) {
    // Malformed CSI-u is not a key and must never be handed to Ink 5's keypress parser either.
    this.protocolFrame = csi.endsWith("u");
    if (csi === "200~") { this.armed = undefined; this.paste = true; this.quarantinePaste = !!this.id || !!owner; return; }
    if (csi === "I" || csi === "O") { this.protocolFrame = true; this.armed = undefined; this.focused = csi === "I"; return; }
    const focus = /^\?1004;([0-4])\$y$/.exec(csi);
    if (focus) {
      this.protocolFrame = true;
      if (this.focusQueries && --this.focusQueries === 0 && this.id && owner === this.id && ["1", "2", "3"].includes(focus[1]!)) {
        this.focusWasReset = focus[1] === "2"; this.stage = "probe"; this.flagQueries++;
        this.write?.(CSI + "?u");
      }
      return;
    }
    const reported = /^\?(\d{1,3})u$/.exec(csi);
    if (reported) {
      this.protocolFrame = true;
      if (this.flagQueries && --this.flagQueries === 0) {
        if (this.id && owner === this.id && this.stage === "probe") {
          this.pushed = true; this.enabledFocus = this.focusWasReset; this.stage = "confirm"; this.flagQueries++;
          this.write?.((this.enabledFocus ? CSI + "?1004h" : "") + `${CSI}>${flags}u${CSI}?u`);
        } else if (this.id && owner === this.id && this.stage === "confirm") {
          this.stage = "idle"; this.ready = Number(reported[1]) === flags;
          if (this.ready) this.reportReady(this.id);
        }
      }
      return;
    }
    const key = /^(\d{1,6})(?:;(\d{0,3})(?::([123]))?)?u$/.exec(csi);
    if (key) this.protocolFrame = true;
    if (!this.id || this.id !== owner || this.done) { this.armed = undefined; return; }
    const code = Number(key?.[1]), mod = Number(key?.[2] || 1), event = Number(key?.[3] || 1);
    const previous = this.armed; this.armed = undefined;
    if (key && this.ready && this.focused && mod === 1 && (code === 121 || code === 13 || code === 97)) {
      if (event === 1 && previous === undefined) this.armed = code;
      if (event === 3 && previous === code) this.send("", {}, code === 97 ? "session" : "once");
      return;
    }
    if (key && event === 1) {
      if (mod === 5 && (code === 99 || code === 121)) this.send(String.fromCharCode(code), { ctrl: true });
      if (mod === 1 && (code === 110 || code === 118)) this.send(String.fromCharCode(code), {});
      if (mod === 1 && code === 27) this.send("", { escape: true });
    }
    // Arrows/page keys retain their CSI final bytes under Kitty; act on press, not keyup.
    const nav = /^(1?|5|6)(?:;1(?::1)?)?([AB~])$/.exec(csi);
    if (nav) {
      const name = nav[2] === "~" ? ({ "5": "pageUp", "6": "pageDown" } as const)[nav[1] as "5" | "6"]
        : nav[2] === "A" ? "upArrow" : "downArrow";
      if (name) this.send("", { [name]: true });
    }
  }

  /** Ink's readable chunks are not key boundaries. Keep only a bounded control prefix, not pasted content. */
  feed(chunk: string): string {
    const owner = this.id;
    if (!owner && !this.sequence && !this.paste && !this.controlString && !/[\x1b\x90\x98\x9d-\x9f]/.test(chunk)) return chunk;
    let normal = "", continued = !!this.sequence;
    for (const c of chunk) {
      if (this.paste) {
        if (!owner && !this.quarantinePaste) normal += c;
        this.pasteMatch = c === pasteEnd[this.pasteMatch] ? this.pasteMatch + 1 : c === ESC ? 1 : 0;
        if (this.pasteMatch === pasteEnd.length) { this.paste = this.quarantinePaste = false; this.pasteMatch = 0; }
        continue;
      }
      if (this.controlString) {
        if (!owner && !this.quarantineString) normal += c;
        if (this.controlString === "osc" && c === "\x07" || c === "\x9c" || this.stringEscape && c === "\\") {
          this.controlString = undefined; this.quarantineString = false;
        }
        this.stringEscape = c === ESC; continue;
      }
      if ("\x90\x98\x9d\x9e\x9f".includes(c)) {
        this.controlString = c === "\x9d" ? "osc" : "st"; this.stringEscape = false;
        this.armed = undefined; this.sequence = ""; this.quarantineString = !!owner;
        if (!owner) normal += c; continue;
      }
      if (c === ESC) {
        if (this.sequence) this.armed = undefined;
        this.sequence = ESC; this.sequenceOwner = owner; this.overflow = continued = false;
        // Only a possible late protocol prefix needs buffering, never text / Ctrl+C awaiting an ACK.
        this.holdPrefix = !!owner || chunk.length > 1 || !!this.flagQueries || !!this.focusQueries;
        if (!this.holdPrefix) normal += c;
        continue;
      }
      if (this.sequence === ESC) {
        if (c === "[") { this.sequence = CSI; if (!this.holdPrefix && !owner) normal += c; }
        else {
          this.sequence = ""; this.armed = undefined; this.stringEscape = false;
          this.controlString = c === "]" ? "osc" : "P_^X".includes(c) ? "st" : undefined;
          this.quarantineString = !!owner || !!this.sequenceOwner;
          if (!owner) normal += this.holdPrefix && !continued && !this.sequenceOwner ? ESC + c : c;
          else if (continued && chunk.length === 1) this.legacy(c);
        }
        continue;
      }
      if (this.sequence) {
        if (c >= "@" && c <= "~") {
          const body = this.sequence.slice(2) + c; this.sequence = "";
          this.protocolFrame = false;
          if (!this.overflow) this.frame(body, this.sequenceOwner);
          if (!owner && !this.protocolFrame && !this.quarantinePaste && !this.overflow) {
            normal += this.holdPrefix && !this.sequenceOwner ? CSI + body : c;
          }
        } else if (c < " ") {
          this.sequence = ""; this.armed = undefined;
          if (!owner) normal += c; else if (chunk.length === 1) this.legacy(c);
        } else if (!this.overflow && c <= "?" && this.sequence.length < 48) {
          this.sequence += c; if (!this.holdPrefix && !owner) normal += c;
        } else { this.overflow = true; this.armed = undefined; }
        continue;
      }
      this.armed = undefined;
      if (!owner) normal += c;
      else if (chunk.length === 1 && owner === this.id) this.legacy(c);
    }
    return normal;
  }

  private legacy(c: string) {
    if (c === "\x03" || c === "\x19") this.send(c === "\x03" ? "c" : "y", { ctrl: true });
    if (/^[nv]$/i.test(c)) this.send(c.toLowerCase(), {});
  }
}

export function useForegroundApprovalInput(id: string | undefined, handle: Handler): boolean {
  const { stdin, isRawModeSupported } = useStdin(), { stdout } = useStdout();
  const [readyId, setReadyId] = useState<string>();
  const latest = useRef({ id, handle }); latest.current = { id, handle };
  const controller = useRef<ForegroundTerminalInput>();
  useLayoutEffect(() => {
    const route = routes.get(stdin);
    if (!route) return; // an embedding without the raw adapter retains denial, never a legacy grant
    let live = true;
    const input = new ForegroundTerminalInput(isRawModeSupported && stdout.isTTY ? s => { stdout.write(s); } : undefined,
      (head, ...args) => head === latest.current.id && latest.current.handle(head, ...args), head => { if (live) setReadyId(head); });
    controller.current = input;
    route.filter = chunk => input.feed(chunk);
    return () => { live = false; input.close(); route.filter = undefined; controller.current = undefined; };
  }, [stdin, isRawModeSupported, stdout]);
  useLayoutEffect(() => { controller.current?.head(id); }, [id, stdin, isRawModeSupported, stdout]);
  return !!id && readyId === id;
}
