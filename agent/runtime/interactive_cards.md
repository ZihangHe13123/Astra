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
