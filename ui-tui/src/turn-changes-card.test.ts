import assert from "node:assert/strict";
import { test } from "node:test";
import stringWidth from "string-width";
import { turnChangesCard } from "./turn-changes-card.js";
import type { TurnChangesEventData } from "./turn-changes.js";

const text = (cells: { text: string }[]) => cells.map((cell) => cell.text).join("");
const file = (path: string, state = "modified", added: number | null = 4, removed: number | null = 1) => ({ path, state, added, removed, compare: "ok", reason: "" });
const report = (overrides: Partial<TurnChangesEventData> = {}): TurnChangesEventData => ({
  session_id: "s", request_id: "r", turn_seq: 1, unknown_count: 0,
  files: [file("agent/runtime/turn_change_store.py", "modified", 3, 1), file("tests/test_turn_change_store.py", "added", 6, 0)],
  totals: { files: 2, added: 9, removed: 1 },
  ...overrides,
});

test("the totals head the card, and each file has a row", () => {
  const card = turnChangesCard(report(), 82)!;
  assert.match(text(card[0]), /^✎ 本轮改动 2 个文件  \+9 −1 +\/changes 查看$/);
  // The command that shows the diff ends the head at the card's right edge.
  assert.equal(stringWidth(text(card[0])), 82);
  assert.deepEqual(card.slice(1).map(text), [
    "├ M agent/runtime/turn_change_store.py  +3 −1",
    "└ A tests/test_turn_change_store.py     +6 −0",
  ]);
  // Added, removed and the kind of change are told apart by colour as well as by sign.
  assert.deepEqual(card[2].map((cell) => cell.tone), ["muted", "success", "text", "success", "danger"]);
  assert.equal(turnChangesCard(report({ files: [file("gone.txt", "deleted", 0, 7)] }), 82)![1][1].tone, "danger");
});

test("nothing changed and nothing unconfirmed is no card", () => {
  assert.equal(turnChangesCard(report({ files: [], totals: {} }), 82), null);
  assert.equal(turnChangesCard(null as unknown as TurnChangesEventData, 82), null);
});

test("paths that could not be confirmed are counted, with or without changed files", () => {
  assert.match(text(turnChangesCard(report({ unknown_count: 1 }), 82)![0]), /^✎ 本轮改动 2 个文件  \+9 −1 · 另有 1 个路径未能确认 +\/changes 查看$/);
  const only = turnChangesCard(report({ files: [], totals: {}, unknown_count: 2 }), 82)!;
  assert.equal(only.length, 1);
  assert.match(text(only[0]), /^✎ 另有 2 个路径未能确认 +\/changes 查看$/);
});

test("a long list is cut, and says how many files it leaves out", () => {
  const many = report({ files: Array.from({ length: 9 }, (_, index) => file(`src/file-${index}.ts`)), totals: { files: 9, added: 36, removed: 9 } });
  const card = turnChangesCard(many, 82)!;
  assert.equal(card.length, 1 + 6);
  assert.equal(text(card.at(-1)!), "└ 还有 4 个文件");
  assert.match(text(card[5]), /^├ M src\/file-4\.ts/);
  // Exactly as many as fit are all shown.
  const six = turnChangesCard(report({ files: many.files.slice(0, 6) }), 82)!;
  assert.equal(six.length, 7);
  assert.match(text(six.at(-1)!), /^└ M src\/file-5\.ts/);
});

test("a path too long for the card keeps its end, and no row is wider than the card", () => {
  const long = report({ files: [file("packages/some/very/deeply/nested/directory/structure/with/a/long/name/component.test.tsx", "modified", 120, 45), file("短.md")] });
  for (const width of [82, 60, 40, 28]) {
    const card = turnChangesCard(long, width)!;
    for (const row of card) assert.ok(stringWidth(text(row)) <= width, `${width}: ${text(row)}`);
    assert.match(text(card[1]), width >= 40 ? /^├ M …[^ ]*component\.test\.tsx +\+120 −45$/ : /^├ M …[^ ]*test\.tsx +\+120 −45$/, `${width}: ${text(card[1])}`);
  }
  // The command is shown whole or not at all.
  assert.doesNotMatch(text(turnChangesCard(long, 28)![0]), /\/changes/);
  assert.match(text(turnChangesCard(long, 60)![0]), /\/changes 查看$/);
});

test("a file whose lines could not be counted says what happened to it", () => {
  const card = turnChangesCard(report({ files: [file("image.png", "added", null, null), file("a.ts")] }), 82)!;
  assert.match(text(card[1]), /^├ A image\.png +added$/);
  assert.match(text(card[2]), /^└ M a\.ts +\+4 −1$/);
});

test("a malformed report is shown as far as it can be read", () => {
  const card = turnChangesCard({ files: [file("a.ts"), null, { path: 3 }], unknown_count: "x", totals: null } as unknown as TurnChangesEventData, 60)!;
  assert.equal(card.length, 2);
  assert.match(text(card[0]), /^✎ 本轮改动 1 个文件  \+0 −0/);
});
