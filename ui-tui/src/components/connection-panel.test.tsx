import assert from "node:assert/strict";
import { PassThrough, Writable } from "node:stream";
import React from "react";
import { render } from "ink";
import stripAnsi from "strip-ansi";
import { ThemeProvider } from "../theme-context.js";
import { THEMES } from "../theme.js";
import { ConnectionPanel } from "./connection-panel.js";
import type { TuiCommand } from "../types.js";
class Input extends PassThrough { isTTY = true; setRawMode() {} ref() { return this; } unref() { return this; } }
class Output extends Writable {
  columns = 90; rows = 24; isTTY = true; chunks: string[] = [];
  _write(chunk: Buffer | string, _: BufferEncoding, cb: (e?: Error | null) => void) { this.chunks.push(String(chunk)); cb(); }
}
const stdin = new Input(), stdout = new Output();
const saves: TuiCommand[] = [];
let cancelled = 0;
const instance = render(<ThemeProvider theme={THEMES.glitchcity}>
  <ConnectionPanel routes={[{ id: "provider", provider: "Provider", label: "Regular API", base_url: "https://provider.example/v1", api_key_env: "SAMPLE_KEY", key_available: false }]}
    pending={false} error="" onSave={r => saves.push(r)} onCancel={() => cancelled++} />
</ThemeProvider>, { stdin: stdin as unknown as NodeJS.ReadStream, stdout: stdout as unknown as NodeJS.WriteStream,
  stderr: stdout as unknown as NodeJS.WriteStream, debug: true, patchConsole: false, exitOnCtrlC: false });
const press = async (text: string) => { stdin.write(text); await new Promise(r => setTimeout(r, 40)); };
await new Promise(r => setTimeout(r, 30));
await press("\r"); // provider
await press("\r"); // route
await press("\r"); // paste key
await press("secret-test-123");
assert.match(stripAnsi(stdout.chunks.at(-1) ?? ""), /\*{8}/);
assert.equal(stdout.chunks.some(c => c.includes("secret-test-123")), false);
await press("\r");
assert.equal(saves.length, 1);
assert.equal(saves[0].type, "connect_provider");
assert.equal((saves[0] as Extract<TuiCommand, { type: "connect_provider" }>).api_key, "secret-test-123");
assert.equal(stdout.chunks.some(c => c.includes("secret-test-123")), false);
await press("\u001b");
assert.equal(cancelled, 1);
instance.unmount();

const oauthInput = new Input(), oauthOutput = new Output();
const oauthSaves: TuiCommand[] = [];
let oauthCancelled = 0;
const oauthProps = {
  routes: [{ id: "codex", provider: "ChatGPT / Codex", label: "Subscription", base_url: "https://chatgpt.com/backend-api/codex",
    api_key_env: "", key_available: false, auth_mode: "oauth" as const }],
  pending: false, error: "", onSave: (r: TuiCommand) => oauthSaves.push(r), onCancel: () => oauthCancelled++,
};
const oauth = render(<ThemeProvider theme={THEMES.glitchcity}><ConnectionPanel {...oauthProps} /></ThemeProvider>,
  { stdin: oauthInput as unknown as NodeJS.ReadStream, stdout: oauthOutput as unknown as NodeJS.WriteStream,
    stderr: oauthOutput as unknown as NodeJS.WriteStream, debug: true, patchConsole: false, exitOnCtrlC: false });
const oauthPress = async (key: string) => { oauthInput.write(key); await new Promise(r => setTimeout(r, 40)); };
await new Promise(r => setTimeout(r, 30));
await oauthPress("\r");
await oauthPress("\r");
assert.equal(oauthSaves.length, 1);
assert.equal((oauthSaves[0] as Extract<TuiCommand, { type: "connect_provider" }>).api_key, "");
assert.equal(oauthOutput.chunks.some(c => c.includes("Paste API key")), false);
oauth.rerender(<ThemeProvider theme={THEMES.glitchcity}>
  <ConnectionPanel {...oauthProps} pending authorization={{ verification_uri: "https://auth.openai.com/codex/device", user_code: "TEST-CODE" }} />
</ThemeProvider>);
await new Promise(r => setTimeout(r, 40));
const authorizationFrame = stripAnsi(oauthOutput.chunks.at(-1) ?? "");
assert.match(authorizationFrame, /TEST-CODE/);
assert.match(authorizationFrame, /Settings.*Security/);
assert.match(authorizationFrame, /cancel connection/);
await oauthPress("\u001b");
assert.equal(oauthCancelled, 1);
oauth.unmount();

// A terminal in bracketed-paste mode wraps pasted text in ESC[200~ … ESC[201~. The
// markers can arrive in the same read as the text, in their own, or split in two.
const paste = async (chunks: string[]) => {
  const input = new Input(), output = new Output();
  const saved: Extract<TuiCommand, { type: "connect_provider" }>[] = [];
  const panel = render(<ThemeProvider theme={THEMES.glitchcity}>
    <ConnectionPanel routes={[{ id: "custom", provider: "Provider", label: "Regular API", base_url: "", api_key_env: "", key_available: false }]}
      pending={false} error="" onSave={r => saved.push(r)} onCancel={() => {}} />
  </ThemeProvider>, { stdin: input as unknown as NodeJS.ReadStream, stdout: output as unknown as NodeJS.WriteStream,
    stderr: output as unknown as NodeJS.WriteStream, debug: true, patchConsole: false, exitOnCtrlC: false });
  await new Promise(r => setTimeout(r, 30));
  let atSubmit = ""; // the frame on screen when the last chunk, Enter, is pressed
  for (const chunk of chunks) {
    atSubmit = stripAnsi(output.chunks.at(-1) ?? "");
    input.write(chunk); await new Promise(r => setTimeout(r, 40));
  }
  panel.unmount();
  return { saved: saved.map(r => [r.base_url, r.api_key, r.api_key_env]), atSubmit, frames: output.chunks.map(chunk => stripAnsi(chunk)) };
};
const wrap = (text: string) => `\u001b[200~${text}\u001b[201~`;
const url = "https://provider.example/v1", secret = "secret-test-123";
for (const key of [
  [wrap(secret)],
  ["\u001b[200~", secret, "\u001b[201~"],
  ["\u001b[20", `0~${secret.slice(0, 6)}`, `${secret.slice(6)}\u001b[2`, "01~"],
  [wrap(`${secret}\r\n`)], // copied together with its line ending
]) {
  const { saved, atSubmit, frames } = await paste(["\r", "\r", wrap(url), "\r", "\r", ...key, "\r"]);
  assert.deepEqual(saved, [[url, secret, ""]]);
  // One mask character per key character: nothing else is left in the field.
  assert.equal((atSubmit.match(/\*/g) ?? []).length, secret.length);
  assert.equal(frames.some(frame => frame.includes(secret)), false);
}
const environment = await paste(["\r", "\r", wrap(url), "\r", "\u001b[B", "\r", wrap("OTHER_KEY"), "\r"]);
assert.deepEqual(environment.saved, [[url, "", "OTHER_KEY"]]);
const filtered = await paste([wrap("prov"), "\r", "\r", wrap(url), "\r", "\r", wrap(secret), "\r"]);
assert.equal(filtered.frames.some(frame => /Filter: prov +│/.test(frame)), true);
assert.deepEqual(filtered.saved, [[url, secret, ""]]);
