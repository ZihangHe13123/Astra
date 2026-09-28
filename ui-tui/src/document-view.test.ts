import assert from "node:assert/strict";
import stringWidth from "string-width";
import {
  alignTable,
  documentDetail,
  documentOpenCommand,
  documentPath,
  documentSummary,
  findDocumentResult,
  followDocumentView,
  openDocument,
  renderDocument,
} from "./document-view.js";
import { toolResultBody, wrapStyledLines } from "./tool-results.js";

const path = "/work/notes/报告.md";
const result = (id: number, name: string, output: unknown, error = "") => ({ id, name, error, output: JSON.stringify(output) });

// Which results are documents, and the one-line transcript summary.
const written = result(7, "doc_write_section", {
  path, pending_sections: 1, open_comments: 2, section: { id: "results", heading: "结果\u001b[2J" },
});
assert.equal(documentPath(written), path);
assert.equal(documentPath(result(1, "doc_export", { path: "/work/notes/报告.docx", source: path })), path);
assert.equal(documentPath(result(1, "doc_write_section", { path }, "section_changed")), undefined);
assert.equal(documentPath(result(1, "write_file", { path })), undefined);
assert.equal(documentPath(result(1, "doc_outline", { path: "/work/a.txt" })), undefined);
assert.equal(documentPath({ name: "doc_outline", error: "", output: "not json" }), undefined);
assert.equal(documentSummary(written), "文档 报告.md · 写入「结果 [2J」 · 1 节待写 · 2 条评论 · /doc 7 查看");
assert.equal(toolResultBody(written), documentSummary(written));
assert.match(documentSummary(result(2, "doc_write_section", { path, pending_sections: 0, section: { id: "_lead" } }))!, /写入「导语」/);
assert.match(documentSummary(result(3, "doc_create", { path, pending_sections: 3, sections: [{}, {}, {}] }))!, /新建 3 节 · 3 节待写/);
assert.match(documentSummary(result(4, "doc_resolve_comment", { path, open_comments: 1 }))!, /已处理 1 条评论，剩 1 条 · \/doc 4/);
assert.match(documentSummary(result(5, "doc_export", { path: "/w/a.pdf", source: path, format: "pdf", engine: "pandoc + LibreOffice", warnings: ["x"] }))!,
  /导出 pdf（pandoc \+ LibreOffice） · 1 条警告/);
// doc_edit is summarized by its action, like the per-action aliases.
assert.equal(documentSummary(result(10, "doc_edit", { action: "write", path, section: { id: "results", heading: "结果" } })),
  "文档 报告.md · 写入「结果」 · /doc 10 查看");
assert.match(documentSummary(result(11, "doc_edit", { action: "add", path, pending_sections: 1, section: { id: "risks", heading: "风险" } }))!,
  /新增「风险」 · 1 节待写/);
assert.match(documentSummary(result(12, "doc_edit", { action: "remove", path, removed: { heading: "风险" } }))!, /删除「风险」/);
assert.equal(documentSummary(result(13, "doc_edit", { action: "resolve_comment", path, open_comments: 1 })),
  "文档 报告.md · 已处理 1 条评论，剩 1 条 · /doc 13 查看");
assert.equal(documentPath(result(14, "doc_edit", { action: "write", path })), path);

// Rendering hides markers, marks comments and pending text, keeps code, aligns tables.
const lines = renderDocument([
  "# 评测报告",
  "",
  "导语 <!-- @astra: 更短一些 --> 结束。",
  '<!-- astra:section id="results" -->',
  "## 结果",
  "",
  "| 朝向 | F1 |",
  "| --- | ---: |",
  "| 侧对 | 0.71 |",
  "",
  "```py",
  "# not a heading",
  "<!-- @astra: not a comment -->",
  "```",
  '<!-- astra:section id="plan" status="pending" intent="Later" -->',
  "## 计划",
  "",
  "*Pending: Later*",
  "<!-- @astra:",
  "  跨行的评论",
  "-->",
].join("\r\n"));
assert.ok(!lines.some((line) => line.text.includes("astra:section")));
assert.deepEqual(lines.filter((line) => line.style === "title").map((line) => line.text), ["# 评测报告"]);
assert.deepEqual(lines.filter((line) => line.style === "heading").map((line) => line.text), ["## 结果", "## 计划"]);
assert.deepEqual(lines.find((line) => line.text.startsWith("导语")), { text: "导语 [评论：更短一些] 结束。", style: "comment" });
assert.deepEqual(lines.filter((line) => line.style === "pending").map((line) => line.text), ["*Pending: Later*"]);
assert.equal(lines.at(-1)?.text, "[评论] 跨行的评论");
const code = lines.filter((line) => line.style === "code").map((line) => line.text);
assert.deepEqual(code, ["```py", "# not a heading", "<!-- @astra: not a comment -->", "```"]);
const table = lines.filter((line) => line.style === "table").map((line) => line.text);
assert.equal(table.length, 3);
assert.equal(new Set(table.map((row) => stringWidth(row))).size, 1);
assert.deepEqual(alignTable(["|a|b|", "|-|-|", "|中文|x|"]), ["| a    | b   |", "| ---- | --- |", "| 中文 | x   |"]);

// The details view reads the current file; failures stay visible instead of throwing.
const detail = documentDetail(result(9, "doc_export", { path: "/w/a.docx", source: path, format: "docx", engine: "pandoc", warnings: ["Image not found"] }),
  (file) => { assert.equal(file, path); return "# T\n\nBody"; })!;
assert.equal(detail[0].style, "heading");
assert.ok(!detail[0].text.includes("/doc"));
assert.deepEqual(detail.slice(1, 3), [{ text: path, style: "meta" }, { text: "警告：Image not found", style: "comment" }]);
assert.deepEqual(detail.slice(-3).map((line) => line.text), ["# T", "", "Body"]);
const unreadable = documentDetail(written, () => { throw new Error("gone"); })!;
assert.equal(unreadable.at(-1)?.text, "无法读取文档：gone");
assert.equal(documentDetail(result(1, "read_file", { path })), undefined);

// Wrapped continuation rows keep their style.
assert.deepEqual(wrapStyledLines([{ text: "abcdef", style: "heading" }, { text: "", style: "plain" }], 4), [
  { text: "abcd", style: "heading" }, { text: "ef", style: "heading" }, { text: " ", style: "plain" },
]);

// /doc chooses the newest document result, or a numbered one.
const results = [result(1, "doc_create", { path, sections: [] }), result(2, "read_file", { path }), result(3, "doc_outline", { path: "/w/b.md", sections: [] })];
assert.equal(findDocumentResult(results)?.id, 3);
assert.equal(findDocumentResult(results, "1")?.id, 1);
assert.equal(findDocumentResult(results, "2"), undefined);
assert.equal(findDocumentResult([results[1]]), undefined);

// An open view follows later writes to the same document only.
const later = result(8, "doc_write_section", { path: "/w/b.md", section: { id: "x", heading: "X" } });
const withLater = [...results, later];
assert.equal(followDocumentView(3, withLater, later), 8);
assert.equal(followDocumentView(1, withLater, later), 1);
assert.equal(followDocumentView(2, withLater, later), 2);
assert.equal(followDocumentView(null, withLater, later), null);
assert.equal(followDocumentView(3, withLater, result(9, "read_file", { path: "/w/b.md" })), 3);

// /doc open uses the platform opener and validates the path.
assert.deepEqual(documentOpenCommand(path, "darwin"), ["open", [path]]);
assert.deepEqual(documentOpenCommand(path, "linux"), ["xdg-open", [path]]);
assert.deepEqual(documentOpenCommand("C:\\w\\a.md", "win32")[0], "rundll32.exe");
assert.throws(() => documentOpenCommand("notes/a.md", "darwin"));
assert.throws(() => documentOpenCommand("/w/a.md\n", "darwin"));
assert.throws(() => documentOpenCommand("/w/a.sh", "darwin"));
const opened: string[][] = [];
assert.match(await openDocument(results, "", async (command, args) => { opened.push([command, ...args]); }), /已用系统程序打开 b\.md/);
assert.equal(opened[0].at(-1), "/w/b.md");
assert.match(await openDocument(results, "x"), /用法/);
assert.match(await openDocument([], ""), /没有可打开的文档/);
console.log("document-view tests passed");
