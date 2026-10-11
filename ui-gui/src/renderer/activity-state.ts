/** What the conversation shows of the agent's work: each tool call as one step, in the place it happened. */
import type { Message, TurnChangeNote, UIEvent } from "@astra/ui-core/session-state";
import { toolStatus } from "./tool-status.js";

export type Step = {
  /** The kind of work, as one short word. */
  verb: string;
  /** What it was done to: a path, a command, a query. */
  target: string;
  /** What came of it, when one line can say so. */
  result: string;
  tone: "ok" | "bad" | "live" | "plain";
};

const VERBS: [RegExp, string][] = [
  [/^browser_/, "BROWSE"], [/^computer_/, "SCREEN"], [/^git_/, "GIT"],
  [/^(search_web|search_images|fetch_url|extract_url|web_)/, "WEB"],
  [/^(search_|session_search|activity_search|hindsight_recall)/, "SEARCH"],
  [/^(read_|stat_|inspect_|doc_(outline|check|comments)|skill_view|skills_list|context_|checkpoint_list|process_read|delegate_read)/, "READ"],
  [/^(edit_|write_|apply_patch|str_replace|begin_file|commit_file|abort_file|doc_)/, "EDIT"],
  [/^(execute_|bash$|run_code|notebook_|process_)/, "RUN"],
  [/^(delegate_|team|peer_|conclave)/, "DELEGATE"],
  [/^(memory|plan_|goal|hindsight_|skill_|schedule_|report_)/, "NOTE"],
  [/^ask_/, "ASK"], [/^mcp__/, "MCP"],
];
const TARGETS = ["path", "file_path", "command", "code", "pattern", "query", "url", "goal", "selector", "name", "action", "text"];

function argumentsOf(tool: UIEvent): Record<string, unknown> {
  const raw = tool.arguments ?? tool.args;
  if (raw && typeof raw === "object") return raw as Record<string, unknown>;
  if (typeof raw !== "string") return {};
  try { const parsed = JSON.parse(raw); return parsed && typeof parsed === "object" ? parsed : {}; } catch { return {}; }
}
const oneLine = (text: string, limit: number) => {
  const line = text.split(/\r?\n/).map(part => part.trim()).find(Boolean) || "";
  return line.length > limit ? line.slice(0, limit - 1) + "…" : line;
};

export function describeTool(tool: UIEvent): Step {
  const name = String(tool.name || "tool");
  const verb = VERBS.find(([pattern]) => pattern.test(name))?.[1] ?? "TOOL";
  const args = argumentsOf(tool);
  let target = "";
  if (name === "apply_patch" && typeof args.patch === "string") {
    const files = [...args.patch.matchAll(/^\*\*\* (?:Add|Update|Delete) File: (.+)$/gm)].map(match => match[1].trim());
    target = files.length > 1 ? `${files[0]} +${files.length - 1}` : files[0] || "";
  }
  // A search is known by what it looks for, and only then by where.
  const keys = verb === "SEARCH" || verb === "WEB" ? ["pattern", "query", "url", ...TARGETS] : TARGETS;
  for (const key of keys) { if (target) break; if (typeof args[key] === "string" && args[key]) target = oneLine(args[key] as string, 160); }
  if (verb === "SEARCH" && typeof args.path === "string" && args.path && target !== args.path) target += `  in ${args.path}`;
  if (verb === "MCP") target = [name.split("__").slice(1).join(" · "), target].filter(Boolean).join("  ");
  else if (verb === "TOOL" || !target) target = [name, target].filter(Boolean).join("  ");
  const status = toolStatus(tool);
  if (status === "running") {
    const progress = tool.progress;
    const percent = typeof progress?.percent === "number" ? `${Math.round(progress.percent)}%` : "";
    return { verb, target, result: [progress?.message || progress?.stage || "", percent].filter(Boolean).join(" · "), tone: "live" };
  }
  if (tool.error || status === "failed") return { verb, target, result: oneLine(String(tool.error || "failed"), 72), tone: "bad" };
  if (status !== "completed") return { verb, target, result: status, tone: "plain" };
  const output = typeof tool.output === "string" ? tool.output.trimEnd() : "";
  // A run usually ends on its own summary line ("158 passed in 4.2s"); other output is better counted than quoted.
  const last = output.split(/\r?\n/).filter(line => line.trim()).pop()?.trim() || "";
  const result = tool.partial ? "partial" : verb === "RUN" && last && last.length <= 48 ? last
    : verb === "READ" || verb === "SEARCH" ? (output ? `${output.split(/\r?\n/).length} lines` : "") : "";
  return { verb, target, result, tone: "ok" };
}

/** 0.1s, 4.2s, 1:05, 1:02:03. */
export function span(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "";
  if (seconds < 60) return `${seconds < 10 ? seconds.toFixed(1) : Math.round(seconds)}s`;
  const whole = Math.round(seconds), pad = (value: number) => String(value).padStart(2, "0");
  return whole < 3600 ? `${Math.floor(whole / 60)}:${pad(whole % 60)}` : `${Math.floor(whole / 3600)}:${pad(Math.floor(whole / 60) % 60)}:${pad(whole % 60)}`;
}

export type Row = Message & { tools?: UIEvent[]; changes?: TurnChangeNote };
/** The rows of a conversation with the agent's work in it: after each message, the tool calls made
 * then, and the file changes reported then. A tool result with no place (a restored history) is
 * returned in `unplaced`, and so is one whose message is not among these rows. */
export function withActivity(rows: Message[], tools: UIEvent[] = [], changes: TurnChangeNote[] = []): { rows: Row[]; unplaced: UIEvent[] } {
  if (!tools.length && !changes.length) return { rows, unplaced: [] };
  const known = new Set(rows.map(row => row.id));
  const here = (place: unknown): place is string => typeof place === "string" && (place === "" || known.has(place));
  const toolsAfter = new Map<string, UIEvent[]>(), changesAfter = new Map<string, TurnChangeNote[]>(), unplaced: UIEvent[] = [];
  for (const tool of tools) {
    if (!here(tool.after_message)) { unplaced.push(tool); continue; }
    toolsAfter.set(tool.after_message, [...(toolsAfter.get(tool.after_message) || []), tool]);
  }
  for (const note of changes) if (here(note.after_message)) changesAfter.set(note.after_message, [...(changesAfter.get(note.after_message) || []), note]);
  const result: Row[] = [];
  const add = (place: string) => {
    const made = toolsAfter.get(place);
    if (made) result.push({ id: `activity:${place}`, role: "activity", content: "", tools: made });
    for (const note of changesAfter.get(place) || []) result.push({ id: `changes:${note.request_id}:${note.turn_seq}`, role: "changes", content: "", changes: note });
  };
  add("");
  for (const row of rows) { result.push(row); add(row.id); }
  return { rows: result, unplaced };
}
