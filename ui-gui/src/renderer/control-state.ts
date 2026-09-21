import type { CommandDescription } from "../bridge.js";
import type { UIEvent } from "@astra/ui-core/session-state";

const navigationCommands = new Set(["/model", "/connect", "/session", "/permissions", "/persona", "/theme", "/changes", "/tool", "/gallery"]);
export function opensCommandInterface(command: string): boolean { return navigationCommands.has(command); }
export function filterCommands(commands: CommandDescription[], query: string, names: Record<string, string>): CommandDescription[] {
  const text = query.trim().toLowerCase();
  const root = text.startsWith("/") ? text.split(/\s+/)[0] : "";
  if (root && /\s/.test(text) && commands.some(c => c.command === root)) return commands.filter(c => c.command === root);
  return commands.filter(c => `${c.command} ${names[c.command.slice(1)] || ""} ${c.description}`.toLowerCase().includes(text));
}
export function moveSelection(current: number, count: number, direction: number): number {
  return count ? (current + direction + count) % count : 0;
}
export function connectionFlow(info: Record<string, UIEvent> | undefined, localRequest = "") {
  const pending = info?.connection_pending;
  const savedAuth = info?.connection_auth;
  const savedResult = info?.connection_result;
  const requestId = localRequest || pending?.request_id || savedAuth?.request_id || savedResult?.request_id || "";
  const auth = requestId && savedAuth?.request_id === requestId ? savedAuth : undefined;
  const result = requestId && savedResult?.request_id === requestId ? savedResult : undefined;
  return { requestId: String(requestId), auth: result ? undefined : auth, result, connecting: !!requestId && !result,
    routeId: pending?.request_id === requestId ? String(pending?.route_id || "") : "" };
}
