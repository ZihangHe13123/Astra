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

function typedAfterEscape() {
  const { keys } = setup(); keys.feed("\x1b"); assert.equal(keys.finishEscapeWait(), "\x1b");
  return keys;
}

test("spent composer Escape preserves independent code after ] and the successor's first safe controls", () => {
  for (const text of ["];", "]; next();", "]42", "]7 + 1"]) for (const parts of [[text], [...text]]) {
    for (const first of ["n", "\x03", "\x18y"]) {
      const keys = typedAfterEscape();
      assert.equal(parts.map(p => keys.feed(p)).join(""), text);
      assert.equal(keys.feed("\r"), "\r");
      keys.head(undefined, {}, true);
      assert.equal(keys.feed(first), first === "\x18y" ? "" : first);
      if (first !== "\x18y") {
        assert.equal(keys.feed("y"), ""); assert.ok(keys.needsStringBoundary);
        keys.feed("\x18");
      }
      assert.equal(keys.feed("y"), "y");
    }
  }
});

test("Python is composer text after a remotely retired foreground Escape, before or after its timeout", () => {
  for (const timing of ["timely", "expired-on-A", "expired-after-removal"]) for (const negative of ["N", "\x03"]) {
    const { keys } = setup(); keys.head("A"); keys.feed("\x1b");
    if (timing === "expired-on-A") assert.equal(keys.finishEscapeWait(), "");
    keys.head();
    if (timing === "expired-after-removal") assert.equal(keys.finishEscapeWait(), "");
    assert.equal(keys.feed("P") + keys.feed("ython"), "Python", timing);
    assert.equal(keys.feed("\r"), "\r");
    keys.head(undefined, {}, true);
    assert.equal(keys.feed(negative), negative); assert.equal(keys.feed("y"), "");
    assert.equal(keys.needsStringBoundary, true);
    keys.feed("\x07"); assert.equal(keys.feed("a"), "", "BEL is not DCS ST");
    assert.equal(keys.feed("\x18y"), ""); assert.equal(keys.feed("y"), "y");
  }
});

test("after a delivered composer Escape, ]list is literal there but a possible OSC title never grants the next card", () => {
  for (const text of ["]list", "]List"]) for (const parts of [[text], [...text]]) for (const boundary of ["\x07", "\x1b\\", "\x18"]) {
    const keys = typedAfterEscape();
    assert.equal(parts.map(part => keys.feed(part)).join(""), text);
    assert.equal(keys.feed("\r"), "\r");
    keys.head(undefined, {}, true); assert.equal(keys.needsStringBoundary, true);
    assert.equal(keys.feed("n"), "n"); assert.equal(keys.feed("y"), ""); assert.equal(keys.feed("\x03"), "\x03");
    keys.feed(boundary); assert.equal(keys.feed("a"), "a");
  }
});

test("a delivered composer Escape preserves bracketed words, Unicode and long literal prefixes exactly once", () => {
  for (const word of ["[中文]", "[user]", "[INFO]", "[OK]", "[u]", "[A]", "[👨‍👩‍👧‍👦é]", "[" + "1".repeat(70) + "text]"]) {
    for (const pieces of [[word], Array.from(word)]) {
      const keys = typedAfterEscape(); assert.equal(pieces.map(c => keys.feed(c)).join(""), word);
      assert.equal(keys.feed("\r"), "\r");
    }
  }
});

test("an actual OSC frame is protocol data, including when fragmented on the composer", () => {
  for (const intro of ["\x1b]", "\x9d"]) for (const parts of [[intro + "52;c;payload"], [...intro, "52", ";c;", "payload"]]) {
    const { keys } = setup();
    assert.equal(parts.map(p => keys.feed(p)).join(""), "");
    keys.head(undefined, {}, true);
    for (const key of ["y", "n", "\x03"]) assert.equal(keys.feed(key), "");
    keys.feed("\x07"); assert.equal(keys.feed("y"), "y");
  }
});

test("a late explicit numeric OSC selector drains its payload, unlike independent punctuation after Escape", () => {
  for (const parts of [["]52;c;payload"], [..."]52;c;payload"]]) {
    const keys = typedAfterEscape();
    const text = parts.map(part => keys.feed(part)).join("");
    assert.ok(!text.includes("payload"));
    keys.head(undefined, {}, true); assert.equal(keys.feed("y"), ""); assert.equal(keys.feed("n"), "");
    keys.feed("\x07"); assert.equal(keys.feed("y"), "y");
  }
});

test("submitting a word after a spent Escape does not strand the next card's refusal or cancel", () => {
  for (const word of ["Python", "P2", "P.S.", "_value", "X11", "^2"]) for (const key of ["n", "\x03"]) {
    const keys = typedAfterEscape(); assert.equal(keys.feed(word), word); assert.equal(keys.feed("\r"), "\r");
    keys.head(undefined, {}, true); assert.equal(keys.feed(key), key);
  }
});

test("a composer submission is not a DCS terminator: any unterminated late string keeps positive authority closed", () => {
  // Even the private-function spelling of a DCS can be indistinguishable from
  // a word. A newline in its payload is not ST. Refusing is always safe;
  // positively choosing requires a real boundary, never a guess about the word.
  for (const text of ["P$qfixture", "Pqfixture", "Python", "P2P", "_Gf=1", "X11"]) {
    const keys = typedAfterEscape(); keys.feed(text); keys.feed("\r");
    keys.head(undefined, {}, true); assert.equal(keys.feed("y"), "");
    assert.equal(keys.feed("n"), "n"); keys.head(undefined, {}, true); assert.equal(keys.feed("a"), "");
    assert.equal(keys.feed("\x18y"), "", "aborting a string must not release a coalesced answer");
    assert.equal(keys.feed("y"), "y", "fresh decision after VT Cancel");
  }
});

test("a possible opaque tail must not reinterpret the original composer's cursor key or let a paste abort the fence", () => {
  const keys = typedAfterEscape(); assert.equal(keys.feed("Python"), "Python");
  assert.equal(keys.feed(C + "A"), C + "A");
  keys.head(undefined, {}, true); keys.feed(C + "200~");
  assert.equal(keys.feed("\x18"), ""); assert.equal(keys.feed("y"), "");
  keys.feed(C + "201~"); assert.equal(keys.feed("a"), ""); keys.feed("\x18"); assert.equal(keys.feed("a"), "a");
});

test("a paste opener split across literal-string ownership protects every pasted reset or shortcut", () => {
  const start = C + "200~";
  for (const word of ["Python", "]list"]) for (let cut = 0; cut <= start.length; cut++) {
    const keys = typedAfterEscape(); keys.feed(word); keys.feed(start.slice(0, cut));
    keys.head(undefined, {}, true); keys.feed(start.slice(cut));
    for (const c of ["n", "\x03", "\x18", "\x07", "\x1b\\", "y"]) assert.equal(keys.feed(c), "");
    keys.feed(C + "201~"); assert.equal(keys.feed("a"), ""); keys.feed("\x18"); assert.equal(keys.feed("a"), "a");
  }
});

test("an ambiguous title does not shorten a subsequent ST-only string or promote an embedded Kitty gesture", () => {
  for (const open of ["\x1bP", "\x9f"]) {
    const { keys, grants, negotiate } = setup(); keys.feed("\x1b"); keys.finishEscapeWait(); keys.feed("]list");
    keys.feed(open); keys.head("B"); keys.feed("\x07");
    negotiate(); keys.feed(press + release); assert.deepEqual(grants, []);
    keys.feed("\x1b\\"); negotiate(); keys.feed(release); assert.deepEqual(grants, []);
    keys.feed(press + release); assert.deepEqual(grants, ["B:once"]);
  }
});

test("after Escape has actually rejected A the first independent refusal or cancel on B is usable", () => {
  for (const standalone of ["n", "N", "\x03"]) {
    const { keys } = setup(); keys.head(undefined, {}, true); keys.feed("\x1b");
    assert.equal(keys.finishEscapeWait(), "\x1b"); keys.head(undefined, {}, true);
    assert.equal(keys.feed(standalone), standalone);
  }
});

test("after a standalone Escape answers A, one late key cannot also answer B", () => {
  for (const suffix of [["[", "A"], ["O", "A"], ["[?1004;2$", "y"], ["y"], ["a"], ["\r"]]) {
    const { keys } = setup(); keys.head(undefined, {}, true);
    assert.equal(keys.feed("\x1b"), ""); assert.equal(keys.finishEscapeWait(), "\x1b");
    keys.head(undefined, {}, true);
    assert.equal(suffix.map(s => keys.feed(s)).join(""), "", "no late suffix to the new card");
    assert.equal(keys.feed("y"), "y", "a fresh choice is still available");
  }
});

test("a late paste opener survives both revocation into the composer and arrival of an ordinary card", () => {
  for (let cut = 0; cut <= "[200~".length; cut++) {
    const { keys } = setup(); keys.head("old"); keys.feed("\x1b"); keys.head();
    assert.equal(keys.finishEscapeWait(), "");
    assert.equal(keys.feed("[200~".slice(0, cut)), ""); keys.head(undefined, {}, true);
    assert.equal(keys.feed("[200~".slice(cut)), "");
    for (const piece of ["y", "a", "\r", C + "20", "1~"]) assert.equal(keys.feed(piece), "");
    assert.equal(keys.feed("y"), "y");
  }
});

test("ambiguous Alt+] leaves a separate denial, standalone Escape and cancel available", () => {
  for (const changed of [false, true]) for (const negative of ["n", "N", "\x03", "\x1b"]) {
    const { keys } = setup(); keys.head(undefined, {}, true); keys.feed("\x1b]");
    if (changed) keys.head(undefined, {}, true);
    assert.equal(keys.feed(negative) + keys.finishEscapeWait(), negative);
  }
  const { keys } = setup(); keys.head(undefined, {}, true);
  keys.feed("\x1b]52;c;"); // an actual OSC with payload, not just the ambiguous Alt+] prefix
  for (const s of ["n", "y", "a", "\x07"]) assert.equal(keys.feed(s), "");
  assert.equal(keys.feed("n"), "n");
});

test("Alt+Escape is not standalone Escape, even across reads or a FIFO turnover", () => {
  for (const split of [false, true]) {
    const { keys } = setup(); keys.head(undefined, {}, true);
    if (split) { keys.feed("\x1b"); keys.head(undefined, {}, true); keys.feed("\x1b"); }
    else keys.feed("\x1b\x1b");
    assert.equal(keys.finishEscapeWait(), "", "no accidental denial");
    assert.equal(keys.feed("n"), "n");
  }
});

test("an ambiguous OSC never promotes an embedded foreground frame, and a refused chord need not strand cancellation", () => {
  const { keys, grants, negotiate } = setup(); keys.head("F"); negotiate();
  keys.feed("\x1b]"); keys.feed("\x1b"); keys.feed(press.slice(1) + release); keys.feed("\x07");
  assert.deepEqual(grants, []);
  keys.head(undefined, {}, true); keys.feed("\x1b]"); assert.equal(keys.feed("y"), "");
  assert.equal(keys.feed("\x03"), "\x03");
});

test("a literal bracket before a separate Enter is not lost or merged into a key for the next modal", () => {
  for (const interrupted of [false, true]) {
    const { keys } = setup(); keys.head(undefined, {}, true); keys.feed("\x1b"); keys.finishEscapeWait(); keys.head();
    const text = keys.feed("[") + keys.feed("\r");
    assert.equal(text, "[");
    if (interrupted) keys.head(undefined, {}, true);
    assert.equal(keys.takeFollowingKey(), interrupted ? "" : "\r");
  }
});

test("spent Escape permits independent composer submit without turning a delayed OSC into approval input", () => {
  const { keys } = setup(); keys.feed("\x1b"); assert.equal(keys.finishEscapeWait(), "\x1b");
  assert.equal(keys.feed("\r"), "\r");
  keys.feed("\x1b"); keys.finishEscapeWait();
  assert.equal(keys.feed("[\r"), "["); assert.equal(keys.takeFollowingKey(), "\r");
});

test("a possible OSC selector survives a composer hop without capturing unrelated literal ]", () => {
  for (const before of ["]", "]52;c;"]) {
    const { keys } = setup(); keys.head("F"); keys.feed("\x1b"); keys.head(); keys.finishEscapeWait();
    keys.feed(before); keys.head(undefined, {}, true);
    keys.feed("]52;c;".slice(before.length)); assert.equal(keys.feed("y"), "");
    keys.feed("\x07"); assert.equal(keys.feed("y"), "y");
  }
  const { keys } = setup(); keys.head("F"); keys.feed("\x1b"); keys.head(); keys.finishEscapeWait();
  assert.equal(keys.feed("]") + keys.feed("words"), "]words"); assert.equal(keys.feed("\r"), "\r");
  keys.head(undefined, {}, true); assert.equal(keys.feed("y"), "y");
});

test("other string controls split across a retired card and composer also cannot deliver payload to an approval", () => {
  for (const opener of ["P", "_", "^", "X"]) {
    const { keys } = setup(); keys.head("F"); keys.feed("\x1b"); keys.head(); keys.finishEscapeWait();
    keys.feed(opener + "1;"); keys.head(undefined, {}, true);
    assert.equal(keys.feed("y"), ""); keys.feed("\x1b\\"); assert.equal(keys.feed("y"), "y");
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

test("without Kitty reporting, one independent key answers its foreground head on press, once", () => {
  for (const [key, decision] of [["y", "once"], ["Y", "once"], ["\r", "once"], ["a", "session"], ["A", "session"]]) {
    const { keys, grants } = setup(); keys.head("F"); // probe written, never answered
    keys.feed(key); keys.feed(key); assert.deepEqual(grants, [`F:${decision}`], JSON.stringify(key));
  }
});

test("a plain-byte answer is never an Escape suffix, a coalesced read, a paste or string payload", () => {
  const cases: [string, (keys: ForegroundTerminalInput) => void][] = [
    ["Alt+y", keys => keys.feed("\x1by")],
    ["Alt+y split inside the Escape wait", keys => { keys.feed("\x1b"); keys.feed("y"); }],
    ["suffix after an expired Escape", keys => { keys.feed("\x1b"); assert.equal(keys.finishEscapeWait(), ""); keys.feed("y"); }],
    ["two bytes", keys => keys.feed("yy")],
    ["y with Return", keys => keys.feed("y\r")],
    ["bracketed paste", keys => keys.feed(C + "200~y" + C + "201~")],
    ["OSC payload", keys => { keys.feed("\x1b]2;"); keys.feed("y"); keys.feed("\x07"); }],
    ["DCS payload", keys => { keys.feed("\x1bP"); keys.feed("y"); keys.feed("\x1b\\"); }],
    ["CSI final byte", keys => { keys.feed(C + "?1004;2$"); keys.feed("y"); }],
  ];
  for (const [name, feed] of cases) {
    const { keys, grants } = setup(); keys.head("F");
    feed(keys); assert.deepEqual(grants, [], name);
    keys.feed("y"); assert.deepEqual(grants, ["F:once"], `${name}: a fresh key still answers`);
  }
});

test("once Kitty reporting is live, plain bytes cannot answer; only a matching release frame can", () => {
  const { keys, grants, negotiate } = setup(); keys.head("K"); negotiate();
  for (const key of ["y", "Y", "\r", "a"]) keys.feed(key);
  assert.deepEqual(grants, []);
  keys.feed(press + release); assert.deepEqual(grants, ["K:once"]);
});
