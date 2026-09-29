import assert from "node:assert/strict";
import { test } from "node:test";
import { QuitCoordinator, type QuitChoice } from "../src/main/quit.js";

function fixture(busy = true) {
  const calls: string[] = [];
  let answer: (choice: QuitChoice) => void = () => {};
  const quit = new QuitCoordinator({
    inspect: () => busy,
    confirm: reason => { calls.push(`prompt:${reason}`); return new Promise(resolve => { answer = resolve; }); },
    perform: async () => { calls.push("quit"); },
    hide: () => { calls.push("hide"); },
    focus: () => { calls.push("focus"); },
  });
  return { quit, calls, answer: (choice: QuitChoice) => answer(choice) };
}

test("idle exit needs no confirmation", async () => {
  const f = fixture(false);
  await f.quit.request("quit");
  assert.deepEqual(f.calls, ["quit"]);
});

test("ordinary app quit waits for confirmation and Cancel preserves work", async () => {
  const f = fixture();
  const pending = f.quit.request("quit");
  await Promise.resolve();
  assert.deepEqual(f.calls, ["prompt:quit"]);
  f.answer("cancel"); await pending;
  assert.deepEqual(f.calls, ["prompt:quit"]);
});

test("window close can keep work running in the background", async () => {
  const f = fixture(); const pending = f.quit.request("window");
  await Promise.resolve(); f.answer("background"); await pending;
  assert.deepEqual(f.calls, ["prompt:window", "hide"]);
});

test("repeated quit entries share one decision and shut down once", async () => {
  const f = fixture(); const first = f.quit.request("window");
  const second = f.quit.request("quit");
  await Promise.resolve(); f.answer("quit"); await Promise.all([first, second]);
  assert.equal(f.calls.filter(x => x.startsWith("prompt:")).length, 1);
  assert.equal(f.calls.filter(x => x === "quit").length, 1);
});

test("failed task inspection still requires explicit confirmation", async () => {
  let performed = false, prompted = false;
  const quit = new QuitCoordinator({ inspect: () => { throw new Error("unavailable"); },
    confirm: async () => { prompted = true; return "cancel"; }, perform: async () => { performed = true; },
    hide: () => {}, focus: () => {} });
  await quit.request("quit");
  assert.equal(prompted, true); assert.equal(performed, false);
});

test("a failed dialog cannot turn into a quit", async () => {
  let performed = false;
  const quit = new QuitCoordinator({ inspect: () => true,
    confirm: async () => { throw new Error("dialog failed"); }, perform: async () => { performed = true; },
    hide: () => {}, focus: () => {} });
  await assert.rejects(quit.request("quit"), /dialog failed/);
  assert.equal(performed, false);
});
