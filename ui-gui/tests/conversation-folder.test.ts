import { test } from "node:test";
import assert from "node:assert/strict";
import { join, resolve } from "node:path";
import { projectFolder } from "../src/main/conversation-folder.js";

// This platform's own spelling of the folders: on Windows they resolve under a drive.
const home = resolve("/Users/me"), at = (...parts: string[]) => join(home, ...parts);
const folders = new Set([at("Project"), at("astra-master"), at("old", "astra-master"), home]);
const isFolder = (path: string) => folders.has(path);

test("the personal thread names a project by path or by its folder name among the ones the desktop knows", () => {
  const known = [at("Project"), at("astra-master"), at("Project"), at("gone")];
  assert.deepEqual(projectFolder(at("astra-master"), known, isFolder), { folder: at("astra-master") });
  assert.deepEqual(projectFolder("~/Project", known, isFolder, home), { folder: at("Project") });
  assert.deepEqual(projectFolder("ASTRA-MASTER", known, isFolder), { folder: at("astra-master") });
  const unknown = projectFolder("nowhere", known, isFolder);
  assert.ok("error" in unknown && unknown.error.includes(`Project (${at("Project")})`) && !unknown.error.includes("gone"));
  assert.match((projectFolder(at("missing"), known, isFolder) as { error: string }).error, /not a folder/);
  const twice = projectFolder("astra-master", [...known, at("old", "astra-master")], isFolder);
  assert.match((twice as { error: string }).error, /More than one project is called astra-master/);
  assert.ok("error" in projectFolder("  ", known, isFolder));
});
