import { test } from "node:test";
import assert from "node:assert/strict";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { initialSession, type SessionState } from "@astra/ui-core/session-state";
import { applyStream, emptySessions, parseEventStream, sessionTitle } from "../src/phone/state.js";
import { PhoneSettingsView } from "../src/renderer/phone-settings.js";
import type { PhoneStatus } from "../src/phone-protocol.js";

const session = (id: string, fields: Partial<SessionState> = {}): SessionState => ({ ...initialSession(id, "/work"), status: "ready", ...fields });

test("the stream is read in whatever pieces the network delivers, and heartbeats are skipped", () => {
  const whole = 'data: {"type":"refresh"}\n\n: ping\n\ndata: {"type":"removed","runtime":"a"}\n\n';
  let buffer = "", payloads: string[] = [];
  for (const piece of [whole.slice(0, 7), whole.slice(7, 30), whole.slice(30)]) {
    const parsed = parseEventStream(buffer + piece);
    buffer = parsed.rest; payloads = [...payloads, ...parsed.payloads];
  }
  assert.deepEqual(payloads.map(text => JSON.parse(text).type), ["refresh", "removed"]);
  assert.equal(buffer, "");
});

test("the phone keeps the window's rules: a snapshot, newer revisions only, and a fresh snapshot for a session it lacks", () => {
  let state = applyStream(emptySessions, { type: "hello", sessions: [session("a", { revision: 5 }), session("b")], active: "b" });
  assert.deepEqual(state.order, ["a", "b"]);
  state = applyStream(state, { type: "event", runtime: "a", revision: 4, event: { type: "chunk", content: "旧" } as any });
  assert.equal(state.sessions.a.messages.length, 0, "an older revision is not applied");
  state = applyStream(state, { type: "event", runtime: "a", revision: 6, event: { type: "chunk", content: "新" } as any });
  assert.equal(state.sessions.a.messages[0].content, "新");
  state = applyStream(state, { type: "removed", runtime: "b" });
  assert.deepEqual([state.order, state.active], [["a"], ""]);
  state = applyStream(state, { type: "event", runtime: "c", revision: 1, event: { type: "chunk", content: "x" } as any });
  assert.equal(state.stale, true);
  state = applyStream(state, { type: "hello", sessions: [session("c")], active: "c" });
  assert.equal(state.stale, false);
});

test("a session is named after its first request, as the window has its own titles", () => {
  assert.equal(sessionTitle(session("a", { messages: [{ id: "1", role: "assistant", content: "你好" }, { id: "2", role: "user", content: "  帮我\n检查 CI  " }] })), "帮我 检查 CI");
  assert.equal(sessionTitle(session("a", { messages: [{ id: "1", role: "user", content: "很".repeat(60) }] })).length, 40);
  assert.equal(sessionTitle(session("a", { isDraft: true })), "新会话");
});

test("settings show what the desktop reports: the switch, the serve command, the code and the paired phones", () => {
  const status: PhoneStatus = { enabled: true, port: 8930, running: true, url: "https://mac.tail1234.ts.net",
    devices: [{ id: "d1", name: "纸航的 iPhone", createdAt: "2026-10-17T04:00:00.000Z" }],
    pairing: { code: "ABCD2345", expiresAt: 1_000_000 + 125_000 } };
  const html = renderToStaticMarkup(React.createElement(PhoneSettingsView, { status, run: () => {}, now: 1_000_000 }));
  assert.match(html, /tailscale serve --bg 8930/);
  assert.match(html, /https:\/\/mac\.tail1234\.ts\.net/);
  assert.match(html, /ABCD 2345/);
  assert.match(html, /2:05 内有效/);
  assert.match(html, /纸航的 iPhone/);
  assert.match(html, /最近连接 从未/);
  const expired = renderToStaticMarkup(React.createElement(PhoneSettingsView, { status, run: () => {}, now: 1_000_000 + 200_000 }));
  assert.match(expired, />添加手机</);
  const off = renderToStaticMarkup(React.createElement(PhoneSettingsView, { status: { ...status, enabled: false }, run: () => {} }));
  assert.doesNotMatch(off, /添加手机|tailscale serve|纸航的 iPhone/);
  const busy = renderToStaticMarkup(React.createElement(PhoneSettingsView, { status: { ...status, pairing: undefined, error: "端口 8930 已被占用。" }, run: () => {} }));
  assert.match(busy, /role="alert">端口 8930 已被占用。/);
});

test("the Mac's tailnet name comes from whichever Tailscale install answers, and only a ts.net name is shown", async () => {
  const { tailscaleName } = await import("../src/main/phone-tailscale.js");
  const tried: string[] = [];
  const answer = (name: string) => async (file: string) => {
    tried.push(file);
    if (!file.startsWith("/opt/homebrew")) throw new Error("not here");
    return JSON.stringify({ Self: { DNSName: name } });
  };
  assert.equal(await tailscaleName(answer("Zhihang-MacBook.tail1234.ts.net.")), "zhihang-macbook.tail1234.ts.net");
  assert.deepEqual(tried.slice(0, 2), ["/Applications/Tailscale.app/Contents/MacOS/Tailscale", "/opt/homebrew/bin/tailscale"]);
  assert.equal(await tailscaleName(answer("evil.example.com.")), undefined);
  assert.equal(await tailscaleName(async () => { throw new Error("absent"); }), undefined);
});
