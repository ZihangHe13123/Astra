/** What the desktop decides about a page before and while its frame runs. */
export const PAGE_FRAME_URL = "astra://card/page.html";

/** A file the page preview can render. */
export const isPagePath = (path: string) => /\.html?$/i.test(path);
/**
 * A page made to be looked at. The name is how the model says so: writing `<name>.page.html`
 * opens it beside the conversation, where an ordinary HTML file of a project being worked on
 * does not.
 */
export const isCanvasPath = (path: string) => /\.page\.html?$/i.test(path);

export type PageMessage = { type: "ready" } | { type: "pong"; id: number } | { type: "error"; message: string };
/** A page's script can post anything to the host. Only these three reports mean something, and none grants anything. */
export function pageMessage(data: unknown): PageMessage | undefined {
  if (!data || typeof data !== "object" || (data as { astraPage?: unknown }).astraPage !== true) return undefined;
  const { type, message, id } = data as { type?: unknown; message?: unknown; id?: unknown };
  if (type === "ready") return { type };
  if (type === "pong" && typeof id === "number" && Number.isSafeInteger(id)) return { type, id };
  if (type === "error" && typeof message === "string") return { type, message: message.slice(0, 300) };
  return undefined;
}

// Tool entries from the session state; only these fields are read.
type ToolLike = { readonly [key: string]: unknown };
const WRITERS = /^(?:write_file|edit_file|apply_patch|commit_file|str_replace\w*|multi_edit)$/;

/** The canvas page a finished write refers to: what the call was asked to write, if it wrote it. */
export function pagePathFromTool(tool: ToolLike): string | undefined {
  const name = typeof tool.name === "string" ? tool.name : "";
  if (!WRITERS.test(name) || tool.error || tool.type !== "tool_result") return undefined;
  let args: unknown = tool.arguments;
  if (typeof args === "string") { try { args = JSON.parse(args); } catch { return undefined; } }
  if (!args || typeof args !== "object") return undefined;
  const { path, file_path, patch } = args as { path?: unknown; file_path?: unknown; patch?: unknown };
  const named = [path, file_path].find((value): value is string => typeof value === "string" && isCanvasPath(value));
  if (named) return named;
  if (typeof patch !== "string") return undefined;
  // A patch names its files; the last page it adds or updates is the one to look at.
  const files = [...patch.matchAll(/^\*\*\* (?:Add|Update) File: (.+)$/gm)].map(match => match[1].trim()).filter(isCanvasPath);
  return files.at(-1);
}
