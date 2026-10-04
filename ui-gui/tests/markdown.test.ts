import assert from "node:assert/strict";
import { test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { fromMarkdown } from "mdast-util-from-markdown";
import type { Code } from "mdast";
import { Markdown } from "../src/renderer/markdown.js";
import { MAX_HIGHLIGHT_CHARS, remarkCodeMetadata } from "../src/renderer/code-markdown.js";

const render = (text: string) => renderToStaticMarkup(React.createElement(Markdown, { text, fail() {} }));
const codes = (text: string) => {
  const tree = fromMarkdown(text); remarkCodeMetadata()(tree, { value: text });
  const result: Code[] = [];
  const walk = (node: any) => { if (node.type === "code") result.push(node); node.children?.forEach(walk); };
  walk(tree); return result;
};

test("shared rich Markdown retains GFM, escaped HTML, and existing link restrictions", () => {
  const html = render('# 标题\n\n**粗体** ~~旧文~~\n\n| A | B |\n|---|---|\n|1|2|\n\n- [x] 完成\n\n<script>bad()</script>\n\n[危险](javascript:alert(1))');
  assert.match(html, /<h1>标题<\/h1>/); assert.match(html, /<strong>粗体<\/strong>/);
  assert.match(html, /<del>旧文<\/del>/); assert.match(html, /<table>/); assert.match(html, /type="checkbox"/);
  assert.doesNotMatch(html, /<script|href="javascript:/i);
});

test("explicit language code is highlighted while quotes and HTML remain text", () => {
  const html = render('```python\ndef greet(name):\n    return "<script>hello</script>"\n```');
  assert.match(html, /class="hljs language-python"/); assert.match(html, /class="hljs-keyword">def/);
  assert.match(html, /aria-label="复制代码"/); assert.match(html, /&lt;script&gt;hello&lt;\/script&gt;/);
  assert.doesNotMatch(html, /<script>/);
});

test("unknown and unlabelled code stay readable without guessed syntax", () => {
  const unknown = render('```some-future-language\nhello <world>\n```');
  assert.match(unknown, /hello &lt;world&gt;/); assert.doesNotMatch(unknown, /hljs-keyword/);
  const plain = render('```\ndef plain = 1\n```');
  assert.doesNotMatch(plain, /class="hljs/); assert.match(plain, /def plain = 1/);
});

test("large streamed code skips tokenization without losing its text", () => {
  const code = "x".repeat(MAX_HIGHLIGHT_CHARS + 1);
  const html = render('```python\n' + code + '\n```');
  assert.match(html, /no-highlight/); assert.ok(html.includes(code));
  assert.doesNotMatch(html, /class="hljs /);
});

test("literal math and LaTeX code fences remain code rather than formulas", () => {
  for (const lang of ["math", "latex"]) {
    const html = render('```' + lang + '\n$\\frac{a}{b}$\n```');
    assert.match(html, /<pre><code/); assert.doesNotMatch(html, /class="katex/);
    assert.match(html, /frac/);
  }
});

test("complete Mermaid fence detection respects delimiter length and nested Markdown", () => {
  const closed = ['```mermaid\ngraph TD; A-->B\n```', '~~~mermaid\ngraph TD; A-->B\n~~~~', '> ```mermaid\n> graph TD; A-->B\n> ```', '- item\n\n  ```mermaid\n  graph TD; A-->B\n  ```'];
  for (const input of closed) assert.equal(codes(input)[0].data?.hProperties?.["data-fence-complete"], "true", input);
  for (const input of ['```mermaid\ngraph TD; A-->B', '````mermaid\ngraph TD; A-->B\n```', '```mermaid']) {
    assert.equal(codes(input)[0].data?.hProperties?.["data-fence-complete"], "false", input);
  }
});

test("math-aware rendering never changes the original message value", () => {
  const input = '答案 $x^2$\n\n\\[\\frac{a}{b}\\]\n\n```python\nprint("$x$")\n```';
  const before = input;
  const html = render(input);
  assert.equal(input, before); assert.match(html, /class="katex/); assert.match(html, /print/);
});
