import assert from "node:assert/strict";
import { test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import type { Element, ElementContent, Root, RootContent } from "hast";
import { Markdown } from "../src/renderer/markdown.js";
import { MessageRow } from "../src/renderer/messages.js";
import { EarlierResponseVersions } from "../src/renderer/response-versions.js";
import { autoTitle } from "../src/renderer/sidebar.js";
import { codeText } from "../src/renderer/code-markdown.js";
import { SECRET_TAG, SecretRevealContext, SecretValue, rehypeMaskSecrets, revealedKeys, toggleRevealed } from "../src/renderer/secret-markdown.js";

// Obviously fake fixtures of the published shapes, never real credentials.
const KEY = "sk-test" + "0".repeat(28);
const TOKEN = "ghp_" + "0".repeat(16) + "T".repeat(20);
const MASK = "•";
const masked = (value: string) => value.slice(0, 8) + MASK.repeat(value.length - 8);
const tail = (value: string) => value.slice(8);
const render = (text: string, maskSecrets?: boolean) => renderToStaticMarkup(React.createElement(Markdown, { text, fail() {}, maskSecrets }));
const drawn = (html: string) => html.replace(/<[^>]*>/g, "");

test("a sent message draws each credential masked wherever Markdown puts it", () => {
  const text = [
    `请用 ${KEY} 调用。`,
    `**加粗 ${TOKEN}** 和行内 \`${KEY}\``,
    "```python\nclient = OpenAI(api_key=\"" + KEY + "\")\n```",
    `- 列表 ${TOKEN}`,
    `| 名称 | 值 |\n|---|---|\n| key | ${KEY} |`,
    "数据库 postgres://admin:hunter2-test@db.local/app",
  ].join("\n\n");
  const html = render(text, true);
  const text_ = drawn(html);
  assert.ok(!text_.includes(tail(KEY)) && !text_.includes(tail(TOKEN)) && !text_.includes("hunter2-test"));
  assert.ok(text_.includes(masked(KEY)) && text_.includes(masked(TOKEN)));
  assert.equal(html.match(/class="secret-value"/g)?.length, 7);
  // The code block is still highlighted Python, and the rest of the message reads as before.
  assert.match(html, /class="hljs language-python"/);
  assert.match(html, /<strong>加粗 /); assert.match(html, /<table>/); assert.match(html, /<li>列表 /);
  assert.match(text_, /数据库 postgres:\/\/admin:•{12}@db\.local\/app/);
});

test("masking leaves messages without credentials, and every other message, exactly as before", () => {
  const plain = "# 标题\n\n**粗体** `task-1` sk-learn\n\n```js\nconst id = 'e3b0c44298fc1c149afbf4c8996fb924';\n```\n\n[链接](https://example.com)";
  assert.equal(render(plain, true), render(plain));
  const withKey = `用 ${KEY}`;
  assert.ok(drawn(render(withKey)).includes(KEY), "only sent user text is masked");
});

test("a value inside a link is masked while the link still points where it did", () => {
  const html = render(`打开 https://user:pw-test-0@example.com/path 看看`, true);
  assert.match(html, /href="https:\/\/user:pw-test-0@example\.com\/path"/);
  assert.ok(!drawn(html).includes("pw-test-0"));
});

test("only the user's rows are masked", () => {
  const props = { timeline: false, reasoning: false, expanded: new Map<string, boolean>(), fail() {} };
  const user = renderToStaticMarkup(React.createElement(MessageRow, { ...props, message: { id: "u", role: "user", content: `key ${KEY}` } }));
  const assistant = renderToStaticMarkup(React.createElement(MessageRow, { ...props, message: { id: "a", role: "assistant", content: `key ${KEY}` } }));
  assert.ok(!drawn(user).includes(KEY) && drawn(user).includes(masked(KEY)));
  assert.ok(drawn(assistant).includes(KEY));
  // An earlier response version names the request it answered by the user's text.
  const versions = [{ id: "a", branch_id: "a", number: 1, status: "completed" }, { id: "b", branch_id: "b", number: 2, status: "completed" }];
  const group = { id: "g", selected_version: "a", user_text: `key ${KEY}`, response_text: "ok", source_ref: { index: 1, digest: "d" }, versions };
  const earlier = drawn(renderToStaticMarkup(React.createElement(EarlierResponseVersions, { messages: [], controls: {
    versions: { revision: 1, branch_id: "a", active_branch: "a", choices: [], groups: [group], targets: [] }, busy: false, regenerate() {}, select() {} } })));
  assert.ok(earlier.includes(`key ${masked(KEY)}`) && !earlier.includes(KEY));
});

const text = (value: string): ElementContent => ({ type: "text", value });
const element = (tagName: string, children: ElementContent[], className?: string[]): Element => ({ type: "element", tagName, properties: className ? { className } : {}, children });
/** The tree with every masked piece replaced by the original text it holds. */
const unwrap = (node: Root | RootContent): unknown => {
  if (node.type === "text") return { ...node };
  if (node.type !== "element" && node.type !== "root") return node;
  const children: unknown[] = [];
  for (const child of node.children) {
    if (child.type === "element" && child.tagName === SECRET_TAG) children.push(...child.children.map(unwrap));
    else children.push(unwrap(child));
  }
  // Adjacent text left by unwrapping reads as one.
  const merged = children.reduce<unknown[]>((all, child) => {
    const last = all.at(-1) as { type?: string; value?: string } | undefined;
    const next = child as { type?: string; value?: string };
    if (last?.type === "text" && next.type === "text") all[all.length - 1] = { type: "text", value: last.value! + next.value! };
    else all.push(child);
    return all;
  }, []);
  return { ...node, children: merged };
};
const secrets = (node: Root | Element): Element[] => node.children.flatMap(child => child.type !== "element" ? [] : child.tagName === SECRET_TAG ? [child] : secrets(child));

test("a value split by emphasis or highlight tokens is one value, and the tree keeps its original text", () => {
  const split = KEY.length - 10;
  const tree: Root = { type: "root", children: [
    element("p", [text("前 "), element("strong", [text(KEY.slice(0, split))]), text(KEY.slice(split) + " 后")]),
    text("\n"),
    element("pre", [element("code", [element("span", [text('key = "' + TOKEN.slice(0, 6)) ], ["hljs-string"]), element("span", [text(TOKEN.slice(6) + '"')], ["hljs-string"])], ["hljs", "language-python"])]),
    element("p", [element("span", [text(KEY)], ["katex"])]),
  ] };
  const before = unwrap(structuredClone(tree));
  rehypeMaskSecrets()(tree);
  assert.deepEqual(unwrap(tree), before, "only masked wrappers are added; the text and structure are the original");
  const [paragraph, , pre, formula] = tree.children as Element[];
  const keyPieces = secrets(paragraph);
  assert.equal(keyPieces.length, 2);
  assert.equal(new Set(keyPieces.map(piece => piece.properties.dataSecretKey)).size, 1, "both pieces reveal together");
  assert.equal(keyPieces.map(piece => piece.properties.dataMasked).join(""), masked(KEY));
  // Code copy and diagram source read the original text.
  assert.equal(codeText(pre.children[0] as Element), 'key = "' + TOKEN + '"');
  assert.equal(secrets(pre).map(piece => piece.properties.dataMasked).join(""), masked(TOKEN));
  assert.equal(secrets(formula).length, 0, "typeset formulas are left alone");
});

test("revealing shows one value at a time and resets for a different text", () => {
  let state = { text: "a", keys: new Set<string>() as ReadonlySet<string> };
  state = toggleRevealed(state, "a", "0:1");
  assert.deepEqual([...revealedKeys(state, "a")], ["0:1"]);
  state = toggleRevealed(state, "a", "0:9");
  state = toggleRevealed(state, "a", "0:1");
  assert.deepEqual([...revealedKeys(state, "a")], ["0:9"]);
  assert.deepEqual([...revealedKeys(state, "b")], []);
  const node = element(SECRET_TAG, [text(KEY)]);
  node.properties = { dataSecretKey: "0:4", dataMasked: masked(KEY) };
  const draw = (shown: boolean) => drawn(renderToStaticMarkup(React.createElement(SecretRevealContext.Provider, { value: { shown: key => shown && key === "0:4", toggle() {} } },
    React.createElement(SecretValue, { node }, KEY))));
  assert.equal(draw(false), masked(KEY));
  assert.equal(draw(true), KEY);
});

test("a conversation named from its first line never stores a credential", () => {
  const title = autoTitle(`  连接 ${KEY} 试试\n第二行 ${TOKEN}`);
  assert.equal(title, [...`连接 ${masked(KEY)} 试试`].slice(0, 36).join(""));
  assert.ok(!title.includes(tail(KEY).slice(0, 10)));
  assert.equal(autoTitle("帮我看看 task-1 的进度\n细节"), "帮我看看 task-1 的进度");
  assert.equal(autoTitle("   "), "附件对话");
  // Masking first means a value cut by the 36-character limit is still recognised whole.
  assert.ok(!autoTitle("x".repeat(20) + " " + KEY).includes(KEY.slice(8, 15)));
});
