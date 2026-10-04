import assert from "node:assert/strict";
import { test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { MermaidBlock } from "../src/renderer/mermaid-block.js";
import { checkedMermaidSource, createMermaidRenderer, localSvgReferences, mermaidConfig, mermaidErrorMessage, mermaidViewBox, MAX_MERMAID_SOURCE, MAX_MERMAID_SVG } from "../src/renderer/mermaid-renderer.js";

test("ordinary Mermaid arrows and Chinese labels are preserved; unsafe extensions remain source", () => {
  const source = "flowchart LR\n 开始[输入] --> 判断{完成？}\n 判断 <-->|是| 结束";
  assert.equal(checkedMermaidSource(source), source);
  for (const input of ["", "x".repeat(MAX_MERMAID_SOURCE + 1), "\n".repeat(501),
    "---\nconfig:\n securityLevel: loose\n---\nflowchart LR\nA-->B",
    "%%{init: {'securityLevel': 'loose'}}%%\nflowchart LR\nA-->B",
    'flowchart LR\nA@{img:"https://example.test/leak"}',
    'flowchart LR\nA["<img src=x onerror=alert(1)>"]',
    'flowchart LR\nA["`![image](https://example.test/leak)`"]',
    "flowchart LR\nclassDef red fill:red;", "flowchart LR; style A fill:url(https://example.test)"]) {
    assert.throws(() => checkedMermaidSource(input));
  }
});

test("app-owned Mermaid config locks security and uses bounded local rendering", () => {
  const config = mermaidConfig("dark");
  assert.equal(config.securityLevel, "strict"); assert.equal(config.startOnLoad, false);
  assert.equal(config.htmlLabels, false); assert.equal(config.layout, "dagre");
  assert.equal(config.theme, "dark"); assert.equal(mermaidConfig("light").theme, "default");
  assert.equal(config.maxTextSize, MAX_MERMAID_SOURCE); assert.equal(config.maxEdges, 300);
  for (const key of ["secure", "securityLevel", "startOnLoad", "maxTextSize", "maxEdges", "dompurifyConfig", "htmlLabels", "themeCSS"]) assert.ok(config.secure!.includes(key));
});

test("SVG markers remain usable but resource URLs and escaped CSS cannot survive", () => {
  for (const value of ["fill:#fff", "url(#marker-1)", "url('#astra.marker:2')", 'url("#arrow")', "#fff"]) assert.equal(localSvgReferences(value), true, value);
  for (const value of ["url(https://example.test/x)", "url(data:image/svg+xml,x)", "url(file:///private)",
    "url(//example.test/x)", "@import 'https://example.test/x'", "u\\72l(https://example.test)", "expression(alert(1))"]) assert.equal(localSvgReferences(value), false, value);
});

test("SVG intrinsic dimensions preserve small diagrams and reject malformed or excessive viewBoxes", () => {
  assert.deepEqual(mermaidViewBox("0 0 220 400"), { x: 0, y: 0, width: 220, height: 400 });
  assert.deepEqual(mermaidViewBox("-8, -4, 120.5, 2.5e2"), { x: -8, y: -4, width: 120.5, height: 250 });
  for (const value of [null, "", "0 0 10", "0 0 10 20 30", "0 0 0 100", "0 0 -1 100",
    "0 0 Infinity 100", "0 0 1e400 100", "0 0 100% 100", "0 0 0xff 100", "0 0 9000 100",
    "0 0 4000 4000", "99999 0 100 100"]) assert.throws(() => mermaidViewBox(value), /尺寸/);
});

test("renderer serializes themes, coalesces identical requests, and caches only sanitized output", async () => {
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  const events: string[] = [];
  const render = createMermaidRenderer({
    load: async () => ({ initialize(config) { events.push(String(config.theme)); }, async render(id, source) {
      events.push(id); await gate; return { svg: source };
    } }),
    sanitize: async svg => `safe:${svg}`,
  });
  const first = render("flowchart LR\nA-->B", "light");
  assert.equal(render("flowchart LR\nA-->B", "light"), first);
  const second = render("flowchart LR\nA-->B", "dark");
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(events, ["default", "astra-mermaid-1"]);
  release();
  assert.equal(await first, "safe:flowchart LR\nA-->B"); await second;
  assert.deepEqual(events, ["default", "astra-mermaid-1", "dark", "astra-mermaid-2"]);
  await render("flowchart LR\nA-->B", "light"); assert.equal(events.length, 4);
});

test("failed, oversized and excessive queued diagrams cannot poison later renders", async () => {
  let loads = 0;
  const render = createMermaidRenderer({ load: async () => ({ initialize() {}, async render(_id, source) {
    loads++; if (source === "bad") throw new Error("parse failed");
    return { svg: source === "big" ? "x".repeat(MAX_MERMAID_SVG + 1) : "<svg/>" };
  } }), sanitize: async svg => svg });
  await assert.rejects(render("bad", "light"), /图表暂时无法显示/);
  await assert.rejects(render("big", "light"), /较大/);
  assert.equal(await render("good", "light"), "<svg/>");
  await assert.rejects(render("bad", "light"), /图表暂时无法显示/); assert.equal(loads, 4);
  const pending = Array.from({ length: 16 }, (_, i) => render(`flowchart LR\nA${i}-->B`, "light"));
  await assert.rejects(render("overflow", "light"), /较多/);
  await Promise.all(pending);
});

test("parse errors never copy internal tokens or diagram text into user-facing notices", () => {
  assert.equal(mermaidErrorMessage(new Error('Parser stack: private-source <img src="bad">')), "图表暂时无法显示，请检查源码后重试。");
  assert.equal(mermaidErrorMessage("unexpected raw parser output"), "图表暂时无法显示，请检查源码后重试。");
});

test("cache evicts old diagrams instead of retaining unbounded conversation text", async () => {
  let draws = 0;
  const render = createMermaidRenderer({ load: async () => ({ initialize() {}, async render() { draws++; return { svg: "<svg/>" }; } }), sanitize: async svg => svg });
  for (let i = 0; i < 17; i++) await render(`graph${i}`, "light");
  await render("graph0", "light"); assert.equal(draws, 18);
});

test("incomplete fences show escaped source and keep copying tied to original text", () => {
  const html = renderToStaticMarkup(React.createElement(MermaidBlock, { source: '<svg onload="bad()">', complete: false, fail() {} }));
  assert.match(html, /&lt;svg onload=/); assert.doesNotMatch(html, /<img|<svg/);
  assert.match(html, /图表输入中/); assert.match(html, /复制 Mermaid 源码/);
});
