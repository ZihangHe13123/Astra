/**
 * Display masking for credentials pasted into sent user messages. Only values with a published,
 * recognisable format are found: provider key and token prefixes, JSON Web Tokens, private key
 * blocks, and the password of an address that carries one. Free-form passwords and `NAME=value`
 * assignments have no such format and stay as written: a mask on ordinary text costs more than a
 * missed value. Masking changes what is drawn only; callers keep the original text for copying,
 * history, and the model.
 *
 * The scan is linear. Every offset is visited once; each format check is a bounded comparison or a
 * character-class run, and a run's end (likewise the next digit, the next long alphanumeric stretch
 * and the next key-block `END` line) is remembered, so later candidates inside it reuse the answer.
 */

/** What a found value is; a range that overlaps another joins the earlier one and keeps its kind. */
export type SecretKind = "token" | "jwt" | "private-key" | "url-password";

/** One credential in the scanned text, as UTF-16 offsets. */
export interface SecretSpan {
  readonly start: number;
  readonly end: number;
  readonly kind: SecretKind;
  /** The hidden part, inside `[start, end)`. Line breaks inside it stay as written. */
  readonly maskStart: number;
  readonly maskEnd: number;
}

/** Drawn in place of each hidden character (one per code point). */
export const SECRET_MASK = "•";
/** Leading characters of a key or token left readable: the provider prefix and little else. */
export const SECRET_VISIBLE_PREFIX = 8;

// Character classes of one ASCII code unit, as bits.
const KEY = 1; // [A-Za-z0-9_-]
const ALNUM = 2; // [A-Za-z0-9]
const WORD = 4; // \w
const ALNUM_DASH = 8; // [A-Za-z0-9-]
const UPPER_ALNUM = 16; // [A-Z0-9]
const LABEL = 32; // [A-Z0-9 ]
const SCHEME = 64; // [a-z0-9+.-]
const LOWER = 128; // [a-z]

const CLASSES = new Uint8Array(128);
for (let code = 0; code < 128; code++) {
  const upper = code >= 65 && code <= 90, lower = code >= 97 && code <= 122, digit = code >= 48 && code <= 57;
  const alnum = upper || lower || digit;
  CLASSES[code] = (alnum || code === 95 || code === 45 ? KEY : 0) | (alnum ? ALNUM : 0) | (alnum || code === 95 ? WORD : 0)
    | (alnum || code === 45 ? ALNUM_DASH : 0) | (upper || digit ? UPPER_ALNUM : 0) | (upper || digit || code === 32 ? LABEL : 0)
    | (lower || digit || code === 43 || code === 46 || code === 45 ? SCHEME : 0) | (lower ? LOWER : 0);
}
/** `charCodeAt` past the end is NaN, which belongs to no class. */
const inClass = (code: number, bit: number): boolean => code < 128 && (CLASSES[code] & bit) !== 0;

// How the run after a prefix is checked.
/** The whole run, at least `min`. */
const RUN = 0;
/** `min` to `max`, and no word character after it. */
const BOUNDED = 1;
/** At least `min`, a digit among the first 256, and 16 alphanumerics in a row somewhere in it. */
const SK = 2;
/** Exactly `min`, from a run at least that long. */
const EXACT = 3;
/** A JSON Web Token: three runs joined by `.eyJ` and `.`. */
const JWT = 4;

interface Format {
  readonly literal: string;
  readonly shape: number;
  readonly run: number;
  readonly min: number;
  readonly max: number;
  readonly kind: SecretKind;
}

const prefixed = (literal: string, shape: number, run: number, min: number, max = 0): Format =>
  ({ literal, shape, run, min, max, kind: shape === JWT ? "jwt" : "token" });

/** No literal begins another at the same offset, and each match is longer than the readable prefix. */
const FORMATS: readonly Format[] = [
  // Model services. Hyphenated names rarely carry a digit and a 16-character random stretch.
  prefixed("sk-", SK, KEY, 20),
  prefixed("AIza", EXACT, KEY, 35),
  // Code hosting and package registries.
  ...["ghp_", "gho_", "ghu_", "ghs_", "ghr_"].map(literal => prefixed(literal, RUN, ALNUM, 36)),
  prefixed("github_pat_", RUN, WORD, 22),
  prefixed("glpat-", RUN, KEY, 20),
  prefixed("npm_", RUN, ALNUM, 36),
  prefixed("hf_", RUN, ALNUM, 30),
  // Cloud access key IDs.
  prefixed("AKIA", BOUNDED, UPPER_ALNUM, 16, 16),
  prefixed("ASIA", BOUNDED, UPPER_ALNUM, 16, 16),
  prefixed("LTAI", BOUNDED, ALNUM, 12, 20),
  prefixed("AKID", BOUNDED, ALNUM, 28, 36),
  // Collaboration and payment services.
  ...["xoxa-", "xoxb-", "xoxp-", "xoxr-", "xoxs-"].map(literal => prefixed(literal, RUN, ALNUM_DASH, 10)),
  ...["sk_live_", "sk_test_", "rk_live_", "rk_test_"].map(literal => prefixed(literal, RUN, ALNUM, 16)),
  prefixed("whsec_", RUN, ALNUM, 24),
  prefixed("eyJ", JWT, KEY, 8, 2048),
];
/** Formats by the code of their first character. */
const BY_FIRST: (readonly Format[] | undefined)[] = [];
for (const format of FORMATS) {
  const code = format.literal.charCodeAt(0);
  BY_FIRST[code] = [...BY_FIRST[code] ?? [], format];
}
const KEY_BLOCK_BEGIN = "-----BEGIN ";
const KEY_BLOCK_END = "-----END ";
const PRIVATE_KEY = "PRIVATE KEY-----";
/** Longest alphanumeric stretch an `sk-` key must contain somewhere. */
const SK_STRETCH = 16;

interface OpenSpan { start: number; end: number; kind: SecretKind; maskStart: number; maskEnd: number }

/**
 * Remembers where a scan that started at `from` stopped (`to`). The answer holds for every start in
 * `[from, to]`: a run's end, the next digit and the next stretch are the same from anywhere before them.
 */
class Memo {
  private readonly from: Int32Array;
  private readonly to: Int32Array;
  private next = 0;
  constructor(private readonly measure: (start: number) => number, slots = 1) {
    this.from = new Int32Array(slots).fill(-1);
    this.to = new Int32Array(slots).fill(-1);
  }
  at(start: number): number {
    for (let slot = 0; slot < this.from.length; slot++) {
      if (this.from[slot] <= start && start <= this.to[slot]) return this.to[slot];
    }
    const slot = this.next;
    this.next = (slot + 1) % this.from.length;
    this.from[slot] = start;
    this.to[slot] = this.measure(start);
    return this.to[slot];
  }
}

class SecretScanner {
  private readonly spans: SecretSpan[] = [];
  private open: OpenSpan | undefined;
  /** An address password found at its `://`, drawn once the scan reaches its start. */
  private pending: { start: number; end: number } | undefined;
  private readonly runs = new Map<number, Memo>();
  private readonly user: Memo;
  private readonly password: Memo;
  private readonly digit: Memo;
  private readonly stretch: Memo;
  private blockFrom = -1;
  private blockEndLine = -1;
  private blockEnd = -1;

  constructor(private readonly text: string) {
    // A JSON Web Token measures three runs; four slots keep an earlier candidate's run while it does.
    for (const bit of [KEY, ALNUM, WORD, ALNUM_DASH, UPPER_ALNUM]) this.runs.set(bit, new Memo(start => this.classRun(bit, start), bit === KEY ? 4 : 1));
    this.user = new Memo(start => this.sticky(/[^\s/:@?#]*/y, start));
    this.password = new Memo(start => this.sticky(/[^\s/@?#]*/y, start));
    this.digit = new Memo(start => this.sticky(/\D*/y, start));
    this.stretch = new Memo(start => this.stretchStart(start));
  }

  scan(): SecretSpan[] {
    const text = this.text;
    for (let index = 0; index < text.length; index++) {
      const pending = this.pending;
      if (pending && pending.start === index) {
        this.pending = undefined;
        this.add(pending.start, pending.end, "url-password", pending.start, pending.end);
      }
      const code = text.charCodeAt(index);
      if (code === 58) {
        if (text.startsWith("://", index)) this.address(index);
      } else if (code === 45) {
        if (text.startsWith(KEY_BLOCK_BEGIN, index)) this.keyBlock(index);
      } else {
        for (const format of BY_FIRST[code] ?? []) {
          if (!text.startsWith(format.literal, index)) continue;
          const end = this.prefixedEnd(format, index);
          if (end >= 0) this.add(index, end, format.kind, index + SECRET_VISIBLE_PREFIX, end);
          break;
        }
      }
    }
    if (this.open) this.spans.push(this.open);
    return this.spans;
  }

  /** A candidate that starts inside the open range joins it, so nothing is drawn half masked. */
  private add(start: number, end: number, kind: SecretKind, maskStart: number, maskEnd: number): void {
    const open = this.open;
    if (open && start < open.end) {
      if (end > open.end) open.end = open.maskEnd = end;
      return;
    }
    if (open) this.spans.push(open);
    this.open = { start, end, kind, maskStart, maskEnd };
  }

  private sticky(pattern: RegExp, start: number): number {
    pattern.lastIndex = start;
    pattern.test(this.text);
    return pattern.lastIndex;
  }

  private classRun(bit: number, start: number): number {
    let index = start;
    while (inClass(this.text.charCodeAt(index), bit)) index++;
    return index;
  }

  /** Start of the first 16 alphanumerics in a row at or after `start`, or the text length. */
  private stretchStart(start: number): number {
    let count = 0;
    for (let index = start; index < this.text.length; index++) {
      count = inClass(this.text.charCodeAt(index), ALNUM) ? count + 1 : 0;
      if (count === SK_STRETCH) return index + 1 - SK_STRETCH;
    }
    return this.text.length;
  }

  private isWordAt(index: number): boolean {
    return inClass(this.text.charCodeAt(index), WORD);
  }

  /** End of the key or token whose literal starts at `at`, or -1. */
  private prefixedEnd(format: Format, at: number): number {
    if (this.isWordAt(at - 1)) return -1;
    const start = at + format.literal.length;
    const run = this.runs.get(format.run)!;
    if (format.shape === SK) {
      // The remembered next digit rejects a digit-free `sk-` before any run is measured.
      const digit = this.digit.at(start);
      if (digit - start > 255) return -1;
      const end = run.at(start);
      return end - start >= format.min && digit < end && this.stretch.at(start) + SK_STRETCH <= end ? end : -1;
    }
    const end = run.at(start);
    const length = end - start;
    if (length < format.min) return -1;
    if (format.shape === RUN) return end;
    if (format.shape === EXACT) return start + format.min;
    if (format.shape === BOUNDED) return length <= format.max && !this.isWordAt(end) ? end : -1;
    // JWT: header run of 8 to 2048 after `eyJ`, then `.eyJ` and a payload of 8 or more, `.` and a signature of 8 or more.
    if (length > format.max || !this.text.startsWith(".eyJ", end)) return -1;
    const payloadStart = end + 4;
    const payloadEnd = run.at(payloadStart);
    if (payloadEnd - payloadStart < format.min || this.text.charCodeAt(payloadEnd) !== 46) return -1;
    const signatureEnd = run.at(payloadEnd + 1);
    return signatureEnd - (payloadEnd + 1) >= format.min ? signatureEnd : -1;
  }

  /** Offset after `[A-Z0-9 ]{0,40}PRIVATE KEY-----` at `start`, or -1; reads at most 52 characters. */
  private labelEnd(start: number): number {
    const text = this.text;
    const limit = Math.min(text.length, start + 52);
    let index = start;
    while (index < limit && inClass(text.charCodeAt(index), LABEL)) index++;
    // "PRIVATE KEY" is label characters and the `-` after it is not, so the run ends right after it.
    const tail = index - "PRIVATE KEY".length;
    return index < start + 52 && tail >= start && text.startsWith(PRIVATE_KEY, tail) ? tail + PRIVATE_KEY.length : -1;
  }

  /**
   * A private key block from its `BEGIN` line to the first complete `END` line, or to the end of the
   * text. The two header lines stay readable; everything between them is hidden, wherever the line
   * breaks are (a key quoted in JSON has none).
   */
  private keyBlock(at: number): void {
    const header = this.labelEnd(at + KEY_BLOCK_BEGIN.length);
    if (header < 0) return;
    if (!(this.blockFrom <= header && header <= this.blockEndLine)) {
      const text = this.text;
      this.blockFrom = header;
      this.blockEndLine = this.blockEnd = text.length;
      for (let line = text.indexOf(KEY_BLOCK_END, header); line >= 0; line = text.indexOf(KEY_BLOCK_END, line + 1)) {
        const end = this.labelEnd(line + KEY_BLOCK_END.length);
        if (end < 0) continue;
        this.blockEndLine = line;
        this.blockEnd = end;
        break;
      }
    }
    this.add(at, this.blockEnd, "private-key", header, this.blockEndLine);
  }

  /** Whether `\b[a-z][a-z0-9+.-]{1,15}` ends at `at`; reads at most 17 characters back. */
  private schemeBefore(at: number): boolean {
    for (let start = at - 1; start >= 0 && start >= at - 16; start--) {
      const code = this.text.charCodeAt(start);
      if (!inClass(code, SCHEME)) return false;
      if (inClass(code, LOWER) && at - start >= 2 && !this.isWordAt(start - 1)) return true;
    }
    return false;
  }

  /** The password in `scheme://user:password@` for the `://` at `at`; neither part crosses `/ ? #` or a space. */
  private address(at: number): void {
    if (!this.schemeBefore(at)) return;
    const text = this.text;
    const userStart = at + 3;
    const userEnd = this.user.at(userStart);
    if (userEnd - userStart > 256 || text.charCodeAt(userEnd) !== 58) return;
    const start = userEnd + 1;
    const end = this.password.at(start);
    if (end > start && text.charCodeAt(end) === 64) this.pending = { start, end };
  }
}

/**
 * Every recognised credential in `text`, in order and without overlaps.
 * @param text - the original message text.
 * @returns the ranges; empty when nothing has a recognisable format.
 */
export function findSecrets(text: string): SecretSpan[] {
  return new SecretScanner(text).scan();
}

/** `text[start, end)` with every character except line breaks drawn as one mask; a surrogate pair is one character. */
function masks(text: string, start: number, end: number): string {
  let masked = "";
  let count = 0;
  for (let index = start; index < end; index++) {
    const code = text.charCodeAt(index);
    if (code === 10 || code === 13) {
      masked += SECRET_MASK.repeat(count) + text[index];
      count = 0;
      continue;
    }
    if (code >= 0xd800 && code <= 0xdbff && index + 1 < end) {
      const next = text.charCodeAt(index + 1);
      if (next >= 0xdc00 && next <= 0xdfff) index++;
    }
    count++;
  }
  return masked + SECRET_MASK.repeat(count);
}

/**
 * The display form of `text[from, to)`, a piece of `span`: what the span leaves readable stays, the
 * rest is masked. A renderer that splits one credential across several nodes draws each piece with this.
 */
export function maskSecretSlice(text: string, span: SecretSpan, from: number, to: number): string {
  const maskFrom = Math.min(to, Math.max(from, span.maskStart));
  const maskTo = Math.min(to, Math.max(maskFrom, span.maskEnd));
  return text.slice(from, maskFrom) + masks(text, maskFrom, maskTo) + text.slice(maskTo, to);
}

/**
 * `text` with every recognised credential masked; the same length in code points, line breaks kept.
 * @param text - the original text, which callers keep for copying and sending.
 * @param spans - the spans of `text`, when already found.
 */
export function maskSecrets(text: string, spans: readonly SecretSpan[] = findSecrets(text)): string {
  if (!spans.length) return text;
  let masked = "";
  let cursor = 0;
  for (const span of spans) {
    masked += text.slice(cursor, span.start) + maskSecretSlice(text, span, span.start, span.end);
    cursor = span.end;
  }
  return masked + text.slice(cursor);
}
