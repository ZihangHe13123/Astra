import assert from "node:assert/strict";
import { test } from "node:test";
import * as sidebar from "../src/renderer/sidebar.js";

const saved = { name: "saved", mode: "work", modified: 0 };
const preferences = { "work:saved": "preferred" };

test("opens the session in its recorded folder ahead of preferences and the current folder", () => {
  assert.equal(sidebar.openingFolder([{ ...saved, workspace: "recorded" }], preferences, "current", "saved", "work"), "recorded");
});

for (const workspace of [null, undefined, ""]) {
  test(`uses the preferred folder when the recorded folder is ${JSON.stringify(workspace)}`, () => {
    assert.equal(sidebar.openingFolder([{ ...saved, workspace }], preferences, "current", "saved", "work"), "preferred");
  });
}

test("uses the current folder when neither the session nor preferences supply one", () => {
  assert.equal(sidebar.openingFolder([saved], {}, "current", "saved", "work"), "current");
});

test("ignores saved folders for other names or modes", () => {
  const history = [
    { ...saved, mode: "minimal", workspace: "other mode" },
    { ...saved, name: "another", workspace: "other name" },
  ];
  assert.equal(sidebar.openingFolder(history, {}, "current", "saved", "work"), "current");
});
