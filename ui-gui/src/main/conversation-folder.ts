import { homedir } from "node:os";
import { basename, isAbsolute, resolve } from "node:path";

/**
 * The folder a request from the personal thread names (specs/personal-thread.md D6): a path that exists,
 * or a project by its folder name among the ones the desktop knows (the sidebar's projects and the
 * folders of its sessions). An unknown or ambiguous name is refused with the projects there are.
 */
export function projectFolder(where: string, known: string[], isFolder: (path: string) => boolean, home = homedir()): { folder: string } | { error: string } {
  const asked = where.trim();
  if (!asked) return { error: "Name the project: a folder path, or a project's name." };
  const path = asked === "~" ? home : asked.startsWith("~/") ? resolve(home, asked.slice(2)) : asked;
  if (isAbsolute(path)) return isFolder(path) ? { folder: resolve(path) } : { error: `${path} is not a folder on this Mac.` };
  const folders = [...new Set(known.filter(Boolean).map(folder => resolve(folder)))].filter(isFolder);
  const matches = folders.filter(folder => basename(folder).toLowerCase() === path.toLowerCase());
  if (matches.length === 1) return { folder: matches[0] };
  const listed = folders.map(folder => `${basename(folder)} (${folder})`).join(", ") || "none yet";
  return { error: matches.length
    ? `More than one project is called ${path}: ${matches.join(", ")}. Give the folder's path.`
    : `No project called ${path}. Projects the desktop knows: ${listed}. Or give a folder's path.` };
}
