import { test } from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";

// The main process answers every web permission request with "denied" (src/main/index.ts), so the
// renderer's navigator.clipboard.writeText fails at runtime with "Write permission denied".
// Copying goes through window.astra.copyText, which the main process writes to the clipboard.
test("the renderer never uses the web clipboard, which this window's permissions deny", () => {
  const folder = new URL("../src/renderer/", import.meta.url);
  const offenders = readdirSync(folder).filter(name => /\.tsx?$/.test(name))
    .filter(name => /navigator\s*\.\s*clipboard/.test(readFileSync(new URL(name, folder), "utf8")));
  assert.deepEqual(offenders, []);
});

test("the copy button writes through the desktop bridge", () => {
  assert.match(readFileSync(new URL("../src/renderer/messages.tsx", import.meta.url), "utf8"), /window\.astra\.copyText\(/);
});
