import assert from "node:assert/strict";
import test from "node:test";
import { ForegroundTerminalInput } from "./foreground-approval-input.js";

const C = "\x1b[", press = C + "121u", release = C + "121;1:3u";
function setup() {
  const writes: string[] = [], grants: string[] = [], ready: string[] = [];
  const keys = new ForegroundTerminalInput(s => writes.push(s), (id, _input, _key, answer) => {
    if (answer) { grants.push(`${id}:${answer.decision}`); return true; }
  }, id => { if (id) ready.push(id); });
  const negotiate = (alreadyEnabled = false) => keys.feed(`${C}?1004;${alreadyEnabled ? 1 : 2}$y${C}?0u${C}?11u`);
  return { keys, writes, grants, ready, negotiate };
}

test("pop drains old release frames, preserves ordinary input after its reply, and does not unset existing focus reporting", () => {
  const { keys, writes, grants, negotiate } = setup(); keys.head("old"); negotiate(true);
  keys.feed(press); keys.head();
  assert.equal(keys.feed("composer still works"), "composer still works");
  assert.equal(keys.feed("\x03"), "\x03");
  assert.equal(keys.feed(C + "121;"), ""); assert.equal(keys.feed("1:3ustill works"), "still works");
  assert.equal(keys.feed(release + C + "?0u" + "normal input"), "normal input");
  assert.deepEqual(grants, []);
  assert.ok(!writes.join("").includes("?1004l") && !writes.join("").includes("?1004h"));
});

test("bracketed paste spans a split marker and FIFO turnover without creating grants or becoming the next approval's input", () => {
  const { keys, grants, negotiate } = setup(); keys.head("old"); negotiate();
  keys.feed(C + "20"); keys.feed("0~" + press); keys.head("new");
  keys.feed(release + press + release); keys.feed(C + "20"); keys.feed("1~");
  keys.feed(C + "?0u"); negotiate(); keys.feed(release);
  assert.deepEqual(grants, []);
  keys.feed(press + release); assert.deepEqual(grants, ["new:once"]);

  // A legacy terminal has no keyboard-mode pop barrier. It still must not deliver part of an
  // already-started paste to the legacy Ink handler for a newly arrived non-foreground approval.
  const legacy = setup().keys; legacy.head("legacy"); legacy.feed(C + "200~"); legacy.head();
  assert.equal(legacy.feed("y"), ""); assert.equal(legacy.feed(C + "201~" + "n"), "n");
});

test("shutdown restores only modes actually changed, without asking the departing shell for a reply", () => {
  const { keys, writes, negotiate } = setup(); keys.head("id"); negotiate(); keys.close();
  assert.equal(writes.at(-1), C + "?1004l" + C + "<u");
});

test("control-string payloads, malformed CSI and modified/mismatched releases cannot manufacture consent", () => {
  for (const open of ["\x1bP", "\x1b_", "\x1b^", "\x90", "\x9f"]) {
    const { keys, grants, negotiate } = setup(); keys.head("reader"); negotiate();
    keys.feed(open); keys.feed("\x07" + press + release); keys.feed("\x1b\\");
    keys.feed(C + "1".repeat(80) + "u" + release + press + C + "121;2:3u");
    assert.deepEqual(grants, [], open);
    keys.feed(press + release); assert.deepEqual(grants, ["reader:once"]);
  }
});
