import assert from "node:assert/strict";
import test from "node:test";
import { PassThrough, Writable } from "node:stream";
import React from "react";
import { Box, Text, render, useStdout } from "ink";
import stripAnsi from "strip-ansi";
import stringWidth from "string-width";
import { ThemeProvider } from "../theme-context.js";
import { THEMES } from "../theme.js";
import { ConnectionPanel as Panel, type ConnectionPanelProps } from "./connection-panel.js";
import { approvalPaintOutput, useApprovalPaint } from "../approval-paint.js";
import type { TuiCommand } from "../types.js";
class Input extends PassThrough { isTTY = true; setRawMode() {} ref() { return this; } unref() { return this; } }
class Output extends Writable {
  columns = 90; rows = 24; isTTY = true; chunks: string[] = [];
  _write(chunk: Buffer | string, _: BufferEncoding, cb: (e?: Error | null) => void) { this.chunks.push(String(chunk)); cb(); }
}
const stdin = new Input(), stdout = new Output();
function ConnectionPanel(props: Omit<ConnectionPanelProps, "ownsInput" | "onPaintTarget">) {
  const { stdout } = useStdout(), [target, setTarget] = React.useState<object>();
  const paint = useApprovalPaint(stdout, target, stdout.rows, stdout.columns, false);
  return <Box flexDirection="column" width={stdout.columns - 1}><Panel {...props} width={stdout.columns - 1} onPaintTarget={setTarget} paintTarget={target} paintEpoch={paint.epoch}
    ownsInput={(requirePaint = true, expectedRow) => !requirePaint || paint.current(false) &&
      (expectedRow === undefined || paint.receipt?.draftDigest === expectedRow)} /></Box>;
}
const saves: TuiCommand[] = [];
let cancelled = 0;
const instance = render(<ThemeProvider theme={THEMES.glitchcity}>
  <ConnectionPanel routes={[{ id: "provider", provider: "Provider", label: "Regular API", base_url: "https://provider.example/v1", api_key_env: "SAMPLE_KEY", key_available: false }]}
    pending={false} error="" onSave={r => saves.push(r)} onCancel={() => cancelled++} />
</ThemeProvider>, { stdin: stdin as unknown as NodeJS.ReadStream, stdout: approvalPaintOutput(stdout as unknown as NodeJS.WriteStream),
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
  { stdin: oauthInput as unknown as NodeJS.ReadStream, stdout: approvalPaintOutput(oauthOutput as unknown as NodeJS.WriteStream),
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
  </ThemeProvider>, { stdin: input as unknown as NodeJS.ReadStream, stdout: approvalPaintOutput(output as unknown as NodeJS.WriteStream),
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
const middle = await paste(["\r", "\r", wrap(url), "\r", "\r", "abcdef", "\u001b[D", "\u001b[D", "\u001b[D", wrap("X"), "Y", "\r"]);
assert.deepEqual(middle.saved, [[url, "abcXYdef", ""]]);
assert.equal(middle.frames.some(frame => frame.includes("abcX")), false);
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

test("an overpainted OAuth label or ID cannot authorize from the panel edges alone", async () => {
  const input = new Input(), output = new Output(), saves: string[] = [];
  let overlay: { x: number; y: number } | undefined;
  const label = "Fixture subscription", id = "fixture-oauth-route";
  const routes = [{ id, label, provider: "Fixture", base_url: "", api_key_env: "", key_available: false, auth_mode: "oauth" as const }];
  const scene = () => <ThemeProvider theme={THEMES.glitchcity}><Box flexDirection="column">
    <ConnectionPanel routes={routes} pending={false} error="" onSave={r => saves.push(r.route_id)} onCancel={() => {}} />
    {overlay && <Box position="absolute" marginLeft={overlay.x} marginTop={overlay.y}><Text>×</Text></Box>}
  </Box></ThemeProvider>;
  const app = render(scene(), { stdin: input as any, stdout: approvalPaintOutput(output as any), stderr: output as any,
    debug: true, patchConsole: false, exitOnCtrlC: false });
  const settle = () => new Promise(resolve => setTimeout(resolve, 50));
  const enter = async () => { input.write("\r"); await settle(); };
  const frame = () => stripAnsi(output.chunks.at(-1) ?? "");
  try {
    await settle(); await enter();
    for (const part of [label, id]) {
      const lines = frame().split("\n"), y = lines.findIndex(line => line.includes(`› ${label} [${id}]`));
      assert.notEqual(y, -1); const line = lines[y]!;
      overlay = { x: stringWidth(line.slice(0, line.indexOf(part))) + 2, y };
      app.rerender(scene()); await settle(); assert.ok(frame().includes("×") && !frame().includes(part));
      assert.match(frame(), /Choose API route/); assert.match(frame(), /Enter continue/);
      await enter(); assert.deepEqual(saves, []);
      overlay = undefined; app.rerender(scene()); await settle(); assert.deepEqual(saves, []);
    }
    await enter(); assert.deepEqual(saves, [id]);
  } finally { app.unmount(); }
});
assert.deepEqual(filtered.saved, [[url, secret, ""]]);

const claudeRoute = { id: "claude-code", provider: "Claude / Claude Code", label: "Subscription · your signed-in claude CLI",
  base_url: "", api_key_env: "", key_available: false, auth_mode: "oauth" as const };
const claudeChoice = `› ${claudeRoute.label} [${claudeRoute.id}]`;
const cells = (text: string) => text.replace(/[\s│▏▕↳]/gu, "");

async function withClaude(columns: number, exercise: (h: { frame(): string; rows(): string[]; saves: string[];
  enter(): Promise<void>; cover(wholeRow: boolean): Promise<void>; restore(): Promise<void>;
  resizeWithBlockedWriter(columns: number): Promise<void>; ack(): Promise<void> }) => Promise<void>) {
  const input = new Input(), saves: string[] = [], settle = () => new Promise(resolve => setTimeout(resolve, 50));
  let hold = false, flush: (() => void) | undefined;
  const output = Object.assign(new Writable({ write(chunk, _, done) {
    const deliver = () => { output.chunks.push(String(chunk)); done(); };
    if (hold) flush = deliver; else deliver();
  } }), { columns, rows: 34, isTTY: true, chunks: [] as string[] });
  let overlay: { x: number; y: number; text: string } | undefined;
  const scene = () => <ThemeProvider theme={THEMES.glitchcity}><Box flexDirection="column">
    <ConnectionPanel routes={[claudeRoute]} pending={false} error="" onSave={r => saves.push(r.route_id)} onCancel={() => {}} />
    {overlay && <Box position="absolute" marginLeft={overlay.x} marginTop={overlay.y}><Text>{overlay.text}</Text></Box>}
  </Box></ThemeProvider>;
  const app = render(scene(), { stdin: input as any, stdout: approvalPaintOutput(output as any), stderr: output as any,
    debug: true, patchConsole: false, exitOnCtrlC: false });
  const frame = () => stripAnsi(output.chunks.at(-1) ?? "");
  const rows = () => { const lines = frame().split("\n"); return lines.slice(lines.findIndex(s => s.includes("Choose API route")) + 1,
    lines.findIndex(s => s.includes("Enter continue"))); };
  const enter = async () => { input.write("\r"); await settle(); };
  try {
    await settle(); await enter();
    await exercise({ frame, rows, saves, enter, cover: async whole => {
      const lines = frame().split("\n"), y = lines.indexOf(rows()[1]!);
      assert.notEqual(y, -1); const x = whole ? 2 : 3; // blank the row, or obscure its first content cell
      overlay = { x, y, text: whole ? " ".repeat(columns - 7) + "×" : "×" };
      app.rerender(scene()); await settle();
    }, restore: async () => { overlay = undefined; app.rerender(scene()); await settle(); },
    resizeWithBlockedWriter: async next => {
      hold = true; overlay = undefined; output.columns = next; output.emit("resize"); app.rerender(scene()); await settle(); assert.ok(flush);
    }, ack: async () => { hold = false; flush!(); flush = undefined; await settle(); } });
  } finally { hold = false; flush?.(); app.unmount(); }
}

for (const columns of [60, 65]) test(`fully visible Claude route submits at ${columns}×34`, async () => {
  await withClaude(columns, async h => {
    assert.equal(h.rows().length, columns === 60 ? 2 : 1);
    assert.equal(cells(h.rows().join("")), cells(claudeChoice));
    if (columns === 60) assert.match(h.rows()[1]!, /↳.+▕/);
    await h.enter(); assert.deepEqual(h.saves, [claudeRoute.id]);
  });
});

test("a wrapped OAuth option requires every continuation and a fresh key after the resized frame is acknowledged", async () => {
  await withClaude(60, async h => {
    for (const whole of [false, true]) {
      assert.equal(h.rows().length, 2); assert.equal(cells(h.rows().join("")), cells(claudeChoice));
      await h.cover(whole); assert.match(h.frame(), /Choose API route/); assert.match(h.frame(), /Enter continue/);
      assert.match(h.rows()[1]!, /×/); if (whole) assert.doesNotMatch(h.rows()[1]!, /↳|▕/);
      await h.enter(); assert.deepEqual(h.saves, []);
      if (!whole) await h.restore();
    }
    await h.resizeWithBlockedWriter(65); await h.enter(); assert.deepEqual(h.saves, []);
    await h.ack(); assert.equal(h.rows().length, 1); assert.equal(cells(h.rows()[0]!), cells(claudeChoice));
    assert.deepEqual(h.saves, []); await h.enter(); assert.deepEqual(h.saves, [claudeRoute.id]);
  });
});
