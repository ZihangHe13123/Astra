import { isAbsolute, relative, sep } from "node:path";
import type { Preferences } from "../bridge.js";

export function checkedPreferences(value: unknown): Partial<Preferences> {
  if (!value || typeof value !== "object" || Array.isArray(value) || JSON.stringify(value).length > 4 * 1024 * 1024) throw new Error("Invalid preferences");
  const v = value as Record<string, unknown>;
  const allowed = ["theme", "drafts", "attachments", "workspaces", "pinned", "projects", "titles", "timeline", "lastSession", "detailWidth"];
  if (Object.keys(v).some(k => !allowed.includes(k))) throw new Error("Unknown preference");
  if (v.theme !== undefined) checkedString(v.theme, "theme", 40);
  if (v.timeline !== undefined && typeof v.timeline !== "boolean") throw new Error("Invalid timeline preference");
  if (v.detailWidth !== undefined && (typeof v.detailWidth !== "number" || !Number.isFinite(v.detailWidth) || v.detailWidth < 260 || v.detailWidth > 720)) throw new Error("Invalid details width");
  if (v.lastSession !== undefined && v.lastSession !== null) {
    if (!v.lastSession || typeof v.lastSession !== "object" || Array.isArray(v.lastSession)) throw new Error("Invalid last session");
    const last = v.lastSession as Record<string, unknown>;
    if (Object.keys(last).some(key => !["session", "mode", "workspace"].includes(key))) throw new Error("Invalid last session");
    for (const key of ["session", "mode", "workspace"]) checkedString(last[key], key, 32768);
    if (!["work", "local", "minimal", "bar"].includes(last.mode as string)) throw new Error("Invalid last mode");
  }
  for (const key of ["pinned", "projects"]) {
    const values = v[key];
    if (values === undefined) continue;
    if (!Array.isArray(values) || values.length > 10000) throw new Error(`Invalid ${key}`);
    for (const item of values) checkedString(item, key, 32768);
  }
  for (const key of ["drafts", "titles", "workspaces", "attachments"]) {
    const values = v[key];
    if (values === undefined) continue;
    if (!values || typeof values !== "object" || Array.isArray(values)) throw new Error(`Invalid ${key}`);
    for (const [name, item] of Object.entries(values)) {
      checkedString(name, key, 512);
      if (key === "attachments") {
        if (!Array.isArray(item) || item.length > 1000) throw new Error("Invalid attachment list");
        for (const path of item) checkedString(path, "attachment", 32768);
      } else checkedString(item, key);
    }
  }
  return v as Partial<Preferences>;
}

export function checkedString(value: unknown, label: string, limit = 1024 * 1024): string {
  if (typeof value !== "string" || value.length > limit || value.includes("\0")) throw new Error(`Invalid ${label}`);
  return value;
}
export function inside(root: string, target: string): boolean {
  const rel = relative(root, target);
  return !rel || (rel !== ".." && !rel.startsWith(`..${sep}`) && !isAbsolute(rel));
}
export function externalURL(value: unknown): string {
  const text = checkedString(value, "URL", 8192);
  const url = new URL(text);
  if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) throw new Error("Only HTTP(S) links can open externally");
  return url.toString();
}
export function checkedCommand(value: unknown): Record<string, any> {
  if (!value || typeof value !== "object" || Array.isArray(value) || JSON.stringify(value).length > 2 * 1024 * 1024) throw new Error("Invalid command");
  const c = value as Record<string, any>;
  const fields: Record<string, string[]> = {
    select_model: ["model_key", "request_id"], message: ["text", "submission_id"], image: ["path", "prompt", "submission_id"], command: ["cmd"],
    connect_provider: ["request_id", "route_id", "base_url", "api_key", "api_key_env"],
    cancel_connection: ["request_id"], refresh_models: [], tool_approval_response: ["request_id", "decision"],
    user_question_response: ["request_id"], user_question_cancel: ["request_id"],
    submission_status: ["submission_id"], restart_ack: ["request_id"],
  };
  if (!Object.hasOwn(fields, c.type)) throw new Error("Unsupported command");
  const allowed = ["type", ...fields[c.type], ...(c.type === "refresh_models" ? ["provider_id", "force"] : []),
    ...(c.type === "user_question_response" ? ["answers"] : [])];
  if (Object.keys(c).some(key => !allowed.includes(key))) throw new Error("Unexpected command field");
  for (const field of fields[c.type]) checkedString(c[field], field);
  if (["message", "image", "submission_status"].includes(c.type) && !/^[A-Za-z0-9_-]{1,128}$/.test(c.submission_id)) throw new Error("Invalid submission ID");
  if (c.type === "select_model" && (!/^[^\s]+$/.test(c.model_key) || c.model_key.length > 512)) throw new Error("Invalid model key");
  if (c.type === "command" && !c.cmd.startsWith("/")) throw new Error("Commands must begin with /");
  if (c.type === "tool_approval_response" && !["once", "session", "deny"].includes(c.decision)) throw new Error("Invalid approval decision");
  if (c.type === "user_question_response" && !Array.isArray(c.answers)) throw new Error("Missing answers");
  if (c.type === "refresh_models") {
    if (c.provider_id !== undefined) checkedString(c.provider_id, "provider", 256);
    if (c.force !== undefined && typeof c.force !== "boolean") throw new Error("Invalid force value");
  }
  return c;
}
