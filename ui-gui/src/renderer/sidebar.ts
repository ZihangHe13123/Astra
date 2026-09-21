import type { SessionState } from "@astra/ui-core/session-state";
import type { Preferences, SessionEntry } from "../bridge.js";
export const sessionKey = (s: SessionEntry) => `${s.mode}:${s.name}`;
export function sessionTitle(entry: SessionEntry, titles: Preferences["titles"]): string {
  if (titles[sessionKey(entry)]) return titles[sessionKey(entry)];
  const generated = /^session_(\d{4})(\d{2})(\d{2})_?(\d{2})(\d{2})(\d{2})(?:_|$)/.exec(entry.name);
  return generated ? `${generated[2]}/${generated[3]} ${generated[4]}:${generated[5]} 的对话` : entry.name;
}
export function sidebarGroups(history: SessionEntry[], states: SessionState[], prefs: Pick<Preferences, "titles" | "pinned">, search: string, now = new Date()) {
  const entries = new Map(history.map(e => [sessionKey(e), e]));
  const live = new Set<string>();
  for (const state of states) if (!state.isDraft) {
    const key = `${state.mode}:${state.session}`;
    live.add(key);
    if (!entries.has(key)) entries.set(key, { name: state.session, mode: state.mode, modified: 0 });
  }
  const all = [...entries.values()].filter(e => `${sessionTitle(e, prefs.titles)} ${e.name}`.toLowerCase().includes(search.toLowerCase())).sort((a, b) => b.modified - a.modified);
  const pinned = all.filter(e => prefs.pinned.includes(sessionKey(e)));
  const open = all.filter(e => !prefs.pinned.includes(sessionKey(e)) && live.has(sessionKey(e)));
  const rest = all.filter(e => !prefs.pinned.includes(sessionKey(e)) && !live.has(sessionKey(e)));
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
  return { all, pinned, open, recent: [...groups].map(([label, entries]) => ({ label, entries })) };
}
