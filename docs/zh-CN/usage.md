# 日常使用

[首页](../../README.zh-CN.md) · [文档导航](README.md) · [English](../usage.md)

[连接模型](#model-connections) · [模型时间上下文](#model-time-context) · [推理强度](#reasoning-intensity) · [结构化澄清](#structured-clarification) · [终端主题](#tui-themes) · [持久任务](#durable-tasks)

选择模型、调整终端界面，并管理正在进行的工作。

## 对话式命令

以下命令进入普通模型对话，读取的证据会留在会话里，方便继续追问、纠正或选择下一步：

| 命令 | 行为 |
| --- | --- |
| `/learn review [技能名]` | 先只读审查，再按用户选择修改与验证。 |
| `/doctor [分类或症状]` | 解释当前诊断证据，提出检查或修复方案。 |
| `/diagnostics [分类]` | 解释当前运行时快照。 |
| `/conclave <问题>` | 调用现有研究工具，随后继续讨论结果。 |
| `/skills create [名称] [描述]` | 先起草完整的用户技能，再决定保存。 |
| `/memory review [查询]` | 检查记忆原文、标识和来源，再处理选定的纠正。 |
| `/handoff [说明]` | 整理并保存脱敏交接文档，可继续要求修改。 |

审查、技能起草、记忆整理和诊断的首轮保持只读；建议输出完成后，再回复选择修改或实测范围。整理后的交接文档使用独立文件名，避免退出时的自动快照覆盖它。这些命令需要在工作会话中连接模型。

直接查看报告可用 `/doctor --raw [分类]`、`/diagnostics --raw [分类]`、`/diagnostics json`、`/handoff --raw [说明]`。`/skills create --template <名称> <描述>` 保留空模板创建。状态、配置、历史、撤销和取消命令仍直接执行。

<a id="model-connections"></a>

## 连接模型

输入 `/connect`，依次选择提供商、API 线路，再输入 API Key 或选择已有环境变量。Astra 会读取该端点的模型列表，然后打开可搜索的模型菜单。首次设置不需要先配置默认模型的 Key。连接成功只保存提供商；选定模型后，才会保存为下次启动的默认模型。

```text
/connect
/model
/model deepseek::deepseek-flash
/doctor
```

在 `/model` 中按 Enter 进入已连接的提供商，输入文字筛选模型。第一层还显示最近使用的模型。提供商子菜单有 **Refresh model list**（刷新）和 **Back to providers**（返回）。需要使用未列出的模型时，在 `provider::` 后输入完整 ID，选择 **Use model ID**。

只查询当前打开的提供商；缓存有效期为一小时，也可手动刷新，没有后台定时轮询。条目分别标注 `live`（实时）、`cache`（缓存）、`stale-cache`（查询失败后的旧缓存）、`preset`（预设）或手填模型。失败原因会与缓存/预设一起显示；接口成功返回空列表时不拿旧列表补充；当前选择可能以 `selected` 标记继续显示。能列出模型，不代表已验证聊天权限、套餐额度或工具能力。

向导支持 DeepSeek、Qwen / 百炼、智谱、混元、OpenRouter，以及自定义 OpenAI 兼容端点。普通 API、Coding / Token Plan、不同地域分别保存，不会把套餐 Key 自动重试到普通计费 API。百炼需要业务空间地址的线路，应粘贴控制台给出的 Base URL。本地模型（含 oMLX）沿用现有发现方式；非兼容协议仍需要独立适配器。

内置模型参数仍在 `config/models.yaml`，本机覆盖项仍在 `.astra/models.yaml`。已知模型保留自己的参数；新模型优先使用接口返回的元数据，缺失时按保守的 32K 上下文、4K 输出预算处理，不假定视觉或推理支持。提供商有特殊限制时，可补充本机模型覆盖项。

向导中的 Key 遮蔽显示，经专用控制消息传递，只保存到 `.astra/connections/*.json`，不进入 Git 或会话历史。POSIX 文件仅所有者可读写；Windows 使用安装目录所属用户的目录权限。环境变量方式只保存变量名。模型元数据另存于 `.astra/model-cache`，按端点及凭据隔离。这些文件都属于安装状态（或 `ASTRA_HOME`），`astra update` 会保留。它们是本机私有文件，并非加密凭据库。

旧命令仍可使用：

```text
/connect my-local http://127.0.0.1:8084/v1 LLM_API_KEY
```

第三个参数是环境变量**名称**，不要填 Key 值。纯 CLI 中，`/connect` 提供编号选择和隐藏 Key 输入；`/model <provider>::` 列出该提供商的模型。

<a id="deepseek-model-migration"></a>

### DeepSeek 模型迁移

内置 DeepSeek 配置使用 `deepseek-flash`（DeepSeek V4.1 Flash），支持原生图片输入和思考控制，工作代理的 `flash` / `fast` 别名也指向它。在官方 `api.deepseek.com` 端点上，已保存的 V4 Flash、Vision Exp 和临时 V4.1 名称会解析到这个配置；退休名称不再单独显示。`deepseek-v4-pro` 仍是独立可选的纯文本配置。自定义和本地端点保留各自的模型 ID；1M 上下文与 384K 输出额度保持不变。

[9 月 10 日更新公告](https://api-docs.deepseek.com/zh-cn/updates/)停用了 V4 Flash 和 Vision Exp；原定 9 月 14 日执行的 V4 Pro 重定向后来撤回，官方继续提供 `deepseek-v4-pro`，计费不变。已有 Astra 进程需重启才能加载新版适配器。

<a id="deepseek-vision-tiling"></a>

### DeepSeek 图片切块

`deepseek-flash` 可通过原始像素切块保留大型本地图片或 base64 图片的细节。此选项默认开启，修改后立即生效，并保存到 `.astra/settings.json` 的 `vision_tiles_enabled`：

```text
/vision-tiles
/vision-tiles on
/vision-tiles off
```

启用时，Astra 生成一张概览和若干无损 768 × 768 细节块，相邻块重叠 64 px。单次请求最多 32 张图片；放不下全部切块时，模型通过当前请求的切块工具选择补充区域。缓存位于 `.astra/image-cache/tiles`，并有容量清理限制。

原始像素保证仅适用于本地路径和 base64 数据。外部图片 URL 直接发送，Astra 不主动下载；小型动态 GIF 也直接发送。需要切块的动态 GIF 会返回可恢复错误，避免悄悄改为服务端缩小后的图片。`/vision-tiles off` 恢复直接发送，此时 DeepSeek 可能缩小大图并损失细节。其他模型配置不受影响。

<a id="model-time-context"></a>

## 模型时间上下文

用户消息带有仅供模型读取的时间戳，例如 `<message_time>2026-09-14T00:30:00+08:00 周一</message_time>`。周几按同一个本地日期计算；历史回放保留原日期，相对日期锚点也使用保存日期对应的周几。

界面时间栏不变，输出和历史过滤器同时识别旧版 ISO 标记和新版带周几标记。现有会话无需迁移。需要精确当前时间或其他时区时，使用 `current_time`。

<a id="reasoning-intensity"></a>

## 推理强度

`/mode` 只控制推理强度，与人格、工具和输出预算分开：

```text
/mode
/mode low
/mode high
/mode xhigh
/mode max
```

默认 `high`。`xhigh` 比 `high` 想得更深、token 花费更高，但远低于 `max`；`max` 最耗额度，也容易想过头。选定的 `reasoning_effort` 保存到 `.astra/settings.json`，从下一次模型请求起应用。DeepSeek、Codex 和 Claude 适配器会发送该参数：DeepSeek 把 `xhigh` 当 `high` 执行，不支持 `xhigh` 的 Codex 模型按它低于 `xhigh` 的最高档执行。其他适配器保留原行为，并说明该偏好未生效。输出预算仍由模型配置控制，`/think` 单独控制是否显示思考内容。

旧 `agent_mode` 的 `coding` 迁移到 `max`，`chat` 迁移到 `high`。旧 coding/chat 命令和 `/mode code` 已退休。普通会话使用原生工具，工具暴露方式不再作为用户模式选项。

状态栏中的 `TOOLS NATIVE` 是只读运行状态，位于推理强度之后，随后显示模型、上下文占用和最近工具结果。工具执行时仍显示模型。`Ctrl+L` 展开会话和上下文详情；窄窗口会缩短次要标签，优先保留模型、上下文和展开快捷键，极窄时优先显示 Computer Use 控制状态。

每次成功的模型请求结束后，状态栏显示该次平均输出速度 `tok/s`（紧凑布局为 `t/s`）。它等于提供方报告的 `completion_tokens` 除以请求总耗时，包含首 token 等待，不包含工具执行；DeepSeek 的计数包含思考 token。展开后的 `SPEED` 行显示计数与时长。这是最近完成请求的平均值，不是实时估计；缺少用量时不猜测速度，切换模型或会话会清除旧值。

<a id="structured-clarification"></a>

## 结构化澄清

较复杂的编码任务可能出现带选项和自定义回答的问题卡。回答后继续同一轮；需求明确且原请求已授权实施时，Astra 可填写 Working Plan、建立 Goal Mode 并开始执行。

选择答案不等于工具授权。文件、Shell、MCP、工作流和主机执行仍遵循原有权限检查。消息渠道无法显示交互卡时，工具返回可恢复错误，模型可改用普通文字提问，避免等待不可见的界面。

<a id="tui-themes"></a>

## 终端主题

`/theme` 列出 Ink TUI 主题。`/theme hermes`、`classic`、`nord`、`dracula`、`solarized` 和 `gruvbox` 会立即切换。默认 `hermes` 是参考 Hermes CLI 的暖金与奶油色主题；`classic` 保留鲜明的 ANSI 配色。选择单独保存到 `.astra/tui-settings.json`。

<a id="durable-tasks"></a>

## 持久任务

每轮用户请求通过 SQLite WAL 记入 `.astra/tasks.db`。模型和工具边界保存检查点；重启后可复用已经完成的工具结果，执行状态不确定的工具不会自动重放。

```text
/tasks
/tasks <task-id>
/resume <task-id>
/cancel [task-id]
```

任务运行时终端仍接收命令。第一次 `Ctrl+C` 请求持久化取消，再按一次强制退出进程。恢复仅限原会话，以保证对应的对话检查点可用。

回复流或工具执行期间也可切换 YOLO：`/yolo` 切换，`/yolo on` / `off` 明确设定，`/yolo status` 查询。`Ctrl+Y` 在授权面板、问题卡和工具详情中同样有效，不会提交草稿。启用后输入栏显示 `YOLO`，支持此机制的待审批操作获得一次性放行；强制权限边界仍然有效。关闭后，后续工具边界恢复检查，包括运行中的 Team 成员；已经授权的工具不会被取消，单独授予的会话权限也不会被抹掉。Work 和 Minimal 模式均支持 YOLO，不能在回复期间执行的命令会说明原因。

较长工具结果默认折叠。`/tool <id>` 打开指定结果，`Ctrl+O` 打开最近一次结果；方向键和 PageUp/PageDown 滚动，Escape 或 `Ctrl+O` 关闭。`TUI_TOOL_COLLAPSE_CHARS`、`TUI_TOOL_COLLAPSE_LINES` 和 `TUI_MAX_TOOL_RESULTS` 控制折叠阈值和详情保留数量。命令建议使用固定高度滚动窗口，`TUI_COMMAND_MENU_ROWS` 默认 8，方向键仍可遍历全部匹配项。

向输入框粘贴图片路径时，光标前后的已有文字会保留。路径转换成 `[Image #N]` 占位符并与问题分开处理；粘贴本身不会发送，确认草稿后再按 Enter。
