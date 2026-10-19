import assert from "node:assert/strict";
import { test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ReactMarkdown from "react-markdown";
import remarkMath from "remark-math";
import remarkGfm from "remark-gfm";
import { MAX_MATH_LENGTH, normalizeMathMarkdown, rehypeSafeKatex, remarkMathOptions } from "../src/renderer/math-markdown.js";

function render(text: string): string {
  return renderToStaticMarkup(React.createElement(ReactMarkdown, {
    children: normalizeMathMarkdown(text), skipHtml: true,
    remarkPlugins: [remarkGfm, [remarkMath, remarkMathOptions]], rehypePlugins: [rehypeSafeKatex],
  }));
}
const mathCount = (html: string) => (html.match(/class="katex"/g) || []).length;

test("dollar and bracket delimiters render inline and display math with accessible MathML", () => {
  assert.equal(mathCount(render(String.raw`中文 $x^2$ 与 \(y+1\)`)), 2);
  for (const source of [String.raw`$$x^2$$`, "$$\nx^2\n$$", String.raw`\[x^2\]`, "\\[\nx^2\n\\]"]) {
    const html = render(source);
    assert.equal(mathCount(html), 1, source); assert.match(html, /katex-display/); assert.match(html, /<math/);
  }
  assert.equal(mathCount(render("$$\nx^2\n\ny^2\n$$")), 1);
});

test("every incomplete stream prefix remains readable without premature math or red errors", () => {
  for (const source of [String.raw`\[\frac{a}{b}\]`, String.raw`\(x^2\)`, "$x^2$", "$$\nx^2\n$$"]) {
    for (let end = 1; end < source.length; end++) {
      const html = render(source.slice(0, end));
      assert.equal(mathCount(html), 0, `${source} prefix ${end}`);
      assert.doesNotMatch(html, /katex-error|math-fallback/);
    }
    assert.equal(mathCount(render(source)), 1);
  }
});

test("fenced, nested, indented and inline code retain literal math and source bytes", () => {
  for (const source of [
    '```math\n$x$ \\(y\\)\n```', '```latex\n\\[x\\]\n```',
    '~~~math\n$x$\n~~~', '> ```math\n> $$x$$\n> ```',
    '- ```math\n  $x$\n  ```', '    $x$ \\[y\\]', '`$x$ \\(y\\)`',
    '``a `$x$` b``', '```math\n$$x$$',
  ]) {
    assert.equal(normalizeMathMarkdown(source), source);
    assert.equal(mathCount(render(source)), 0, source);
  }
  assert.equal(mathCount(render('unfinished `$x$')), 0);
  assert.equal(mathCount(render('`$code$` then $x$')), 1);
});

test("normalization preserves links, HTML ranges, escapes and common currency ambiguity", () => {
  for (const source of [String.raw`\$5 and \$10`, '[cost](https://example.test/$5/\\(x\\))', '<span title="$5">']) {
    assert.equal(normalizeMathMarkdown(source), source);
  }
  for (const source of ['$5 and $10', '$5-$10', '$12.50 per month, $25 per year', 'USD $1,000.00 and $2,000.00', '$20 USD$', '$5 per month$']) {
    const html = render(source); assert.equal(mathCount(html), 0, source); assert.match(html, /\$/);
  }
  for (const source of ['$100$', '$2+3$', '$2^2$4', '$x+1$']) assert.equal(mathCount(render(source)), 1, source);
  const source = String.raw`金额 \$5，公式 \(\text{cost } \$5\)。`;
  assert.equal(mathCount(render(source)), 1);
  assert.equal(normalizeMathMarkdown(normalizeMathMarkdown(source)), normalizeMathMarkdown(source));
});

test("unsupported TeX and bounded macro expansion degrade to neutral escaped source", () => {
  for (const source of [String.raw`$\notARealCommand{x}$`, String.raw`$\frac{a}{$`, String.raw`$\def\a{\a}\a$`, '$' + 'x'.repeat(MAX_MATH_LENGTH + 1) + '$']) {
    const html = render(source);
    assert.match(html, /math-fallback/); assert.doesNotMatch(html, /katex-error|<script/);
  }
});

test("macros are isolated per formula and per render; untrusted HTML and links stay disabled", () => {
  const html = render(String.raw`$\gdef\privateMacro{LEAK}\privateMacro$ $\privateMacro$`);
  assert.equal(mathCount(html), 1); assert.match(html, /math-fallback/);
  assert.match(render(String.raw`$\privateMacro$`), /math-fallback/);
  for (const source of [String.raw`$\href{javascript:alert(1)}{x}$`, String.raw`$\includegraphics{https://example.test/tracker}$`, String.raw`$\htmlClass{evil}{x}$`]) {
    assert.doesNotMatch(render(source), /<a\b|<img\b|class="evil"|onclick=|onload=/);
  }
  const size = render(String.raw`$\rule{9999em}{9999em}$`);
  assert.doesNotMatch(size, /style="[^"]*(?:height|width):9999em/);
});


test("numeric variable products and compact comparisons remain math rather than currency or HTML", () => {
  for (const source of ["$2 x$", "$3 n$", "$2 x$4", "$a<b>c$", "$a<em>b</em>c$", String.raw`\(a<b>c\)`]) {
    assert.equal(mathCount(render(source)), 1, source);
    assert.doesNotMatch(render(source), /<b>|<em>/);
  }
  assert.equal(normalizeMathMarkdown('<span title="$5">'), '<span title="$5">');
  assert.equal(mathCount(render('<span title="$5">price</span>')), 0);
});

test("GFM literal links retain dollar characters in their URL and label", () => {
  for (const source of ["https://example.test/$5", "www.example.test/$5", "https://example.test/\\(x\\)/$5"]) {
    assert.equal(normalizeMathMarkdown(source), source);
    assert.doesNotMatch(render(source), /%5C\$|katex/);
  }
});

test("bare TeX math environments render as display math, other environments stay text", () => {
  for (const source of [
    "\\begin{equation}\nE = mc^2\n\\end{equation}",
    "推导如下：\n\n\\begin{align*}\na &= b + c \\\\\n  &= d\n\\end{align*}\n\n所以成立。",
    "\\begin{pmatrix} 1 & 0 \\\\ 0 & 1 \\end{pmatrix}",
    "f(x) = \\begin{cases} 1 & x > 0 \\\\ 0 & \\text{otherwise} \\end{cases}",
    // Nested environments of the same name close at the outer end.
    "\\begin{array}{c} \\begin{array}{c} x \\end{array} \\end{array}",
  ]) {
    const html = render(source);
    assert.equal(mathCount(html), 1, source); assert.match(html, /katex-display/, source);
    // The source stays only in MathML's TeX annotation, which screen readers use.
    assert.doesNotMatch(html.replace(/<annotation[^]*?<\/annotation>/g, ""), /\\begin|math-fallback/, source);
  }
  // Dollar signs inside an environment are its text, not new delimiters.
  assert.equal(mathCount(render("\\begin{aligned} \\text{cost} &= \\$5 \\end{aligned}")), 1);
  // LaTeX document structure is not math; an escaped \begin is literal.
  for (const source of ["\\begin{itemize}\n\\item one\n\\end{itemize}", "\\\\begin{equation} x \\end{equation}"]) {
    assert.equal(mathCount(render(source)), 0, source);
  }
  // A formula already inside $$ is not wrapped twice.
  assert.equal(mathCount(render("$$\\begin{aligned} a &= b \\end{aligned}$$")), 1);
  // While the environment is still streaming it is shown as text, never as an error.
  const whole = "\\begin{equation}\n\\frac{a}{b}\n\\end{equation}";
  for (let end = 1; end < whole.length; end++) {
    const html = render(whole.slice(0, end));
    assert.equal(mathCount(html), 0, whole.slice(0, end)); assert.doesNotMatch(html, /katex-error|math-fallback/);
  }
  assert.equal(mathCount(render(whole)), 1);
});
