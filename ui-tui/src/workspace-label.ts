import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join, sep } from "node:path";

/** Where the terminal is working, as a person would say it: the directory, short of home, and the
 * branch when the directory is a repository's own checkout. */
export function workspaceLabel(cwd: string = process.cwd(), home: string = homedir()): string {
  const path = cwd === home ? "~" : home && cwd.startsWith(home.endsWith(sep) ? home : home + sep) ? `~${cwd.slice(home.length)}` : cwd;
  let branch = "";
  try {
    const head = readFileSync(join(cwd, ".git", "HEAD"), "utf8").trim();
    branch = head.startsWith("ref: refs/heads/") ? head.slice("ref: refs/heads/".length) : /^[0-9a-f]{7,}$/.test(head) ? head.slice(0, 7) : "";
  } catch { /* not a checkout, or one whose .git is a file */ }
  return branch ? `${path} · ${branch}` : path;
}

/** "session_20000102_030405_12345" names its own start; anything else is shown as it is. */
export function sessionLabel(name: string): string {
  const stamp = /^session_\d{4}(\d{2})(\d{2})_(\d{2})(\d{2})\d{2}(?:_\d+)?$/.exec(name);
  return stamp ? `${stamp[1]}-${stamp[2]} ${stamp[3]}:${stamp[4]}` : name;
}
