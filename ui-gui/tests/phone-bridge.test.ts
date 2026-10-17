import { test } from "node:test";
import assert from "node:assert/strict";
import { writeFileSync } from "node:fs";
import { join } from "node:path";
import { RemoteBridge, type BridgeEnvironment } from "../src/phone/bridge.js";
import { PhoneClient } from "../src/phone/client.js";
import type { DesktopEvent } from "../src/bridge.js";
import { memory, setup, until } from "./phone-fixture.js";

let bridgeOf: RemoteBridge | undefined;

function environment(client: PhoneClient, pairing: () => Promise<any>) {
  const state = { saved: "", reloads: 0, copied: [] as string[], opened: [] as string[], notices: [] as (string | undefined)[] };
  const env: BridgeEnvironment = {
    client, load: () => state.saved || null, save: text => { state.saved = text; },
    pair: async notice => { state.notices.push(notice); return pairing(); },
    reload: () => { state.reloads++; }, copy: async text => { state.copied.push(text); }, open: url => { state.opened.push(url); },
    sleep: ms => new Promise(resolve => setTimeout(resolve, Math.min(ms, 20))),
  };
  return { env, state };
}

test("the renderer's bridge on the phone: snapshot after the stream opens, desktop calls, phone-local preferences", async () => {
  const boot = { sessions: [{ id: "rt-1", revision: 3 }], active: "", workspace: "/work",
    preferences: { titles: { "work:s1": "发布检查" }, pinned: ["work:s1"], projects: ["/work"], workspaces: {} },
    appshot: { connection: "disconnected", enabled: false } };
  const { devices, gateway, base, calls } = await setup((_device, method) => method === "bootstrap" ? boot : null);
  try {
    const storage = memory();
    const client = new PhoneClient(storage, base);
    const { env, state } = environment(client, () => client.pair(devices.startPairing().code, "iPhone"));
    const bridge = new RemoteBridge(env); bridgeOf = bridge;
    const received: DesktopEvent[][] = [];
    bridge.onEvents(events => received.push(events));
    // First use pairs this phone, then signs in.
    const first = await bridge.bootstrap();
    assert.deepEqual(state.notices, [undefined]);
    assert.ok(storage.record);
    assert.deepEqual(first.sessions, boot.sessions);
    assert.equal(first.preferences.titles["work:s1"], "发布检查");
    assert.equal(first.preferences.theme, "system");

    // Theme and drafts stay on the phone; the desktop's names and pins are only read.
    const saved = await bridge.preferences({ theme: "dark", drafts: { "work:s1": "草稿" }, titles: { "work:s1": "改掉" } });
    assert.equal(saved.theme, "dark");
    assert.equal(saved.titles["work:s1"], "发布检查");
    assert.deepEqual(JSON.parse(state.saved), { theme: "dark", drafts: { "work:s1": "草稿" } });
    assert.ok(!calls.some(call => call.method === "preferences"));

    gateway.publish({ runtime: "rt-1", revision: 4, event: { type: "chunk", content: "好" } as any });
    gateway.publish({ runtime: "rt-1", revision: 5, event: { type: "done" } as any });
    await until(() => received.flat().length === 2, "events");
    assert.deepEqual(received.flat().map(event => (event as any).revision), [4, 5]);

    await bridge.send("rt-1", { type: "tool_approval_response", request_id: "r1", decision: "once" });
    await bridge.create({ session: "s1" });
    await bridge.query("history", { name: "s1", mode: "work" });
    await bridge.sessionAction("delete", { name: "s1", mode: "work" });
    assert.deepEqual(calls.slice(1).map(call => [call.method, call.args]), [
      ["send", ["rt-1", { type: "tool_approval_response", request_id: "r1", decision: "once" }]],
      ["create", [{ session: "s1" }]],
      ["query", ["history", { name: "s1", mode: "work" }, null]],
      ["deleteSession", [{ name: "s1", mode: "work" }]],
    ]);
  } finally { bridgeOf?.dispose(); await gateway.dispose(); }
});

test("Mac-only actions answer on the phone instead of reaching the desktop", async () => {
  const { devices, gateway, base, calls } = await setup();
  try {
    const client = new PhoneClient(memory(), base);
    const { env, state } = environment(client, () => client.pair(devices.startPairing().code, "iPhone"));
    const bridge = new RemoteBridge(env); bridgeOf = bridge;
    assert.equal(bridge.remote, true);
    await assert.rejects(bridge.choose(), /手机上还不能选择文件/);
    await assert.rejects(bridge.file(), /只在电脑上可用/);
    await assert.rejects(bridge.appshot(), /窗口捕获只在电脑上可用/);
    await assert.rejects(bridge.phone(), /只能在电脑上管理/);
    await assert.rejects(bridge.sessionAction("export", { name: "s1", mode: "work" }), /导出会话只在电脑上可用/);
    assert.equal(await bridge.clipboardImage(), null);
    assert.deepEqual(bridge.droppedPaths(), []);
    await bridge.copyText("复制到手机");
    await bridge.openExternal("https://example.com/x");
    await bridge.openExternal("file:///etc/passwd");
    assert.deepEqual([state.copied, state.opened], [["复制到手机"], ["https://example.com/x"]]);
    assert.equal(calls.length, 0);
  } finally { bridgeOf?.dispose(); await gateway.dispose(); }
});

test("after a phone is removed on the desktop, its next call asks to pair again and then goes through", async () => {
  const { devices, gateway, base, calls } = await setup((_device, method) => method === "bootstrap"
    ? { sessions: [], active: "", workspace: "", preferences: {}, appshot: { connection: "disconnected", enabled: false } } : "ok");
  try {
    const storage = memory();
    const client = new PhoneClient(storage, base);
    const { env, state } = environment(client, () => client.pair(devices.startPairing().code, "iPhone"));
    const bridge = new RemoteBridge(env); bridgeOf = bridge;
    await bridge.bootstrap();
    const firstDevice = storage.record!.deviceId;
    devices.revoke(firstDevice);
    // The token died with the device: the call signs in again, finds the phone removed, and pairs anew.
    assert.equal(await bridge.close("rt-1"), "ok");
    assert.deepEqual(state.notices, [undefined, "这台手机已在电脑上移除，请重新配对。"]);
    assert.notEqual(storage.record!.deviceId, firstDevice);
    assert.equal(calls.at(-1)?.device, storage.record!.deviceId);
  } finally { bridgeOf?.dispose(); await gateway.dispose(); }
});

test("a dropped stream resumes after the last event it saw, and a reload request reloads the page", async () => {
  const afters: (number | undefined)[] = [];
  const scripts = [
    ['{"ready":true,"seq":5}', '{"runtime":"rt-1","revision":1,"event":{"type":"done"}}#6', '{"refresh":true}#7'],
    ['{"reload":true}'],
  ];
  const client = {
    device: async () => ({ deviceId: "d1", name: "iPhone", privateKey: {} as CryptoKey }),
    signIn: async () => "token-token-token-token",
    follow: async (_token: string, after: number | undefined, onEvent: (data: string, id?: number) => void) => {
      afters.push(after);
      for (const line of scripts[afters.length - 1] ?? []) {
        const [data, id] = line.split("#");
        onEvent(data, id === undefined ? undefined : Number(id));
      }
      if (afters.length > 2) await new Promise(() => {});
    },
  } as unknown as PhoneClient;
  const { env, state } = environment(client, async () => { throw new Error("not used"); });
  const bridge = new RemoteBridge(env);
  const received: DesktopEvent[] = [];
  bridge.onEvents(events => received.push(...events));
  try {
    await until(() => state.reloads === 1, "reload requested");
    assert.deepEqual(afters.slice(0, 2), [undefined, 7]);
    assert.equal(received.length, 2);
  } finally { bridge.dispose(); }
});

test("speech the desktop routes to this phone is fetched through the gateway and handed to the phone's speaker", async () => {
  const { devices, gateway, base, audio } = await setup((_device, method) => method === "bootstrap"
    ? { sessions: [], active: "", workspace: "", preferences: {}, appshot: { connection: "disconnected", enabled: false } } : null);
  const id = "fedcba9876543210fedcba9876543210";
  writeFileSync(join(audio, `${id}.wav`), "RIFF-unit");
  const played: { event: any; data: string }[] = [];
  try {
    const client = new PhoneClient(memory(), base);
    const { env } = environment(client, () => client.pair(devices.startPairing().code, "iPhone"));
    // Recorded in the order the bridge hands them over; the audio arrives later.
    env.voice = (event, load) => {
      const entry = { event, data: "" }; played.push(entry);
      if (event.id) void load(event.id).then(data => { entry.data = Buffer.from(data).toString(); });
    };
    const bridge = new RemoteBridge(env); bridgeOf = bridge;
    const seen: DesktopEvent[] = [];
    bridge.onEvents(events => seen.push(...events));
    await bridge.bootstrap();
    gateway.publish({ runtime: "rt-1", revision: 1, event: { type: "voice_audio", id, seconds: 1.2, at: Date.now() / 1000 } as any });
    gateway.publish({ runtime: "rt-1", revision: 2, event: { type: "voice_audio", stop: true, at: Date.now() / 1000 } as any });
    await until(() => played.length === 2 && played[0].data !== "", "speech handed over");
    assert.deepEqual(played.map(item => [item.event.id ?? "stop", item.data]), [[id, "RIFF-unit"], ["stop", ""]]);
    // The window's own handlers still get the events, as they do on the desktop.
    await until(() => seen.length === 2, "events");
  } finally { bridgeOf?.dispose(); await gateway.dispose(); }
});
