export const LARGE_PASTE_CHAR_THRESHOLD = 1000;
export const LARGE_PASTE_LINE_THRESHOLD = 2;
const BRACKETED_PASTE_MARKER = /(?:\u001b)?\[(?:200|201)~/g;
const BRACKETED_PASTE_START = /(?:\u001b)?\[200~/;
const BRACKETED_PASTE_END = /(?:\u001b)?\[201~/;

export type PastedTextAttachment = {
  label: string;
  content: string;
  charCount: number;
  lineCount: number;
};

type InputChange = {
  start: number;
  previousEnd: number;
  nextEnd: number;
  inserted: string;
};

export type PastedTextInputUpdate = {
  displayText: string;
  attachments: PastedTextAttachment[];
};

export type BracketedPasteChunk = {
  handled: boolean;
  buffer: string | null;
  completed?: string;
  trailing?: string;
};

export function consumeBracketedPasteChunk(buffer: string | null, chunk: string): BracketedPasteChunk {
  let pending = buffer;
  if (pending === null) {
    const start = BRACKETED_PASTE_START.exec(chunk);
    if (!start) return { handled: false, buffer: null };
    pending = chunk.slice((start.index ?? 0) + start[0].length);
  } else {
    pending += chunk;
  }

  const end = BRACKETED_PASTE_END.exec(pending);
  if (!end) return { handled: true, buffer: pending };
  const completed = pending.slice(0, end.index);
  const trailing = pending.slice(end.index + end[0].length);
  return {
    handled: true,
    buffer: null,
    completed: normalizePastedText(completed),
    trailing: normalizePastedText(trailing),
  };
}

function inputChange(previousValue: string, nextValue: string): InputChange {
  let start = 0;
  const sharedLength = Math.min(previousValue.length, nextValue.length);
  while (start < sharedLength && previousValue[start] === nextValue[start]) start += 1;

  let previousEnd = previousValue.length;
  let nextEnd = nextValue.length;
  while (
    previousEnd > start
    && nextEnd > start
    && previousValue[previousEnd - 1] === nextValue[nextEnd - 1]
  ) {
    previousEnd -= 1;
    nextEnd -= 1;
  }

  return {
    start,
    previousEnd,
    nextEnd,
    inserted: nextValue.slice(start, nextEnd),
  };
}

export function normalizePastedText(text: string): string {
  return text
    .replace(BRACKETED_PASTE_MARKER, "")
    .replace(/\r\n/g, "\n")
    .replace(/\r/g, "\n")
    .replace(/\u0000/g, "");
}

// Plain text fields read the terminal through ink-text-input or their own
// handler, with no paste buffering. Clean the whole value rather than the
// inserted piece: Ink can hand over one marker in two reads.
export function singleLineFieldText(value: string, lineBreak = ""): string {
  return normalizePastedText(value)
    .replace(/[\n\t]+/g, lineBreak)
    .replace(/[\u0000-\u001f\u007f]/g, "");
}

export function pastedTextPreview(text: string, max = 72): string {
  const first = normalizePastedText(text)
    .split("\n")
    .map((line) => line.trim().replace(/\s+/g, " "))
    .find(Boolean) ?? "empty paste";
  return first.length <= max ? first : `${first.slice(0, Math.max(1, max - 1))}…`;
}

function pasteMetrics(text: string): { charCount: number; lineCount: number } {
  return {
    charCount: Array.from(text).length,
    lineCount: text.length === 0 ? 0 : text.split("\n").length,
  };
}

function isLargePaste(charCount: number, lineCount: number): boolean {
  return charCount > LARGE_PASTE_CHAR_THRESHOLD || lineCount >= LARGE_PASTE_LINE_THRESHOLD;
}

function nextPasteNumber(attachments: PastedTextAttachment[]): number {
  const used = new Set(
    attachments
      .map((attachment) => attachment.label.match(/^\[Pasted text #(\d+)/)?.[1])
      .filter((value): value is string => Boolean(value))
      .map(Number),
  );
  let candidate = 1;
  while (used.has(candidate)) candidate += 1;
  return candidate;
}

function pasteLabel(index: number, charCount: number, lineCount: number): string {
  const size = `${charCount.toLocaleString("en-US")} chars`;
  const lines = lineCount > 1 ? ` · ${lineCount.toLocaleString("en-US")} lines` : "";
  return `[Pasted text #${index} · ${size}${lines}]`;
}

function changeTouchesLabel(change: InputChange, labelStart: number, labelEnd: number): boolean {
  if (change.previousEnd > change.start) {
    return change.start < labelEnd && change.previousEnd > labelStart;
  }
  return change.start > labelStart && change.start < labelEnd;
}

export function updatePastedTextInput(
  previousValue: string,
  nextValue: string,
  attachments: PastedTextAttachment[],
): PastedTextInputUpdate {
  nextValue = normalizePastedText(nextValue);
  const change = inputChange(previousValue, nextValue);
  const normalizedInsert = normalizePastedText(change.inserted);
  const metrics = pasteMetrics(normalizedInsert);

  if (change.inserted.length > 1 && isLargePaste(metrics.charCount, metrics.lineCount)) {
    const label = pasteLabel(nextPasteNumber(attachments), metrics.charCount, metrics.lineCount);
    return {
      displayText: nextValue.slice(0, change.start) + label + nextValue.slice(change.nextEnd),
      attachments: [...attachments, {
        label,
        content: normalizedInsert,
        charCount: metrics.charCount,
        lineCount: metrics.lineCount,
      }],
    };
  }

  for (const attachment of attachments) {
    const labelStart = previousValue.indexOf(attachment.label);
    if (labelStart < 0) continue;
    const labelEnd = labelStart + attachment.label.length;
    if (changeTouchesLabel(change, labelStart, labelEnd)) {
      return {
        displayText: previousValue.slice(0, labelStart) + previousValue.slice(labelEnd),
        attachments: attachments.filter((item) => item !== attachment),
      };
    }
  }

  return {
    displayText: nextValue,
    attachments: attachments.filter((attachment) => nextValue.includes(attachment.label)),
  };
}

export function resolvePastedTextSubmitText(
  displayText: string,
  attachments: PastedTextAttachment[],
): string {
  let result = displayText;
  for (const attachment of attachments) {
    result = result.split(attachment.label).join(attachment.content);
  }
  return result.trim();
}
