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

test("an arrow started on a foreground card cannot become a bare A on its ordinary successor", () => {
  const { keys, negotiate } = setup(); keys.head("old"); negotiate();
  assert.equal(keys.feed(C + "1;1"), ""); keys.head();
  assert.equal(keys.feed("A"), "");
  assert.equal(keys.feed("A"), "A"); // the next independent key still belongs to Ink
});

test("ordinary SS3 keys stay intact for Ink, regardless of read boundaries after a probe", () => {
  for (const pieces of [["\x1bOA"], ["\x1b", "OA"], ["\x1b", "O", "A"], ["\x1bO", "A"]]) {
    const { keys } = setup(); keys.head("unanswered"); keys.head();
    assert.deepEqual(pieces.map(s => keys.feed(s)).filter(Boolean), ["\x1bOA"]);
  }
});

test("an unfinished legacy key belongs to the receiver where its Escape was read", () => {
  const a = {}, b = {};
  for (const first of ["foreground", undefined]) {
    for (const key of ["y", "a", "t", "l", "n", "\r", "\x03", "[A", "OA"]) {
      const { keys } = setup();
      keys.head(first, a, true);
      assert.equal(keys.feed("\x1b"), "");
      keys.head(undefined, b, true);
      assert.equal(keys.feed(key), "", "no suffix can answer the new receiver");
      assert.equal(keys.feed("y"), "y", "independent input following the stale key is still usable");
    }
  }
});

test("normal Alt and cursor keys are framed identically at every read split, even without probe debt", () => {
  for (const key of ["\x1by", "\x1ba", "\x1bn", "\x1b[A", "\x1bOA", "\x1b[6~"]) {
    for (let split = 1; split < key.length; split++) {
      const { keys } = setup();
      assert.deepEqual([keys.feed(key.slice(0, split)), keys.feed(key.slice(split))].filter(Boolean), [key]);
    }
  }
});

test("bracketed paste and control-string payloads are not keyboard choices on an ordinary approval", () => {
  for (const [start, end] of [[C + '200~', C + '201~'], ['\x1b]', '\x07'], ['\x1bP', '\x1b\\']]) {
    const { keys } = setup(); keys.head(undefined, {}, true);
    keys.feed(start); assert.equal(keys.feed('y'), '');
    keys.head(undefined, {}, true); assert.equal(keys.feed('a'), ''); keys.feed(end);
    assert.equal(keys.feed('n'), 'n');
  }
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

test("Kitty lock bits are status, not held keys, including navigation and emergency controls", () => {
  for (const locks of [64, 128, 192]) {
    const actions: string[] = [], keys = new ForegroundTerminalInput(() => {}, (_id, input, key, answer) => {
      actions.push(answer?.decision ?? (key.ctrl ? `ctrl-${input}` : input || Object.keys(key)[0]!));
    }, () => {});
    const k = (code: number, mask = 0, event = 1) => `${C}${code};${1 + (locks | mask)}:${event}u`;
    keys.head("req"); keys.feed(`${C}?1004;2$y${C}?0u${C}?11u`);
    for (const mod of [1, 2, 4, 8, 16, 32, 256]) keys.feed(k(13, mod) + k(13, mod, 3));
    keys.feed(k(121) + k(121, 1, 3) + k(121, 0, 3)); // Shift on release still cancels.
    keys.feed(k(99, 4 | 1)); // Ctrl+Shift+C is not Ctrl+C.
    assert.deepEqual(actions, []);
    keys.feed(k(121) + k(121, 0, 3) + k(110) + k(118) + k(27) + k(99, 4) + k(121, 4));
    keys.feed(`${C}1;${1 + locks}A${C}6;${1 + locks}:1~${C}6;${1 + locks}:3~`);
    assert.deepEqual(actions, ["once", "n", "v", "escape", "ctrl-c", "ctrl-y", "upArrow", "pageDown"]);
  }
});

test("a focus-out is binding only while focus reporting is continuous", () => {
  for (const source of ["temporary", "already-on", "blur-before-confirm"]) {
    const { keys, grants, negotiate } = setup();
    keys.head("one"); negotiate(source === "already-on"); keys.feed(press + C + "O"); keys.head();
    keys.feed(C + "?0u"); keys.head("two");
    keys.feed(`${C}?1004;${source === "already-on" ? 1 : 2}$y${C}?0u`);
    if (source === "blur-before-confirm") keys.feed(C + "O");
    keys.feed(C + "?11u" + release); assert.deepEqual(grants, []);
    keys.feed(press + release);
    assert.deepEqual(grants, source === "temporary" ? ["two:once"] : [], source);
    if (source !== "temporary") {
      keys.feed(C + "I" + release); assert.deepEqual(grants, []);
      keys.feed(press + release); assert.deepEqual(grants, ["two:once"]);
    }
  }
});
