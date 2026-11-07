import { test } from "node:test";
import assert from "node:assert/strict";
import { chmodSync, existsSync, mkdirSync, mkdtempSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { dataCommand, type WipeAnswer } from "../src/main/data-command.js";

const root = fileURLToPath(new URL("../../", import.meta.url));
const venv = join(root, ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
const python = process.env.ASTRA_PYTHON || (existsSync(venv) ? venv : process.platform === "win32" ? "python" : "python3");

/** The real `astra data` command over a temporary Astra home and a stand-in installation, never a real one. */
function fixture() {
  const folder = realpathSync(mkdtempSync(join(tmpdir(), "astra-data-command-")));
  const home = join(folder, "home"), install = join(folder, "install");
  mkdirSync(join(install, "agent"), { recursive: true });
  writeFileSync(join(install, "pyproject.toml"), '[project]\nname = "agent-lab-local"\nversion = "0"\n');
  for (const [name, text] of Object.entries({ "memory/USER.md": "称呼：小林\n", "sessions/s1.json": "{}", "jobs/preferences.json": "{}" })) {
    mkdirSync(join(home, name, ".."), { recursive: true });
    writeFileSync(join(home, name), text);
  }
  const env: NodeJS.ProcessEnv = Object.fromEntries(Object.entries(process.env).filter(([key]) => !/^(AGENT_|ASTRA_)/.test(key)));
  Object.assign(env, { ASTRA_HOME: home, AGENT_SESSION_DIR: join(home, "sessions") });
  const run = (...args: string[]) => dataCommand(python, root, args, 120_000, env);
  return { folder, home, run, wipe: () => run("wipe", "--confirm", "清除", "--desktop", "--root", install) };
}

// A folder its owner cannot change keeps its files; root ignores that, and Windows has no such mode.
const unchangeable = process.platform === "win32" || process.getuid?.() === 0;

test("a wipe that could not delete everything is reported with what stayed and why, not as success", { skip: unchangeable }, async () => {
  const { folder, home, wipe } = fixture();
  try {
    chmodSync(join(home, "sessions"), 0o500);
    const failure = await wipe().then(answer => answer, (error: Error & { result?: WipeAnswer }) => error);
    assert.ok(failure instanceof Error && failure.result, `rejected with what stayed, got ${JSON.stringify(failure)}`);
    assert.deepEqual(failure.result.failed.map(item => item.key), ["sessions"]);
    assert.match(failure.message, /对话记录（.*Permission denied/);
    // The rest went: the desktop must reload even though it reports the failure.
    assert.ok(failure.result.removed.some(item => item.key === "core-user"));
    assert.ok(!existsSync(join(home, "memory", "USER.md")) && existsSync(join(home, "sessions", "s1.json")));

    // Once nothing holds it, wiping again takes the rest and is a success.
    chmodSync(join(home, "sessions"), 0o700);
    const done = await wipe();
    assert.deepEqual(done.failed, []);
    assert.ok(!existsSync(join(home, "sessions", "s1.json")));
  } finally {
    if (existsSync(join(home, "sessions"))) chmodSync(join(home, "sessions"), 0o700);
    rmSync(folder, { recursive: true, force: true });
  }
});

test("a refused wipe deletes nothing and says why; plan and export answer as before", async () => {
  const { folder, home, run } = fixture();
  try {
    const refused = await run("wipe", "--confirm", "清楚").then(() => undefined, (error: unknown) => error);
    assert.ok(refused instanceof Error && !("result" in refused), "a refusal is not a wipe that ran");
    assert.match(refused.message, /清除/);
    assert.ok(existsSync(join(home, "memory", "USER.md")));
    const plan = await run("plan");
    assert.equal(plan.confirmation, "清除");
    const out = join(folder, "astra.zip");
    assert.equal((await run("export", "--out", out)).path, out);
  } finally { rmSync(folder, { recursive: true, force: true }); }
});
