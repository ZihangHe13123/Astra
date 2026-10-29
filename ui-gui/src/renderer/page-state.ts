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

/**
 * The canvas a path belongs to (specs/canvas.md), named by its canvas.json: the first folder on the
 * path whose name ends in `.canvas`. A canvas is drawn whole, so writing any of its files shows it.
 */
export function canvasManifestFor(path: string): string | undefined {
  const match = /^(.*?\.canvas)(?=[\\/]|$)/i.exec(path.trim());
  if (!match) return undefined;
  const separator = path.includes("\\") && !path.includes("/") ? "\\" : "/";
  return `${match[1]}${separator}canvas.json`;
}

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

/** What the canvas tool made, looked at or printed: the canvas, by its canvas.json. */
function canvasFromTool(tool: ToolLike): string | undefined {
  if (typeof tool.output !== "string") return undefined;
  const created = /^Canvas: (.+canvas\.json)$/m.exec(tool.output);
  if (created) return created[1].trim();
  try { const result = JSON.parse(tool.output); return typeof result?.canvas === "string" ? result.canvas : undefined; }
  catch { return undefined; }
}

/** The page or canvas a finished write refers to: what the call was asked to write, if it wrote it. */
export function pagePathFromTool(tool: ToolLike): string | undefined {
  const name = typeof tool.name === "string" ? tool.name : "";
  if (tool.error || tool.type !== "tool_result") return undefined;
  if (name === "canvas" || name === "canvas_look") return canvasFromTool(tool);
  if (!WRITERS.test(name)) return undefined;
  let args: unknown = tool.arguments;
  if (typeof args === "string") { try { args = JSON.parse(args); } catch { return undefined; } }
  if (!args || typeof args !== "object") return undefined;
  const { path, file_path, patch } = args as { path?: unknown; file_path?: unknown; patch?: unknown };
  const shown = (value: unknown) => typeof value !== "string" ? undefined : isCanvasPath(value) ? value : canvasManifestFor(value);
  const named = shown(path) ?? shown(file_path);
  if (named) return named;
  if (typeof patch !== "string") return undefined;
  // A patch names its files; the last page or canvas it adds or updates is the one to look at.
  const files = [...patch.matchAll(/^\*\*\* (?:Add|Update) File: (.+)$/gm)].map(match => shown(match[1].trim())).filter(Boolean);
  return files.at(-1);
}
