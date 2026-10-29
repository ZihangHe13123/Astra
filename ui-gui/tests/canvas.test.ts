import { test } from "node:test";
import assert from "node:assert/strict";
import { existsSync, mkdtempSync, mkdirSync, readFileSync, readdirSync, realpathSync, rmSync, rmdirSync, writeFileSync, symlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { composeDeck, inlineAssets, slideCount } from "../src/main/canvas-deck.js";
import { canvasVersion, commentOnDocument, exportTarget, isCanvasManifest, listVersions, readCanvas, recordVersion, restoreVersion, slidesTarget } from "../src/main/canvas-files.js";
import { insertComment } from "../src/main/canvas-doc.js";
import { canvasPreview } from "../src/main/file-preview.js";
import { docPathFromTool, openComments, outline } from "../src/renderer/doc-preview.js";
import { PRINT_POLICY, cardFrameResponse, printAddress } from "../src/main/card-frame.js";
import { canvasManifestFor, pagePathFromTool } from "../src/renderer/page-state.js";

const slides = `<section class="slide title"><h1>Deck</h1></section>
<section class="slide columns"><h1>Two</h1><img src="assets/dot.png"></section>
<section data-x class="big slide"><p style="background:url('assets/dot.png')">x</p></section>`;
const png = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);

function canvas(files: Record<string, string | Uint8Array>, type = "slides"): string {
  const folder = join(mkdtempSync(join(tmpdir(), "astra-canvas-")), "deck.canvas");
  mkdirSync(join(folder, "assets"), { recursive: true });
  writeFileSync(join(folder, "canvas.json"), JSON.stringify({ type, title: "季度回顾", version: 1 }));
  for (const [name, content] of Object.entries(files)) writeFileSync(join(folder, name), content);
  return join(folder, "canvas.json");
}

test("each way of showing a deck is one document with the slides and the template's code", () => {
  assert.equal(slideCount(slides), 3);
  for (const mode of ["view", "capture", "print"] as const) {
    const html = composeDeck(slides, { mode, slide: 2, title: "<Deck>" });
    assert.match(html, new RegExp(`<body data-mode="${mode}" data-slide="2">`));
    assert.ok(html.includes(slides), "the slides go in as written");
    assert.match(html, /<title>&lt;Deck&gt;<\/title>/);
    assert.match(html, /@page \{ size: 1920px 1080px; margin: 0; \}/);
  }
  // A picture or a print is the slides alone: what a script adds around them takes no room or page.
  assert.match(composeDeck(slides, { mode: "print" }), /body\[data-mode=print\] > :not\(\.astra-deck\), body\[data-mode=print\] \.astra-deck > :not\(section\.slide\) \{ display: none !important; \}/);
  assert.match(composeDeck(slides, { mode: "capture", slide: -3 }), /data-slide="1"/);
});

test("images under assets/ are written in as data, and nothing else is", () => {
  const read = (name: string) => name === "dot.png" ? { mime: "image/png", data: png } : name === "big.png" ? { mime: "image/png", data: new Uint8Array(20) } : undefined;
  const { html, missing } = inlineAssets(`${slides}<img src="./assets/dot.png"><img src="assets/../secret.png"><img src="assets/nope.png">
    <img src="assets/note.txt"><img src="https://example.com/a.png"><img src="assets/big.png">`, read, { each: 10, total: 100 });
  assert.equal(html.match(/data:image\/png;base64,/g)?.length, 3, "src, url() and ./assets each inlined");
  assert.match(html, /url\('data:image\/png;base64,[^']+'\)/);
  assert.match(html, /src="assets\/\.\.\/secret\.png"/);
  assert.match(html, /src="https:\/\/example\.com\/a\.png"/);
  assert.deepEqual(missing.sort(), ["../secret.png", "big.png", "nope.png", "note.txt"]);
});

test("a canvas on disk reads as its document, its version follows its slides and images, and prints go only to its exports", async () => {
  const manifest = canvas({ "slides.html": slides, "assets/dot.png": png });
  assert.equal(isCanvasManifest(manifest), true);
  assert.equal(isCanvasManifest(join(manifest, "..", "..", "canvas.json")), false);
  const shown = await readCanvas(manifest, "view");
  assert.deepEqual([shown.title, shown.type, shown.slides, shown.missing], ["季度回顾", "slides", 3, []]);
  assert.equal(shown.source, slides);
  assert.match(shown.html, /<img src="data:image\/png;base64,/);
  const before = await canvasVersion(manifest);
  writeFileSync(join(manifest, "..", "slides.html"), slides + "<section class=\"slide\"></section>");
  const afterSlides = await canvasVersion(manifest);
  assert.notEqual(afterSlides, before);
  writeFileSync(join(manifest, "..", "assets", "more.png"), png);
  assert.notEqual(await canvasVersion(manifest), afterSlides);
  const exports = join(manifest, "..", "exports");
  // Where the file really goes: the canvas's own exports folder, made when missing. The real path as the
  // system gives it (native: on Windows a short 8.3 name such as RUNNER~1 is spelled out, as the code does).
  assert.equal(await exportTarget(manifest, join(exports, "deck.pdf")), join(realpathSync.native(exports), "deck.pdf"));
  for (const target of [join(manifest, "..", "deck.pdf"), join(exports, "deck.html"), join(exports, "x", "deck.pdf")])
    await assert.rejects(exportTarget(manifest, target), /exports/);
  // The pictures for a PPTX go to one folder of its own, and nowhere else.
  assert.equal(await slidesTarget(manifest, join(exports, ".slides")), join(realpathSync.native(exports), ".slides"));
  for (const target of [exports, join(exports, ".slides", "x"), join(manifest, "..", ".slides")]) await assert.rejects(slidesTarget(manifest, target), /\.slides/);
  assert.equal((await readCanvas(manifest, "capture", 2)).inlined.includes("data:image/png;base64,"), true);
});

test("an image that leaves the folder, and a canvas of an unknown kind, are refused", async () => {
  const outside = join(mkdtempSync(join(tmpdir(), "astra-outside-")), "secret.png");
  writeFileSync(outside, png);
  const manifest = canvas({ "slides.html": `<section class="slide"><img src="assets/link.png"></section>` });
  symlinkSync(outside, join(manifest, "..", "assets", "link.png"));
  assert.deepEqual((await readCanvas(manifest, "view")).missing, ["link.png"]);
  const odd = canvas({ "slides.html": slides });
  writeFileSync(odd, JSON.stringify({ type: "dashboard" }));
  await assert.rejects(readCanvas(odd, "view"), /不认识的画布类型/);
});

test("a print is served once per address, sandboxed and under the frame's policy", async () => {
  const { address, forget } = printAddress("<p>deck</p>");
  const path = new URL(address).pathname;
  const response = cardFrameResponse(path);
  assert.equal(response.status, 200);
  assert.equal(await response.text(), "<p>deck</p>");
  assert.equal(response.headers.get("Content-Security-Policy"), PRINT_POLICY);
  assert.match(PRINT_POLICY, /default-src 'none'.*; sandbox allow-scripts$/);
  forget();
  assert.equal(cardFrameResponse(path).status, 404);
  assert.equal(cardFrameResponse("/print/../frame.html").status, 404);
});

test("writing into a canvas, or the canvas tool, opens the canvas", () => {
  assert.equal(canvasManifestFor("/w/deck.canvas/slides.html"), "/w/deck.canvas/canvas.json");
  assert.equal(canvasManifestFor("deck.canvas"), "deck.canvas/canvas.json");
  assert.equal(canvasManifestFor("C:\\w\\deck.canvas\\assets\\a.png"), "C:\\w\\deck.canvas\\canvas.json");
  assert.equal(canvasManifestFor("/w/deck.canvasx/slides.html"), undefined);
  const write = (name: string, args: unknown, extra = {}) => pagePathFromTool({ type: "tool_result", name, arguments: JSON.stringify(args), ...extra });
  assert.equal(write("write_file", { path: "/w/deck.canvas/slides.html" }), "/w/deck.canvas/canvas.json");
  assert.equal(write("apply_patch", { patch: "*** Begin Patch\n*** Update File: /w/deck.canvas/slides.html\n*** End Patch" }), "/w/deck.canvas/canvas.json");
  assert.equal(write("write_file", { path: "/w/deck.canvas/slides.html" }, { error: "denied" }), undefined);
  const tool = (output: string, name = "canvas") => pagePathFromTool({ type: "tool_result", name, output });
  assert.equal(tool("Canvas: /w/deck.canvas/canvas.json\nEdit: /w/deck.canvas/slides.html\n\n# Slides canvas"), "/w/deck.canvas/canvas.json");
  assert.equal(tool(JSON.stringify({ success: true, type: "image_attachment", canvas: "/w/deck.canvas/canvas.json", slide: 2 }), "canvas_look"), "/w/deck.canvas/canvas.json");
  assert.equal(tool("Canvas types: slides"), undefined);
});

const doc = `# 季度回顾

开篇一段，说明**本季度**的重点。

## 收入

收入增长了 12%，主要来自新客户。

` + "```" + `
收入增长了 12%，在代码里
` + "```" + `

## 风险 #

- 供应链 [延迟](https://example.com) 两周
`;

test("a comment goes right after the passage it is about, never into a heading or code, else at the end", () => {
  const after = insertComment(doc, "收入增长了 12%", "补一个对比");
  assert.match(after, /收入增长了 12% <!-- @astra: 补一个对比 -->，主要来自新客户。/);
  assert.match(after, /```\n收入增长了 12%，在代码里\n```/, "the code block is left alone");
  assert.deepEqual(openComments(after), ["补一个对比"]);
  // Selected as rendered: the emphasis and the link are not in the selection, so the line is found without them.
  assert.match(insertComment(doc, "说明本季度的重点", "x"), /说明\*\*本季度\*\*的重点。 <!-- @astra: x -->\n/);
  assert.match(insertComment(doc, "供应链 延迟 两周", "x"), /- 供应链 \[延迟\]\(https:\/\/example\.com\) 两周 <!-- @astra: x -->\n/);
  assert.match(insertComment(doc, "收入", "x"), /\n## 收入\n<!-- @astra: x -->\n/, "a heading keeps its text");
  const nowhere = insertComment(doc, "不在文中的一句话", "这里 -- 要改");
  assert.ok(nowhere.endsWith("<!-- @astra: 这里 — 要改（关于：「不在文中的一句话」） -->\n"), "two hyphens would end the comment");
  assert.throws(() => insertComment(doc, "收入", "  "), /空/);
  assert.throws(() => insertComment(doc, " ", "x"), /选中/);
});

test("a document's outline is its headings outside code, as they read", () => {
  assert.deepEqual(outline(doc), [{ level: 1, text: "季度回顾" }, { level: 2, text: "收入" }, { level: 2, text: "风险" }]);
  assert.deepEqual(outline("## **粗** [链](x) `码` <!-- c -->\n```\n# 不是\n```\n#不是标题"), [{ level: 2, text: "粗 链 码" }]);
});

test("a document canvas reads as its Markdown, and each change, comment and restore is a version", async () => {
  const manifest = canvas({ "doc.md": doc }, "doc");
  const shown = await canvasPreview(manifest);
  assert.deepEqual([shown.kind, shown.text, shown.canvas?.type, shown.canvas?.title], ["text", doc, "doc", "季度回顾"]);
  assert.equal(await recordVersion(manifest, "create"), "0001");
  assert.equal(await recordVersion(manifest, "write"), undefined, "the same content is not kept twice");
  // A change that missed the record is kept before the comment goes in.
  writeFileSync(join(manifest, "..", "doc.md"), doc + "\n## 下季度\n");
  assert.equal(await commentOnDocument(manifest, "主要来自新客户", "哪些客户？"), "0003");
  const versions = await listVersions(manifest);
  assert.deepEqual(versions.map(version => [version.id, version.source, version.file]), [["0001", "create", "doc.md"], ["0002", "write", "doc.md"], ["0003", "comment", "doc.md"]]);
  assert.match(readFileSync(join(manifest, "..", "doc.md"), "utf8"), /主要来自新客户 <!-- @astra: 哪些客户？ -->/);
  const first = await canvasPreview(manifest, "0001");
  assert.deepEqual([first.text, first.canvas?.version], [doc, "0001"]);
  assert.equal(await restoreVersion(manifest, "0001"), "0004");
  assert.equal(readFileSync(join(manifest, "..", "doc.md"), "utf8"), doc);
  assert.deepEqual((await listVersions(manifest)).map(version => version.source), ["create", "write", "comment", "restore"]);
  await assert.rejects(restoreVersion(manifest, "../0001"), /没有这个版本/);
  await assert.rejects(restoreVersion(manifest, "0009"), /没有这个版本/);
  // A deck takes no comments; its kept version is drawn like the current one.
  const deck = canvas({ "slides.html": slides });
  await recordVersion(deck, "create");
  await assert.rejects(commentOnDocument(deck, "Deck", "x"), /只有文档画布/);
  const kept = await canvasPreview(deck, "0001");
  assert.deepEqual([kept.kind, kept.canvas?.slides, kept.canvas?.version], ["page", 3, "0001"]);
});

test("versions are kept in the runtime's format, the thirty newest of them", async () => {
  const manifest = canvas({ "doc.md": "# 一\n" }, "doc");
  // As agent/runtime/canvas.py writes one.
  mkdirSync(join(manifest, "..", ".versions", "0007"), { recursive: true });
  writeFileSync(join(manifest, "..", ".versions", "0007", "doc.md"), "# 旧\n");
  writeFileSync(join(manifest, "..", ".versions", "0007", "meta.json"), JSON.stringify({ at: 1, source: "write", sha256: "x", file: "doc.md" }));
  mkdirSync(join(manifest, "..", ".versions", "0008"));
  writeFileSync(join(manifest, "..", ".versions", "0008", "meta.json"), JSON.stringify({ file: "../canvas.json" }));
  assert.deepEqual((await listVersions(manifest)).map(version => version.id), ["0007"], "a version naming another file is left out");
  for (let index = 0; index < 31; index++) {
    writeFileSync(join(manifest, "..", "doc.md"), `# ${index}\n`);
    await recordVersion(manifest, "write");
  }
  const versions = await listVersions(manifest);
  assert.equal(versions.length, 30);
  // Numbered past the folder that could not be read.
  assert.deepEqual([versions[0].id, versions.at(-1)!.id], ["0010", "0039"]);
  assert.ok(!readdirSync(join(manifest, "..", ".versions")).includes("0007"));
  const meta = JSON.parse(readFileSync(join(manifest, "..", ".versions", "0039", "meta.json"), "utf8"));
  assert.deepEqual(Object.keys(meta).sort(), ["at", "file", "sha256", "source"]);
});

test("the doc_* tools writing a canvas's document open the canvas; its exports open as themselves", () => {
  const result = (name: string, output: object) => docPathFromTool({ type: "tool_result", name, output: JSON.stringify(output) });
  assert.equal(result("doc_edit", { path: "/w/plan.canvas/doc.md" }), "/w/plan.canvas/canvas.json");
  assert.equal(result("doc_edit", { path: "/w/plan.md" }), "/w/plan.md");
  assert.equal(result("doc_export", { path: "/w/plan.canvas/exports/plan.docx", source: "/w/plan.canvas/doc.md" }), "/w/plan.canvas/exports/plan.docx");
});

test("nothing of a canvas is read or written through a link out of its folder", async () => {
  const outside = mkdtempSync(join(tmpdir(), "astra-outside-"));
  writeFileSync(join(outside, "secret.md"), "SYNTHETIC_OUTSIDE_SECRET\n");
  writeFileSync(join(outside, "dot.png"), png);
  const linked = (name: string, target: string, type = "doc") => {
    const manifest = canvas({}, type);
    symlinkSync(target, join(manifest, "..", name));
    return manifest;
  };
  // The content file, a document's or a deck's, pointing out of the canvas.
  await assert.rejects(readCanvas(linked("doc.md", join(outside, "secret.md")), "view"), /不在画布文件夹里/);
  await assert.rejects(readCanvas(linked("slides.html", join(outside, "secret.md"), "slides"), "view"), /不在画布文件夹里/);
  // The whole assets folder pointing out: its images are not written in.
  const deck = canvas({ "slides.html": `<section class="slide"><img src="assets/dot.png"></section>` });
  rmdirSync(join(deck, "..", "assets"));
  symlinkSync(outside, join(deck, "..", "assets"));
  const shown = await readCanvas(deck, "view");
  assert.deepEqual(shown.missing, ["dot.png"]);
  assert.doesNotMatch(shown.html, /data:image\/png/);
  // Versions kept through a link out are neither listed nor written there.
  const doc = canvas({ "doc.md": "# 一\n" }, "doc");
  symlinkSync(outside, join(doc, "..", ".versions"));
  assert.deepEqual(await listVersions(doc), []);
  await assert.rejects(recordVersion(doc, "write"), /不在画布文件夹里/);
  assert.deepEqual(readdirSync(outside).sort(), ["dot.png", "secret.md"]);
  // A version's own file pointing out is not shown or restored.
  const own = canvas({ "doc.md": "# 一\n" }, "doc");
  await recordVersion(own, "create");
  const keptFile = join(own, "..", ".versions", "0001", "doc.md");
  rmSync(keptFile);
  symlinkSync(join(outside, "secret.md"), keptFile);
  await assert.rejects(canvasPreview(own, "0001"), /不在画布文件夹里/);
  await assert.rejects(restoreVersion(own, "0001"), /不在画布文件夹里/);
  assert.equal(readFileSync(join(own, "..", "doc.md"), "utf8"), "# 一\n");
  // A comment on a document that is a link out: refused, and the outside file is untouched.
  await assert.rejects(commentOnDocument(linked("doc.md", join(outside, "secret.md")), "SYNTHETIC", "x"), /不在画布文件夹里/);
  assert.equal(readFileSync(join(outside, "secret.md"), "utf8"), "SYNTHETIC_OUTSIDE_SECRET\n");
  // Exports through a link out are refused before anything is written or removed there.
  const printed = canvas({ "slides.html": slides });
  symlinkSync(outside, join(printed, "..", "exports"));
  const exports = join(printed, "..", "exports");
  await assert.rejects(exportTarget(printed, join(exports, "deck.pdf")), /不在画布文件夹里/);
  await assert.rejects(slidesTarget(printed, join(exports, ".slides")), /不在画布文件夹里/);
  assert.ok(!existsSync(join(outside, ".slides")));
});
