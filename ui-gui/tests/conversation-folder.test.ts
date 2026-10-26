import { test } from "node:test";
import assert from "node:assert/strict";
import { projectFolder } from "../src/main/conversation-folder.js";

const folders = new Set(["/Users/me/Project", "/Users/me/astra-master", "/Users/me/old/astra-master", "/Users/me"]);
const isFolder = (path: string) => folders.has(path);

test("the personal thread names a project by path or by its folder name among the ones the desktop knows", () => {
  const known = ["/Users/me/Project", "/Users/me/astra-master", "/Users/me/Project", "/Users/me/gone"];
  assert.deepEqual(projectFolder("/Users/me/astra-master", known, isFolder), { folder: "/Users/me/astra-master" });
  assert.deepEqual(projectFolder("~/Project", known, isFolder, "/Users/me"), { folder: "/Users/me/Project" });
  assert.deepEqual(projectFolder("ASTRA-MASTER", known, isFolder), { folder: "/Users/me/astra-master" });
  const unknown = projectFolder("nowhere", known, isFolder);
  assert.ok("error" in unknown && unknown.error.includes("Project (/Users/me/Project)") && !unknown.error.includes("gone"));
  assert.match((projectFolder("/Users/me/missing", known, isFolder) as { error: string }).error, /not a folder/);
  const twice = projectFolder("astra-master", [...known, "/Users/me/old/astra-master"], isFolder);
  assert.match((twice as { error: string }).error, /More than one project is called astra-master/);
  assert.ok("error" in projectFolder("  ", known, isFolder));
});
