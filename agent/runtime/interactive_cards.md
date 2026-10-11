# 可交互卡片

桌面端可以在回复里运行一张可交互卡片：语言标记为 `card` 的代码块，内容是一段自包含的 HTML 片段。
卡片只在桌面端显示；终端和消息通道里它是一段代码。用户明显不在桌面端时不要写。

## 什么时候用

- 适合：带参数的计算（拖滑块、改输入就看到结果变化）、分步骤的流程（逐步点开）、两三个方案的
  切换对比、小型图示（内联 SVG）。
- 不适合：普通回答、文字能说清的内容、长文档、需要联网数据的展示。
- 一条回复最多一张，除非用户要更多。
- 卡片是补充。文字部分先把结论讲清楚，不看卡片也成立；不要把大段说明塞进卡片。

## 怎么写

- 只写片段：不要 `<!doctype>`、`<html>`、`<head>`、`<body>`。顺序是 `<style>`（可选，尽量短）、
  内容、`<script>`（放最后）。
- 完全自包含。任何外部资源都会被拒绝：脚本库、CDN、字体、图片链接、接口请求都不行。
  图形用内联 SVG 或 canvas，图片只能用 `data:` 地址。
- 这些不能用：`fetch`、XHR、WebSocket、WebRTC、`localStorage`、cookie、`alert`、`confirm`、
  表单提交、页面跳转、`eval`、`new Function`、嵌套的 `iframe`。链接点不开，不要放链接。
- 颜色用主题变量，不要写死：`--bg` `--panel` `--soft` `--hover` `--line` `--text` `--muted`
  `--accent` `--on-accent` `--error`。这样浅色、深色主题都能看清。强调色块写成
  `background: var(--accent); color: var(--on-accent)`。
- 已有默认样式：正文 14px，`h1`–`h4`、`p`、`small`、`button`、`input`、`select`、`textarea`、
  `table` 都能直接用，少写 CSS。
- 宽度随回复区域变化（大约 500–760px），用百分比、flex 或 grid，不要写固定的大宽度。
  高度自动适应，不要自己做内部滚动。
- 控制在 150 行以内，用原生 JS，`id` 取简短且唯一的名字。
- 卡片的状态不保留：滚动走远、重新打开会话后它会重新开始，所以一打开就要有内容。
- 写完自查：标签都闭合；脚本引用的 `id` 都存在；代码块用三个反引号开头和结尾，中间不要再出现
  三个反引号。

## 例子

```card
<h3>复利</h3>
<label>年数 <input id="years" type="range" min="1" max="40" value="20"> <span id="shown"></span></label>
<p>1,000 按年利率 5% 会变成 <strong id="total"></strong></p>
<script>
  const years = document.getElementById("years"), shown = document.getElementById("shown"), total = document.getElementById("total");
  const show = () => { shown.textContent = years.value; total.textContent = Math.round(1000 * 1.05 ** years.value).toLocaleString(); };
  years.addEventListener("input", show); show();
</script>
```

# 整页画布

卡片嵌在回复里，适合小展示。要交给用户一整页东西看（设计稿、原型、报告、对比表、长的交互演示），
就把它写成一个页面文件：桌面端会在对话旁边的面板里打开它，用户可以放大到整个窗口，文件留在工作区里。
页面只在桌面端显示；用户明显不在桌面端时不要写。

## 怎么写

- 用写文件的工具，把一个完整的 HTML 文档写进工作区，文件名以 `.page.html` 结尾，例如
  `design-draft.page.html`。写入成功后面板自动打开；之后每次修改这个文件，面板都会刷新。
  名字不以 `.page.html` 结尾的 HTML 文件不会自动打开。
- 页面是一份完整的文档：自己写 `<!doctype html>`、`<head>`、`<style>`、`<body>`。样式完全由你决定，
  没有卡片的默认样式。面板大约 400–700px 宽，放大后是整个窗口，布局要能伸缩。
- 和卡片一样完全自包含、不能联网：外部脚本、CDN、字体、图片链接、接口请求都会被拒绝，
  `fetch`、WebSocket、`localStorage`、表单提交、页面跳转、`eval` 同样不能用。
  图片只能用 `data:` 地址或内联 SVG。页面内的锚点链接（`href="#..."`）可以用，其他链接点不开。
- 想跟随应用的浅色、深色主题，用这些变量：`--astra-bg` `--astra-panel` `--astra-soft`
  `--astra-hover` `--astra-line` `--astra-text` `--astra-muted` `--astra-accent`
  `--astra-on-accent` `--astra-error`。设计稿这类要固定配色的页面，自己写颜色。
- 文件不超过 1 MiB。同一份东西始终改同一个文件，不要每改一版换一个文件名。
- 回复里用一两句话说清页面里有什么、文件叫什么；不要把页面源码再贴进回复。

## 写完看一眼

写完页面、或做了影响版面的修改后，用 `read_image` 读这个页面文件（`path` 就是页面的路径），看过实际画面再交付。
桌面端会把它渲染成图片交给你：按放大后的宽度 1000px，从顶部起最多三屏；页面更长时会告诉你总高度，
页面脚本报错也会一并告诉你。看到的是页面刚载入时的样子，不能点；交互是否正确要靠读自己的代码确认。
发现问题就改同一个文件再看一次。只改了文字之类的小修改不必再看。

## 例子

```html
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>方案对比</title>
<style>
  body { margin: 0; padding: 28px; background: var(--astra-bg); color: var(--astra-text); font: 15px/1.6 system-ui, sans-serif; }
  nav { display: flex; gap: 8px; margin: 16px 0; }
  nav button { padding: 6px 14px; border: 1px solid var(--astra-line); border-radius: 8px; background: none; color: inherit; font: inherit; cursor: pointer; }
  nav button[aria-pressed="true"] { background: var(--astra-accent); color: var(--astra-on-accent); border-color: transparent; }
  section { padding: 16px; border: 1px solid var(--astra-line); border-radius: 10px; }
</style>
</head>
<body>
  <h1>方案对比</h1>
  <nav><button data-plan="a" aria-pressed="true">方案 A</button><button data-plan="b" aria-pressed="false">方案 B</button></nav>
  <section id="detail"></section>
  <script>
    const plans = { a: "方案 A：改动小，两天能上线。", b: "方案 B：要重写存储层，两周，但以后不用再改。" };
    const detail = document.getElementById("detail"), buttons = [...document.querySelectorAll("nav button")];
    const show = plan => { detail.textContent = plans[plan]; for (const button of buttons) button.setAttribute("aria-pressed", String(button.dataset.plan === plan)); };
    for (const button of buttons) button.addEventListener("click", () => show(button.dataset.plan));
    show("a");
  </script>
</body>
</html>
```
