// Show Markdown documents written with the doc_* tools inside the terminal.
import { execFile } from "node:child_process";
import { readFileSync, statSync } from "node:fs";
import { isAbsolute, posix, win32 } from "node:path";
import stringWidth from "string-width";
import type { ToolResultRecord } from "./tool-results.js";

export type DocumentLineStyle = "title" | "heading" | "meta" | "pending" | "comment" | "table" | "code" | "plain";
export interface DocumentLine { text: string; style: DocumentLineStyle }

type DocumentResult = Pick<ToolResultRecord, "name" | "output" | "error">;

const MAX_DOCUMENT_BYTES = 512 * 1024;
const MARKER = /^\s*<!--\s*astra:section\b[^\n]*-->\s*$/;
const FENCE = /^ {0,3}(`{3,}|~{3,})/;
const COMMENT_START = /<!--\s*@astra\b\s*:?/;
const COMMENT = /<!--\s*@astra\b\s*:?([\s\S]*?)-->/g;
const CONTROL = /[\u0000-\u001f\u007f]/gu;

function payload(result: DocumentResult): Record<string, unknown> | undefined {
  if (!result.name.startsWith("doc_") || result.error || !result.output) return undefined;
  try {
    const value: unknown = JSON.parse(result.output);
    return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : undefined;
  } catch {
    return undefined;
  }
}

const clean = (value: unknown) => String(value ?? "").replace(CONTROL, " ").trim();
const fileName = (path: string) => path.split(/[\\/]/).pop() || path;

/** The Markdown document a successful doc_* result refers to (the source of an export). */
export function documentPath(result: DocumentResult): string | undefined {
  const data = payload(result);
  const path = data && (result.name === "doc_export" ? data.source : data.path);
  return typeof path === "string" && /\.(md|markdown)$/i.test(path) ? path : undefined;
}

/** One line for the transcript: what changed and what is still open. */
export function documentSummary(result: DocumentResult & { id?: number }): string | undefined {
  const data = payload(result);
  const path = documentPath(result);
  if (!data || !path) return undefined;
  const section = (data.section ?? {}) as Record<string, unknown>;
  const heading = section.id === "_lead" ? "导语" : clean(section.heading || section.id);
  const count = (value: unknown) => typeof value === "number" ? value : 0;
  const parts = [`文档 ${clean(fileName(path))}`];
  switch (result.name) {
    case "doc_create": parts.push(`新建 ${Array.isArray(data.sections) ? data.sections.length : 0} 节`); break;
    case "doc_write_section": parts.push(`写入「${heading}」`); break;
    case "doc_add_section": parts.push(`新增「${heading}」`); break;
    case "doc_remove_section": parts.push(`删除「${clean((data.removed as Record<string, unknown> | undefined)?.heading)}」`); break;
    case "doc_outline": parts.push(`${Array.isArray(data.sections) ? data.sections.length : 0} 节`); break;
    case "doc_comments": parts.push(`${count(data.open_comments)} 条待处理评论`); break;
    case "doc_resolve_comment": parts.push(`已处理 1 条评论，剩 ${count(data.open_comments)} 条`); break;
    case "doc_export": parts.push(`导出 ${clean(data.format)}（${clean(data.engine)}）`); break;
  }
  if (count(data.pending_sections)) parts.push(`${count(data.pending_sections)} 节待写`);
  if (count(data.open_comments) && !["doc_comments", "doc_resolve_comment"].includes(result.name)) {
    parts.push(`${count(data.open_comments)} 条评论`);
  }
  if (Array.isArray(data.warnings) && data.warnings.length) parts.push(`${data.warnings.length} 条警告`);
  parts.push(result.id === undefined ? "/doc 查看" : `/doc ${result.id} 查看`);
  return parts.join(" · ");
}

function closesFence(line: string, fence: string): boolean {
  const indent = line.length - line.trimStart().length;
  const body = line.trim();
  return indent <= 3 && body.length >= fence.length && [...body].every((char) => char === fence[0]);
}

function splitRow(row: string): string[] {
  let body = row.trim();
  if (body.startsWith("|")) body = body.slice(1);
  if (body.endsWith("|") && !body.endsWith("\\|")) body = body.slice(0, -1);
  return body.split(/(?<!\\)\|/).map((cell) => cell.trim());
}

/** Pad pipe-table cells to their display width (CJK counts double). */
export function alignTable(rows: string[]): string[] {
  const cells = rows.map(splitRow);
  const isRule = (row: string[]) => row.length > 0 && row.every((cell) => /^:?-+:?$/.test(cell));
  const columns = Math.max(...cells.map((row) => row.length));
  const widths = Array.from({ length: columns }, (_, column) => Math.max(
    3,
    ...cells.filter((row) => !isRule(row)).map((row) => stringWidth(row[column] ?? "")),
  ));
  return cells.map((row) => "| " + widths.map((width, column) => {
    if (isRule(row)) return "-".repeat(width);
    const cell = row[column] ?? "";
    return cell + " ".repeat(Math.max(0, width - stringWidth(cell)));
  }).join(" | ") + " |");
}

/** Terminal view of a document: markers hidden, comments marked, tables aligned. */
export function renderDocument(text: string): DocumentLine[] {
  const lines: DocumentLine[] = [];
  let fence = "";
  let comment: string[] | null = null;
  let table: string[] = [];
  let seenHeading = false;
  const flushTable = () => {
    if (!table.length) return;
    lines.push(...alignTable(table).map((row): DocumentLine => ({ text: row, style: "table" })));
    table = [];
  };
  for (const raw of text.replace(/\r\n?/g, "\n").split("\n")) {
    const source = raw.replace(CONTROL, " ");
    if (fence) {
      lines.push({ text: source, style: "code" });
      if (closesFence(source, fence)) fence = "";
      continue;
    }
    if (comment) {
      const end = source.indexOf("-->");
      comment.push((end < 0 ? source : source.slice(0, end)).trim());
      if (end < 0) continue;
      lines.push({ text: `[评论] ${comment.filter(Boolean).join(" ")}`, style: "comment" });
      comment = null;
      continue;
    }
    const opening = source.match(FENCE);
    if (opening) {
      flushTable();
      fence = opening[1];
      lines.push({ text: source, style: "code" });
      continue;
    }
    if (MARKER.test(source)) continue;
    let line = source.replace(COMMENT, (_match, body: string) => `[评论：${body.split(/\s+/).filter(Boolean).join(" ")}]`);
    const unclosed = line.search(COMMENT_START);
    if (unclosed >= 0) {
      comment = [line.slice(unclosed).replace(COMMENT_START, "").trim()];
      line = line.slice(0, unclosed).trimEnd();
      if (!line.trim()) continue;
    }
    if (/^\s*\|/.test(line)) {
      table.push(line);
      continue;
    }
    flushTable();
    const heading = /^ {0,3}#{1,6}(\s|$)/.test(line);
    const style: DocumentLineStyle = heading
      ? (!seenHeading && /^ {0,3}#(\s|$)/.test(line) ? "title" : "heading")
      : /^\s*\*Pending\b.*\*\s*$/.test(line) ? "pending"
      : line.includes("[评论") ? "comment"
      : "plain";
    if (heading) seenHeading = true;
    lines.push({ text: line, style });
  }
  flushTable();
  if (comment) lines.push({ text: `[评论] ${comment.filter(Boolean).join(" ")}`, style: "comment" });
  return lines;
}

function readDocument(path: string): string {
  if (!isAbsolute(path)) throw new Error("文档路径不是绝对路径");
  const size = statSync(path).size;
  if (size > MAX_DOCUMENT_BYTES) throw new Error(`文档超过 ${MAX_DOCUMENT_BYTES / 1024} KB，请用 /doc open 打开`);
  return readFileSync(path, "utf8");
}

/** Lines for the tool details view: summary, path, warnings, then the current document. */
export function documentDetail(
  result: DocumentResult & { id?: number },
  read: (path: string) => string = readDocument,
): DocumentLine[] | undefined {
  const summary = documentSummary({ ...result, id: undefined });
  const path = documentPath(result);
  if (!summary || !path) return undefined;
  const header: DocumentLine[] = [
    { text: summary.replace(/ · \/doc 查看$/, ""), style: "heading" },
    { text: clean(path), style: "meta" },
  ];
  const warnings = payload(result)?.warnings;
  if (Array.isArray(warnings)) header.push(...warnings.map((warning): DocumentLine => ({ text: `警告：${clean(warning)}`, style: "comment" })));
  header.push({ text: "", style: "plain" });
  try {
    return [...header, ...renderDocument(read(path))];
  } catch (error) {
    return [...header, { text: `无法读取文档：${error instanceof Error ? error.message : String(error)}`, style: "comment" }];
  }
}

/** The newest document result, or the one with the given id. */
export function findDocumentResult<T extends DocumentResult & { id: number }>(results: T[], argument = ""): T | undefined {
  const id = argument.trim();
  if (id) return results.find((item) => item.id === Number(id) && documentPath(item));
  return [...results].reverse().find((item) => documentPath(item));
}

/** Keep an open document view on the newest result for the same document. */
export function followDocumentView<T extends DocumentResult & { id: number }>(
  current: number | null,
  results: T[],
  incoming: T,
): number | null {
  const path = documentPath(incoming);
  if (current === null || !path) return current;
  const shown = results.find((item) => item.id === current);
  return shown && documentPath(shown) === path ? incoming.id : current;
}

export function documentOpenCommand(path: string, platform: NodeJS.Platform = process.platform): [string, string[]] {
  const absolute = platform === "win32" ? win32.isAbsolute(path) : posix.isAbsolute(path);
  if (!absolute || /[\u0000-\u001f\u007f]/u.test(path) || !/\.(md|markdown)$/i.test(path)) {
    throw new Error("Invalid document path");
  }
  if (platform === "darwin") return ["open", [path]];
  if (platform === "win32") return ["rundll32.exe", ["url.dll,FileProtocolHandler", path]];
  return ["xdg-open", [path]];
}

/** Handle `/doc open [id]`: open the document with the system's default app. */
export async function openDocument(
  results: (DocumentResult & { id: number })[],
  argument = "",
  run: (command: string, args: string[]) => Promise<void> = (command, args) => new Promise((resolve, reject) => {
    execFile(command, args, { timeout: 10000, windowsHide: true }, (error) => error ? reject(error) : resolve());
  }),
): Promise<string> {
  if (argument && !/^[1-9]\d*$/u.test(argument)) return "用法：/doc [编号] · /doc open [编号]";
  const result = findDocumentResult(results, argument);
  const path = result && documentPath(result);
  if (!path) return "没有可打开的文档。先让 Astra 用文档工具写一份文档，再使用 /doc。";
  const [command, args] = documentOpenCommand(path);
  await run(command, args);
  return `已用系统程序打开 ${fileName(path)}。`;
}
