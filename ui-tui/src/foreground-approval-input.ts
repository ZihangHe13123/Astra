import { useLayoutEffect, useRef, useState } from "react";
import { useStdin, useStdout, type Key } from "ink";
import type { ForegroundRelease } from "./approval-interaction.js";

const ESC = "\x1b", CSI = ESC + "[", pasteStart = CSI + "200~", pasteEnd = CSI + "201~";
const legacyEscapeWaitMS = 25;
// Kitty keyboard protocol: disambiguate + report event types + encode even ordinary letters/Enter.
const flags = 1 | 2 | 8;
// Kitty encodes 1 + a bitmask. Ignore only the Caps/Num Lock indicators; an unknown
// bit or a physically held modifier must not turn a chord into a plain approval key.
function heldModifiers(wire?: string): number { return (Number(wire || 1) - 1) & ~(64 | 128); }
type Handler = (id: string, input: string, key: Partial<Key>, released?: ForegroundRelease) => boolean | void;

type InputOwner = object | string;
type Receiver = { id?: string; view: InputOwner; approval: boolean };
type Route = { parser?: ForegroundTerminalInput; notifyBoundary?: () => void; cancelEscape(): void };
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
      const following = route.parser?.takeFollowingKey();
      if (following) return following;
      if (escapeDue) {
        route.cancelEscape();
        const key = route.parser?.finishEscapeWait();
        route.notifyBoundary?.();
        if (key) return key;
      }
      const data = source.read(size), parser = route.parser;
      if (data === null || !parser) return data;
      route.cancelEscape();
      const normal = parser.feed(String(data));
      route.notifyBoundary?.();
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
  // Delivery to Ink and framing of bytes still in flight are independent. A spent
  // prefix can drain a late key/paste marker, but can never mint another decision.
  private escape: "waiting" | "expired" | "delivered" | "metaEscape" = "waiting";
  private literalReceiver?: Receiver;
  private followingKey?: { receiver: Receiver; key: string };
  private overflow = false;
  private paste = false;
  private quarantinePaste = false;
  private pasteMatch = 0;
  private controlString?: "osc" | "st";
  private stringEscape = false;
  private oscHeader?: "empty" | "unrecognized";
  private oscEscapeOwner?: Receiver;
  // A ']' typed after a spent/retired prefix is not a live ESC ] frame.
  // Keep only its owner and selector syntax, never the payload or a digit buffer.
  private oscLiteral?: { receiver: Receiver; digits: boolean };
  private literalString?: { receiver: Receiver; kind: "st" | "osc"; drainPayload?: boolean };
  private literalEscape = false;
  private literalPasteMatch = 0;
  private literalEscapeOwner?: Receiver;

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
    return !!this.literalEscapeOwner || !!this.oscEscapeOwner || (this.sequence === ESC && this.escape === "waiting");
  }

  get needsStringBoundary(): boolean { return !!(this.literalString || this.oscLiteral); }

  private keepStringBoundary(receiver: Receiver, kind: "st" | "osc", drainPayload = false) {
    if (!this.literalString) {
      this.literalString = { receiver, kind }; this.literalEscape = false; this.literalPasteMatch = 0;
    } else if (kind === "st") {
      // A possible DCS always needs ST, even if OSC is another interpretation.
      this.literalString.kind = "st";
    }
    if (drainPayload) this.literalString.drainPayload = true;
  }

  finishEscapeWait(): string {
    if (!this.awaitingEscapeTimeout) return "";
    if (this.literalEscapeOwner) {
      const recipient = this.literalEscapeOwner; this.literalEscapeOwner = undefined;
      return recipient === this.receiver && !this.id ? ESC : ""; // never end the string on a refusal/timeout
    }
    if (this.oscEscapeOwner) {
      // Not a timely string terminator. Only the standalone *negative* Escape
      // may be delivered, to the receiver that saw it; no payload becomes a key.
      this.sequence = ESC; this.sequenceOwner = this.oscEscapeOwner; this.oscEscapeOwner = undefined;
      this.controlString = this.oscHeader = this.oscLiteral = undefined; this.stringEscape = false;
    }
    // Never dispatch a timeout to a foreground or replacement card. Keep the origin
    // so a late Alt-letter/CSI cannot be reinterpreted as its successor's shortcut.
    if (this.id || this.sequenceOwner !== this.receiver) { this.escape = "expired"; return ""; }
    this.escape = "delivered";
    return ESC;
  }

  takeFollowingKey(): string {
    const next = this.followingKey; this.followingKey = undefined;
    return next?.receiver === this.receiver ? next.key : "";
  }

  head(id?: string, view: InputOwner = "composer", approval = !!id) {
    const previous = this.receiver;
    if (id === previous.id && view === previous.view && approval === previous.approval) return;
    if (previous.id !== id) this.restore(true);
    // An old prefix retains this exact epoch until its suffix is drained. A→B→A does
    // not make an old key current, and changing ordinary cards never reopens terminal modes.
    this.receiver = { id, view, approval }; this.armed = undefined;
    if (this.paste) this.quarantinePaste = true;

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
    if (!owner && !this.sequence && !this.paste && !this.controlString && !this.literalString && !this.oscLiteral && !/[\x1b\x90\x98\x9d-\x9f]/.test(chunk)) return chunk;
    let normal = "", remaining = chunk.length, aborted = false;
    for (const c of chunk) {
      remaining -= c.length;
      if (this.paste) {
        if (!owner && !this.quarantinePaste) normal += c;
        this.pasteMatch = c === pasteEnd[this.pasteMatch] ? this.pasteMatch + 1 : c === ESC ? 1 : 0;
        if (this.pasteMatch === pasteEnd.length) { this.paste = this.quarantinePaste = false; this.pasteMatch = 0; }
        continue;
      }
      // ESC ] with no selector/payload is also the legacy encoding of Alt+]. A
      // separate negative key is usable in that state. Never interpret payload
      // from a started OSC (or paste) as a shortcut, even after a view change.
      if (this.oscLiteral) {
        const prefix = this.oscLiteral;
        if (c >= "0" && c <= "9") prefix.digits = true;
        else this.oscLiteral = undefined;
        // Default selector, number without a separator, or title text: two
        // interpretations. Allow typing only on this composer. An explicit
        // numeric selector plus separator, on the other hand, has a payload
        // to drain even on the originating composer.
        if (prefix.digits || prefix.receiver !== receiver || /[;lL\x07\x9c\x18\x1a]/.test(c)) {
          this.keepStringBoundary(prefix.receiver, "osc", prefix.digits && c === ";");
        }
      }
      if (this.oscHeader && !this.stringEscape && chunk.length === 1 && receiver.approval) {
        if (c === ESC) { this.oscEscapeOwner = receiver; this.stringEscape = true; continue; }
        if (c === "\x03" || /^[nN]$/.test(c)) {
          this.controlString = this.oscHeader = undefined;
        }
      }
      if (this.literalString) {
        // The same bytes can be the word “Python” following Escape, or DCS with
        // a private function and opaque data. Preserve the composer's input, but
        // CR/LF can occur in that data: submitting text never grants a new card.
        this.literalEscapeOwner = undefined;
        const cancel = c === "\x18" || c === "\x1a"; // VT CAN/SUB; paste has priority above
        if (cancel || (this.literalString.kind === "osc" && c === "\x07") || c === "\x9c" || (this.literalEscape && c === "\\")) {
          this.controlString = this.oscHeader = this.oscLiteral = this.literalString = undefined;
          this.sequence = ""; this.sequenceOwner = this.literalReceiver = this.oscEscapeOwner = this.armed = undefined;
          this.literalPasteMatch = 0; this.stringEscape = this.literalEscape = false;
          if (cancel) { aborted = true; this.armed = undefined; }
          continue;
        }
        const previousEsc = this.literalEscape; this.literalEscape = c === ESC;
        // If the title prefix was just literal text, a subsequent raw DCS/APC/PM/SOS
        // may have started a real ST-only string. BEL alone cannot open the next card.
        if ((previousEsc && "P_^X".includes(c)) || "\x90\x98\x9e\x9f".includes(c)) this.literalString.kind = "st";
        // The opener can start on the composer and finish on the next card.
        this.literalPasteMatch = c === pasteStart[this.literalPasteMatch] ? this.literalPasteMatch + 1 : c === ESC ? 1 : 0;
        if (this.literalString.receiver !== receiver || this.literalString.drainPayload) {
          if (this.literalPasteMatch === pasteStart.length) {
            this.paste = this.quarantinePaste = true; this.pasteMatch = this.literalPasteMatch = 0;
          } else if (!this.literalString.drainPayload && chunk.length === 1) {
            if (c === ESC && !previousEsc) this.literalEscapeOwner = receiver;
            if (c === "\x03" || (receiver.approval && /^[nN]$/.test(c))) { if (owner) this.legacy(c); else normal += c; }
          }
          continue;
        }
        // Same composer: observe boundaries in parallel, but let Ink receive
        // its real typing, cursor keys and bracketed paste through the normal path.
        if (c === "\r" && normal && !remaining && !this.controlString && !this.sequence && !aborted) {
          // Ink expects one key per read. Only a still-current composer can
          // receive this Return, never the card that might open afterwards.
          this.followingKey = { receiver, key: c }; continue;
        }
      }
      if (this.controlString) {
        this.oscEscapeOwner = undefined;
        // XTerm OSC functions start with a numeric/default selector; l/L also
        // introduce title replies. Unknown data never provides an affirmative key.
        if (this.stringEscape || (this.oscHeader === "empty" && /[\d;lL]/.test(c))) this.oscHeader = undefined;
        else if (this.oscHeader) this.oscHeader = "unrecognized";
        if (this.controlString === "osc" && c === "\x07" || c === "\x9c" || this.stringEscape && c === "\\") {
          this.controlString = this.oscHeader = undefined;
        }
        this.stringEscape = c === ESC; continue;
      }
      if ("\x90\x98\x9d\x9e\x9f".includes(c)) {
        this.controlString = c === "\x9d" ? "osc" : "st"; this.stringEscape = false;
        this.armed = undefined; this.sequence = ""; continue;
      }
      if (c === ESC) {
        if (this.sequence) this.armed = undefined;
        if (this.sequence === ESC && this.escape === "waiting") {
          // Two timely ESC bytes encode Alt+Esc, not a denial. Its optional function
          // key suffix is still non-authoritative (e.g. Alt+Up = ESC ESC [ A).
          this.escape = "metaEscape"; continue;
        }
        if (this.literalReceiver === receiver && !this.overflow) normal += this.sequence.slice(1);
        this.literalReceiver = undefined;
        this.sequence = ESC; this.sequenceOwner = aborted ? undefined : receiver; this.overflow = false; this.escape = "waiting";
        continue;
      }
      if (this.sequence === ESC) {
        const same = this.sequenceOwner === receiver;
        const spent = this.escape === "delivered" || this.escape === "metaEscape" || (this.escape === "expired" && !same);
        const text = spent && !receiver.approval && receiver.view === "composer";
        if (this.escape === "delivered" && receiver.approval && chunk.length === 1 && (c === "\x03" || /^[nN]$/.test(c))) {
          // No actual CSI/paste/string has begun. The new card can refuse or
          // cancel immediately. Never take this path for an affirmative key.
          this.sequence = c === "N" ? ESC + "N" : ""; // also drain a possible late SS2 suffix
          this.sequenceOwner = this.armed = undefined;
          if (owner) this.legacy(c); else normal += c;
        } else if (c === "[" || c === "O" || c === "N") {
          this.sequence += c;
          this.literalReceiver = text ? receiver : undefined;
          if (spent) this.sequenceOwner = undefined; // parse structure, never dispatch a spent key
        }
        else {
          this.sequence = ""; this.armed = undefined; this.stringEscape = false;
          const string = c === "]" ? "osc" : "P_^X".includes(c) ? "st" : undefined;
          const retiredIntoComposer = !same && !receiver.approval && receiver.view === "composer";
          if (text || (string && retiredIntoComposer)) {
            if (string === "st") this.keepStringBoundary(receiver, string);
            if (string === "osc") this.oscLiteral = { receiver, digits: false };
            if ((same && this.escape === "delivered") || (c >= " " && c !== "\x7f")) normal += c;
          } else if (string) {
            // The introducer was framed, not an independent printable suffix.
            // Never stream a terminal/control reply into the composer or a card.
            this.controlString = string;
            this.oscHeader = string === "osc" ? "empty" : undefined;
          } else if (this.escape === "metaEscape" && (!receiver.approval || /^[nv]$/i.test(c) || c === "\x03")) {
            if (!owner) normal += c; else if (chunk.length === 1) this.legacy(c);
          } else if (same && !owner && !spent) normal += ESC + c; // preserve Alt for Ink
          else if (same && this.escape === "expired" && chunk.length === 1) this.legacy(c);
          this.sequenceOwner = undefined;
        }
        continue;
      }
      if (this.sequence) {
        const literal = this.literalReceiver === receiver;
        if (c >= "@" && c <= "~") {
          const prefix = this.sequence.slice(0, 2), body = this.sequence.slice(2) + c; this.sequence = "";
          this.protocolFrame = false;
          // The composer did not type an ESC here. Only positively recognized
          // paste framing or a reply we actually requested may consume its text.
          const knownLateFrame = body === "200~" || (this.focusQueries > 0 && /^\?1004;[0-4]\$y$/.test(body)) ||
            (this.flagQueries > 0 && /^\?\d{1,3}u$/.test(body));
          if (!this.overflow && prefix === CSI && (!literal || knownLateFrame)) this.frame(body, this.sequenceOwner);
          else this.armed = undefined;
          // Only the starting receiver gets the complete key, never a bare suffix or payload.
          if (!owner && this.sequenceOwner === receiver && !this.protocolFrame && !this.quarantinePaste && !this.overflow) {
            normal += prefix + body;
          } else if (literal && !this.protocolFrame && !this.paste) {
            normal += this.overflow ? c : prefix.slice(1) + body;
          }
          this.sequenceOwner = this.literalReceiver = undefined;
        } else if (c < " " || c === "\x7f") {
          if (literal) {
            if (!this.overflow) normal += this.sequence.slice(1);
            // Ink parses one read as one key. Finish the literal text first, then
            // let a separate Return act only if its composer still owns input.
            if (normal && !remaining) this.followingKey = { receiver, key: c }; else normal += c;
          } else if (this.sequenceOwner === receiver && (!receiver.approval || c === "\x03")) {
            if (!owner) normal += c; else this.legacy(c);
          }
          this.sequence = ""; this.armed = undefined; this.sequenceOwner = this.literalReceiver = undefined;
        } else if (literal && c > "~") {
          // Unicode is never a CSI parameter. Preserve it (and the bracket)
          // instead of dropping the rest of a word waiting for an ASCII final.
          normal += (this.overflow ? "" : this.sequence.slice(1)) + c;
          this.sequence = ""; this.sequenceOwner = this.literalReceiver = this.armed = undefined;
        } else if (!this.overflow && c <= "?" && this.sequence.length < 48) this.sequence += c;
        else if (literal) {
          normal += (this.overflow ? "" : this.sequence.slice(1)) + c; this.overflow = true;
        }
        else { this.overflow = true; this.armed = undefined; }
        continue;
      }
      this.armed = undefined;
      if (!owner) normal += c;
      else if (chunk.length === 1 && receiver === this.receiver) this.legacy(c);
    }
    return !aborted && receiver === this.receiver ? normal : "";
  }

  private legacy(c: string) {
    if (c === "\x03" || c === "\x19") this.send(c === "\x03" ? "c" : "y", { ctrl: true });
    if (/^[nv]$/i.test(c)) this.send(c.toLowerCase(), {});
  }
}

export function useForegroundApprovalInput(id: string | undefined, normalOwner: InputOwner, handle: Handler, approval = !!id,
  onBoundary?: (blocked: boolean) => void): boolean {
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
    let blocked = false;
    route.notifyBoundary = () => {
      if (blocked !== input.needsStringBoundary) { blocked = input.needsStringBoundary; onBoundary?.(blocked); }
    };
    return () => {
      live = false; route.cancelEscape(); input.close(); route.notifyBoundary = undefined;
      if (blocked) onBoundary?.(false);
      route.parser = undefined; controller.current = undefined;
    };
  }, [stdin, isRawModeSupported, stdout, onBoundary]);
  useLayoutEffect(() => { controller.current?.head(id, normalOwner, approval); }, [id, normalOwner, approval, stdin, isRawModeSupported, stdout]);
  return !!id && readyId === id;
}
