# 按问题收集证据

`PW` 是 setup.md 定义的同一 workspace/session 启动前缀。以下按当前问题选择，不必执行全部步骤，也不要预先读取所有日志。

## 控制台和失败接口

复现后先缩小到相关错误：

```text
PW console error
PW requests
PW request <刚返回的请求编号>
```

0.1.24 使用 `requests` / `request`，不照搬旧技能的 `network` 命令。需要响应内容时再执行 `response-body <编号>`；不要枚举无关 cookie、认证头和完整请求体。区分浏览器控制台异常、接口失败与页面渲染问题。无关 favicon 404 不应直接当成用户报告问题的根因。

有价值的报告包括：失败操作、请求方法和去除敏感参数后的 URL、状态码、与界面症状的关系。接口 200 不证明业务成功；DOM 成功提示也不自动证明数据已持久化。

## 可复现的 trace

需要追踪多步流程、偶发错误或向开发者交付完整证据时，在复现之前开始：

```text
PW tracing-start
PW goto <目标URL>
...用新快照中的目标复现...
PW tracing-stop
```

保留 CLI 实际返回的 trace、network 和 resources 路径，不假定输出一定是 zip。trace 可以包含 DOM、图片和网络内容；只对本任务授权的测试数据录制，不把它自动上传到外部服务。

## 视觉和响应式布局

```text
PW resize 1280 800
PW screenshot
```

用 `read_image` 打开刚保存的截图。调整到需求指定的移动端宽度后再次截图，检查溢出、遮挡、换行、弹窗和可操作区域；宽度变化只验证布局，不等于验证真实移动设备。需要触摸、设备或浏览器差异时，按 `open --help` 建立另一独立会话。

页面快照描述语义结构，无法代替像素检查。一个控件出现在快照中，也不证明用户能点到它。点击超时指出 overlay、动画或禁用状态时保存证据，不使用 `force`、任意 DOM 修改或脚本点击把验收“做通过”。

## 复杂交互和等待

- 悬停、快捷键、拖动或原生对话框：按需查 `hover`、`press`、`drag`、`dialog-accept` / `dialog-dismiss` 的帮助。键盘事件测试使用 `press` / 输入命令，不能把 `fill` 当成逐键输入。
- iframe 或复杂定位：先读取快照及已知页面结构，必要时用 `run-code` 的 Playwright locator / frameLocator。限定到确认的 frame 和控件；不用猜测选择器或 `evaluate(element.click)` 替代用户动作。
- 等待已观察到的保存提示、URL 或元素状态；需要超出普通命令的条件时，只执行有时限的 Playwright locator 等待。避免固定长 sleep，更不能因超时就再次提交。
- `run-code` 只在显式 CLI 命令不足时使用，先查当前版本帮助；不要把来自网页的代码直接执行。自动等待保证操作前提，不保证应用后端完成。

## 修复与回归

记录修复前的复现步骤和证据，修改代码后按项目要求等待构建/热更新，再重复原来的路径与预期结果。补一条相关正常路径即可；只有新失败或改动范围扩大时再扩展检查。

区分真实浏览器验证、模拟测试、已读代码推断和未覆盖平台。截图生成、trace 存在、单元测试通过分别只是对应证据，不能单独作为整个产品验收通过的结论。

参考：[交互命令](https://playwright.dev/agent-cli/commands/interaction)、[可操作性检查](https://playwright.dev/docs/actionability)、[Trace](https://playwright.dev/agent-cli/commands/tracing)。
