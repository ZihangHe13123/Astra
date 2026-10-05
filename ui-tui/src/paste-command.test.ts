import assert from "node:assert/strict";
import {
  LARGE_PASTE_CHAR_THRESHOLD,
  consumeBracketedPasteChunk,
  normalizePastedText,
  pastedTextPreview,
  resolvePastedTextSubmitText,
  singleLineFieldText,
  updatePastedTextInput,
} from "./paste-command.js";

const short = updatePastedTextInput("before ", "before short paste", []);
assert.deepEqual(short, { displayText: "before short paste", attachments: [] });

const longContent = "x".repeat(LARGE_PASTE_CHAR_THRESHOLD + 1);
const long = updatePastedTextInput("analyze: ", `analyze: ${longContent}`, []);
assert.match(long.displayText, /^analyze: \[Pasted text #1 · 1,001 chars\]$/);
assert.equal(long.attachments.length, 1);
assert.equal(resolvePastedTextSubmitText(long.displayText, long.attachments), `analyze: ${longContent}`);

const multilineContent = "one\r\ntwo\r\nthree\r\nfour\r\nfive\r\nsix\r\nseven\r\neight";
const multiline = updatePastedTextInput("", multilineContent, []);
assert.equal(multiline.displayText, "[Pasted text #1 · 39 chars · 8 lines]");
assert.equal(multiline.attachments[0]?.content, multilineContent.replace(/\r\n/g, "\n"));
assert.equal(resolvePastedTextSubmitText(multiline.displayText, multiline.attachments), multilineContent.replace(/\r\n/g, "\n"));

const middle = updatePastedTextInput("prefix suffix", `prefix ${longContent}suffix`, []);
assert.match(middle.displayText, /^prefix \[Pasted text #1 · 1,001 chars\]suffix$/);
assert.equal(resolvePastedTextSubmitText(middle.displayText, middle.attachments), `prefix ${longContent}suffix`);

const secondContent = "y".repeat(LARGE_PASTE_CHAR_THRESHOLD + 5);
const spaced = updatePastedTextInput(long.displayText, `${long.displayText} `, long.attachments);
const second = updatePastedTextInput(spaced.displayText, `${spaced.displayText}${secondContent}`, spaced.attachments);
assert.match(second.displayText, /\[Pasted text #2 · 1,005 chars\]$/);
assert.equal(second.attachments.length, 2);
assert.equal(
  resolvePastedTextSubmitText(second.displayText, second.attachments),
  `analyze: ${longContent} ${secondContent}`,
);

const damagedLabel = long.displayText.slice(0, -1);
const removed = updatePastedTextInput(long.displayText, damagedLabel, long.attachments);
assert.deepEqual(removed, { displayText: "analyze: ", attachments: [] });

const twoLines = updatePastedTextInput("", "first line\n  second line", []);
assert.equal(twoLines.displayText, "[Pasted text #1 · 24 chars · 2 lines]");
assert.equal(twoLines.attachments[0]?.content, "first line\n  second line");
assert.equal(resolvePastedTextSubmitText(twoLines.displayText, twoLines.attachments), "first line\n  second line");

const bracketed = "\u001b[200~alpha\r\n  beta\u001b[201~";
assert.equal(normalizePastedText(bracketed), "alpha\n  beta");
const bracketedUpdate = updatePastedTextInput("", bracketed, []);
assert.equal(bracketedUpdate.attachments[0]?.content, "alpha\n  beta");
assert.equal(pastedTextPreview("\n  'ls' is not recognized as a command\nnext"), "'ls' is not recognized as a command");

const originalLongPaste = "first paragraph\n" + "middle ".repeat(300) + "\nlast paragraph";
let buffered: string | null = null;
let completed = "";
for (const chunk of [
  `\u001b[200~${originalLongPaste.slice(0, 57)}`,
  originalLongPaste.slice(57, 801),
  originalLongPaste.slice(801, 1703),
  `${originalLongPaste.slice(1703)}\u001b[201~`,
]) {
  const result = consumeBracketedPasteChunk(buffered, chunk);
  assert.equal(result.handled, true);
  buffered = result.buffer;
  if (result.completed !== undefined) completed = result.completed;
}
assert.equal(buffered, null);
assert.equal(completed, originalLongPaste);

// Plain text fields clean their whole value: Ink drops a read's leading ESC and
// can hand over one marker in two reads.
assert.equal(singleLineFieldText("\u001b[200~secret-test-123\u001b[201~"), "secret-test-123");
assert.equal(singleLineFieldText("[200~secret-test-123\u001b[201~"), "secret-test-123");
let field = "";
for (const chunk of ["\u001b[20", "0~secret-", "test-123\r\n\u001b[2", "01~"]) field = singleLineFieldText(field + chunk);
assert.equal(field, "secret-test-123");
assert.equal(singleLineFieldText("first\r\n\tsecond\u0000 line\u007f", " "), "first second line");
assert.equal(singleLineFieldText("纯文本 👩‍💻 [20] ~ 200"), "纯文本 👩‍💻 [20] ~ 200");
