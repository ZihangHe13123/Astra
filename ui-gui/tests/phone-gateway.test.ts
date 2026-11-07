import { test } from "node:test";
import assert from "node:assert/strict";
import { request } from "node:http";
import { existsSync, readdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { PhoneClient } from "../src/phone/client.js";
import type { PhoneStreamPosition } from "../src/phone-protocol.js";
import { uploadName } from "../src/main/phone-gateway.js";
import { memory, pairedClient, setup, until } from "./phone-fixture.js";

function raw(port: number, path: string, headers: Record<string, string>, method = "GET"): Promise<number> {
  return new Promise((resolve, reject) => {
    const outgoing = request({ host: "127.0.0.1", port, path, method, headers }, response => { response.resume(); resolve(response.statusCode ?? 0); });
    outgoing.on("error", reject); outgoing.end(method === "POST" ? "{}" : undefined);
  });
}

/** Reads a stream into a list until aborted; returns the list and the abort. */
function listen(client: PhoneClient, token: string, from?: PhoneStreamPosition) {
  const seen: { data: any; id?: number }[] = [];
  const controller = new AbortController();
  const done = client.follow(token, from, (data, id) => seen.push({ data: JSON.parse(data), id }), controller.signal).catch(() => undefined);
  return { seen, stop: () => controller.abort(), done };
}

test("the phone gets the desktop's page with its bridge loaded first, and only the app's files", async () => {
  const { gateway, port, base } = await setup();
  try {
    const page = await fetch(`${base}/`);
    const html = await page.text();
    assert.match(page.headers.get("content-security-policy") ?? "", /frame-src 'none'/);
    assert.ok(html.indexOf('src="./phone/bridge.js"') < html.indexOf('src="./assets/index-1.js"'), "the bridge comes before the app");
    assert.match(html, /viewport-fit=cover/);
    assert.equal(html.match(/name="viewport"/g)?.length, 1);
    assert.equal(await (await fetch(`${base}/phone/bridge.js`)).text(), "bridge.js");
    assert.equal(await (await fetch(`${base}/sw.js`)).text(), "sw.js");
    assert.equal((await fetch(`${base}/manifest.webmanifest`)).headers.get("content-type"), "application/manifest+json");
    assert.equal((await fetch(`${base}/assets/index-1.js`)).headers.get("cache-control"), "public, max-age=31536000, immutable");
    for (const path of ["/..%2Fsecret.txt", "/phone/..%2F..%2Fsecret.txt", "/missing.js"]) assert.equal((await fetch(base + path)).status, 404, path);
    assert.equal(await raw(port, "/", { Host: "attacker.example" }), 421);
    assert.equal(await raw(port, "/", { Host: "mac.tail1234.ts.net" }), 200);
  } finally { await gateway.dispose(); }
});

test("calls need a token, go to the desktop with the phone's identity, and only the listed ones are taken", async () => {
  const { devices, gateway, port, base, calls } = await setup((_device, method, args) => {
    if (method === "close") throw new Error("该会话仍有任务或提醒，请先停止后再关闭。");
    return { echoed: args };
  });
  try {
    const anonymous = await fetch(`${base}/api/invoke`, { method: "POST", body: JSON.stringify({ method: "bootstrap" }) });
    assert.equal(anonymous.status, 401);
    const { client, record, token } = await pairedClient(devices, base);
    assert.deepEqual(await client.invoke(token, "send", ["rt-1", { type: "command", cmd: "/yolo on" }]), { echoed: ["rt-1", { type: "command", cmd: "/yolo on" }] });
    assert.deepEqual(calls.at(-1), { device: record.deviceId, method: "send", args: ["rt-1", { type: "command", cmd: "/yolo on" }] });
    await assert.rejects(client.invoke(token, "close", ["rt-1"]), /仍有任务/);
    await assert.rejects(client.invoke(token, "phone" as any, ["pair"]), /Unsupported call/);
    assert.equal(await raw(port, "/api/invoke", { Host: `127.0.0.1:${port}`, Origin: "https://attacker.example", Authorization: `Bearer ${token}` }, "POST"), 403);
  } finally { await gateway.dispose(); }
});

test("the stream says when it is ready, resumes where it stopped, and asks for a reload once it cannot", async () => {
  const { devices, gateway, base } = await setup();
  try {
    const { client, token } = await pairedClient(devices, base);
    assert.equal((await fetch(`${base}/api/events`)).status, 401);
    const first = listen(client, token);
    await until(() => first.seen.length === 1, "ready");
    const { instance } = first.seen[0].data;
    assert.ok(typeof instance === "string" && instance.length > 0, "the stream names the gateway's run");
    assert.deepEqual(first.seen[0].data, { ready: true, seq: 0, instance });
    gateway.publish({ appshot: { connection: "connected", enabled: true } });
    gateway.publish({ runtime: "rt-1", revision: 1, event: { type: "chunk", content: "一" } as any });
    gateway.publish({ refresh: true });
    await until(() => first.seen.length === 3, "events");
    assert.deepEqual(first.seen.slice(1).map(item => item.id), [1, 2]);
    assert.ok(!first.seen.some(item => "appshot" in item.data), "Appshot state stays in the window");
    first.stop(); await first.done;

    // Missed while away: replayed in order, then ready.
    gateway.publish({ runtime: "rt-1", revision: 2, event: { type: "chunk", content: "二" } as any });
    gateway.publish({ removed: "rt-1" });
    const resumed = listen(client, token, { seq: 2, instance });
    await until(() => resumed.seen.some(item => item.data.ready), "resumed");
    assert.deepEqual(resumed.seen.map(item => item.id ?? item.data), [3, 4, { ready: true, seq: 4, instance }]);
    resumed.stop(); await resumed.done;

    // More was missed than the desktop keeps.
    for (let i = 0; i < 4001; i++) gateway.publish({ refresh: true });
    const late = listen(client, token, { seq: 4, instance });
    await late.done;
    assert.deepEqual(late.seen.map(item => item.data), [{ reload: true }]);
  } finally { await gateway.dispose(); }
});

test("after the desktop restarts, a phone resuming from before reloads instead of missing what changed", async () => {
  const { devices, gateway, base, restarted } = await setup();
  let next: ReturnType<typeof restarted> | undefined;
  try {
    const { client, token } = await pairedClient(devices, base);
    const before = listen(client, token);
    await until(() => before.seen.length === 1, "ready");
    const { instance } = before.seen[0].data;
    for (let i = 0; i < 3; i++) gateway.publish({ refresh: true });
    await until(() => before.seen.length === 4, "events before the restart");
    before.stop(); await before.done;
    await gateway.dispose();

    next = restarted();
    const port = await next.start();
    const phone = new PhoneClient(memory(), `http://127.0.0.1:${port}`);
    // The new run has numbered fewer events than the phone saw, so the number alone looks caught up.
    next.publish({ removed: "rt-1" });
    const resumed = listen(phone, token, { seq: 3, instance });
    await until(() => resumed.seen.length > 0, "answer after the restart");
    resumed.stop(); await resumed.done;
    assert.deepEqual(resumed.seen.map(item => item.data), [{ reload: true }]);

    // A page loaded before streams named their run resumes with a bare number and reloads too.
    const opened = new AbortController();
    const old = await fetch(`http://127.0.0.1:${port}/api/events?after=1`, { headers: { Authorization: `Bearer ${token}` }, signal: opened.signal });
    const first = new TextDecoder().decode((await old.body!.getReader().read()).value);
    opened.abort();
    assert.equal(first, `data: ${JSON.stringify({ reload: true })}\n\n`);

    // The reloaded page starts fresh on the new run and follows it from there.
    const fresh = listen(phone, token);
    await until(() => fresh.seen.length === 1, "fresh ready");
    assert.notEqual(fresh.seen[0].data.instance, instance);
    assert.equal(fresh.seen[0].data.seq, 1);
    fresh.stop(); await fresh.done;
  } finally { await next?.dispose(); }
});

test("what is kept for a phone to catch up is bounded in bytes, and nothing is kept while the gateway is stopped", async () => {
  const { devices, gateway, base } = await setup();
  try {
    const { client, token } = await pairedClient(devices, base);
    const first = listen(client, token);
    await until(() => first.seen.length === 1, "ready");
    const { instance } = first.seen[0].data;
    first.stop(); await first.done;

    // Away while long replies came in: a few events, but far more bytes than one stream may hold unread.
    const long = "字".repeat(512 * 1024);
    for (let revision = 1; revision <= 40; revision++) gateway.publish({ runtime: "rt-1", revision, event: { type: "chunk", content: long } as any });
    const late = listen(client, token, { seq: 0, instance });
    await until(() => late.seen.length > 0, "answer to a long absence");
    late.stop(); await late.done;
    assert.deepEqual(late.seen.map(item => item.data), [{ reload: true }]);
    // Away for the last of them only: still caught up.
    const recent = listen(client, token, { seq: 39, instance });
    await until(() => recent.seen.some(item => item.data.ready), "caught up");
    assert.deepEqual(recent.seen.map(item => item.id ?? item.data), [40, { ready: true, seq: 40, instance }]);
    recent.stop(); await recent.done;

    // Turned off, the window's events go on, but none is numbered or kept for a phone.
    await gateway.stop();
    gateway.publish({ runtime: "rt-1", revision: 41, event: { type: "chunk", content: "while stopped" } as any });
    const port = await gateway.start();
    const phone = new PhoneClient(memory(), `http://127.0.0.1:${port}`);
    const resumed = listen(phone, token, { seq: 40, instance });
    await until(() => resumed.seen.length > 0, "answer after starting again");
    resumed.stop(); await resumed.done;
    assert.deepEqual(resumed.seen.map(item => item.data), [{ reload: true }]);
    const fresh = listen(phone, token);
    await until(() => fresh.seen.length === 1, "fresh ready");
    assert.notEqual(fresh.seen[0].data.instance, instance);
    assert.equal(fresh.seen[0].data.seq, 0, "nothing was numbered while stopped");
    fresh.stop(); await fresh.done;
  } finally { await gateway.dispose(); }
});

test("after a wipe no phone is shown what came before: a connected one reloads, and so does one resuming later", async () => {
  const { devices, gateway, base } = await setup();
  try {
    const away = await pairedClient(devices, base);
    const before = listen(away.client, away.token);
    await until(() => before.seen.length === 1, "ready");
    const { instance } = before.seen[0].data;
    before.stop(); await before.done;
    const open = await pairedClient(devices, base);
    const connected = listen(open.client, open.token);
    await until(() => connected.seen.length === 1, "second phone ready");
    gateway.publish({ runtime: "rt-1", revision: 1, event: { type: "chunk", content: "before the wipe" } as any });
    gateway.publish({ removed: "rt-1" });
    await until(() => connected.seen.length === 3, "events before the wipe");

    gateway.reset();
    await until(() => connected.seen.length === 4, "answer to the wipe");
    await connected.done;
    assert.deepEqual(connected.seen.at(-1)?.data, { reload: true });
    const resumed = listen(away.client, away.token, { seq: 0, instance });
    await until(() => resumed.seen.length > 0, "answer to a phone resuming from before the wipe");
    resumed.stop(); await resumed.done;
    assert.deepEqual(resumed.seen.map(item => item.data), [{ reload: true }]);
    // The reloaded page follows a new run that holds nothing from before.
    const fresh = listen(away.client, away.token);
    await until(() => fresh.seen.length === 1, "fresh ready");
    assert.notEqual(fresh.seen[0].data.instance, instance);
    assert.equal(fresh.seen[0].data.seq, 0);
    fresh.stop(); await fresh.done;
  } finally { await gateway.dispose(); }
});

test("removing the phone ends its stream at once, and the gateway listens on loopback only", async () => {
  const { devices, gateway, base, port } = await setup();
  try {
    const { client, record, token } = await pairedClient(devices, base);
    const stream = listen(client, token);
    await until(() => stream.seen.length === 1, "ready");
    devices.revoke(record.deviceId);
    await stream.done;
    await assert.rejects(client.signIn(record), /移除|没有配对/);
    await gateway.stop();
    await assert.rejects(fetch(`http://127.0.0.1:${port}/`));
    const again = await gateway.start();
    assert.equal((await fetch(`http://127.0.0.1:${again}/`)).status, 200);
  } finally { await gateway.dispose(); }
});

test("the phone client calls fetch the way Safari requires: never as a method of another object", async () => {
  const { devices, gateway, base } = await setup();
  const real = globalThis.fetch;
  // Safari throws when fetch runs with any receiver other than the window.
  globalThis.fetch = function (this: unknown, ...args: Parameters<typeof fetch>) {
    if (this !== undefined && this !== globalThis) throw new TypeError("Can only call Window.fetch on instances of Window");
    return real(...args);
  } as typeof fetch;
  try {
    const client = new PhoneClient(memory(), base);
    const record = await client.pair(devices.startPairing().code, "iPhone");
    assert.ok(await client.signIn(record));
  } finally { globalThis.fetch = real; await gateway.dispose(); }
});

test("speech handed to the phone is served to a signed-in phone only, by its id alone", async () => {
  const { devices, gateway, base, audio } = await setup();
  try {
    const id = "0123456789abcdef0123456789abcdef";
    writeFileSync(join(audio, `${id}.wav`), "RIFF-audio");
    writeFileSync(join(audio, "..", "secret.wav"), "not served");
    assert.equal((await fetch(`${base}/api/voice/${id}`)).status, 401);
    const { client, token } = await pairedClient(devices, base);
    assert.equal(Buffer.from(await client.voice(token, id)).toString(), "RIFF-audio");
    for (const bad of ["..%2Fsecret", "ABCDEF0123456789abcdef0123456789", "0123"]) {
      assert.equal((await fetch(`${base}/api/voice/${bad}`, { headers: { Authorization: `Bearer ${token}` } })).status, 404, bad);
    }
  } finally { await gateway.dispose(); }
});

/** A request sent in two halves: the second goes when `finish` is called. */
function halfSent(port: number, token: string, path: string, [first, second]: [Buffer | string, Buffer | string],
  headers: Record<string, string> = {}) {
  let finish = () => {};
  const answer = new Promise<{ status: number; body: any }>((resolve, reject) => {
    const outgoing = request({ host: "127.0.0.1", port, path, method: "POST",
      headers: { Host: `127.0.0.1:${port}`, Authorization: `Bearer ${token}`, ...headers } }, response => {
      let text = ""; response.setEncoding("utf8");
      response.on("data", chunk => { text += chunk; });
      response.on("end", () => resolve({ status: response.statusCode ?? 0, body: JSON.parse(text || "{}") }));
    });
    outgoing.on("error", reject);
    outgoing.write(first);
    finish = () => { outgoing.end(second); };
  });
  return { answer, finish: () => finish() };
}

const kept = (folder: string): string[] => existsSync(folder) ? readdirSync(folder, { recursive: true, encoding: "utf8" })
  .filter(name => statSync(join(folder, name)).isFile()) : [];

test("while a wipe runs, a phone's uploads and calls are refused and an upload still arriving is not kept", async () => {
  const { devices, gateway, port, base, uploads, uploadRoot } = await setup();
  let arriving: ReturnType<typeof halfSent> | undefined;
  try {
    const { client, token } = await pairedClient(devices, base);
    arriving = halfSent(port, token, "/api/upload", [Buffer.alloc(1024, 1), Buffer.alloc(1024, 2)], { "X-Astra-File-Name": "before.jpg" });
    // The gateway has begun to read it before the wipe starts.
    await new Promise(resolve => setTimeout(resolve, 200));
    await gateway.hold();
    arriving.finish();
    const late = await arriving.answer;
    assert.equal(late.status, 503);
    assert.match(late.body.error, /清除/);
    await assert.rejects(client.upload(token, new Blob([new Uint8Array([1])]), "during.jpg"), /清除/);
    await assert.rejects(client.invoke(token, "bootstrap", []), /清除/);
    assert.deepEqual(kept(uploadRoot), []);
    assert.deepEqual(uploads, []);

    // Once the wipe has ended, phones send again.
    gateway.reset(); gateway.release();
    const path = await client.upload(token, new Blob([new Uint8Array([2])]), "after.jpg");
    assert.ok(path.startsWith(uploadRoot), path);
    assert.equal(kept(uploadRoot).length, 1);
  } finally {
    // A half-sent upload would keep the gateway from closing.
    arriving?.finish(); await arriving?.answer.catch(() => undefined);
    await gateway.dispose();
  }
});

test("a wipe waits for a phone's call already running, and one whose body arrives after it starts never runs", async () => {
  let answer = (_value: unknown) => {};
  const { devices, gateway, port, base, calls } = await setup((_device, method) =>
    method === "jobs" ? new Promise(done => { answer = done; }) : { ok: true });
  let late: ReturnType<typeof halfSent> | undefined;
  try {
    const { client, token } = await pairedClient(devices, base);
    // A call the desktop is still answering when the wipe starts: the wipe waits for it.
    const running = client.invoke(token, "jobs", ["list"]);
    await until(() => calls.length === 1, "call handed to the desktop");
    let held = false;
    const holding = gateway.hold().then(() => { held = true; });
    await new Promise(resolve => setTimeout(resolve, 100));
    assert.equal(held, false, "the wipe waits while the call runs");
    answer({ jobs: [] });
    await holding;
    assert.deepEqual(await running, { jobs: [] });
    gateway.release();

    // A call begun before the wipe whose body comes in after it started is refused, never handed over.
    const body = JSON.stringify({ method: "memory", args: ["forget", "m1"] });
    late = halfSent(port, token, "/api/invoke", [body.slice(0, 10), body.slice(10)], { "Content-Type": "application/json" });
    await new Promise(resolve => setTimeout(resolve, 200));
    await gateway.hold();
    late.finish();
    const refused = await late.answer;
    assert.equal(refused.status, 503);
    assert.match(refused.body.error, /清除/);
    assert.deepEqual(calls.map(call => call.method), ["jobs"]);
    gateway.release();

    // A call that does not finish in time keeps the wipe from starting.
    const stuck = client.invoke(token, "jobs", ["list"]);
    await until(() => calls.length === 2, "second call handed to the desktop");
    await assert.rejects(gateway.hold(50), /还没做完/);
    gateway.release();
    answer({ jobs: [] });
    await stuck;
  } finally {
    // Calls left unanswered or half sent would keep the gateway from closing.
    answer({ jobs: [] }); late?.finish(); await late?.answer.catch(() => undefined);
    await gateway.dispose();
  }
});

test("a signed-in phone hands photos and files to the Mac, kept under the day's folder for this user alone", async () => {
  const { devices, gateway, base, uploads, uploadRoot } = await setup();
  try {
    const { client, record, token } = await pairedClient(devices, base);
    const path = await client.upload(token, new Blob([new Uint8Array([1, 2, 3, 4])]), "../照片\n 1.jpg");
    assert.ok(path.startsWith(uploadRoot), path);
    assert.match(path.split(/[\\/]/).pop()!, /^\d{6}-[0-9a-f]{6}-照片 1\.jpg$/);
    assert.deepEqual([...readFileSync(path)], [1, 2, 3, 4]);
    if (process.platform !== "win32") assert.equal(statSync(path).mode & 0o777, 0o600);
    assert.deepEqual(uploads, [{ device: record.deviceId, path, bytes: 4 }]);
    // Without a signed-in phone nothing is kept.
    const refused = await fetch(`${base}/api/upload`, { method: "POST", body: new Uint8Array([9]) });
    assert.equal(refused.status, 401);
    assert.equal(uploads.length, 1);
  } finally { await gateway.dispose(); }
});

test("a sent file's name keeps no folders or control characters, and stays short", () => {
  assert.equal(uploadName(encodeURIComponent("..\\..\\etc/passwd")), "passwd");
  assert.equal(uploadName("a%E5%9B%BE.png"), "a图.png");
  assert.equal(uploadName(undefined), "file");
  assert.equal(uploadName(".."), "file");
  const long = uploadName("x".repeat(300) + ".pdf");
  assert.equal(long.length, 120); assert.ok(long.endsWith(".pdf"));
});
