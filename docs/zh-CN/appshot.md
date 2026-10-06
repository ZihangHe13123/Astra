# Appshot：把窗口带入对话

[首页](../../README.zh-CN.md) · [文档导航](README.md) · [English](../appshot.md)

[安装](#setup) · [捕获与发送](#capture-and-send) · [故障排查](#troubleshooting) · [模型与附件限制](#model-and-attachment-limits) · [验证](#verification)

macOS 和 Windows 可用快捷键把一个窗口的截图与可用界面文字加入草稿，何时发送由你决定。

<a id="setup"></a>

## 安装

需要交互式 Astra TUI。Windows x64 的原生辅助程序通过以下命令构建并安装：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build_windows_computer_helper.ps1 -Configuration release -Install
```

重启 Astra 后运行 `/appshot enable`；快捷键默认关闭。Windows 包含运行所需 DLL，采用 WGC/UI Automation 和 Windows 专用私有存储，不会启用通用电脑操作工具。更多构建、测试和限制见 [Windows 辅助程序指南（英文）](../../native/windows-computer-helper/README.md)。

macOS 需要 14+，使用 `./scripts/build_macos_computer_helper.sh` 构建并签名。在系统“隐私与安全性”中给辅助程序屏幕录制和辅助功能权限。临时签名程序重建后身份可能变化，旧权限不一定继续适用。

<a id="capture-and-send"></a>

## 捕获与发送

首次捕获前在目标 TUI 中实际按一次键。只打开终端或后台查询状态不算选定接收方。切到来源应用按 **Control+Shift+Z**，最近活跃的合格 TUI 会收到草稿占位符；添加问题后明确按 Enter 才发送。多个 TUI 活动时间相同会拒绝，而不是广播。

捕获绑定快捷键选中的准确可见窗口，系统设置、活动监视器和对话框不因类型被一概排除。macOS 必须有屏幕录制权限，辅助功能用于附带可读界面文字。无法读取文字时可只返回截图及原因，不扩大到整屏，也不激活来源应用。

AX 覆盖标记为 `reported_ax_subtree`，不保证整页完整。安全/遮罩字段不会被展开成隐藏明文，普通静态文本缺少可选 subrole 不会丢失值。遍历上限 64 层、2,000 节点、256 KiB 及既有时间预算，可包含应用提供的屏外内容；截断会明确说明。升级辅助程序后重启 TUI，旧附件需重新捕获才能获得以前遗漏的文字。

本地命令包括 `/appshot status`、`enable`、`disable` 和 `/appshot shortcut Control+Shift+Z`。可编辑与待确认附件共享 4 个上限，删除未发送占位符会释放文件。后端忙时可以捕获进草稿，但提交会返回 `backend_busy` 并保留内容。

回执丢失时查询状态，不自动重发：`/appshot pending status` 核对待确认提交，`/appshot pending discard` 明确丢弃本地保留片段，不取消已被后端接收的工作。后端重启可能报告 `unknown`。

<a id="troubleshooting"></a>

## 故障排查

| 状态 | 处理方式 |
| --- | --- |
| `no_receiving_session` | 先在目标 TUI 中操作一次。 |
| `receiving_session_ambiguous` | 再次使用预期接收的 TUI。 |
| `shortcut_conflict` | 换快捷键。 |
| `permission_unavailable` | 检查辅助程序权限。 |
| `source_window_unavailable` | 当前无法确定唯一可见窗口。 |
| `protected_ui` | 控制台会话锁定或不可用。 |
| `attachment_limit_reached` | 先发送或移除现有附件。 |

macOS Apple Terminal 中，聚焦 Astra 标签页即可选为接收方，无需输入；只读探测每 400 ms 将前台标签 TTY 与进程 TTY 对比。切回来源应用后保留最近选择，重复观察和后台输出不抢占活跃状态。该行为需要 Terminal 的自动化权限；不可用或使用其他终端时，在目标 TUI 按方向键。后台新开窗口不会自行成为接收者。

连接采用指数退避，连续 5 次短连接失败后停止，并合并重复断连提示。在断连 TUI 输入会重试；暂停重试的提示在再次连上之前不会重复出现。稳定连接 10 秒后重置预算。未安装原生辅助程序时，TUI 不显示 Appshot 提示，输入也不会重试；安装后重启 Astra。缩放窗口或切换显示模式不重建客户端，也不清除接收活动状态。

`context_budget_exceeded` / `context_budget_unavailable` 保留草稿。Appshot 按解析后的上下文窗口减去输出预留检查完整新请求，状态栏和普通主动压缩则使用独立的 50% 阈值，以及近期用量或估算，因此两者不能直接对照。

模型未声明 `vision` 时，`appshot_vision_unavailable` 保留草稿，需先切换图像模型再提交。已有 Appshot 历史不阻止纯文字跟进：保留 AX 文字并注明当前模型看不到像素；原图仍在会话存储，切回视觉模型后再次可见。

<a id="model-and-attachment-limits"></a>

## 模型与附件限制

任何声明 `vision` 的配置都可使用，包括本地和自定义端点。官方 Qwen、GPT-4o/4.1 使用对应图片限制；DeepSeek Vision 使用[官方 1,024-token 图片上限](https://api-docs.deepseek.com/guides/vision/#token-usage)。

文字/schema 使用 Astra 估算器并留 25% 余量；UTF-8 字节只用于传输大小，不当作 token。其他模型每图至少预留 4,096 token，大图按 32 像素 patch 网格增加。这是估算，不是精确账单或保证的提供方上限。模型名和端点域名只选择图片规则，不决定文字计数或准入权限。Qwen 按[公开最大值](https://help.aliyun.com/zh/model-studio/vision)预留每图 16,386 token，不采用默认缩放成本。

原生 PNG 限制为 10 MiB，每边 1–16,384 像素、总计 3,200 万像素。Qwen 还要求每边大于 10 像素、长宽比不超过 200:1。

<a id="verification"></a>

## 验证

`./scripts/test_appshot_e2e.sh` 覆盖私有 Swift broker → Node 客户端/草稿 → Python 准入、媒体和提供方投影，不注册真实快捷键或捕获桌面。自动夹具、签名身份和本机实测分开记录；夹具通过不证明当前机器的捕获与权限可用。
