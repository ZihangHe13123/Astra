import { test } from "node:test";
import assert from "node:assert/strict";
import { createDecipheriv, createECDH, createPublicKey, hkdfSync, verify } from "node:crypto";
import { mkdtempSync, readFileSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { runInNewContext } from "node:vm";
import { PhonePush, phonePushRequest, type PushNotice } from "../src/main/phone-push.js";
import { PhoneAuthError } from "../src/main/phone-devices.js";
import { VapidKey, encryptPayload, webPushSender, type PushSubscription } from "../src/main/web-push.js";
import { RemoteBridge, type BridgeEnvironment } from "../src/phone/bridge.js";
import { PhoneClient } from "../src/phone/client.js";
import { memory, setup, until } from "./phone-fixture.js";

const b64 = (text: string) => Buffer.from(text, "base64url");

/** A phone's browser: its own key pair and auth secret, and how it reads a push it receives (RFC 8291). */
function phoneBrowser(endpoint = "https://web.push.apple.com/QGuH1") {
  const ecdh = createECDH("prime256v1");
  ecdh.generateKeys();
  const auth = Buffer.from("0123456789abcdef");
  const subscription: PushSubscription = { endpoint, keys: { p256dh: ecdh.getPublicKey().toString("base64url"), auth: auth.toString("base64url") } };
  const read = (body: Buffer) => {
    const salt = body.subarray(0, 16), length = body.readUInt8(20), sender = body.subarray(21, 21 + length);
    const ikm = Buffer.from(hkdfSync("sha256", ecdh.computeSecret(sender), auth,
      Buffer.concat([Buffer.from("WebPush: info\0"), ecdh.getPublicKey(), sender]), 32));
    const key = Buffer.from(hkdfSync("sha256", ikm, salt, Buffer.from("Content-Encoding: aes128gcm\0"), 16));
    const nonce = Buffer.from(hkdfSync("sha256", ikm, salt, Buffer.from("Content-Encoding: nonce\0"), 12));
    const sealed = body.subarray(21 + length);
    const decipher = createDecipheriv("aes-128-gcm", key, nonce);
    decipher.setAuthTag(sealed.subarray(-16));
    const plain = Buffer.concat([decipher.update(sealed.subarray(0, -16)), decipher.final()]);
    assert.equal(plain.at(-1), 2, "one record, marked last");
    return JSON.parse(plain.subarray(0, -1).toString("utf8"));
  };
  return { subscription, read };
}

test("the payload is encrypted exactly as RFC 8291's worked example", () => {
  const sender = createECDH("prime256v1");
  sender.setPrivateKey(b64("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"));
  const receiver = createECDH("prime256v1");
  receiver.setPrivateKey(b64("q1dXpw3UpT5VOmu_cf_v6ih07Aems3njxI-JWgLcM94"));
  const body = encryptPayload({ endpoint: "https://push.example.net/push/JzLQ3raZJfFBR0aqvOMsLrt54w4rJUsV",
    keys: { p256dh: receiver.getPublicKey().toString("base64url"), auth: "BTBZMqHH6r4Tts7J_aSIgg" } },
  Buffer.from("When I grow up, I want to be a watermelon"), { salt: b64("DGv6ra1nlYgDCS1FRnbzlw"), sender });
  assert.equal(body.toString("base64url"), "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN");
  assert.throws(() => encryptPayload({ endpoint: "https://x", keys: { p256dh: "AAAA", auth: "BTBZMqHH6r4Tts7J_aSIgg" } }, Buffer.from("x")));
});

test("a push is signed with the desktop's own key, kept for this user, and only the phone can read it", async () => {
  const path = join(mkdtempSync(join(tmpdir(), "astra-push-")), "gui", "phone-push-key.json");
  const vapid = new VapidKey(path);
  assert.equal(new VapidKey(path).publicKey, vapid.publicKey, "made once, then reused");
  assert.equal(b64(vapid.publicKey).length, 65);
  if (process.platform !== "win32") assert.equal(statSync(path).mode & 0o777, 0o600);  // Windows has no owner-only mode bits

  const phone = phoneBrowser();
  const sent: { url: string; headers: Record<string, string>; body: Buffer }[] = [];
  const fetcher = (async (url: string, init: { headers: Record<string, string>; body: Uint8Array }) => {
    sent.push({ url, headers: init.headers, body: Buffer.from(init.body) });
    return { status: 201 };
  }) as unknown as typeof fetch;
  const send = webPushSender(() => vapid, () => "https://mac.example.ts.net", fetcher);
  assert.equal(await send(phone.subscription, { title: "需要批准", body: "git push", runtime: "rt-1" }, { urgent: true }), 201);

  const [request] = sent;
  assert.equal(request.url, phone.subscription.endpoint);
  assert.deepEqual([request.headers.TTL, request.headers.Urgency, request.headers["Content-Encoding"]], ["600", "high", "aes128gcm"]);
  assert.deepEqual(phone.read(request.body), { title: "需要批准", body: "git push", runtime: "rt-1" });
  assert.doesNotMatch(request.body.toString("latin1"), /git push/, "the push service sees ciphertext only");
  // RFC 8292: a JWT for the push service's origin, verified with the key the phone subscribed with.
  const [, token, key] = /^vapid t=([^,]+), k=(.+)$/.exec(request.headers.Authorization) ?? [];
  assert.equal(key, vapid.publicKey);
  const [header, claims, signature] = token.split(".");
  const point = b64(key);
  const publicKey = createPublicKey({ format: "jwk", key: { kty: "EC", crv: "P-256",
    x: point.subarray(1, 33).toString("base64url"), y: point.subarray(33).toString("base64url") } });
  assert.ok(verify("sha256", Buffer.from(`${header}.${claims}`), { key: publicKey, dsaEncoding: "ieee-p1363" }, b64(signature)));
  const body = JSON.parse(b64(claims).toString());
  assert.equal(body.aud, "https://web.push.apple.com");
  assert.equal(body.sub, "https://mac.example.ts.net");
  assert.ok(body.exp > Date.now() / 1000 && body.exp <= Date.now() / 1000 + 24 * 3600);

  await send(phone.subscription, { title: "完成", body: "", runtime: "" }, { urgent: false });
  assert.deepEqual([sent[1].headers.TTL, sent[1].headers.Urgency], ["3600", "normal"]);
});

function pushes(live = new Set<string>(), status = 201) {
  const phones = new Map([["a", phoneBrowser()], ["b", phoneBrowser()]]);
  const delivered: { device: string; payload: any; urgent: boolean }[] = [];
  const dropped: string[] = [];
  const targets = () => [...phones].map(([id, phone]) => ({ id, push: phone.subscription }));
  const push = new PhonePush({
    targets, live: id => live.has(id), drop: id => { dropped.push(id); phones.delete(id); }, windowMs: 30,
    send: async (subscription, payload, { urgent }) => {
      delivered.push({ device: [...phones].find(([, phone]) => phone.subscription === subscription)![0], payload, urgent });
      return status;
    },
  });
  return { push, delivered, dropped, phones };
}

test("an approval or a question rings at once; anything else waits and comes as one notification", async () => {
  const { push, delivered } = pushes(new Set(["b"]));
  const notice = (title: string, urgent = false): PushNotice => ({ title, body: `${title}的内容`, runtime: "rt-1", urgent });
  try {
    push.notify(notice("需要批准", true));
    await until(() => delivered.length === 1, "urgent push");
    assert.deepEqual(delivered[0], { device: "a", payload: { title: "需要批准", body: "需要批准的内容", runtime: "rt-1" }, urgent: true });

    push.notify(notice("任务完成"));
    push.notify({ ...notice("新闻摘要"), runtime: "rt-2" });
    assert.equal(delivered.length, 1, "held for the window");
    await until(() => delivered.length === 2, "batched push");
    assert.deepEqual(delivered[1], { device: "a", payload: { title: "Astra：2 条新消息", body: "任务完成；新闻摘要", runtime: "rt-2" }, urgent: false });
    // "b" has the app open and hears it there.
    assert.ok(delivered.every(item => item.device === "a"));
  } finally { push.dispose(); }
});

test("a phone whose subscription the push service no longer knows stops being sent to", async () => {
  for (const status of [404, 410]) {
    const { push, delivered, dropped } = pushes(new Set(), status);
    push.notify({ title: "问题", body: "要继续吗", urgent: true });
    await until(() => dropped.length === 2, `dropped after ${status}`);
    push.notify({ title: "问题", body: "要继续吗", urgent: true });
    await new Promise(resolve => setTimeout(resolve, 20));
    assert.equal(delivered.length, 2);
    push.dispose();
  }
  const { push, dropped } = pushes(new Set(), 500);
  push.notify({ title: "问题", body: "要继续吗", urgent: true });
  await new Promise(resolve => setTimeout(resolve, 20));
  assert.deepEqual(dropped, [], "a failing push service is not a gone subscription");
  push.dispose();
});

type PushEnv = NonNullable<BridgeEnvironment["push"]>;
function phoneEnvironment(client: PhoneClient, pair: () => Promise<any>, push?: Partial<PushEnv>, opening?: string) {
  const subscribed: string[] = [];
  const browser = phoneBrowser();
  const env: BridgeEnvironment = {
    client, load: () => null, save: () => undefined, pair: async () => pair(), reload: () => undefined,
    copy: async () => undefined, open: () => undefined, sleep: ms => new Promise(resolve => setTimeout(resolve, Math.min(ms, 20))),
    push: push && {
      unsupported: () => "", permission: async () => "granted", unsubscribe: async () => undefined,
      subscribe: async key => { subscribed.push(key); return browser.subscription; }, ...push,
    },
    opening,
  };
  return { env, subscribed, browser };
}

test("the phone turns its notifications on and off itself, and the desktop lists it as notified", async () => {
  const folder = mkdtempSync(join(tmpdir(), "astra-push-"));
  const vapid = new VapidKey(join(folder, "phone-push-key.json"));
  let devicesOf: any;
  const { devices, gateway, base } = await setup((device, method, args) => method === "push" ? phonePushRequest(devicesOf, () => vapid, device, args[0], args[1]) : null);
  devicesOf = devices;
  const bridges: RemoteBridge[] = [];
  try {
    const client = new PhoneClient(memory(), base);
    const { env, subscribed, browser } = phoneEnvironment(client, () => client.pair(devices.startPairing().code, "iPhone"), {});
    const bridge = new RemoteBridge(env); bridges.push(bridge);
    assert.deepEqual(await bridge.notifications("status"), { state: "off" });
    assert.deepEqual(await bridge.notifications("enable"), { state: "on" });
    // It subscribed with this desktop's key, and the desktop keeps where to send.
    assert.deepEqual(subscribed, [vapid.publicKey]);
    const [device] = devices.list();
    assert.equal(device.notifications, true);
    assert.deepEqual(devices.pushTargets(), [{ id: device.id, push: browser.subscription }]);
    assert.deepEqual(await bridge.notifications("status"), { state: "on" });
    assert.deepEqual(await bridge.notifications("disable"), { state: "off" });
    assert.deepEqual(devices.pushTargets(), []);
    assert.equal(devices.list()[0].notifications, undefined);

    // Turned on again, then the phone is removed: nothing is sent to it any more.
    await bridge.notifications("enable");
    devices.revoke(device.id);
    assert.deepEqual(devices.pushTargets(), []);

    // Only a real subscription is kept: HTTPS, and keys of the right size.
    assert.throws(() => phonePushRequest(devices, () => vapid, "x", "subscribe", { ...browser.subscription, endpoint: "http://push.example/1" }), PhoneAuthError);
    assert.throws(() => phonePushRequest(devices, () => vapid, "x", "subscribe", { endpoint: browser.subscription.endpoint, keys: { p256dh: "short", auth: "x" } }), PhoneAuthError);
    assert.throws(() => phonePushRequest(devices, () => vapid, "x", "forward"), /Unsupported/);
  } finally { for (const bridge of bridges) bridge.dispose(); await gateway.dispose(); }
});

test("without the Home Screen app, or with notifications refused in iOS, the phone says so and subscribes nothing", async () => {
  const { devices, gateway, base, calls } = await setup();
  try {
    const client = new PhoneClient(memory(), base);
    const pair = () => client.pair(devices.startPairing().code, "iPhone");
    const reason = "先在 Safari 里把这个页面“添加到主屏幕”";
    const opened = phoneEnvironment(client, pair, { unsupported: () => reason });
    assert.deepEqual(await new RemoteBridge(opened.env).notifications("enable"), { state: "unsupported", reason });
    assert.equal((await new RemoteBridge(phoneEnvironment(client, pair).env).notifications("status")).state, "unsupported");
    const refused = phoneEnvironment(client, pair, { permission: async () => "denied" });
    assert.equal((await new RemoteBridge(refused.env).notifications("enable")).state, "denied");
    assert.deepEqual(refused.subscribed, []);
    assert.ok(!calls.some(call => call.method === "push"));
  } finally { await gateway.dispose(); }
});

test("a notification opens the app on its conversation while that conversation is still open", async () => {
  const boot = { sessions: [{ id: "rt-1", revision: 1 }, { id: "rt-2", revision: 1 }], active: "rt-1", workspace: "", preferences: {},
    appshot: { connection: "disconnected", enabled: false } };
  const { devices, gateway, base } = await setup((_device, method) => method === "bootstrap" ? boot : null);
  const bridges: RemoteBridge[] = [];
  try {
    const client = new PhoneClient(memory(), base);
    const pair = () => client.pair(devices.startPairing().code, "iPhone");
    for (const [opening, active] of [["rt-2", "rt-2"], ["rt-gone", "rt-1"], [undefined, "rt-1"]] as const) {
      const bridge = new RemoteBridge(phoneEnvironment(client, pair, undefined, opening).env); bridges.push(bridge);
      assert.equal((await bridge.bootstrap()).active, active, String(opening));
    }
  } finally { for (const bridge of bridges) bridge.dispose(); await gateway.dispose(); }
});

test("a hidden app closes its stream, so the desktop notifies; shown again, it follows from where it was", async () => {
  const boot = { sessions: [], active: "", workspace: "", preferences: {}, appshot: { connection: "disconnected", enabled: false } };
  const { devices, gateway, base } = await setup((_device, method) => method === "bootstrap" ? boot : null);
  let bridge: RemoteBridge | undefined;
  try {
    const storage = memory();
    const client = new PhoneClient(storage, base);
    bridge = new RemoteBridge(phoneEnvironment(client, () => client.pair(devices.startPairing().code, "iPhone")).env);
    const received: unknown[] = [];
    bridge.onEvents(events => received.push(...events));
    await bridge.bootstrap();
    const device = storage.record!.deviceId;
    await until(() => gateway.isLive(device), "stream open");
    bridge.hide();
    await until(() => !gateway.isLive(device), "stream closed while hidden");
    gateway.publish({ runtime: "rt-1", revision: 2, event: { type: "chunk", content: "后台时写的" } as any });
    await new Promise(resolve => setTimeout(resolve, 50));
    assert.equal(gateway.isLive(device), false, "stays closed until shown");
    bridge.wake();
    await until(() => received.length === 1, "replayed on wake");
    assert.equal((received[0] as any).event.content, "后台时写的");
    assert.ok(gateway.isLive(device));
  } finally { bridge?.dispose(); await gateway.dispose(); }
});

test("the phone's service worker shows what the push carries and opens its conversation", async () => {
  const listeners = new Map<string, (event: any) => void>();
  const shown: { title: string; options: any }[] = [];
  const navigated: string[] = [];
  let windows: any[] = [];
  const opened: string[] = [];
  const self = {
    addEventListener: (type: string, listener: (event: any) => void) => listeners.set(type, listener),
    registration: { scope: "https://mac.example.ts.net/", showNotification: async (title: string, options: any) => { shown.push({ title, options }); } },
    clients: { matchAll: async () => windows, openWindow: async (url: string) => { opened.push(url); }, claim: async () => undefined },
    skipWaiting: () => undefined,
  };
  runInNewContext(readFileSync(new URL("../phone-public/sw.js", import.meta.url), "utf8"), { self, URL });
  const dispatch = async (type: string, event: any) => {
    let waited: Promise<unknown> = Promise.resolve();
    listeners.get(type)!({ ...event, waitUntil: (promise: Promise<unknown>) => { waited = promise; } });
    await waited;
  };

  await dispatch("push", { data: { json: () => ({ title: "Astra 需要你批准", body: "git push", runtime: "rt 1" }) } });
  await dispatch("push", { data: { json: () => { throw new Error("not JSON"); } } });
  assert.deepEqual(shown.map(item => [item.title, item.options.body, item.options.data.url]),
    [["Astra 需要你批准", "git push", "./?runtime=rt%201"], ["Astra", "", "./"]]);

  const click = (options: any) => ({ notification: { data: options.data, close: () => undefined } });
  await dispatch("notificationclick", click(shown[0].options));
  assert.deepEqual(opened, ["https://mac.example.ts.net/?runtime=rt%201"], "no window open: a new one");
  let focused = 0;
  windows = [{ navigate: async (url: string) => { navigated.push(url); return { focus: async () => { focused++; } }; } }];
  await dispatch("notificationclick", click(shown[1].options));
  assert.deepEqual([navigated, focused], [["https://mac.example.ts.net/"], 1], "the open app is brought forward");
});
