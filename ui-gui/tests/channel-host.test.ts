import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { ChannelHost, channelsEnabled, type HostedBackend } from "../src/main/channel-host.js";

function fixture() {
  const config = join(mkdtempSync(join(tmpdir(), "astra-channel-host-")), "channels.json");
  const backends: (HostedBackend & { closed: boolean })[] = [];
  let now = 0;
  const host = new ChannelHost({
    configPath: config, intervalMs: 60_000, retryMs: 30_000, now: () => now,
    create: () => {
      const backend = { state: { status: "ready" }, closed: false, close: async () => { backend.closed = true; } };
      backends.push(backend);
      return backend;
    },
  });
  return { config, backends, host, later: (ms: number) => { now += ms; } };
}

test("channels.json is read the way the backend reads it", () => {
  const { config } = fixture();
  assert.equal(channelsEnabled(config), false, "no file: nothing to host");
  for (const [text, expected] of [
    ['{"weixin": {"enabled": true}}', true], ['{"qq": {"enabled": "on"}}', true], ['{"onebot": {"enabled": "1"}}', true],
    ['{"weixin": {"enabled": false}, "qq": {"enabled": "no"}}', false], ["not json", false], ["[]", false],
  ] as const) {
    writeFileSync(config, text);
    assert.equal(channelsEnabled(config), expected, text);
  }
});

test("a backend runs for the channels while one is on, comes back after it exits, and closes with the desktop", async () => {
  const { config, backends, host, later } = fixture();
  await host.check();
  assert.equal(backends.length, 0, "no channel switched on");

  writeFileSync(config, '{"weixin": {"enabled": true}}');
  await host.check();
  await host.check();
  assert.equal(backends.length, 1, "one backend, not one per check");

  // It exited: started again, but not more often than the retry interval.
  backends[0].state.status = "disconnected";
  later(10_000);
  await host.check();
  assert.equal(backends.length, 1);
  later(25_000);
  await host.check();
  assert.equal(backends.length, 2);

  writeFileSync(config, '{"weixin": {"enabled": false}}');
  await host.check();
  assert.ok(backends[1].closed && !host.running, "switched off: the backend closes");

  writeFileSync(config, '{"weixin": {"enabled": true}}');
  later(30_000);
  await host.check();
  await host.stop();
  assert.ok(backends[2].closed, "the desktop quits: the backend closes");
  later(60_000);
  await host.check();
  assert.equal(backends.length, 3, "nothing starts after the desktop quit");
});
