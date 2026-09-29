// Follow saved documents that the agent writes or exports with the doc_* tools.
// Tool entries from the session state; only these fields are read.
type ToolLike = { readonly [key: string]: unknown };

export const isMarkdownPath = (path: string) => /\.(md|markdown)$/i.test(path);

/** The saved document a successful doc_* tool result refers to. */
export function docPathFromTool(tool: ToolLike): string | undefined {
  const name = typeof tool.name === "string" ? tool.name : "";
  if (!name.startsWith("doc_") || tool.error || typeof tool.output !== "string" || !tool.output) return undefined;
  try {
    const result = JSON.parse(tool.output);
    if (result?.success === false || result?.error) return undefined;
    const path = result?.path;
    if (typeof path === "string" && /\.(md|markdown|pdf|docx|pptx|xlsx)$/i.test(path)) return path;
    return name === "doc_export" && typeof result?.source === "string" && isMarkdownPath(result.source) ? result.source : undefined;
  } catch { return undefined; }
}

/** The newest document written in this session; restored history is ignored. */
export function latestDoc(tools: ToolLike[]): { path: string; index: number } | undefined {
  let latest: { path: string; index: number } | undefined;
  for (const tool of tools) {
    if (tool.historical) continue;
    const path = docPathFromTool(tool);
    const index = Number(tool.result_index) || 0;
    if (path && (!latest || index >= latest.index)) latest = { path, index };
  }
  return latest;
}

export function dirname(path: string): string {
  const cut = Math.max(path.lastIndexOf("/"), path.lastIndexOf("\\"));
  return cut > 0 ? path.slice(0, cut) : cut === 0 ? path.slice(0, 1) : "";
}

/** Resolve a relative image or link target against the document's folder. */
export function resolveAgainst(base: string | undefined, target: string): string {
  if (!base || !target || /^(?:[a-z][a-z\d+.-]*:|\/|\\|[A-Za-z]:[\\/]|#)/i.test(target)) return target;
  let decoded = target;
  try { decoded = decodeURI(target); } catch { /* keep the raw target */ }
  const separator = base.includes("\\") && !base.includes("/") ? "\\" : "/";
  return `${base.replace(/[\\/]+$/, "")}${separator}${decoded}`;
}

/** Open review comments (<!-- @astra: ... -->) outside fenced code. */
export function openComments(text: string): string[] {
  const comments: string[] = [];
  let fence = "";
  const visible = text.split(/\r?\n/).map(line => {
    const marker = line.match(/^ {0,3}(`{3,}|~{3,})/);
    if (fence) { if (marker && marker[1][0] === fence[0] && marker[1].length >= fence.length && !line.trim().slice(marker[1].length).trim()) fence = ""; return ""; }
    if (marker) { fence = marker[1]; return ""; }
    return line;
  }).join("\n");
  for (const match of visible.matchAll(/<!--[ \t]*@astra\b[ \t]*:?([\s\S]*?)-->/g)) {
    const comment = match[1].split(/\s+/).filter(Boolean).join(" ");
    if (comment) comments.push(comment);
  }
  return comments;
}
