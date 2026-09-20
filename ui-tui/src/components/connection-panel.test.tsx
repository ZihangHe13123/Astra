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
