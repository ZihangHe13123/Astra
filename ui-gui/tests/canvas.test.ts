import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, writeFileSync, symlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { composeDeck, inlineAssets, slideCount } from "../src/main/canvas-deck.js";
import { canvasVersion, exportTarget, isCanvasManifest, readCanvas } from "../src/main/canvas-files.js";
import { PRINT_POLICY, cardFrameResponse, printAddress } from "../src/main/card-frame.js";
import { canvasManifestFor, pagePathFromTool } from "../src/renderer/page-state.js";

const slides = `<section class="slide title"><h1>Deck</h1></section>
<section class="slide columns"><h1>Two</h1><img src="assets/dot.png"></section>
<section data-x class="big slide"><p style="background:url('assets/dot.png')">x</p></section>`;
const png = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);

function canvas(files: Record<string, string | Uint8Array>): string {
  const folder = join(mkdtempSync(join(tmpdir(), "astra-canvas-")), "deck.canvas");
  mkdirSync(join(folder, "assets"), { recursive: true });
  writeFileSync(join(folder, "canvas.json"), JSON.stringify({ type: "slides", title: "季度回顾", version: 1 }));
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
  assert.equal(exportTarget(manifest, join(exports, "deck.pdf")), join(exports, "deck.pdf"));
  for (const target of [join(manifest, "..", "deck.pdf"), join(exports, "deck.html"), join(exports, "x", "deck.pdf")])
    assert.throws(() => exportTarget(manifest, target), /exports/);
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
