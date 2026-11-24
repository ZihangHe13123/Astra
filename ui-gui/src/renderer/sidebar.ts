import type { SessionState } from "@astra/ui-core/session-state";
import { PERSONAL_SESSION, type Preferences, type SessionEntry } from "../bridge.js";
import { maskSecrets } from "@astra/ui-core/secret-mask";
export const sessionKey = (s: SessionEntry) => `${s.mode}:${s.name}`;
export function openingFolder(history: SessionEntry[], workspaces: Preferences["workspaces"], current: string, session: string, mode: string): string {
  const recorded = history.find(entry => entry.name === session && entry.mode === mode)?.workspace;
  if (typeof recorded === "string" && recorded) return recorded;
  const preferred = workspaces[`${mode}:${session}`];
  return typeof preferred === "string" && preferred ? preferred : current;
}
export function sessionTitle(entry: SessionEntry, titles: Preferences["titles"]): string {
  if (titles[sessionKey(entry)]) return titles[sessionKey(entry)];
  const generated = /^session_(\d{4})(\d{2})(\d{2})_?(\d{2})(\d{2})(\d{2})(?:_|$)/.exec(entry.name);
  return generated ? `${generated[2]}/${generated[3]} ${generated[4]}:${generated[5]} 的对话` : entry.name;
}
/** The name a new conversation takes from its first message: the first line, at most 36 characters,
 * with recognised credentials masked first, so preferences (shared with a phone) never hold one. */
export function autoTitle(draft: string): string {
  return [...maskSecrets(draft.trim()).split("\n")[0]].slice(0, 36).join("") || "附件对话";
}
/** The personal thread has its own place at the top of the sidebar, never among the sessions. */
export const isPersonal = (entry: SessionEntry) => entry.name === PERSONAL_SESSION && entry.mode === "work";
function projectPath(folder: string): string {
  return /^(?:[a-z]:)?[\\/]+$/i.test(folder) ? folder : folder.replace(/[\\/]+$/, "");
}
function projectName(folder: string): string {
  return projectPath(folder).split(/[\\/]+/).filter(Boolean).at(-1) || folder;
}
export function projectLabel(entry: SessionEntry, projects: string[]): string {
  if (!entry.workspace) return "";
  const key = projectPath(entry.workspace);
  const project = projects.find(folder => projectPath(folder) === key);
  return project ? projectName(project) : "";
}
export function sidebarGroups(history: SessionEntry[], states: SessionState[], prefs: Pick<Preferences, "titles" | "pinned"> & Partial<Pick<Preferences, "projects">>, search: string, now = new Date()) {
  const entries = new Map(history.filter(e => !isPersonal(e)).map(e => [sessionKey(e), e]));
  const live = new Set<string>();
  for (const state of states) if (!state.isDraft && !state.personal) {
    const key = `${state.mode}:${state.session}`;
    live.add(key);
    if (!entries.has(key)) entries.set(key, { name: state.session, mode: state.mode, modified: 0, workspace: state.workspace });
  }
  const projects = (prefs.projects ?? []).map(folder => ({ folder, name: projectName(folder), entries: [] as SessionEntry[] }));
  const projectsByPath = new Map(projects.map(project => [projectPath(project.folder), project]));
  const projectOf = (entry: SessionEntry) => entry.workspace ? projectsByPath.get(projectPath(entry.workspace)) : undefined;
  const searchTerm = search.toLowerCase();
  const all = [...entries.values()].filter(e => `${sessionTitle(e, prefs.titles)} ${e.name}`.toLowerCase().includes(searchTerm)
    || projectOf(e)?.name.toLowerCase().includes(searchTerm)).sort((a, b) => b.modified - a.modified);
  for (const entry of all) projectOf(entry)?.entries.push(entry);
  const pinned = all.filter(e => prefs.pinned.includes(sessionKey(e)));
  const open = all.filter(e => !prefs.pinned.includes(sessionKey(e)) && live.has(sessionKey(e)));
  const rest = all.filter(e => !prefs.pinned.includes(sessionKey(e)) && !live.has(sessionKey(e)) && !projectOf(e));
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const yesterday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1).getTime();
  const week = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 7).getTime();
  const groups = new Map<string, SessionEntry[]>();
  for (const entry of rest) {
    const ms = entry.modified * 1000;
    const label = ms >= today ? "今天" : ms >= yesterday ? "昨天" : ms >= week ? "过去 7 天" : "更早";
    if (!groups.has(label)) groups.set(label, []);
    groups.get(label)!.push(entry);
  }
  return { all, pinned, open, recent: [...groups].map(([label, entries]) => ({ label, entries })),
    projects: search ? projects.filter(project => project.entries.length) : projects };
}
