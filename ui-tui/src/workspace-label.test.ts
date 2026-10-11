import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { sessionLabel, workspaceLabel } from "./workspace-label.js";

test("the working directory is named short of home, with its branch when it has one", () => {
  const home = mkdtempSync(join(tmpdir(), "astra-label-"));
  const project = join(home, "code", "astra");
  mkdirSync(join(project, ".git"), { recursive: true });
  writeFileSync(join(project, ".git", "HEAD"), "ref: refs/heads/feature/x\n");
  assert.equal(workspaceLabel(project, home), `~${project.slice(home.length)} · feature/x`);
  writeFileSync(join(project, ".git", "HEAD"), "0123456789abcdef0123456789abcdef01234567\n");
  assert.equal(workspaceLabel(project, home), `~${project.slice(home.length)} · 0123456`);
  // No repository, a worktree whose .git is a file, and home itself.
  assert.equal(workspaceLabel(join(home, "code"), home), `~${join(home, "code").slice(home.length)}`);
  writeFileSync(join(home, "code", ".git"), "gitdir: elsewhere\n");
  assert.equal(workspaceLabel(join(home, "code"), home), `~${join(home, "code").slice(home.length)}`);
  assert.equal(workspaceLabel(home, home), "~");
  assert.equal(workspaceLabel("/srv/app", home), "/srv/app");
});

test("a session named by its start time reads as that time", () => {
  assert.equal(sessionLabel("session_20000102_030405_12345"), "01-02 03:04");
  assert.equal(sessionLabel("session_20000102_030405"), "01-02 03:04");
  assert.equal(sessionLabel("发布门禁转绿"), "发布门禁转绿");
  assert.equal(sessionLabel(""), "");
});
