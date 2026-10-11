import { test } from "node:test";
import assert from "node:assert/strict";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { readFileSync } from "node:fs";
import { PAGE_FRAME_URL, isCanvasPath, isPagePath, pageMessage, pagePathFromTool } from "../src/renderer/page-state.js";
import { latestDoc } from "../src/renderer/doc-preview.js";
import { DocumentPreviewBody } from "../src/renderer/file-preview-panel.js";
import { CARD_FRAME_POLICY, PAGE_FRAME_URL as SERVED_URL, cardFrameResponse, isCardFrameAddress } from "../src/main/card-frame.js";
import { PAGE_PREVIEW_LIMIT, filePreview } from "../src/main/file-preview.js";
import { CARD_THEME_COLORS } from "../src/renderer/card-state.js";

test("any HTML file can be shown as a page, and only one named as a canvas opens by itself", () => {
  for (const path of ["a.html", "a.HTM", "/work/site/index.html", "C:\\work\\draft.page.html"]) assert.equal(isPagePath(path), true, path);
  for (const path of ["a.md", "html", "a.html.txt", "a.xhtml"]) assert.equal(isPagePath(path), false, path);
  for (const path of ["draft.page.html", "/work/DESIGN.PAGE.HTML", "notes/v2.page.htm"]) assert.equal(isCanvasPath(path), true, path);
  for (const path of ["index.html", "page.html", "a.page.html.bak", "pages/home.html"]) assert.equal(isCanvasPath(path), false, path);
});

test("a finished write of a canvas names the page to open, and nothing else does", () => {
  const written = (name: string, args: unknown, more: object = {}) => pagePathFromTool({ type: "tool_result", name, arguments: args, output: "ok", ...more });
  assert.equal(written("write_file", JSON.stringify({ path: "draft.page.html", content: "<h1>x</h1>" })), "draft.page.html");
  assert.equal(written("edit_file", { file_path: "/work/draft.page.html", old: "a", new: "b" }), "/work/draft.page.html");
  // A patch names its files; the page it touched last is the one to look at.
  assert.equal(written("apply_patch", { patch: "*** Begin Patch\n*** Update File: src/app.ts\n*** Add File: a.page.html\n*** Update File: b.page.html\n*** Delete File: c.page.html\n*** End Patch" }), "b.page.html");
  // An ordinary HTML file of a project being worked on is not something to pop open.
  assert.equal(written("write_file", { path: "site/index.html", content: "" }), undefined);
  assert.equal(written("apply_patch", { patch: "*** Update File: site/index.html" }), undefined);
  // Nor is a write that failed, one still running, a read, or arguments that cannot be read.
  assert.equal(written("write_file", { path: "draft.page.html" }, { error: "denied" }), undefined);
  assert.equal(pagePathFromTool({ type: "tool_calls", name: "write_file", arguments: { path: "draft.page.html" } }), undefined);
  assert.equal(written("read_file", { path: "draft.page.html" }), undefined);
  assert.equal(written("write_file", '{"path": "draft.page.h'), undefined);
  assert.equal(written("write_file", null), undefined);
});

test("the newest document or page written in the session is the one followed", () => {
  const doc = { type: "tool_result", name: "doc_create", output: JSON.stringify({ path: "/work/report.md" }), result_index: 1 };
  const page = { type: "tool_result", name: "write_file", arguments: { path: "draft.page.html" }, output: "ok", result_index: 2 };
  assert.deepEqual(latestDoc([doc, page]), { path: "draft.page.html", index: 2 });
  assert.deepEqual(latestDoc([page, { ...doc, result_index: 3 }]), { path: "/work/report.md", index: 3 });
  // Rewriting the page is a new thing to show, which is what makes the preview reload.
  assert.deepEqual(latestDoc([page, { ...page, result_index: 5 }]), { path: "draft.page.html", index: 5 });
  assert.equal(latestDoc([{ ...page, historical: true }]), undefined);
});

test("a page's script can tell the host three things and no more", () => {
  assert.deepEqual(pageMessage({ astraPage: true, type: "ready" }), { type: "ready" });
  assert.deepEqual(pageMessage({ astraPage: true, type: "pong", id: 7 }), { type: "pong", id: 7 });
  for (const id of [undefined, "7", 1.5, Number.NaN]) assert.equal(pageMessage({ astraPage: true, type: "pong", id }), undefined);
  assert.deepEqual(pageMessage({ astraPage: true, type: "error", message: "x".repeat(900) }), { type: "error", message: "x".repeat(300) });
  for (const data of [null, "ready", { type: "ready" }, { astraCard: true, type: "ready" }, { astraPage: true, type: "height", height: 9 }, { astraPage: true, type: "error" }, { astraPage: true, type: "open", path: "/etc/passwd" }])
    assert.equal(pageMessage(data), undefined, JSON.stringify(data));
});

test("the page document is served under the card's policy, and is the only other address a frame may hold", async () => {
  assert.equal(PAGE_FRAME_URL, SERVED_URL);
  const response = cardFrameResponse("/page.html");
  assert.equal(response.status, 200);
  assert.equal(response.headers.get("Content-Security-Policy"), CARD_FRAME_POLICY);
  assert.equal(response.headers.get("X-Content-Type-Options"), "nosniff");
  assert.match(CARD_FRAME_POLICY, /default-src 'none'/);
  assert.doesNotMatch(CARD_FRAME_POLICY, /https?:|\*|connect-src|'self'/);
  for (const address of [PAGE_FRAME_URL, `${PAGE_FRAME_URL}?dark`]) assert.equal(isCardFrameAddress(address), true);
  for (const address of [`${PAGE_FRAME_URL}?x`, `${PAGE_FRAME_URL}#x`, "astra://card/other.html", "astra://app/index.html", "https://example.com/page.html"]) assert.equal(isCardFrameAddress(address), false, address);
  assert.equal(cardFrameResponse("/pages.html").status, 404);
  // What it writes ahead of the page: the doctype once, the script that must run first, a ground to read on.
  const text = await response.text();
  const script = text.slice(text.indexOf("<script>") + 8, text.lastIndexOf("</script>"));
  assert.doesNotThrow(() => new Function(script), "the served script parses");
  assert.match(script, /replace\(\/</);
  assert.match(script, /"\\\\u003c"/, "a theme value cannot close the script it is written into");
  assert.match(script, /--astra-/);
});

test("the window's own policy lets a frame hold nothing but that host", () => {
  const html = readFileSync(new URL("../index.html", import.meta.url), "utf8");
  assert.match(html, /frame-src astra:\/\/card;/);
});

const fixture = async (run: (dir: string) => Promise<void>) => { const dir = await mkdtemp(join(tmpdir(), "astra-page-preview-")); try { await run(dir); } finally { await rm(dir, { recursive: true, force: true }); } };

test("an HTML file is previewed as a page, whole or not at all", () => fixture(async dir => {
  const source = "<!doctype html><title>t</title><h1>页面</h1>";
  const path = join(dir, "draft.page.html"); await writeFile(path, source);
  const page = await filePreview(path);
  assert.deepEqual([page.kind, page.text, page.truncated], ["page", source, undefined]);
  assert.ok(page.sourceVersion);
  const plain = join(dir, "index.htm"); await writeFile(plain, source);
  assert.equal((await filePreview(plain)).kind, "page");
  // Half an HTML document is not a smaller page: past the limit it is source, and says why.
  const big = join(dir, "big.html"); await writeFile(big, "<p>" + "x".repeat(PAGE_PREVIEW_LIMIT));
  const shown = await filePreview(big);
  assert.equal(shown.kind, "text");
  assert.equal(shown.truncated, true);
  assert.match(shown.warnings?.join("") ?? "", /页面超过 1 MiB，只显示源码。/);
  // Something that is not text is not a page either.
  const binary = join(dir, "binary.html"); await writeFile(binary, Buffer.from([60, 104, 0, 1, 2]));
  await assert.rejects(filePreview(binary), /二进制/);
  // Other text files are as they were.
  const note = join(dir, "note.txt"); await writeFile(note, "plain");
  assert.equal((await filePreview(note)).kind, "text");
}));

test("a page is drawn in the isolated frame, and its source is one switch away", () => {
  const body = (docView: "rendered" | "source") => renderToStaticMarkup(React.createElement(DocumentPreviewBody, { preview: { path: "/x/a.page.html", kind: "page", text: "<h1 onclick=\"x()\">标题</h1>" }, runtime: "r", fail: () => {}, docView }));
  const frame = /<iframe[^>]*>/.exec(body("rendered"))?.[0] ?? "";
  assert.match(frame, /sandbox="allow-scripts"/);
  assert.match(frame, new RegExp(`src="${PAGE_FRAME_URL}"`));
  assert.match(frame, /referrerPolicy="no-referrer"|referrerpolicy="no-referrer"/);
  // The source is handed to the frame, never written into the host page.
  assert.doesNotMatch(body("rendered"), /标题/);
  assert.match(body("source"), /<pre class="file-preview">&lt;h1 onclick=&quot;x\(\)&quot;&gt;标题&lt;\/h1&gt;<\/pre>/);
  assert.doesNotMatch(body("source"), /<iframe/);
});

test("what the skill and the guides say a page may use is what the page frame provides", () => {
  // The frame hands a page the app's colours under names of their own, one for each colour a card gets.
  const provided = new Set(CARD_THEME_COLORS.map(name => `--astra-${name.slice(2)}`));
  for (const [path, heading] of [["../../agent/runtime/interactive_cards.md", "# 整页画布"], ["../../docs/gui.md", "## Pages"], ["../../docs/zh-CN/gui.md", "## 整页画布"]]) {
    const whole = readFileSync(new URL(path, import.meta.url), "utf8");
    const level = heading.slice(0, heading.indexOf(" ") + 1);
    const start = whole.indexOf(`${heading}\n`), next = whole.indexOf(`\n${level}`, start + heading.length);
    assert.notEqual(start, -1, path);
    const text = whole.slice(start, next === -1 ? undefined : next);
    const named = new Set([...text.matchAll(/`(--[a-z-]+)`/g)].map(match => match[1]));
    assert.deepEqual([...named].sort(), [...provided].sort(), path);
    // The name that opens a page by itself and the size past which it is only source are told as they are.
    assert.match(text, /\.page\.html/, path);
    assert.match(text, /1 MiB/, path);
  }
  // The example the skill teaches is a whole document that names its file the way the skill says to.
  const skill = readFileSync(new URL("../../agent/runtime/interactive_cards.md", import.meta.url), "utf8");
  const example = /```html\n([\s\S]*?)\n```/.exec(skill)?.[1] ?? "";
  assert.match(example, /^<!doctype html>/);
  assert.doesNotMatch(example, /https?:|src=|href="(?!#)/, "the example loads nothing");
  assert.ok(example.length < PAGE_PREVIEW_LIMIT);
});
