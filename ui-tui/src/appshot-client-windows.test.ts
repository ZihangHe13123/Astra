import test from "node:test";
import assert from "node:assert/strict";
import { Duplex } from "node:stream";
import { AppshotClient, type AppshotConsumer } from "./appshot-client.js";
import { WindowsAppshotFrameDecoder, encodeWindowsAppshotMessage, type WindowsAppshotMessage } from "./appshot-protocol-windows.js";
import type { WindowsAppshotDependencies, WindowsAppshotOffer } from "./appshot-windows.js";
import type { AppshotInputOffer } from "./appshot-input.js";

const tick = () => new Promise<void>(r => setImmediate(r));
class Pipe extends Duplex {
  sent: WindowsAppshotMessage[] = [];
  _read() {}
  _write(data: Buffer, _encoding: BufferEncoding, done: (error?: Error | null) => void) {
    for (const message of new WindowsAppshotFrameDecoder().feed(data)) {
      this.sent.push(message);
      if (message.type === "hello") queueMicrotask(() => this.deliver({ type: "hello_ack", version: 2,
        platform: "windows", instance_id: "broker", broker_nonce: "nonce", session_id: message.session_id }));
    }
    done();
  }
  deliver(m: WindowsAppshotMessage) { this.push(encodeWindowsAppshotMessage(m)); }
}
async function fixture() {
  const pipes: Pipe[] = [], staged: AppshotInputOffer[] = [], commits: string[] = [], revokes: string[] = [];
  const reads: { offer: AppshotInputOffer; signal: AbortSignal; resolve: (v: AppshotInputOffer) => void; reject: (e: Error) => void }[] = [];
  const windowsDeps: WindowsAppshotDependencies = {
    identity: async () => ({ version: 2, platform: "windows", pid: process.pid, process_start: "42", user_sid: "S-1-5-21-1", monotonic_ns: "100" }),
    discover: async () => ({ version: 2, platform: "windows", scope: "test", instance_id: "broker", broker_nonce: "nonce", process: { pid: 123, process_start: "1", user_sid: "S-1-5-21-1" } }),
    connect: async () => { const pipe = new Pipe(); pipes.push(pipe); return pipe; },
    launch: async () => { throw Error("unexpected launch"); }, now: () => 10n,
    verifyOffer: (offer, binding, signal) => new Promise((resolve, reject) => reads.push({
      offer: { requestId: offer.request_id, manifestPath: offer.manifest_path, appLabel: "Fixture", windowTitle: "Test", binding },
      signal, resolve, reject,
    })),
  };
  const consumer: AppshotConsumer = {
    stage: () => { throw Error("Windows must not use POSIX reader"); },
    stageWindows: o => { staged.push(o); return true; },
    commit: m => { commits.push(m.request_id); return { appshotCount: commits.length, canAccept: commits.length < 4 }; },
    revoke: e => { revokes.push(e.requestID); },
    disconnect: () => ({ appshotCount: 0, canAccept: true }),
  };
  const client = new AppshotClient({ platform: "win32", windowsDeps, consumer, heartbeatMS: 10000, retryMS: 1 });
  await client.start(); client.updateDraft(0n, 0, true);
  const offer = (id = "offer"): WindowsAppshotOffer => ({ type: "attach_offer", version: 2, platform: "windows", request_id: id,
    broker_id: "broker", session_id: client.recipientBinding!.session_id, manifest_path: "C:/Fixture/appshot-0123456789abcdef0123456789abcdef.manifest.json" });
  return { client, pipes, staged, commits, revokes, reads, offer };
}
test("Windows v2 proves SID/start, verifies before ack and inserts only on commit", async () => {
  const f = await fixture(); try {
    assert.equal(f.client.state.connection, "connected");
    assert.deepEqual(f.pipes[0].sent[0], { type: "hello", version: 2, platform: "windows", session_id: f.client.recipientBinding!.session_id,
      pid: process.pid, process_start: "42", user_sid: "S-1-5-21-1", client_nonce: (f.pipes[0].sent[0] as any).client_nonce });
    const o = f.offer(); f.pipes[0].deliver(o); await tick();
    assert.equal(f.reads.length, 1); assert.equal(f.staged.length, 0);
    assert.equal(f.pipes[0].sent.filter(m => m.type === "attach_ack").length, 0);
    f.reads[0].resolve(f.reads[0].offer); await tick();
    assert.equal(f.staged.length, 1); assert.deepEqual(f.commits, []);
    assert.equal((f.pipes[0].sent.at(-1) as any).accepted, true);
    f.pipes[0].deliver({ ...o, type: "attach_commit" }); await tick();
    assert.deepEqual(f.commits, [o.request_id]);
    assert.equal((f.pipes[0].sent.at(-1) as any).appshot_count, 1);
    f.client.release(o.request_id); assert.equal(f.pipes[0].sent.at(-1)?.type, "release");
  } finally { await f.client.close(); }
});
test("revoke aborts pending Windows verification without a late ack or draft", async () => {
  const f = await fixture(); try {
    const o = f.offer(); f.pipes[0].deliver(o); await tick();
    const { manifest_path, ...binding } = o;
    f.pipes[0].deliver({ ...binding, type: "attach_revoke", reason: "cancelled" }); await tick();
    assert.equal(f.reads[0].signal.aborted, true); f.reads[0].resolve(f.reads[0].offer); await tick();
    assert.equal(f.staged.length, 0); assert.equal(f.pipes[0].sent.filter(m => m.type === "attach_ack").length, 0);
  } finally { await f.client.close(); }
});
test("Windows verification timeout frees capacity even if native adapter ignores abort", async () => {
  const f = await fixture(); try {
    f.pipes[0].deliver(f.offer()); await tick();
    await new Promise(r => setTimeout(r, 1550));
    assert.equal(f.reads[0].signal.aborted, true);
    assert.equal((f.pipes[0].sent.at(-1) as any).accepted, false);
    f.reads[0].resolve(f.reads[0].offer); await tick(); assert.equal(f.staged.length, 0);
    f.pipes[0].deliver(f.offer("next")); await tick();
    f.reads[1].resolve(f.reads[1].offer); await tick(); assert.equal(f.staged.length, 1);
  } finally { await f.client.close(); }
});
test("old Windows read cannot cross a reconnect epoch", async () => {
  const f = await fixture(); try {
    f.pipes[0].deliver(f.offer()); await tick(); const old = f.client.recipientBinding!.session_id;
    f.pipes[0].destroy();
    for (let i = 0; i < 50 && f.pipes.length < 2; i++) await new Promise(r => setTimeout(r, 5));
    assert.equal(f.pipes.length, 2); assert.notEqual(f.client.recipientBinding!.session_id, old);
    assert.equal(f.reads[0].signal.aborted, true); f.reads[0].resolve(f.reads[0].offer); await tick();
    assert.equal(f.staged.length, 0); assert.equal(f.pipes[1].sent.filter(m => m.type === "attach_ack").length, 0);
  } finally { await f.client.close(); }
});
test("Windows read with different recipient is rejected and close cancels outstanding reads", async () => {
  const f = await fixture();
  f.pipes[0].deliver(f.offer()); await tick();
  f.reads[0].resolve({ ...f.reads[0].offer, binding: { instance_id: "broker", session_id: "wrong", recipient: { pid: process.pid, process_start: "42", user_sid: "S-1-5-21-1" } } });
  await tick(); assert.equal(f.staged.length, 0); assert.equal((f.pipes[0].sent.at(-1) as any).accepted, false);
  f.pipes[0].deliver(f.offer("next")); await tick(); await f.client.close();
  assert.equal(f.reads[1].signal.aborted, true); f.reads[1].resolve(f.reads[1].offer); await tick(); assert.equal(f.staged.length, 0);
});
test("Windows installation without the optional helper stays idle under input and never offers a retry", async () => {
  const { mkdtempSync, realpathSync, rmSync } = await import("node:fs");
  const { tmpdir } = await import("node:os");
  const { join } = await import("node:path");
  const { productionWindowsAppshotDependencies } = await import("./appshot-windows.js");
  // A source installation: the root and LOCALAPPDATA exist, the helper bundle was never built.
  const root = mkdtempSync(join(realpathSync(tmpdir()), "appshot-no-helper-"));
  const client = new AppshotClient({ platform: "win32", retryMS: 1,
    windowsDeps: productionWindowsAppshotDependencies({ LOCALAPPDATA: root, AGENT_PROJECT_ROOT: root }) });
  const notices: string[] = []; let attempts = 0;
  client.on("notice", code => notices.push(code));
  client.on("change", state => { if (state.connection === "connecting") attempts++; });
  try {
    await client.start();
    for (let key = 0; key < 10; key++) { client.recordInput(); await new Promise(r => setTimeout(r, 1)); }
    assert.deepEqual(notices, ["helper_unavailable"]); assert.equal(attempts, 0);
    assert.equal(client.state.connection, "disconnected");
  } finally { await client.close(); rmSync(root, { recursive: true, force: true }); }
});
test("v1 bytes fail the Windows client without invoking any file reader", async () => {
  const f = await fixture(); try {
    f.pipes[0].push(Buffer.from(JSON.stringify({ ...f.offer(), version: 1 }) + "\n")); await tick();
    assert.equal(f.pipes[0].destroyed, true); assert.equal(f.reads.length, 0);
  } finally { await f.client.close(); }
});
