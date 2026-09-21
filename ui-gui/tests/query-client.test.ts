import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, mkdir, writeFile, rm } from "node:fs/promises";
import { existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { QueryClient } from "../src/main/query-client.js";

const root = fileURLToPath(new URL("../../", import.meta.url));
const venv = join(root, ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
const python = process.env.ASTRA_PYTHON || (existsSync(venv) ? venv : process.platform === "win32" ? "python" : "python3");

// Exercise real transport/lifecycle without accounts, user state, or Electron.
// The actual Python service replay is covered by tests/test_gui_history_index.py.
async function fixture(timeoutMs?: number) {
  const dir = await mkdtemp(join(tmpdir(), "astra-query-client-"));
  await mkdir(join(dir, "agent", "ui"), { recursive: true });
  await writeFile(join(dir, "agent", "__init__.py"), "");
  await writeFile(join(dir, "agent", "ui", "__init__.py"), "");
  await writeFile(join(dir, "agent", "ui", "queries.py"), `
import json, os, sys, time
calls = 0
for line in sys.stdin:
    request = json.loads(line)
    method = request["method"]
    calls += 1
    if method == "crash":
        sys.exit(7)
    if method == "malformed":
        print("not JSON", flush=True)
        continue
    if method == "blocked":
        time.sleep(60)
    result = {"pid": os.getpid(), "calls": calls, "params": request["params"]}
    reply = {"id": request["id"], "ok": method != "error", "result": result, "error": "fixture failure"}
    print(json.dumps(reply), flush=True)
`);
  return { dir, client: new QueryClient(dir, python, timeoutMs) };
}

test("queries reuse a worker, correlate replies, cache commands, and survive a query error", async () => {
  const { dir, client } = await fixture();
  try {
    const [first, second] = await Promise.all([client.query("history", { before: 10 }), client.query("history", { before: 20 })]);
    assert.equal(first.pid, second.pid);
    assert.deepEqual(first.params, { before: 10 }); assert.deepEqual(second.params, { before: 20 });
    const commands = client.query("commands");
    assert.equal(client.query("commands"), commands);
    assert.equal((await commands).calls, 3);
    await assert.rejects(client.query("error"), /fixture failure/);
    const next = await client.query("history");
    assert.equal(next.pid, first.pid); assert.equal(next.calls, 5);
    assert.equal(client.query("commands"), commands);
    await client.close();
    assert.throws(() => process.kill(first.pid, 0), { code: "ESRCH" });
    await assert.rejects(client.query("history"), /service closed/);
  } finally { await client.close(); await rm(dir, { recursive: true, force: true }); }
});

test("worker death rejects outstanding requests and next query starts a fresh process", async () => {
  const { dir, client } = await fixture();
  try {
    const first = await client.query("history");
    await assert.rejects(client.query("crash"), /disconnected/);
    const next = await client.query("history");
    assert.notEqual(next.pid, first.pid); assert.equal(next.calls, 1);
  } finally { await client.close(); await rm(dir, { recursive: true, force: true }); }
});

test("timed-out worker is recycled, its queued requests fail, and immediate retry works", async () => {
  const { dir, client } = await fixture(250);
  try {
    const commands = client.query("commands");
    const first = await commands;
    const blocked = assert.rejects(client.query("blocked"), /timed out/);
    const queued = assert.rejects(client.query("history"), /timed out/);
    await Promise.all([blocked, queued]);
    // A successful static catalog remains useful across transport replacement.
    assert.equal(client.query("commands"), commands);
    const next = await client.query("history");
    assert.notEqual(next.pid, first.pid); assert.equal(next.calls, 1);
    await client.close();
    assert.throws(() => process.kill(first.pid, 0), { code: "ESRCH" });
    assert.throws(() => process.kill(next.pid, 0), { code: "ESRCH" });
  } finally { await client.close(); await rm(dir, { recursive: true, force: true }); }
});

test("malformed output rejects pending queries; shutdown reaps a blocked worker", async () => {
  const { dir, client } = await fixture();
  try {
    await assert.rejects(client.query("malformed"), /Invalid local query response/);
    await client.close();
    await assert.rejects(client.query("history"), /service closed/);
  } finally { await client.close(); await rm(dir, { recursive: true, force: true }); }
  const blocked = await fixture();
  try {
    const ready = await blocked.client.query("history");
    const pending = assert.rejects(blocked.client.query("blocked"), /service closed/);
    await blocked.client.close(); await pending;
    assert.throws(() => process.kill(ready.pid, 0), { code: "ESRCH" });
  } finally { await blocked.client.close(); await rm(blocked.dir, { recursive: true, force: true }); }
});

test("spawn failure rejects a query rather than causing an unhandled stream error", async () => {
  const dir = await mkdtemp(join(tmpdir(), "astra-query-client-missing-"));
  const client = new QueryClient(dir, join(dir, "missing-python"));
  try { await assert.rejects(client.query("history"), /ENOENT/); }
  finally { await client.close(); await rm(dir, { recursive: true, force: true }); }
});
