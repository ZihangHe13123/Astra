/** Private cross-process fixture. No installed helper, real registrar or capture. */
import assert from "node:assert/strict";
import { spawn, fork } from "node:child_process";
import { createInterface } from "node:readline";
import { createConnection } from "node:net";
import {
  readFileSync,
  writeFileSync,
  mkdirSync,
  mkdtempSync,
  rmSync,
} from "node:fs";
import { randomUUID } from "node:crypto";
import { fileURLToPath } from "node:url";
import {
  AppshotFrameDecoder,
  encodeAppshotMessage,
} from "../../ui-tui/src/appshot-protocol.js";
import {
  AppshotClient,
  readAppshotDescriptor,
} from "../../ui-tui/src/appshot-client.js";
import {
  emptyDraft,
  appshotCount,
  appendAppshot,
  validateAppshotOffer,
  readAppshotManifestInRuntime,
  appshotSubmission,
  freezeAppshotSubmission,
  settleAppshotSubmission,
  revokeAppshots,
  reconcileAppshotInput,
} from "../../ui-tui/src/appshot-input.js";

const root = process.argv[2];
const sessionRoot = mkdtempSync("/private/tmp/as-python-");
const worker = process.argv.includes("--worker");
const pause = (ms = 30) => new Promise((r) => setTimeout(r, ms));
async function until(test: () => boolean | Promise<boolean>, label: string) {
  const end = Date.now() + 6000;
  while (!(await test())) {
    if (Date.now() > end) throw Error(`deadline: ${label}`);
    await pause(10);
  }
}
let seq = 0;
const waiting = new Map<number, any>();
function receive(message: any) {
  const cb = waiting.get(message.id);
  if (cb) {
    waiting.delete(message.id);
    cb(message.value);
  }
}
if (worker)
  process.on("message", (m: any) => {
    if (m.kind === "native") receive(m);
  });
else
  createInterface({ input: process.stdin }).on("line", (l) =>
    receive(JSON.parse(l)),
  );
function native(op: string, extra: any = {}) {
  return new Promise<any>((resolve, reject) => {
    const id = ++seq;
    const timer = setTimeout(() => reject(Error(`native ${op}`)), 7000);
    waiting.set(id, (v: any) => {
      clearTimeout(timer);
      resolve(v);
    });
    const message = { op, id, ...extra };
    if (worker) process.send!({ kind: "native", ...message });
    else process.stdout.write(JSON.stringify(message) + "\n");
  });
}
let state: any = { draft: emptyDraft() };
const staged = new Map();
let dropAck = false,
  dropCommit = false,
  lastAck: any;
let socket: any;
const count = () => ({
  appshotCount: appshotCount(state),
  canAccept: appshotCount(state) < 4,
});
const client = new AppshotClient({
  retryMS: 50,
  heartbeatMS: 1000,
  deps: {
    currentUID: () => process.getuid!(),
    now: () => process.hrtime.bigint(),
    identity: () => native("identity", { pid: process.pid }),
    discover: async () => {
      const d = readAppshotDescriptor(root, process.getuid!());
      return {
        instance_id: d.instance_id,
        broker_nonce: d.broker_nonce,
        socketPath: root + "/broker.sock",
      };
    },
    connect: async (path) => {
      const s = createConnection(path);
      await new Promise<void>((resolve, reject) => {
        s.once("connect", resolve);
        s.once("error", reject);
      });
      socket = s;
      const write = s.write.bind(s);
      s.write = ((data: any, ...args: any[]) => {
        const m = JSON.parse(data.toString());
        if (m.type === "attach_ack" && dropAck) {
          lastAck = data;
          return true;
        }
        return write(data, ...args);
      }) as any;
      const emit = s.emit.bind(s);
      const incoming = new AppshotFrameDecoder();
      s.emit = ((event: string, ...args: any[]) => {
        if (event === "data") {
          const chunk = Buffer.from(args[0]);
          // Socket chunks are not frames. Also force a fragment boundary while
          // injecting loss, so that path cannot depend on native write batching.
          const messages = dropCommit
            ? [...incoming.feed(chunk.subarray(0, 1)), ...incoming.feed(chunk.subarray(1))]
            : incoming.feed(chunk);
          const forwarded = messages.filter(
            (message) => !dropCommit || message.type !== "attach_commit",
          );
          if (!forwarded.length) return true;
          args[0] = Buffer.concat(forwarded.map(encodeAppshotMessage));
        }
        return emit(event, ...args);
      }) as any;
      return s;
    },
    launch: async () => {
      throw Error("fixture broker must already run");
    },
  },
  consumer: {
    stage(offer, binding) {
      if (appshotCount(state) + staged.size >= 4) return false;
      staged.set(
        offer.request_id,
        validateAppshotOffer(offer, binding, (p) =>
          readAppshotManifestInRuntime(p, root, process.getuid!()),
        ),
      );
      return true;
    },
    commit(commit) {
      const offer = staged.get(commit.request_id);
      assert(offer);
      staged.delete(commit.request_id);
      state = { ...state, draft: appendAppshot(state.draft, offer) };
      return count();
    },
    revoke(e) {
      staged.delete(e.requestID);
      state = revokeAppshots(state, new Set([e.requestID]));
    },
    disconnect() {
      staged.clear();
      state = revokeAppshots(state);
      return count();
    },
  },
});
function update(activity = client.activityNS) {
  client.updateDraft(activity, appshotCount(state), appshotCount(state) < 4);
}
async function connect() {
  await client.start();
  await until(() => client.state.connection === "connected", "connected");
  update();
}
async function capture() {
  const before = appshotCount(state);
  await native("capture");
  try {
    await until(() => appshotCount(state) === before + 1, "commit");
  } catch (e) {
    process.stderr.write(
      JSON.stringify({
        native: await native("inspect"),
        state,
        client: client.state,
      }) + "\n",
    );
    throw e;
  }
  await pause();
}
function remove() {
  for (const a of state.draft.attachments) client.release(a.requestId);
  state = { ...state, draft: emptyDraft(state.draft.nextNumber) };
  update();
}
function freeze() {
  const id = randomUUID();
  const s = appshotSubmission(state.draft, id);
  state = freezeAppshotSubmission(state, id);
  update();
  return {
    type: "message",
    text: s.text,
    appshots: s.appshots,
    submission_id: id,
    appshot_session_id: s.appshotSessionId,
    appshot_broker_id: s.appshotBrokerId,
  };
}
function settle(event: any) {
  if (event.type === "message_accepted" || event.status === "accepted")
    for (const a of state.pending?.draft.attachments ?? [])
      client.release(a.requestId);
  state = settleAppshotSubmission(
    state,
    event.submission_id,
    event.type === "message_accepted"
      ? "accepted"
      : event.type === "message_rejected"
        ? "rejected"
        : event.status,
  );
  update();
}
let backend: any, backendRead: any;
async function startBackend() {
  mkdirSync(sessionRoot + "/sessions", { recursive: true, mode: 0o700 });
  backend = spawn(
    "../.venv/bin/python",
    ["../tests/fixtures/appshot_backend.py", root, sessionRoot],
    {
      stdio: ["pipe", "pipe", "inherit"],
      env: {
        ...process.env,
        AGENT_SESSION_DIR: sessionRoot + "/sessions",
        ASTRA_SESSION_RECALL_DB: sessionRoot + "/recall.sqlite",
        ASTRA_CONTEXT_INDEX_SESSIONS_DB: sessionRoot + "/recall.sqlite",
      },
    },
  );
  backendRead = createInterface({ input: backend.stdout })[
    Symbol.asyncIterator
  ]();
}
async function py(message: any) {
  backend.stdin.write(JSON.stringify(message) + "\n");
  const r: any = await Promise.race([
    backendRead.next(),
    pause(10000).then(() => {
      throw Error("Python response deadline");
    }),
  ]);
  assert(!r.done);
  return JSON.parse(r.value);
}
async function stopBackend() {
  if (backend) {
    const child = backend;
    backend = undefined;
    child.stdin.end();
    await Promise.race([
      new Promise((r) => child.once("exit", r)),
      pause(1500).then(() => child.kill("SIGKILL")),
    ]);
  }
}
async function noArtifacts() {
  await until(() => true, "noop");
  for (let i = 0; i < 100; i++) {
    if ((await native("inspect")).files.length === 0) return;
    await pause(10);
  }
  throw Error("artifact cleanup");
}
let second: any;
async function run() {
  await connect();
  if (worker) {
    process.on("message", async (m: any) => {
      if (m.kind !== "command") return;
      try {
        if (m.op === "activity") {
          update(BigInt(m.value));
          assert.ok((await client.command("status", "")).ok, "broker received worker activity");
        }
        if (m.op === "remove") remove();
        if (m.op === "close") client.close();
        process.send!({
          kind: "result",
          id: m.id,
          value: { count: appshotCount(state) },
        });
      } catch (e) {
        process.send!({ kind: "result", id: m.id, error: String(e) });
      }
    });
    process.send!({ kind: "ready" });
    return;
  }
  // Status and heartbeat cannot create activity.
  assert.equal(client.activityNS, 0n);
  await client.command("status");
  await native("capture");
  await pause();
  assert.equal((await native("inspect")).captures, 0);
  client.recordInput();
  await pause();
  await capture();
  const screenshotOnly = process.env.ASTRA_APPSHOT_E2E_SCREENSHOT_ONLY === "1";
  assert.equal(state.draft.text, `[Appshot #1 · Fixture App · Fixture Window${screenshotOnly ? " · 仅截图" : ""}]`);
  const expected = JSON.parse(
    readFileSync(state.draft.attachments[0].manifestPath, "utf8"),
  ).png.sha256;
  await startBackend();
  let command = freeze();
  state.draft = reconcileAppshotInput(state.draft, "newer edit");
  let result = await py({ op: "submit", command, busy: true });
  assert.equal(result.event.code, "backend_busy");
  assert.equal(result.launches, 0);
  settle(result.event);
  assert(state.draft.text.endsWith("newer edit"));
  assert.equal((await native("inspect")).files.length, 3);
  command = freeze();
  result = await py({ op: "submit", command, budget: 1 });
  assert.equal(result.event.code, "context_budget_exceeded");
  settle(result.event);
  assert.equal(appshotCount(state), 1);
  command = freeze();
  result = await py({ op: "submit", command });
  assert.equal(result.event.type, "message_accepted");
  assert.equal(result.launches, 1);
  // Deliberately lose ACK; draft stays frozen while new captures share capacity.
  await capture();
  await capture();
  await capture();
  const pendingCap = await native("inspect");
  await native("capture");
  await pause();
  assert.equal((await native("inspect")).captures, pendingCap.captures);
  state.draft = reconcileAppshotInput(
    state.draft,
    state.draft.text + " after pending",
  );
  assert.equal(appshotCount(state), 4);
  assert.throws(() => freeze(), /submission_pending/);
  const duplicate = await py({ op: "submit", command });
  assert.equal(duplicate.launches, 1);
  assert.equal(duplicate.event.type, "message_accepted");
  assert.equal(duplicate.event.submission_id, command.submission_id);
  assert.equal(duplicate.event.replayed, true);
  const conflict = await py({
    op: "submit",
    command: { ...command, text: "changed" },
  });
  assert.equal(conflict.event.code, "submission_payload_conflict");
  result = await py({ op: "status", submission_id: command.submission_id });
  settle(result.event);
  assert.equal(appshotCount(state), 3);
  assert(state.draft.text.endsWith(" after pending"));
  let provider = await py({ op: "provider" });
  assert.deepEqual(provider.hashes, [expected]);
  assert.equal(provider.ax_count, 1);
  assert.deepEqual(provider.internal_types, [
    "text",
    "image_url",
    "appshot_context",
  ]);
  remove();
  await noArtifacts();
  provider = await py({ op: "provider" });
  assert.deepEqual(provider.hashes, [expected]);
  await capture();
  command = freeze();
  await stopBackend();
  await startBackend();
  result = await py({ op: "status", submission_id: command.submission_id });
  assert.equal(result.event.status, "unknown");
  settle(result.event);
  assert.equal(state.pending.status, "unknown");
  assert.equal(result.launches, 0);
  const oldEpoch = client.recipientBinding!.instance_id;
  await native("restart");
  await until(
    () => client.recipientBinding?.instance_id !== oldEpoch,
    "restart epoch",
  );
  await connect();
  assert.equal(state.pending.status, "unknown");
  assert.equal(appshotCount(state), 1);
  // Explicit discard resolves uncertainty locally; never automatic replay.
  state = { draft: state.draft };
  update();
  await pause();
  await noArtifacts();
  client.recordInput();
  await pause();
  for (let i = 0; i < 4; i++) await capture();
  const before = await native("inspect");
  await native("capture");
  await pause();
  const after = await native("inspect");
  assert.equal(after.captures, before.captures);
  assert.equal(after.files.length, 12);
  assert(after.outcomes.includes("attachment_limit_reached"));
  remove();
  await noArtifacts();
  // Lost offer ACK, expiry and late ACK cannot resurrect staged ownership.
  dropAck = true;
  await native("capture");
  await until(() => staged.size === 1, "stage");
  assert.equal(appshotCount(state), 0);
  await native("expire");
  await until(() => staged.size === 0, "revoke");
  dropAck = false;
  socket.write(lastAck);
  await pause();
  assert.equal(appshotCount(state), 0);
  await noArtifacts();
  // Lost commit expires the incorporation barrier and revokes through disconnect.
  dropCommit = true;
  await native("capture");
  await until(() => staged.size === 1, "lost commit stage");
  await pause();
  await native("expire");
  await until(() => staged.size === 0, "lost commit disconnect");
  dropCommit = false;
  await connect();
  update();
  client.recordInput();
  await pause();
  await noArtifacts();
  // Independent Node PID establishes the second real authenticated connection.
  second = fork(fileURLToPath(import.meta.url), [root, "--worker"], {
    stdio: ["ignore", "ignore", "inherit", "ipc"],
  });
  const calls = new Map();
  let ready = false;
  second.on("message", async (m: any) => {
    if (m.kind === "native") {
      const value = await native(
        m.op,
        m.op === "identity" ? { pid: m.pid } : {},
      );
      second.send({ kind: "native", id: m.id, value });
    }
    if (m.kind === "ready") ready = true;
    if (m.kind === "result") calls.get(m.id)?.(m);
  });
  await until(() => ready, "second client");
  let id = 0;
  const ask = (op: string, value?: string) =>
    new Promise<any>((resolve, reject) => {
      const key = ++id;
      const timer = setTimeout(
        () => reject(Error("second client deadline")),
        5000,
      );
      calls.set(key, (m: any) => {
        clearTimeout(timer);
        assert(!m.error, m.error);
        resolve(m.value);
      });
      second.send({ kind: "command", id: key, op, value });
    });
  const stamp = BigInt(
    (await native("identity", { pid: process.pid })).monotonic_ns,
  );
  update(stamp);
  assert.ok((await client.command("status", "")).ok, "broker received first client activity");
  await ask("activity", (stamp + 1n).toString());
  await native("capture");
  await until(async () => (await ask("inspect")).count === 1, "second client committed the capture");
  assert.equal(appshotCount(state), 0);
  assert.equal((await ask("inspect")).count, 1);
  await ask("remove");
  await noArtifacts();
  update(stamp + 2n);
  assert.ok((await client.command("status", "")).ok);
  await ask("activity", (stamp + 2n).toString());
  const tie = await native("inspect");
  await native("capture");
  await pause();
  const tied = await native("inspect");
  assert.equal(tied.captures, tie.captures);
  assert(tied.outcomes.includes("receiving_session_ambiguous"));
  await ask("close");
  second.kill();
  second = undefined;
  update(stamp + 3n);
  await pause();
  await capture();
  writeFileSync(root + "/unowned-fixture.txt", "preserve unrelated identity", {
    mode: 0o600,
  });
  client.close();
  await noArtifacts();
  assert.equal(
    readFileSync(root + "/unowned-fixture.txt", "utf8"),
    "preserve unrelated identity",
  );
  await stopBackend();
  process.stderr.write(
    "Appshot cross-process: success, busy/budget rejection, provider projection, lost ACK, duplicate/conflict, restart uncertainty, pending edits, four-item cap, lost offer/commit, two clients/tie, exact cleanup passed\n",
  );
}
run()
  .then(() => {
    if (!worker) process.exit(0);
  })
  .catch(async (error) => {
    process.stderr.write(String(error.stack ?? error) + "\n");
    client.close();
    second?.kill();
    await stopBackend();
    process.exit(1);
  });
process.on("SIGTERM", async () => {
  client.close();
  second?.kill();
  await stopBackend();
  process.exit(143);
});
process.on("exit", () => rmSync(sessionRoot, { recursive: true, force: true }));
process.on("disconnect", () => {
  client.close();
  process.exit(0);
});
