import { test } from "node:test";
import assert from "node:assert/strict";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { Markdown } from "../src/renderer/markdown.js";
import { readFileSync } from "node:fs";
import { CARD_FRAME_URL, CARD_THEME_COLORS, MAX_CARD_HEIGHT, MAX_CARD_SOURCE, MIN_CARD_HEIGHT, cardHeight, cardMessage, cardSourceKey, cardTheme, isCardLanguage, rememberCardHeight, rememberedCardHeight } from "../src/renderer/card-state.js";
import { CARD_FRAME_POLICY, CARD_FRAME_URL as SERVED_URL, cardFrameResponse, isCardFrameAddress } from "../src/main/card-frame.js";

const render = (text: string) => renderToStaticMarkup(React.createElement(Markdown, { text, fail: () => {} }));
const fence = (language: string, body: string, closed = true) => "```" + language + "\n" + body + (closed ? "\n```" : "");

test("only a finished card block becomes a frame, and the frame may run scripts and nothing else", () => {
  const html = render(`说明\n\n${fence("card", "<button>加一</button><script>1</script>")}\n\n后文`);
  const frame = /<iframe[^>]*>/.exec(html)?.[0] || "";
  assert.match(frame, /sandbox="allow-scripts"/);
  assert.match(frame, new RegExp(`src="${CARD_FRAME_URL}"`));
  assert.match(frame, /referrerPolicy="no-referrer"|referrerpolicy="no-referrer"/);
  // The source is handed to the frame, never written into the host page.
  assert.doesNotMatch(html, /<button>加一<\/button>/);
  assert.match(html, /说明[\s\S]*后文/);
  assert.equal(CARD_FRAME_URL, SERVED_URL);
});

test("a card still being written, an empty one and an oversized one show source instead of running", () => {
  const writing = render(fence("card", "<button>加一</button>", false));
  assert.doesNotMatch(writing, /<iframe/);
  assert.match(writing, /卡片输入中…/);
  assert.match(writing, /&lt;button&gt;加一&lt;\/button&gt;/);
  assert.doesNotMatch(render(fence("card", "   ")), /<iframe/);
  const oversized = render(fence("card", "x".repeat(MAX_CARD_SOURCE + 1)));
  assert.doesNotMatch(oversized, /<iframe/);
  assert.match(oversized, /卡片内容过长，只显示源码。/);
  assert.match(render(fence("card", "x".repeat(MAX_CARD_SOURCE - 1))), /<iframe/);
});

test("other code stays code, whatever it contains", () => {
  for (const language of ["html", "xml", "svg", "javascript", "cards", "card-demo", ""]) {
    const html = render(fence(language, "<script>document.title = 'RAN'</script>"));
    assert.doesNotMatch(html, /<iframe|<script/, language);
    assert.match(html.replace(/<[^>]+>/g, ""), /document\.title/, language);
  }
  assert.equal(isCardLanguage("card"), true);
  assert.equal(isCardLanguage(" Card "), true);
  assert.equal(isCardLanguage("cards"), false);
  assert.match(render(fence("Card", "<p>x</p>")), /<iframe/);
});

test("the host believes a frame's reports only within bounds, and nothing else it says", () => {
  assert.deepEqual(cardMessage({ astraCard: true, type: "ready" }), { type: "ready" });
  assert.deepEqual(cardMessage({ astraCard: true, type: "height", height: 312.2 }), { type: "height", height: 313 });
  assert.deepEqual(cardMessage({ astraCard: true, type: "height", height: 9e9 }), { type: "height", height: MAX_CARD_HEIGHT });
  assert.deepEqual(cardMessage({ astraCard: true, type: "height", height: -5 }), { type: "height", height: MIN_CARD_HEIGHT });
  assert.equal(cardMessage({ astraCard: true, type: "height", height: "400" }), undefined);
  assert.equal(cardMessage({ astraCard: true, type: "height", height: NaN }), undefined);
  assert.deepEqual(cardMessage({ astraCard: true, type: "error", message: "x".repeat(900) }), { type: "error", message: "x".repeat(300) });
  // A card can post whatever it likes; none of it is an instruction to the host.
  for (const forged of [null, "ready", { type: "ready" }, { astraCard: "true", type: "ready" }, { astraCard: true, type: "open", url: "https://example.com" },
    { astraCard: true, type: "command", cmd: "/yolo on" }, { astraCard: true, type: "render", html: "<p>x</p>" }, { astraCard: true, type: "error" }])
    assert.equal(cardMessage(forged), undefined, JSON.stringify(forged));
  assert.equal(cardHeight(Infinity), undefined);
});

test("a card gets the theme's colours and nothing else about the page", () => {
  const values: Record<string, string> = { "--text": " #282a2b ", "--bg": "#fff", "--line": "", "--secret": "token" };
  const theme = cardTheme({ getPropertyValue: name => values[name] || "", colorScheme: "dark" });
  assert.deepEqual(theme, { dark: true, colors: { "--text": "#282a2b", "--bg": "#fff" } });
  assert.equal(cardTheme({ getPropertyValue: () => "", colorScheme: "light" }).dark, false);
});

test("a card that returns to the list starts at the height it had", () => {
  const source = `<p>${Math.random()}</p>`;
  assert.equal(rememberedCardHeight(source), undefined);
  rememberCardHeight(source, 420);
  assert.equal(rememberedCardHeight(source), 420);
  assert.equal(rememberedCardHeight(source + " "), undefined);
  assert.notEqual(cardSourceKey("ab"), cardSourceKey("ba"));
  for (let i = 0; i < 260; i++) rememberCardHeight(`filler ${i} ${source}`, 100 + i);
  assert.equal(rememberedCardHeight(source), undefined, "old entries make room");
  assert.equal(rememberedCardHeight(`filler 259 ${source}`), 359);
});

test("the card document is served with a policy that names no place to load from", async () => {
  const response = cardFrameResponse("/frame.html");
  assert.equal(response.status, 200);
  assert.equal(response.headers.get("content-security-policy"), CARD_FRAME_POLICY);
  assert.match(response.headers.get("content-type") || "", /^text\/html/);
  const directives = Object.fromEntries(CARD_FRAME_POLICY.split("; ").map(part => { const [name, ...sources] = part.split(" "); return [name, sources]; }));
  assert.deepEqual(directives["default-src"], ["'none'"]);
  assert.deepEqual(directives["script-src"], ["'unsafe-inline'"]);
  assert.deepEqual(directives["frame-src"], ["'none'"]);
  assert.deepEqual(directives["form-action"], ["'none'"]);
  for (const [name, sources] of Object.entries(directives))
    for (const source of sources) assert.match(source, /^(?:'none'|'unsafe-inline'|data:)$/, `${name} ${source}`);
  assert.equal(directives["script-src"].includes("'unsafe-eval'"), false);
  const body = await response.text();
  assert.match(body, /<div id="card"><\/div>/);
  for (const path of ["/", "/index.html", "/frame.html/x", "/../app/index.html"]) assert.equal(cardFrameResponse(path).status, 404, path);
});

test("only the card document is a card frame address", () => {
  assert.equal(isCardFrameAddress("astra://card/frame.html"), true);
  assert.equal(isCardFrameAddress("astra://card/frame.html?dark"), true);
  for (const url of ["astra://card/frame.html?dark=1&x=https://example.com", "astra://card/frame.html#x", "astra://card/other.html", "astra://app/index.html", "https://example.com/", "about:blank", "data:text/html,x"])
    assert.equal(isCardFrameAddress(url), false, url);
});

test("what the built-in skill and the guides tell the model is what the desktop provides", () => {
  const provided = new Set<string>(CARD_THEME_COLORS);
  // The skill is all about cards; each guide has one section about them.
  for (const [path, heading] of [["../../agent/runtime/interactive_cards.md", "# 可交互卡片"], ["../../docs/gui.md", "## Interactive cards"], ["../../docs/zh-CN/gui.md", "## 可交互卡片"]]) {
    const whole = readFileSync(new URL(path, import.meta.url), "utf8");
    const start = whole.indexOf(`${heading}\n`), next = whole.indexOf("\n## ", start + heading.length);
    assert.notEqual(start, -1, path);
    const text = heading.startsWith("## ") ? whole.slice(start, next === -1 ? undefined : next) : whole.slice(start);
    const named = [...text.matchAll(/`(--[a-z-]+)`/g)].map(match => match[1]);
    assert.ok(named.length >= provided.size, path);
    for (const name of named) assert.equal(provided.has(name), true, `${path} names ${name}`);
    // Each example is a finished card block, so it runs where it is shown.
    const example = /```card\n[\s\S]*?\n```/.exec(text)?.[0] || "";
    assert.match(render(example), /<iframe/, path);
  }
});
