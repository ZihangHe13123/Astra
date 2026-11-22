import { useLayoutEffect, useRef, useState } from "react";
import { useStdin, useStdout, type Key } from "ink";
import type { ForegroundRelease } from "./approval-interaction.js";

const ESC = "\x1b", CSI = ESC + "[", pasteEnd = CSI + "201~";
const legacyEscapeWaitMS = 25;
// Kitty keyboard protocol: disambiguate + report event types + encode even ordinary letters/Enter.
const flags = 1 | 2 | 8;
// Kitty encodes 1 + a bitmask. Ignore only the Caps/Num Lock indicators; an unknown
// bit or a physically held modifier must not turn a chord into a plain approval key.
function heldModifiers(wire?: string): number { return (Number(wire || 1) - 1) & ~(64 | 128); }
type Handler = (id: string, input: string, key: Partial<Key>, released?: ForegroundRelease) => boolean | void;

type InputOwner = object | string;
type Receiver = { id?: string; view: InputOwner; approval: boolean };
type Route = { parser?: ForegroundTerminalInput; cancelEscape(): void };
const routes = new WeakMap<NodeJS.ReadStream, Route>();

/** Ink 5 can throw while *parsing* CSI-u, even for an inactive composer. Keep its normal raw-mode
 * and readable ownership, filtering the same TTY at the read boundary, before key normalization. */
export function approvalAwareStdin(source: NodeJS.ReadStream): NodeJS.ReadStream {
  // The timer only wakes Ink. The parser owns the bytes and must *consume* a pending
  // Escape for its original receiver before anything can be handed to Ink.
  let wait: ReturnType<typeof setTimeout> | undefined, escapeDue = false;
  const route: Route = { cancelEscape() { clearTimeout(wait); wait = undefined; escapeDue = false; } };
  const read = (size?: number) => {
    for (;;) {
      if (escapeDue) {
        route.cancelEscape();
        const key = route.parser?.finishEscapeWait();
        if (key) return key;
      }
      const data = source.read(size), parser = route.parser;
      if (data === null || !parser) return data;
      route.cancelEscape();
      const normal = parser.feed(String(data));
      if (parser.awaitingEscapeTimeout) {
        wait = setTimeout(() => { escapeDue = true; source.emit("readable"); }, legacyEscapeWaitMS);
        wait.unref();
      }
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
  private receiver: Receiver = { view: "composer", approval: false };
  private get id() { return this.receiver.id; }
  private ready = false;
  private done = false;
  private armed?: number;
  private blurred = false;
  private enabledFocus = false;
  private focusWasReset = false;
  private stage: "idle" | "probe" | "confirm" = "idle";
  private pushed = false;
  private protocolFrame = false;
  // Replies are ordered but have no request IDs. Ignore replies to older heads, even across an idle interval.
  private focusQueries = 0;
  private flagQueries = 0;
  private sequence = "";
  private sequenceOwner?: Receiver;
  private escapeExpired = false;
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

  close() { this.restore(false); this.receiver = { view: "closed", approval: false }; this.armed = undefined; }

  get awaitingEscapeTimeout(): boolean {
    return this.sequence === ESC && !this.escapeExpired;
  }

  finishEscapeWait(): string {
    if (!this.awaitingEscapeTimeout) return "";
    // Never dispatch a timeout to a foreground or replacement card. Keep the origin
    // so a late Alt-letter/CSI cannot be reinterpreted as its successor's shortcut.
    this.escapeExpired = true;
    if (this.id || this.sequenceOwner !== this.receiver) return "";
    this.sequence = ""; this.sequenceOwner = undefined;
    return ESC;
  }

  head(id?: string, view: InputOwner = "composer", approval = !!id) {
    const previous = this.receiver;
    if (id === previous.id && view === previous.view && approval === previous.approval) return;
    if (previous.id !== id) this.restore(true);
    // An old prefix retains this exact epoch until its suffix is drained. A→B→A does
    // not make an old key current, and changing ordinary cards never reopens terminal modes.
    this.receiver = { id, view, approval }; this.armed = undefined;
    if (this.paste) this.quarantinePaste = true;
    if (this.controlString) this.quarantineString = true;
    if (previous.id === id) return;
    this.ready = this.done = this.enabledFocus = false; this.stage = "idle";
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

  private frame(csi: string, origin?: Receiver) {
    // Malformed CSI-u is not a key and must never be handed to Ink 5's keypress parser either.
    this.protocolFrame = csi.endsWith("u");
    if (csi === "200~") { this.armed = undefined; this.paste = true; this.quarantinePaste = this.receiver.approval || origin !== this.receiver; return; }
    if (csi === "I" || csi === "O") { this.protocolFrame = true; this.armed = undefined; this.blurred = csi === "O"; return; }
    const focus = /^\?1004;([0-4])\$y$/.exec(csi);
    if (focus) {
      this.protocolFrame = true;
      if (this.focusQueries && --this.focusQueries === 0 && this.id && origin === this.receiver && ["1", "2", "3"].includes(focus[1]!)) {
        this.focusWasReset = focus[1] === "2"; this.stage = "probe"; this.flagQueries++;
        this.write?.(CSI + "?u");
      }
      return;
    }
    const reported = /^\?(\d{1,3})u$/.exec(csi);
    if (reported) {
      this.protocolFrame = true;
      if (this.flagQueries && --this.flagQueries === 0) {
        if (this.id && origin === this.receiver && this.stage === "probe") {
          this.pushed = true; this.enabledFocus = this.focusWasReset; this.stage = "confirm"; this.flagQueries++;
          // A blur from before a gap in reporting is not evidence about this subscription.
          // Keep it if reporting was already on. In either case only a new complete gesture grants.
          if (this.enabledFocus) this.blurred = false;
          this.write?.((this.enabledFocus ? CSI + "?1004h" : "") + `${CSI}>${flags}u${CSI}?u`);
        } else if (this.id && origin === this.receiver && this.stage === "confirm") {
          this.stage = "idle"; this.ready = Number(reported[1]) === flags;
          if (this.ready) this.reportReady(this.id);
        }
      }
      return;
    }
    const key = /^(\d{1,6})(?:;(\d{0,3})(?::([123]))?)?u$/.exec(csi);
    if (key) this.protocolFrame = true;
    if (!this.id || origin !== this.receiver || this.done) { this.armed = undefined; return; }
    const code = Number(key?.[1]), mod = heldModifiers(key?.[2]), event = Number(key?.[3] || 1);
    const previous = this.armed; this.armed = undefined;
    if (key && this.ready && !this.blurred && mod === 0 && (code === 121 || code === 13 || code === 97)) {
      if (event === 1 && previous === undefined) this.armed = code;
      if (event === 3 && previous === code) this.send("", {}, code === 97 ? "session" : "once");
      return;
    }
    if (key && event === 1) {
      if (mod === 4 && (code === 99 || code === 121)) this.send(String.fromCharCode(code), { ctrl: true });
      if (mod === 0 && (code === 110 || code === 118)) this.send(String.fromCharCode(code), {});
      if (mod === 0 && code === 27) this.send("", { escape: true });
    }
    // Arrows/page keys retain their CSI final bytes under Kitty; act on press, not keyup.
    const nav = /^(1?|5|6)(?:;(\d{1,3})(?::1)?)?([AB~])$/.exec(csi);
    if (nav && heldModifiers(nav[2]) === 0) {
      const name = nav[3] === "~" ? ({ "5": "pageUp", "6": "pageDown" } as const)[nav[1] as "5" | "6"]
        : nav[3] === "A" ? "upArrow" : "downArrow";
      if (name) this.send("", { [name]: true });
    }
  }

  /** Ink's readable chunks are not key boundaries. Keep only a bounded control prefix, not pasted content. */
  feed(chunk: string): string {
    const receiver = this.receiver, owner = receiver.id;
    if (!owner && !this.sequence && !this.paste && !this.controlString && !/[\x1b\x90\x98\x9d-\x9f]/.test(chunk)) return chunk;
    let normal = "";
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
        this.armed = undefined; this.sequence = ""; this.quarantineString = receiver.approval;
        if (!owner && !this.quarantineString) normal += c; continue;
      }
      if (c === ESC) {
        if (this.sequence) this.armed = undefined;
        this.sequence = ESC; this.sequenceOwner = receiver; this.overflow = this.escapeExpired = false;
        continue;
      }
      if (this.sequence === ESC) {
        const same = this.sequenceOwner === receiver;
        if (this.escapeExpired && !same && !receiver.approval && receiver.view === "composer") {
          // An undeliverable Escape is no longer a control prefix for fresh composer
          // text. Do not submit an existing draft with a possibly stale Return/Ctrl key.
          this.sequence = ""; this.sequenceOwner = undefined; this.armed = undefined;
          if (c >= " " && c !== "\x7f") normal += c;
        } else if (c === "[" || c === "O" || c === "N") { this.sequence = ESC + c; }
        else {
          this.sequence = ""; this.armed = undefined; this.stringEscape = false;
          const string = c === "]" ? "osc" : "P_^X".includes(c) ? "st" : undefined;
          if (string) {
            this.controlString = string; this.quarantineString = receiver.approval || !same;
            if (!this.quarantineString) normal += ESC + c;
          } else if (same && !owner) normal += ESC + c; // preserve Alt for Ink; never emit a bare suffix
          else if (same && this.escapeExpired && chunk.length === 1) this.legacy(c); // independent N/V/Ctrl; never a grant
          this.sequenceOwner = undefined;
        }
        continue;
      }
      if (this.sequence) {
        if (c >= "@" && c <= "~") {
          const prefix = this.sequence.slice(0, 2), body = this.sequence.slice(2) + c; this.sequence = "";
          this.protocolFrame = false;
          if (!this.overflow && prefix === CSI) this.frame(body, this.sequenceOwner);
          else this.armed = undefined;
          // Only the starting receiver gets the complete key, never a bare suffix or payload.
          if (!owner && this.sequenceOwner === receiver && !this.protocolFrame && !this.quarantinePaste && !this.overflow) {
            normal += prefix + body;
          }
          this.sequenceOwner = undefined;
        } else if (c < " ") {
          this.sequence = ""; this.armed = undefined;
          if (this.sequenceOwner === receiver && (!receiver.approval || c === "\x03")) {
            if (!owner) normal += c; else this.legacy(c);
          }
          this.sequenceOwner = undefined;
        } else if (!this.overflow && c <= "?" && this.sequence.length < 48) this.sequence += c;
        else { this.overflow = true; this.armed = undefined; }
        continue;
      }
      this.armed = undefined;
      if (!owner) normal += c;
      else if (chunk.length === 1 && receiver === this.receiver) this.legacy(c);
    }
    return receiver === this.receiver ? normal : "";
  }

  private legacy(c: string) {
    if (c === "\x03" || c === "\x19") this.send(c === "\x03" ? "c" : "y", { ctrl: true });
    if (/^[nv]$/i.test(c)) this.send(c.toLowerCase(), {});
  }
}

export function useForegroundApprovalInput(id: string | undefined, normalOwner: InputOwner, handle: Handler, approval = !!id): boolean {
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
    route.parser = input;
    return () => {
      live = false; route.cancelEscape(); input.close();
      route.parser = undefined; controller.current = undefined;
    };
  }, [stdin, isRawModeSupported, stdout]);
  useLayoutEffect(() => { controller.current?.head(id, normalOwner, approval); }, [id, normalOwner, approval, stdin, isRawModeSupported, stdout]);
  return !!id && readyId === id;
}
