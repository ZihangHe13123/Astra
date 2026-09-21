import assert from "node:assert/strict";
import test from "node:test";
import { localCommandEntry, modeChoices, modeCommand } from "../src/local-mode.js";

test("public desktop exposes builtins without a private mode or installed controller", () => {
  assert.deepEqual(modeChoices(null).map(m => m.mode), ["work", "minimal", "bar"]);
  assert.deepEqual(localCommandEntry(null), []);
  assert.equal(modeCommand("local", null), undefined);
  assert.equal(modeCommand("writing", null), undefined);
  assert.equal(modeCommand("minimal", null), "/minimal");
});
test("an installed local mode keeps its own command and label", () => {
  const local = { command: "/example", label: "Example", description: "Local extension" };
  assert.equal(modeCommand("local", local), "/example");
  assert.deepEqual(modeChoices(local).at(-1), { mode: "local", label: "Example", description: "Local extension" });
  assert.deepEqual(localCommandEntry(local)[0].options.map(o => o.submitValue), ["/example new", "/example list", "/example leave"]);
});
