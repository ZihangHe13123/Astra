/** What the memory page shows, from the backend's read-only `memory` query. */

export type CoreEntry = { id: string; content: string; scope: string };
export type CoreFile = {
  scope: "user" | "memory"; file: string; entries: CoreEntry[];
  usage: { chars: number; limit: number; available_chars: number; over_limit: boolean } | null;
};
export type SavedMemory = {
  id: string; summary: string; content: string; kind: string; status: string; scope: string; saved_by_astra: boolean;
  created_at: string; valid_until: string; supersedes: string; source_session: string; uses: number; last_used_at: string;
};
export type Proposal = {
  id: number; action: string; summary: string; content: string; previous: string; memory_id: string; reason: string;
  evidence: { date: string; text: string }[];
};
export type MemoryOverview = { core: CoreFile[]; saved: SavedMemory[]; proposals: Proposal[] };

export const CORE_TITLES: Record<string, { title: string; hint: string }> = {
  user: { title: "关于你", hint: "USER.md：称呼、身份、长期偏好。每次对话都带着。" },
  memory: { title: "长期笔记", hint: "MEMORY.md：环境、约定、工作方式。每次对话都带着。" },
};

const KINDS: Record<string, string> = {
  user_fact: "关于你", preference: "偏好", observation: "观察", episode: "经历", persona: "人格", task_reference: "任务",
};

export function kindLabel(kind: string) {
  return KINDS[kind] || kind;
}

const STATUSES: Record<string, string> = { superseded: "已被新版本替换", forgotten: "已忘记", expired: "已过期" };

export function statusLabel(status: string) {
  return status === "active" ? "" : STATUSES[status] || status;
}

/** How full a core file is; past nine tenths the page suggests making room. */
export function coreRoom(file: CoreFile) {
  if (!file.usage) return { percent: 0, label: "还没有内容", nearlyFull: false };
  const percent = Math.min(100, Math.round(file.usage.chars / Math.max(1, file.usage.limit) * 100));
  return { percent, label: `${file.usage.chars} / ${file.usage.limit} 字`, nearlyFull: percent >= 90 };
}

export function dateLabel(iso: string) {
  return iso ? iso.slice(0, 10) : "";
}

export function usedLabel(memory: SavedMemory) {
  if (!memory.uses) return "还没用到过";
  return `用到 ${memory.uses} 次${memory.last_used_at ? `，最近 ${dateLabel(memory.last_used_at)}` : ""}`;
}

/** Saved memories matching a filter, by summary, full text or kind. */
export function filterMemories(memories: SavedMemory[], text: string) {
  const needle = text.trim().toLowerCase();
  if (!needle) return memories;
  return memories.filter(memory => [memory.summary, memory.content, kindLabel(memory.kind)]
    .some(value => value.toLowerCase().includes(needle)));
}

const ACTIONS: Record<string, string> = { remember: "记住", revise: "更新", forget: "忘记" };

export function proposalLabel(proposal: Proposal) {
  return ACTIONS[proposal.action] || proposal.action;
}
