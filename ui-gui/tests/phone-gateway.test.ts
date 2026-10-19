import { test } from "node:test";
import assert from "node:assert/strict";
import { request } from "node:http";
import { writeFileSync } from "node:fs";
import { join } from "node:path";
import { PhoneClient } from "../src/phone/client.js";
import type { PhoneStreamPosition } from "../src/phone-protocol.js";
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
