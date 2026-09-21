import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, readFile, readdir, rm, writeFile, truncate } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { initialSession } from "@astra/ui-core/session-state";
import { hasOngoingWork } from "../src/main/ongoing-work.js";
import { PreferenceWriter } from "../src/main/preferences.js";
import { filePreview, TEXT_PREVIEW_LIMIT } from "../src/main/file-preview.js";
import { ClipboardFiles } from "../src/main/clipboard-files.js";

test("session lifecycle protects scheduled and running reminders, but not completed plans", () => {
  const state = { ...initialSession("fixture"), status: "ready" };
  for (const planState of ["scheduled", "running", "active"]) {
    state.info.wakeup_status = { type: "wakeup_status", plan: { state: planState } };
    assert.equal(hasOngoingWork(state), true, planState);
  }
  for (const planState of ["idle", "completed", "cancelled", "expired", "session_changed", "failed"]) {
    state.info.wakeup_status = { type: "wakeup_status", plan: { state: planState } };
    assert.equal(hasOngoingWork(state), false, planState);
  }
  state.info.task_status = { type: "task_status", task: { status: "cancelling" } };
  assert.equal(hasOngoingWork(state), true);
  state.status = "disconnected";
  assert.equal(hasOngoingWork(state), false);
});

test("session lifecycle protects approvals, questions, and idle keep-alive teams", () => {
  const state = { ...initialSession("fixture"), status: "ready" };
  state.approvals = [{ type: "tool_approval_request" }]; assert.equal(hasOngoingWork(state), true);
  state.approvals = []; state.questions = [{ type: "user_question_request" }]; assert.equal(hasOngoingWork(state), true);
  state.questions = []; state.teams = { team: { id: "team", name: "idle team", status: "active", agents: [], tasks: [], messageCount: 0 } };
  assert.equal(hasOngoingWork(state), true);
  state.teams.team.status = "stopped"; assert.equal(hasOngoingWork(state), false);
  state.processes.job = { type: "process_status", process_id: "job", status: "running" };
  assert.equal(hasOngoingWork(state), true);
  state.processes.job.status = "completed"; assert.equal(hasOngoingWork(state), false);
});

test("final preference flush cannot be overwritten by an earlier asynchronous write", async () => {
  const dir = await mkdtemp(join(tmpdir(), "astra-preferences-"));
  try {
    const path = join(dir, "preferences.json");
    let release!: () => void;
    const gate = new Promise<void>(resolve => { release = resolve; });
    const writer = new PreferenceWriter(path, assert.fail, 60_000, async (path, data, options) => {
      await gate; await writeFile(path, data, options);
    });
    writer.schedule({ draft: "old" }); const pending = writer.flush();
    writer.flushSync({ draft: "latest before unload" }); release(); await pending;
    assert.deepEqual(JSON.parse(await readFile(path, "utf8")), { draft: "latest before unload" });
    assert.deepEqual(await readdir(dir), ["preferences.json"]);
  } finally { await rm(dir, { recursive: true, force: true }); }
});

test("preference flushes serialize and coalesce a burst to its final value", async () => {
  const dir = await mkdtemp(join(tmpdir(), "astra-preferences-"));
  try {
    let concurrent = 0, maximum = 0, writes = 0;
    const path = join(dir, "preferences.json");
    const writer = new PreferenceWriter(path, assert.fail, 60_000, async (path, data, options) => {
      maximum = Math.max(maximum, ++concurrent); writes++;
      try { await writeFile(path, data, options); } finally { concurrent--; }
    });
    writer.schedule({ draft: "first" }); const first = writer.flush();
    for (let i = 0; i < 100; i++) writer.schedule({ draft: `draft ${i}` });
    await Promise.all([first, writer.flush(), writer.flush()]);
    assert.equal(maximum, 1); assert.ok(writes <= 2);
    assert.deepEqual(JSON.parse(await readFile(path, "utf8")), { draft: "draft 99" });
  } finally { await rm(dir, { recursive: true, force: true }); }
});

test("large text previews read only a bounded prefix and preserve UTF-8 boundaries", async () => {
  const dir = await mkdtemp(join(tmpdir(), "astra-preview-"));
  try {
    const path = join(dir, "large.txt");
    await writeFile(path, "a".repeat(TEXT_PREVIEW_LIMIT - 1) + "😀");
    await truncate(path, 100 * 1024 * 1024);
    const preview = await filePreview(path);
    assert.equal(preview.truncated, true);
    assert.equal(preview.text, "a".repeat(TEXT_PREVIEW_LIMIT - 1));
    const binary = join(dir, "data.bin"); await writeFile(binary, Buffer.from([1, 0, 1]));
    await assert.rejects(filePreview(binary), /二进制/);
  } finally { await rm(dir, { recursive: true, force: true }); }
});

test("clipboard cleanup only removes this launch's abandoned files, preserving sent and saved references", async () => {
  const dir = await mkdtemp(join(tmpdir(), "astra-clipboard-"));
  try {
    const files = new ClipboardFiles();
    const paths = Object.fromEntries(["old", "abandoned", "attached", "sent", "typed"].map(name => [name, join(dir, `${name}.png`)]));
    for (const [name, path] of Object.entries(paths)) { await writeFile(path, "fixture"); if (name !== "old") files.add(path); }
    files.sent({ text: `Please read "${paths.sent}"` });
    await files.cleanup({ draft: [paths.attached] }, { draft: `Please read ${paths.typed}` });
    assert.deepEqual((await readdir(dir)).sort(), ["attached.png", "old.png", "sent.png", "typed.png"]);
  } finally { await rm(dir, { recursive: true, force: true }); }
});
