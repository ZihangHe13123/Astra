import { test } from "node:test";
import assert from "node:assert/strict";
import { request } from "node:http";
import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { initialSession, type SessionState } from "@astra/ui-core/session-state";
import { PhoneDevices } from "../src/main/phone-devices.js";
import { PhoneGateway } from "../src/main/phone-gateway.js";
import { PhoneClient, PhoneUnpairedError, type DeviceStorage, type PhoneDeviceRecord } from "../src/phone/client.js";
import { applyStream, emptySessions } from "../src/phone/state.js";
import type { PhoneStreamEvent } from "../src/phone-protocol.js";

function memory(): DeviceStorage & { record?: PhoneDeviceRecord } {
  const storage: DeviceStorage & { record?: PhoneDeviceRecord } = {
    load: async () => storage.record, save: async record => { storage.record = record; }, clear: async () => { storage.record = undefined; },
  };
  return storage;
}

async function setup() {
  const folder = mkdtempSync(join(tmpdir(), "astra-phone-gateway-"));
  const site = join(folder, "phone"); mkdirSync(join(site, "assets"), { recursive: true });
  writeFileSync(join(site, "phone.html"), "<!doctype html><title>Astra</title>");
  writeFileSync(join(site, "assets", "phone-1.js"), "console.log(1)");
  writeFileSync(join(folder, "secret.txt"), "not served");
  const devices = new PhoneDevices(join(folder, "gui", "phone-devices.json"));
  devices.setEnabled(true);
  const session: SessionState = { ...initialSession("rt-1", "/work"), session: "session_1", status: "ready",
    messages: [{ id: "m1", role: "user", content: "检查一下测试" }] };
  const gateway = new PhoneGateway(devices, { staticRoot: site, snapshot: () => ({ sessions: [session], active: "rt-1" }), port: 0 });
  const port = await gateway.start();
  return { devices, gateway, port, base: `http://127.0.0.1:${port}` };
}

function raw(port: number, path: string, headers: Record<string, string>, method = "GET"): Promise<number> {
  return new Promise((resolve, reject) => {
    const outgoing = request({ host: "127.0.0.1", port, path, method, headers }, response => { response.resume(); resolve(response.statusCode ?? 0); });
    outgoing.on("error", reject); outgoing.end(method === "POST" ? "{}" : undefined);
  });
}

async function until(check: () => boolean, label: string) {
  const deadline = Date.now() + 3000;
  while (!check()) { if (Date.now() > deadline) throw new Error(`timed out: ${label}`); await new Promise(r => setTimeout(r, 10)); }
}

test("a paired phone follows the desktop's sessions live, and removing it ends the stream", async () => {
  const { devices, gateway, base } = await setup();
  const storage = memory();
  const client = new PhoneClient(storage, base);
  try {
    await assert.rejects(client.pair("WRONG123", "iPhone"), /没有进行中的配对/);
    const { code } = devices.startPairing();
    const record = await client.pair(code, "iPhone");
    assert.equal(storage.record?.deviceId, record.deviceId);
    assert.equal(devices.list()[0].name, "iPhone");

    const token = await client.signIn(record);
    let state = emptySessions;
    const seen: PhoneStreamEvent[] = [];
    const controller = new AbortController();
    const following = client.follow(token, message => { seen.push(message); state = applyStream(state, message); }, controller.signal);
    await until(() => state.order.length === 1, "snapshot");
    assert.equal(state.active, "rt-1");
    assert.equal(state.sessions["rt-1"].messages[0].content, "检查一下测试");

    gateway.publish({ appshot: { connection: "connected", enabled: true } });
    gateway.publish({ runtime: "rt-1", revision: 1, event: { type: "chunk", content: "好的" } as any });
    // The same revision again (a replay) changes nothing, as in the window.
    gateway.publish({ runtime: "rt-1", revision: 1, event: { type: "chunk", content: "好的" } as any });
    gateway.publish({ runtime: "rt-1", revision: 2, event: { type: "done" } as any });
    await until(() => state.sessions["rt-1"].revision === 2, "live events");
    assert.deepEqual(state.sessions["rt-1"].messages.map(message => message.content), ["检查一下测试", "好的"]);
    assert.ok(!seen.some(message => JSON.stringify(message).includes("appshot")), "window-only events stay in the window");

    devices.revoke(record.deviceId);
    await following;
    await assert.rejects(client.signIn(record), PhoneUnpairedError);
    controller.abort();
  } finally { await gateway.dispose(); }
});

test("nothing is served without a valid token, to another host or origin, or outside the phone app", async () => {
  const { gateway, port, base } = await setup();
  try {
    assert.equal((await fetch(`${base}/api/events`)).status, 401);
    assert.equal((await fetch(`${base}/api/events`, { headers: { Authorization: "Bearer not-a-real-token-at-all" } })).status, 401);
    // A web page that resolved its own name to this machine is not answered.
    assert.equal(await raw(port, "/", { Host: "attacker.example" }), 421);
    assert.equal(await raw(port, "/api/challenge", { Host: `127.0.0.1:${port}`, Origin: "https://attacker.example", "Content-Type": "application/json" }, "POST"), 403);
    assert.equal(await raw(port, "/", { Host: "mac.tail1234.ts.net" }), 200);
    assert.equal(await raw(port, "/api/challenge", { Host: "mac.tail1234.ts.net", Origin: "https://mac.tail1234.ts.net", "Content-Type": "application/json" }, "POST"), 403,
      "same origin reaches the API, which refuses an unknown device");

    const page = await fetch(`${base}/`);
    assert.equal(page.status, 200);
    assert.match(page.headers.get("content-security-policy") ?? "", /default-src 'self'/);
    assert.match(await page.text(), /<title>Astra<\/title>/);
    assert.equal((await fetch(`${base}/assets/phone-1.js`)).headers.get("cache-control"), "public, max-age=31536000, immutable");
    assert.equal((await fetch(`${base}/..%2Fsecret.txt`)).status, 404);
    assert.equal((await fetch(`${base}/missing.js`)).status, 404);
    assert.equal((await fetch(`${base}/api/pair`, { method: "POST", body: "x".repeat(20_000) })).status, 400);
  } finally { await gateway.dispose(); }
});

test("the gateway listens on loopback only and can be stopped and started again", async () => {
  const { gateway, port } = await setup();
  try {
    assert.equal(gateway.running, true);
    await gateway.stop();
    assert.equal(gateway.running, false);
    await assert.rejects(fetch(`http://127.0.0.1:${port}/`));
    const again = await gateway.start();
    assert.ok(again > 0);
    assert.equal((await fetch(`http://127.0.0.1:${again}/`)).status, 200);
  } finally { await gateway.dispose(); }
});
