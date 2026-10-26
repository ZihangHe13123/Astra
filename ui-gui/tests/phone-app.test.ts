import { test } from "node:test";
import assert from "node:assert/strict";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { parseEventStream } from "../src/phone-protocol.js";
import { PhoneNotificationsView, PhoneSettingsView } from "../src/renderer/phone-settings.js";
import type { PhoneStatus } from "../src/phone-protocol.js";


test("the stream is read in whatever pieces the network delivers, with ids, and heartbeats are skipped", () => {
  const whole = 'data: {"ready":true,"seq":0}\n\n: ping\n\nid: 7\ndata: {"refresh":true}\n\n';
  let buffer = "", events: { id?: number; data: string }[] = [];
  for (const piece of [whole.slice(0, 7), whole.slice(7, 40), whole.slice(40)]) {
    const parsed = parseEventStream(buffer + piece);
    buffer = parsed.rest; events = [...events, ...parsed.events];
  }
  assert.deepEqual(events, [{ data: '{"ready":true,"seq":0}' }, { id: 7, data: '{"refresh":true}' }]);
  assert.equal(buffer, "");
});

test("settings show what the desktop reports: the switch, the serve command, the code and the paired phones", () => {
  const status: PhoneStatus = { enabled: true, port: 8930, running: true, url: "https://mac.tail1234.ts.net",
    devices: [{ id: "d1", name: "测试 iPhone", createdAt: "2026-10-17T04:00:00.000Z" }],
    pairing: { code: "ABCD2345", expiresAt: 1_000_000 + 125_000 } };
  const html = renderToStaticMarkup(React.createElement(PhoneSettingsView, { status, run: () => {}, now: 1_000_000 }));
  assert.match(html, /tailscale serve --bg 8930/);
  assert.match(html, /https:\/\/mac\.tail1234\.ts\.net/);
  assert.match(html, /ABCD 2345/);
  assert.match(html, /2:05 内有效/);
  assert.match(html, /测试 iPhone/);
  assert.match(html, /最近连接 从未/);
  const expired = renderToStaticMarkup(React.createElement(PhoneSettingsView, { status, run: () => {}, now: 1_000_000 + 200_000 }));
  assert.match(expired, />添加手机</);
  const off = renderToStaticMarkup(React.createElement(PhoneSettingsView, { status: { ...status, enabled: false }, run: () => {} }));
  assert.doesNotMatch(off, /添加手机|tailscale serve|测试 iPhone/);
  const busy = renderToStaticMarkup(React.createElement(PhoneSettingsView, { status: { ...status, pairing: undefined, error: "端口 8930 已被占用。" }, run: () => {} }));
  assert.match(busy, /role="alert">端口 8930 已被占用。/);
  assert.doesNotMatch(html, /通知已开启/);
  const notified = renderToStaticMarkup(React.createElement(PhoneSettingsView, { status: { ...status, devices: [{ ...status.devices[0], notifications: true }] }, run: () => {} }));
  assert.match(notified, /最近连接 从未 · 通知已开启/);
});

test("on the phone, notifications show as a switch, or say why they cannot be turned on there", () => {
  const view = (state: any, busy = false) => renderToStaticMarkup(React.createElement(PhoneNotificationsView, { state, busy, run: () => {} }));
  assert.match(view({ state: "on" }), /checked=""[^>]*\/>手机通知/);
  assert.match(view({ state: "on" }), /批准、回答问题时马上通知/);
  assert.doesNotMatch(view({ state: "off" }), /checked/);
  assert.match(view({ state: "off" }, true), /disabled=""/);
  const unsupported = view({ state: "unsupported", reason: "先添加到主屏幕" });
  assert.match(unsupported, /先添加到主屏幕/);
  assert.doesNotMatch(unsupported, /checkbox/);
  assert.match(view({ state: "denied", reason: "到 iPhone 的设置里打开" }), /role="alert">到 iPhone 的设置里打开/);
  assert.match(view(undefined), /正在读取/);
});

test("the Mac's tailnet name comes from whichever Tailscale install answers, and only a ts.net name is shown", async () => {
  const { tailscaleName } = await import("../src/main/phone-tailscale.js");
  const tried: string[] = [];
  const answer = (name: string) => async (file: string) => {
    tried.push(file);
    if (!file.startsWith("/opt/homebrew")) throw new Error("not here");
    return JSON.stringify({ Self: { DNSName: name } });
  };
  assert.equal(await tailscaleName(answer("Studio-Mac.tail1234.ts.net.")), "studio-mac.tail1234.ts.net");
  assert.deepEqual(tried.slice(0, 2), ["/Applications/Tailscale.app/Contents/MacOS/Tailscale", "/opt/homebrew/bin/tailscale"]);
  assert.equal(await tailscaleName(answer("evil.example.com.")), undefined);
  assert.equal(await tailscaleName(async () => { throw new Error("absent"); }), undefined);
});
