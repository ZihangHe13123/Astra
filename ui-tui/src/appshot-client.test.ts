import test from "node:test";
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { AppshotClient } from "./appshot-client.js";
import {
  AppshotFrameDecoder,
  encodeAppshotMessage,
} from "./appshot-protocol.js";
class FakeSocket extends EventEmitter {
  sent: any[] = [];
  nonce = "nonce";
  broker = "broker";
  write(data: Buffer) {
    for (const m of new AppshotFrameDecoder().feed(data)) {
      this.sent.push(m);
      if (m.type === "hello")
        queueMicrotask(() =>
          this.emit(
            "data",
            encodeAppshotMessage({
              type: "hello_ack",
              version: 1,
              instance_id: this.broker,
              broker_nonce: this.nonce,
              session_id: m.session_id,
            }),
          ),
        );
    }
    return true;
  }
  destroy() {
    this.emit("close");
    return this;
  }
}
function fixture(consumer?: any) {
  const socket = new FakeSocket();
  let launches = 0,
    connects = 0;
  const deps: any = {
    currentUID: () => 501,
    identity: async () => ({
      pid: process.pid,
      uid: 501,
      process_start: "1",
      monotonic_ns: "100",
    }),
    discover: async () => ({
      instance_id: "broker",
      broker_nonce: "nonce",
      socketPath: "/fixture",
    }),
    connect: async () => {
      connects++;
      return socket;
    },
    launch: async () => {
      launches++;
    },
    now: () => 10n,
  };
  const client = new AppshotClient({
    platform: "darwin",
    deps,
    consumer,
    heartbeatMS: 10000,
    retryMS: 1,
  });
  return {
    client,
    socket,
    deps,
    get launches() {
      return launches;
    },
    get connects() {
      return connects;
    },
  };
}
/** A broker that accepts hello, then drops the client on its first state report. */
function droppingSocket() {
  const socket = new FakeSocket();
  const write = socket.write.bind(socket);
  socket.write = (data: Buffer) => {
    const result = write(data);
    if (socket.sent.at(-1)?.type === "client_state") queueMicrotask(() => socket.destroy());
    return result;
  };
  return socket;
}
async function eventually(predicate: () => boolean, label: string) {
  for (let i = 0; i < 400 && !predicate(); i++) await new Promise((r) => setTimeout(r, 5));
  assert.ok(predicate(), label);
}
test("injected Darwin transport authenticates when the host has no POSIX uid API", async () => {
  const getuid = process.getuid;
  try {
    Object.defineProperty(process, "getuid", {
      configurable: true,
      value: undefined,
    });
    const f = fixture();
    await f.client.start();
    assert.equal(f.client.state.connection, "connected");
    await f.client.close();
  } finally {
    Object.defineProperty(process, "getuid", {
      configurable: true,
      value: getuid,
    });
  }
});

test("authenticates existing broker; starts with zero activity and refuses offers without consumer", async () => {
  const f = fixture();
  await f.client.start();
  assert.equal(f.client.state.connection, "connected");
  assert.equal(f.socket.sent[1].activity_ns, "0");
  assert.equal(f.socket.sent[1].can_accept, false);
  f.socket.emit(
    "data",
    encodeAppshotMessage({
      type: "attach_offer",
      version: 1,
      request_id: "offer",
      broker_id: "broker",
      session_id: f.socket.sent[0].session_id,
      manifest_path: "/fixture/manifest",
    }),
  );
  assert.equal(f.socket.sent.at(-1).accepted, false);
  f.client.recordInput();
  assert.equal(f.socket.sent.at(-1).activity_ns, "100");
  await f.client.close();
  assert.equal(f.launches, 0);
});
test("launches once after missing broker and bounded retries reconnect", async () => {
  const f = fixture();
  let count = 0;
  f.deps.discover = async () => {
    if (count++ === 0) throw Error();
    return {
      instance_id: "broker",
      broker_nonce: "nonce",
      socketPath: "/fixture",
    };
  };
  await f.client.start();
  assert.equal(f.launches, 1);
  await f.client.close();
});
test("nonmacOS never invokes dependencies", async () => {
  const client = new AppshotClient({
    platform: "win32",
    deps: new Proxy(
      {},
      {
        get() {
          throw Error("side effect");
        },
      },
    ) as any,
  });
  await client.start();
  await client.close();
});
test("fourth offer excludes staged count, commit confirms matching request with current count", async () => {
  let count = 3;
  const revoked: any[] = [];
  const f = fixture({
    stage: () => true,
    commit: () => {
      count++;
      return { appshotCount: count, canAccept: count < 4 };
    },
    revoke: (x: any) => revoked.push(x),
    disconnect: (x: any) => revoked.push(x),
  });
  await f.client.start();
  f.client.updateDraft(0n, 3, true);
  const binding = {
    version: 1,
    request_id: "fourth",
    broker_id: "broker",
    session_id: f.socket.sent[0].session_id,
    manifest_path: "/fixture/m",
  };
  f.socket.emit(
    "data",
    encodeAppshotMessage({ type: "attach_offer", ...binding }),
  );
  assert.equal(f.socket.sent.at(-1).type, "attach_ack");
  assert.equal(
    f.socket.sent.filter((m) => m.type === "client_state").at(-1).appshot_count,
    3,
  );
  f.socket.emit(
    "data",
    encodeAppshotMessage({ type: "attach_commit", ...binding }),
  );
  assert.equal(f.socket.sent.at(-1).request_id, "fourth");
  assert.equal(f.socket.sent.at(-1).appshot_count, 4);
  assert.equal(f.socket.sent.at(-1).can_accept, false);
  await f.client.close();
  assert.equal(revoked.at(-1).pending, "unknown");
});
test("invalid parent identity fails before discovery or launch", async () => {
  const f = fixture();
  f.deps.identity = async () => ({
    pid: 1,
    uid: 501,
    process_start: "01",
    monotonic_ns: "100",
  });
  await f.client.start();
  assert.equal(f.connects, 0);
  assert.equal(f.launches, 0);
  await f.client.close();
});
test("close cancels retry and never launches afterward", async () => {
  const f = fixture();
  f.deps.discover = async () => {
    throw Error();
  };
  const start = f.client.start();
  await new Promise((r) => setTimeout(r, 1));
  await f.client.close();
  await start;
  const launches = f.launches;
  await new Promise((r) => setTimeout(r, 10));
  assert.equal(f.launches, launches);
  assert.equal(f.client.state.connection, "disconnected");
});
test("old epoch frame disconnects and preserves pending uncertainty callback", async () => {
  const disconnected: any[] = [];
  const f = fixture({
    stage: () => false,
    commit: () => ({ appshotCount: 0, canAccept: false }),
    revoke: () => {},
    disconnect: (v: any) => disconnected.push(v),
  });
  await f.client.start();
  f.socket.emit(
    "data",
    encodeAppshotMessage({
      type: "attach_revoke",
      version: 1,
      request_id: "old",
      broker_id: "old-broker",
      session_id: f.socket.sent[0].session_id,
      reason: "capture_timeout",
    }),
  );
  assert.equal(disconnected[0].pending, "unknown");
  await f.client.close();
});
test(
  "real private Unix socket authenticates coalesced frames; descriptor rejects replaced socket and duplicates",
  { skip: process.platform === "win32" },
  async () => {
    const {
      mkdtempSync,
      chmodSync,
      writeFileSync,
      lstatSync,
      rmSync,
      realpathSync,
    } = await import("node:fs");
    const { tmpdir } = await import("node:os");
    const { join } = await import("node:path");
    const { createServer, createConnection } = await import("node:net");
    const { randomUUID } = await import("node:crypto");
    const { readAppshotDescriptor } = await import("./appshot-client.js");
    const root = mkdtempSync(join(realpathSync(tmpdir()), "appshot-ts-"));
    chmodSync(root, 0o700);
    const path = join(root, "broker.sock");
    const peers: any[] = [];
    const received: any[] = [];
    const broker = randomUUID(),
      nonce = randomUUID();
    const server = createServer((socket) => {
      peers.push(socket);
      const decoder = new AppshotFrameDecoder();
      socket.on("data", (chunk) => {
        for (const m of decoder.feed(
          typeof chunk === "string" ? Buffer.from(chunk) : chunk,
        )) {
          received.push(m);
          if (m.type === "hello")
            socket.write(
              Buffer.concat([
                encodeAppshotMessage({
                  type: "hello_ack",
                  version: 1,
                  instance_id: broker,
                  broker_nonce: nonce,
                  session_id: m.session_id,
                }),
                encodeAppshotMessage({
                  type: "status",
                  version: 1,
                  request_id: "initial",
                  broker_id: broker,
                  session_id: m.session_id,
                  appshot_count: 0,
                  can_accept: false,
                  enabled: true,
                  chord: "ctrl+shift+s",
                  registration: "registered",
                  connected_tuis: 1,
                  permission: "ready",
                }),
              ]),
            );
        }
      });
    });
    await new Promise<void>((resolve) => server.listen(path, resolve));
    chmodSync(path, 0o600);
    const stat = lstatSync(path, { bigint: true });
    const descriptor = {
      broker_nonce: nonce,
      entries: [
        {
          device: stat.dev.toString(),
          inode: stat.ino.toString(),
          kind: "socket",
          name: "broker.sock",
        },
      ],
      instance_id: broker,
      pid: process.pid,
      process_start: "1",
      schema_version: 1,
      socket_name: "broker.sock",
      uid: process.getuid!(),
    };
    const file = join(root, "broker.json");
    const raw = JSON.stringify(descriptor);
    writeFileSync(file, raw, { mode: 0o600 });
    const f = fixture({
      stage: (_offer: any, binding: any) => {
        assert.equal(binding.process_start, "1");
        return true;
      },
      commit: () => ({ appshotCount: 4, canAccept: false }),
      revoke: () => {},
      disconnect: () => {},
    });
    f.deps.discover = async () => ({
      ...readAppshotDescriptor(root, process.getuid!()),
      socketPath: path,
    });
    f.deps.connect = async () =>
      new Promise((resolve) => {
        const socket = createConnection({ path }, () => resolve(socket));
      });
    try {
      await f.client.start();
      assert.equal(f.client.state.permission, "ready");
      assert.equal(f.launches, 0);
      const until = async (predicate: () => boolean) => {
        for (let i = 0; i < 100 && !predicate(); i++)
          await new Promise((r) => setTimeout(r, 1));
        assert.ok(predicate());
      };
      f.client.updateDraft(0n, 3, true);
      await until(() =>
        received.some(
          (m) => m.type === "client_state" && m.appshot_count === 3,
        ),
      );
      const binding = {
        version: 1,
        request_id: "socket-fourth",
        broker_id: broker,
        session_id: f.client.recipientBinding!.session_id,
        manifest_path: "/fixture/fourth",
      };
      peers[0].write(
        encodeAppshotMessage({ type: "attach_offer", ...binding }),
      );
      await until(() =>
        received.some(
          (m) => m.type === "attach_ack" && m.request_id === "socket-fourth",
        ),
      );
      assert.equal(
        received.filter((m) => m.type === "client_state").at(-1).appshot_count,
        3,
      );
      assert.equal(
        received.find((m) => m.type === "attach_ack").accepted,
        true,
      );
      peers[0].write(
        encodeAppshotMessage({ type: "attach_commit", ...binding }),
      );
      await until(() =>
        received.some(
          (m) => m.type === "client_state" && m.request_id === "socket-fourth",
        ),
      );
      const confirmation = received.find(
        (m) => m.type === "client_state" && m.request_id === "socket-fourth",
      );
      assert.equal(confirmation.appshot_count, 4);
      assert.equal(confirmation.can_accept, false);
      writeFileSync(
        file,
        raw.replace(
          '"schema_version":1',
          '"schema_version":1,"schema_version":1',
        ),
      );
      assert.throws(() => readAppshotDescriptor(root, process.getuid!()));
      writeFileSync(file, raw);
      chmodSync(path, 0o666);
      assert.throws(() => readAppshotDescriptor(root, process.getuid!()));
    } finally {
      await f.client.close();
      for (const s of peers) s.destroy();
      await new Promise<void>((resolve) => server.close(() => resolve()));
      rmSync(root, { recursive: true, force: true });
    }
  },
);

test("reconnect authenticates fresh epoch and invalidates only unsent custody", async () => {
  let disconnected = 0;
  const f = fixture({
    stage: () => false,
    commit: () => ({ appshotCount: 0, canAccept: false }),
    revoke: () => {},
    disconnect: () => {
      disconnected++;
      return { appshotCount: 2, canAccept: false };
    },
  });
  await f.client.start();
  const next = new FakeSocket();
  next.broker = "new-broker";
  next.nonce = "new-nonce";
  f.deps.discover = async () => ({
    instance_id: "new-broker",
    broker_nonce: "new-nonce",
    socketPath: "/fixture",
  });
  f.deps.connect = async () => next;
  f.socket.destroy();
  await new Promise((r) => setTimeout(r, 5));
  assert.equal(f.client.state.connection, "connected");
  assert.equal(disconnected, 1);
  assert.equal(next.sent[1].appshot_count, 2);
  assert.equal(next.sent[1].can_accept, false);
  await f.client.close();
});
test("start is idempotent while connected", async () => {
  const f = fixture();
  await f.client.start();
  await f.client.start();
  assert.equal(f.connects, 1);
  await f.client.close();
});

test("repeated post-handshake disconnects stop and coalesce notices; input retries", async () => {
  const f = fixture();
  let connections = 0;
  const notices: string[] = [];
  f.client.on("notice", code => notices.push(code));
  f.deps.connect = async () => {
    connections++;
    const socket = new FakeSocket();
    const write = socket.write.bind(socket);
    socket.write = (data: Buffer) => {
      const result = write(data);
      if (socket.sent.at(-1)?.type === "client_state") queueMicrotask(() => socket.destroy());
      return result;
    };
    return socket;
  };
  try {
    await f.client.start();
    await new Promise(r => setTimeout(r, 120));
    assert.equal(connections, 6);
    assert.deepEqual(notices, ["recipient_disconnected", "broker_unavailable"]);
    const recovered = new FakeSocket();
    f.deps.connect = async () => { connections++; return recovered; };
    f.client.recordInput();
    await new Promise(r => setTimeout(r, 10));
    assert.equal(connections, 7);
    assert.equal(f.client.state.connection, "connected");
  } finally { await f.client.close(); }
});

test("an unreachable broker announces the paused retry once, however many keys retry it", async () => {
  const f = fixture();
  const notices: string[] = [];
  f.client.on("notice", code => notices.push(code));
  f.deps.discover = async () => { throw Error("no broker"); };
  try {
    await f.client.start();
    assert.deepEqual(notices, ["broker_unavailable"]);
    for (let key = 1; key <= 10; key++) {
      f.client.recordInput();
      assert.equal(f.client.state.connection, "connecting", "the key itself retries");
      await f.client.start(); // joins the retry that key started
      assert.equal(f.launches, 1 + key);
    }
    assert.equal(f.client.state.connection, "disconnected");
    assert.deepEqual(notices, ["broker_unavailable"]);
  } finally { await f.client.close(); }
});

test("a broker that went away is retried by a key; a restored connection re-arms the paused notice", async () => {
  const f = fixture();
  const notices: string[] = [];
  f.client.on("notice", code => notices.push(code));
  let reachable = true, socket = f.socket;
  const discover = f.deps.discover;
  f.deps.discover = async () => { if (!reachable) throw Error("no broker"); return discover(); };
  f.deps.connect = async () => (socket = new FakeSocket());
  const lose = () => { reachable = false; socket.destroy(); };
  const paused = () => notices.filter(code => code === "broker_unavailable").length;
  try {
    await f.client.start();
    assert.equal(f.client.state.connection, "connected");
    lose();
    await eventually(() => paused() === 1, "automatic retries give up");
    assert.deepEqual(notices, ["recipient_disconnected", "broker_unavailable"]);
    for (let key = 0; key < 5; key++) {
      f.client.recordInput();
      assert.equal(f.client.state.connection, "connecting", "the key itself retries");
      await f.client.start();
    }
    assert.equal(paused(), 1);
    reachable = true;
    f.client.recordInput();
    await f.client.start();
    assert.equal(f.client.state.connection, "connected");
    lose();
    await eventually(() => paused() === 2, "the restored connection re-armed the notice");
  } finally { await f.client.close(); }
});

test("keys pressed while the broker keeps dropping connections do not multiply the paused notice", async () => {
  const f = fixture();
  const notices: string[] = [];
  let connections = 0;
  f.client.on("notice", code => notices.push(code));
  f.deps.connect = async () => { connections++; return droppingSocket(); };
  const paused = () => notices.filter(code => code === "broker_unavailable").length;
  try {
    await f.client.start();
    for (let key = 0; key < 10; key++) {
      f.client.recordInput();
      assert.equal(f.client.state.connection, "connecting", "the key itself retries");
      await f.client.start();
    }
    assert.equal(connections, 11);
    await eventually(() => paused() === 1, "the retry budget runs out");
    // Reconnects left waiting by the earlier drops must neither connect again nor pause again.
    await new Promise(r => setTimeout(r, 80));
    assert.equal(paused(), 1);
    assert.equal(connections, 16);
  } finally { await f.client.close(); }
});

test("giving up also cancels a reconnect that is still waiting", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const settled = () => new Promise<void>(r => setImmediate(r));
  const f = fixture();
  const notices: string[] = [];
  let connections = 0;
  f.client.on("notice", code => notices.push(code));
  f.deps.connect = async () => { connections++; return droppingSocket(); };
  try {
    await f.client.start();
    for (const backoff of [1, 2, 4, 8]) {
      await settled();
      t.mock.timers.tick(backoff);
      await settled();
    }
    assert.equal(connections, 5); // the last budgeted reconnect now waits 16 ms
    await f.client.start(); // an explicit start, such as the desktop's connection check
    assert.equal(connections, 6);
    assert.deepEqual(notices, ["recipient_disconnected", "broker_unavailable"]);
    await settled();
    t.mock.timers.tick(16);
    await settled();
    assert.equal(connections, 6);
    assert.deepEqual(notices, ["recipient_disconnected", "broker_unavailable"]);
  } finally { await f.client.close(); }
});

test("a missing optional helper is reported once and keys do not retry it; start() looks again", async () => {
  const f = fixture();
  const notices: string[] = [];
  let installed = false, probes = 0, attempts = 0;
  f.client.on("notice", code => notices.push(code));
  f.client.on("change", state => { if (state.connection === "connecting") attempts++; });
  f.deps.helperInstalled = () => { probes++; return installed; };
  try {
    await f.client.start();
    for (let key = 0; key < 10; key++) {
      f.client.recordInput();
      await new Promise(r => setTimeout(r, 1));
    }
    assert.deepEqual(notices, ["helper_unavailable"]);
    assert.deepEqual([probes, attempts, f.launches, f.connects], [1, 0, 0, 0]);
    assert.equal(f.client.state.connection, "disconnected");
    await f.client.start(); // an explicit start, such as the desktop's connection check
    assert.equal(probes, 2);
    assert.deepEqual(notices, ["helper_unavailable"]);
    installed = true;
    await f.client.start();
    assert.equal(f.client.state.connection, "connected");
    assert.deepEqual(notices, ["helper_unavailable"]);
  } finally { await f.client.close(); }
});

test("a helper that disappears after the client used it is still retried by keys", async () => {
  const f = fixture();
  const notices: string[] = [];
  f.client.on("notice", code => notices.push(code));
  let installed = true, socket = f.socket;
  f.deps.helperInstalled = () => installed;
  const identity = f.deps.identity;
  f.deps.identity = async () => { if (!installed) throw Error("helper gone"); return identity(); };
  f.deps.connect = async () => (socket = new FakeSocket());
  try {
    await f.client.start();
    assert.equal(f.client.state.connection, "connected");
    installed = false; // an uninstall or a rebuild takes the helper and its broker away
    socket.destroy();
    await eventually(() => notices.includes("broker_unavailable"), "automatic retries give up");
    f.client.recordInput();
    assert.equal(f.client.state.connection, "connecting", "the key itself retries");
    await f.client.start();
    installed = true;
    f.client.recordInput();
    await f.client.start();
    assert.equal(f.client.state.connection, "connected");
    assert.deepEqual(notices, ["recipient_disconnected", "broker_unavailable"]);
  } finally { await f.client.close(); }
});

test("production Darwin dependencies without an installed helper stay idle under input", async () => {
  const { productionAppshotDependencies } = await import("./appshot-client.js");
  const fs = await import("node:fs");
  const os = await import("node:os");
  const path = await import("node:path");
  // A complete installation root that simply has no .astra/bin helper bundle.
  const root = fs.mkdtempSync(path.join(fs.realpathSync(os.tmpdir()), "appshot-no-helper-"));
  const client = new AppshotClient({
    platform: "darwin",
    deps: productionAppshotDependencies({ AGENT_PROJECT_ROOT: root }),
    retryMS: 1,
  });
  const notices: string[] = [];
  let attempts = 0;
  client.on("notice", code => notices.push(code));
  client.on("change", state => { if (state.connection === "connecting") attempts++; });
  try {
    await client.start();
    for (let key = 0; key < 10; key++) {
      client.recordInput();
      await new Promise(r => setTimeout(r, 1));
    }
    assert.deepEqual(notices, ["helper_unavailable"]);
    assert.equal(attempts, 0);
  } finally {
    await client.close();
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("recalibration discards a future activity value from the previous connection", async () => {
  const f = fixture();
  await f.client.start();
  f.client.recordInput();
  assert.equal(f.client.activityNS, 100n);
  const next = new FakeSocket();
  f.deps.identity = async () => ({ pid: process.pid, uid: 501, process_start: "1", monotonic_ns: "50" });
  f.deps.connect = async () => next;
  f.socket.destroy();
  await new Promise(r => setTimeout(r, 10));
  assert.equal(f.client.activityNS, 50n);
  assert.equal(next.sent[1].activity_ns, "50");
  await f.client.close();
});
test("output backpressure disconnects without throwing from actual input callback", async () => {
  const f = fixture();
  await f.client.start();
  Object.assign(f.socket, { writableLength: 300000 });
  assert.doesNotThrow(() => f.client.recordInput());
  await f.client.close();
});

test("release backpressure disconnects without throwing from draft removal", async () => {
  const f = fixture({
    stage: () => true,
    commit: () => ({ appshotCount: 1, canAccept: true }),
    revoke: () => {},
    disconnect: () => {},
  });
  await f.client.start();
  f.client.updateDraft(0n, 0, true);
  const binding = {
    version: 1,
    request_id: "committed",
    broker_id: "broker",
    session_id: f.socket.sent[0].session_id,
    manifest_path: "/fixture/committed",
  };
  f.socket.emit(
    "data",
    encodeAppshotMessage({ type: "attach_offer", ...binding }),
  );
  f.socket.emit(
    "data",
    encodeAppshotMessage({ type: "attach_commit", ...binding }),
  );
  Object.assign(f.socket, { writableLength: 300000 });
  assert.doesNotThrow(() => f.client.release("committed"));
  assert.equal(f.client.state.connection, "disconnected");
  await f.client.close();
});

test("status frame itself resolves status command without command_result", async () => {
  const f = fixture();
  await f.client.start();
  const promise = f.client.command("status", "");
  const request = f.socket.sent.at(-1);
  f.socket.emit(
    "data",
    encodeAppshotMessage({
      type: "status",
      version: 1,
      request_id: request.request_id,
      broker_id: "broker",
      session_id: request.session_id,
      appshot_count: 0,
      can_accept: false,
      enabled: true,
      chord: "ctrl+shift+s",
      registration: "registered",
      connected_tuis: 1,
      permission: "ready",
    }),
  );
  assert.equal((await promise).ok, true);
  await f.client.close();
});
test("mismatched private nonce never authenticates; election launches at most once", async () => {
  const f = fixture();
  f.deps.connect = async () => {
    const s = new FakeSocket();
    s.nonce = "wrong";
    return s;
  };
  await f.client.start();
  assert.equal(f.client.state.connection, "disconnected");
  assert.equal(f.launches, 1);
  await f.client.close();
});
test("heartbeat preserves zero activity until real input", async () => {
  const f = fixture();
  const client = new AppshotClient({
    platform: "darwin",
    deps: f.deps,
    heartbeatMS: 2,
  });
  await client.start();
  await new Promise((resolve) => setTimeout(resolve, 10));
  const states = f.socket.sent.filter((m) => m.type === "client_state");
  assert.ok(states.length > 1);
  assert.ok(states.every((m) => m.activity_ns === "0"));
  await client.close();
});

test("close aborts in-flight identity without starting a daemon", async () => {
  const f = fixture();
  f.deps.identity = (signal: AbortSignal) =>
    new Promise((_, reject) =>
      signal.addEventListener("abort", () => reject(Error()), { once: true }),
    );
  const starting = f.client.start();
  await f.client.close();
  await starting;
  assert.equal(f.launches, 0);
  assert.equal(f.connects, 0);
});
test(
  "helper resolution uses configured absolute project root and rejects symlink overrides",
  { skip: process.platform === "win32" },
  async () => {
    const { resolveAppshotHelper } = await import("./appshot-client.js");
    const fs = await import("node:fs");
    const os = await import("node:os");
    const path = await import("node:path");
    const root = fs.mkdtempSync(
      path.join(fs.realpathSync(os.tmpdir()), "appshot-helper-"),
    );
    const helper = path.join(
      root,
      ".astra/bin/AstraMacComputerHelper.app/Contents/MacOS/AstraMacComputerHelper",
    );
    try {
      fs.mkdirSync(path.dirname(helper), { recursive: true });
      fs.writeFileSync(helper, "fixture", { mode: 0o700 });
      assert.equal(resolveAppshotHelper({ AGENT_PROJECT_ROOT: root }), helper);
      assert.throws(() => resolveAppshotHelper({}));
      assert.throws(() =>
        resolveAppshotHelper({ ASTRA_COMPUTER_HELPER_PATH: "relative/helper" }),
      );
      const link = path.join(root, "link");
      fs.symlinkSync(helper, link);
      assert.throws(() =>
        resolveAppshotHelper({ ASTRA_COMPUTER_HELPER_PATH: link }),
      );
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  },
);

test("concurrent election loser reconnects without a second daemon launch", async () => {
  const f = fixture();
  let discoveries = 0,
    launches = 0;
  f.deps.discover = async () => {
    if (discoveries++ === 0) throw Error();
    return {
      instance_id: "broker",
      broker_nonce: "nonce",
      socketPath: "/fixture",
    };
  };
  f.deps.launch = async () => {
    launches++;
    throw Error("other TUI won election");
  };
  await f.client.start();
  assert.equal(f.client.state.connection, "connected");
  assert.equal(launches, 1);
  await f.client.close();
});
