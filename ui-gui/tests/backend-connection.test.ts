import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, mkdir, writeFile, rm } from "node:fs/promises";
import { existsSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { Runtime } from "../src/main/runtime.js";

const root = fileURLToPath(new URL("../../", import.meta.url));
const venv = join(root, ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
const chosen = process.env.ASTRA_PYTHON || (existsSync(venv) ? venv : process.platform === "win32" ? "python" : "python3");
// On Windows a virtual environment's python.exe is a launcher that keeps the input handle open while it waits for
// the real interpreter, so the stand-in closing its input would not reach the desktop: run the base interpreter.
const python = execFileSync(chosen, ["-c", "import sys; print(sys._base_executable)"], { encoding: "utf8" }).trim() || chosen;

// A stand-in backend that greets the desktop and then stops reading its input while it stays alive:
// every later write from the desktop meets a closed pipe.
const backend = `
import json, os, sys, time
def send(event):
    sys.stdout.write(json.dumps(event) + "\\n"); sys.stdout.flush()
send({"type": "backend_hello", "protocol_version": 1, "capabilities": ["ui_queries"]})
send({"type": "gui_ready", "workspace": os.getcwd()})
os.close(0)
send({"type": "gui_notice", "message": "input closed"})
time.sleep(60)
`;

async function fixture() {
  const dir = await mkdtemp(join(tmpdir(), "astra-backend-connection-"));
  await mkdir(join(dir, "agent", "cli"), { recursive: true });
  await mkdir(join(dir, "workspace"));
  await writeFile(join(dir, "agent", "__init__.py"), "");
  await writeFile(join(dir, "agent", "cli", "__init__.py"), "");
  await writeFile(join(dir, "agent", "cli", "backend.py"), backend);
  const runtime = new Runtime(dir, python, join(dir, "workspace"), "fixture", "work", () => {});
  return { dir, runtime };
}

async function until(condition: () => boolean, what: string, ms = 10_000) {
  for (const end = Date.now() + ms; !condition(); await new Promise(resolve => setTimeout(resolve, 20))) {
    if (Date.now() > end) throw new Error(`timed out waiting for ${what}`);
  }
}

test("a command the backend can no longer read fails and ends the connection", async () => {
  const { dir, runtime } = await fixture();
  try {
    await until(() => runtime.state.messages.some(message => message.content === "input closed"), "the closed input");
    await assert.rejects(runtime.send({ type: "performance_ack", samples: [] }), /not delivered/);
    await until(() => runtime.state.status === "disconnected", "the disconnect");
    await assert.rejects(runtime.send({ type: "performance_ack", samples: [] }), /not sent/);
  } finally { await runtime.close(); await rm(dir, { recursive: true, force: true }); }
});

test("a message written into a closed input is settled by the disconnect, not by the receipt timeout", async () => {
  const { dir, runtime } = await fixture();
  try {
    await until(() => runtime.state.messages.some(message => message.content === "input closed"), "the closed input");
    const started = Date.now();
    await assert.rejects(runtime.send({ type: "message", text: "hello", submission_id: "lost" }), /后端已断开/);
    assert.ok(Date.now() - started < 10_000);
    assert.equal(runtime.state.status, "disconnected");
    // The draft is kept and marked as not confirmed rather than sent or lost.
    assert.equal(runtime.state.messages.find(message => message.id === "lost")?.submissionState, "unknown");
  } finally { await runtime.close(); await rm(dir, { recursive: true, force: true }); }
});
