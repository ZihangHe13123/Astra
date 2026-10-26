# 记忆、历史与学习

[首页](../../README.zh-CN.md) · [文档导航](README.md) · [English](../memory.md)

[核心记忆与任务状态](#core-memory-and-task-state) · [按需历史与自动技能](#on-demand-history-and-learned-skills) · [记忆页](#the-memory-page) · [记忆命令](#memory-commands) · [主动 Context Index](#proactive-context-index) · [评测记忆](#evaluating-memory) · [导入 Hermes 历史](#importing-hermes-history) · [可选 Hindsight 服务](#optional-hindsight-service)

Astra 分别维护少量核心记忆、可检索历史和可复用技能，三者用途和生命周期不同。

<a id="core-memory-and-task-state"></a>

## 核心记忆与任务状态

记忆分两层。核心记忆是全局且刻意保持精简的：`MEMORY.md` 和 `USER.md` 是你可以直接编辑的置顶条目，有固定的硬上限。已保存的记忆是持续增长的一层：Astra 用 `memory` 工具（`remember`）写进 `.astra/memory.db` 的记录，每条带一行摘要、来自哪个对话、日期，以及全局或本项目（当前代码目录）的范围。已保存记忆的索引始终可见，排在核心条目之后：每条一行、带日期、新的在前，最多 30 行、2,400 字符；需要摘要以外的内容时，Astra 会读取全文。索引只列出适用于所有项目和当前项目的记忆，已被取代或已过期的不列；在这之前留下的旧记录，只有偏好和关于你的事实会被列出。符合条件的记忆超过上限时，列出最近保存或用过的（被召回进某一轮、被搜索到或被完整读取）；列出的行仍按日期排序，所以只有记忆进出索引时索引才会变化。`read` 会显示一条记忆被用过几次，`/memory` 会统计 90 天没用过的已保存记忆。

Work 模式下，核心条目和索引以单独的“常驻记忆”副本发给模型：只发一次，有变化时才再发，不会在每轮上下文里重复，变化在下一轮可见；受限模式仍加入当前轮上下文。已保存的用户消息不会被改写。

持久执行以 `.astra/tasks.db` 中的 `TaskRun → Step/TaskEvent` 为准。每次请求独立记录检查点、工具结果、阻塞、取消和验证证据，`/tasks` 可查看。长任务通过 `/goal` 明确启用，增加独立验证和有限自动继续，不从普通对话猜测用户意图。

编码日志记录命令实际状态、退出码和有限输出。命令成功或文件回读不代表代码修改已验证；`/tasks` 会将这些回执标为 **UNVERIFIED**，等待评估。后台、被中断和已完成执行分别记录。模型应报告相关检查及限制，不为消除提醒而重复测试。Goal 验证包含命令参数和长输出首尾，没有证据的“完成”不能结束目标；验证器仍是模型判断，不保证正确。

普通请求不会重复注入任务 ID、`running` 标签和原问题。只有恢复、阻塞、调度或工具进度值得保留时，才加入最多 1,000 字符的 `<task-state>`；它省略模型调用记账，优先保留不确定工具结果。内容来自真实任务状态，不依赖输入关键词或额外模型调用。同一轮工具迭代使用冻结的上下文；完整目标和日志仍可通过 `/tasks`、检查点和显式恢复访问。

`.astra/memory.db` 中按会话隔离的工作记忆只作为小段 `<conversation-state>` 注入：临时约束、未决问题、假设和本轮备注。旧 goal/plan/progress/artifact 字段及计划 API 仍可兼容读取，但不再作为执行真相或注入来源。

始终可见的记忆只有两个 Markdown 文件：`MEMORY.md` 硬上限 2,200 字符，`USER.md` 硬上限 1,375 字符。超过限额的写入会被拒绝；读取侧还有保护，防止旧文件或人工编辑膨胀提示。没有 Auto Core 或额外可调 L0 预算。

结构化历史记忆保存在本地 `.astra/memory.db`，不要求 Hindsight。偏好、用户事实、经历、任务引用和观察保留来源、置信度与生命周期。核心 Markdown 和已保存记忆的索引始终可见。其他记录有两条路到达模型：自动的只有 Context Index（`session` 或 `all` 模式，见下文）；按需的是模型调用 `memory(action="search")`。Context Index 关闭时，不会有记录被自动召回进某一轮。搜索沿用 Context Index 的查询规则（去掉对话用语、短中文词保持完整、“上周”这类相对日期按时间窗口理解），列出最多 8 条适用于所有项目和当前项目的有效记忆，最匹配的在前，每条带日期。没有记忆同时包含查询中的两个词时，列出包含其中任意一个词的记忆。

个人事实和纠正通过模型记忆工具或明确的 `/memory` 命令写入，不用“我喜欢”“记住”等句式规则直接转成事实。情况变化时，Astra 会修订记忆：新的取代旧的，旧的保留并标为已取代、带上截止日期，可以用 `/memory timeline` 查看历史。每次变化都会在对话中即时显示（“已记住”“已更新记忆”“已忘记”，附编号）；某一轮的 Context Index 召回包含已保存的记忆时，也会显示召回了几条。如果某一轮读过网页、浏览器页面、MCP 结果，或子 agent、其他会话、后台任务送来的内容，在这一轮里保存记忆需要你确认：这类文本可能夹带专门写来植入记忆的指令。工具提供证据的可选保留默认关闭（`MEMORY_AUTO_RETAIN=0`）；开启后只接受成功工具调用明确提供、可归因的证据；目前没有工具提供这种证据，所以开启后暂时不会记录任何内容。诊断显示有效设置。旧 `conservative` / `evolving` 配置仍能加载，但都使用这套受限证据策略。

记忆维护（由 `/compress` 和 Astra 退出时触发）只处理明确到期时间，并退休来源、范围、寿命均相同的完全重复观察。它保留原记录，不因重复提高置信度，不把临时经历升级成永久事实。升级不改写已有记忆；用 `/memory inspect`、`correct`、`forget` 管理。

后台整理记忆在 Astra 空闲时回顾新的对话并提出修改建议。桌面或终端应用 10 分钟没有对话、且距上次回顾你至少发了 4 条消息时（最多每 30 分钟一次），DeepSeek Flash 会读这些消息（你的完整，助手的回复截短）和已有记忆，提出最多 5 条建议：记住你说过的长期事实或偏好、修订被后来的话改变的记忆、忘记你收回的记忆。每条建议都附上它依据的那句话；只有你自己的消息算依据，像密钥的内容会被丢弃。有建议时会提示数量。`/memory proposals` 查看，`/memory accept <n|all>` 按普通的已保存记忆修改生效（来源记为那句话），`/memory dismiss <n|all>` 丢弃。终端菜单在 `/memory proposals`、`accept`、`dismiss` 后面列出每条待审核的建议，可以直接选；桌面版命令面板在接受和丢弃下面列出它们。第一次回顾只读最近三天。对话会发给 DeepSeek，需要 DeepSeek 密钥；归档里只有你的消息和助手的回复，没有工具输出，极简、酒吧和当前安装提供的本地模式不会被记录。后台整理有三种模式。默认的 `review` 只提建议，你接受之前什么都不改。`auto` 让“记住”和“修订”建议直接生效：每处修改都像普通的记忆提示一样显示，并标上“记忆整理”；修订过的记忆保留旧版本，可以用 `/memory timeline` 查看；新记住的可以用 `/memory forget <id>` 撤销。“忘记”建议仍然等你确认。`off` 停止回顾。`/memory consolidation off|review|auto` 切换模式（以前设的 `on` 按 `review` 处理），回复进行中也能用；`now` 会在下一个空闲时刻回顾一次。桌面版的“设置 → 记忆整理”里也有这三个选项，并显示有几条建议待审核。模式对整个安装生效，已经打开的会话从下一次回顾起按新模式进行。中文问题中的普通英文词是软匹配项，明确字面量和代码标识符仍约束结果。

Hindsight 是搜索的可选附加来源，它的内容不会被自动加进某一轮。启用时，`memory(action="search")` 用同样的词去问它，结果列在 Astra 自己的结果之后，标为历史、非权威，与已列出的记忆重复的不列。服务失败或超过 5 秒没有回答时，保留 Astra 自己的结果并附一句说明。读 Hindsight 的记忆只通过搜索；它的 `hindsight_reflect`（综合作答）和 `hindsight_retain` 工具仍可用。当前用户陈述、核心 Markdown、实际 TaskRun 结果、Goal 和临时对话状态都优先于旧召回内容。

<a id="on-demand-history-and-learned-skills"></a>

## 按需历史与自动技能

模型可用 `session_search` 查找相关的旧对话、修复和环境观察。关掉终端后档案仍存在，不复制进始终可见的记忆。过滤、展开与过时证据处理见[本地历史检索](local-history-retrieval.md)。

可复用方法进入技能库，提示只带目录，`skill_view` 按需读全文。模型总结与用户技能归属分开，`/learn review` 手动检查下一批自动技能，不审用户技能，也不执行其中流程。迁移、游标、保护和撤销见[技能学习](skill-learning.md)。

<a id="the-memory-page"></a>

## 记忆页

桌面端侧栏的 **记忆** 显示 Astra 记着的关于你的内容。两份核心记忆 **关于你**（`USER.md`）和
**长期笔记**（`MEMORY.md`）逐条列出，并显示已用容量，可以编辑、删除或添加。下面是已保存的记忆，
新的在前，每条显示类型、日期、用到过几次和来自哪次对话；可以改正（旧内容作为被替换的版本保留）
或忘记（以后不再使用，勾选 **显示旧版本和已忘记的** 仍能看到）。最后是待确认的整理建议，附上依据的
那句话，可以采纳或不用。这些是你自己的改动，不需要批准，从下一轮对话起生效。删除一段对话不会删除
从中记下的记忆，要在这里忘记它。

也可以在对话里完成初次设置：发送 `/onboard`（或在欢迎页点 **让 Astra 认识你**）。Astra 会问几个问题：
怎么称呼你、你一天的安排、喜欢什么样的回复、希望它主动做什么、在哪里找你；先给出草稿，你确认后把内容写进
USER.md，设置安静时段，并提议定时任务（每个都要你批准）。随时可以再运行一次。

### 导出或清除你的数据

记忆页底部的 **你的数据** 可以把 Astra 记着的关于你的一切导出成一个 zip：两份核心记忆、已保存的记忆、
对话记录和它们的搜索索引、定时任务和结果、学到的技能。导出永远不含 `.env` 和其他密钥、服务商连接、
微信登录、浏览器登录和设置。**清除…** 会先列出要删除和保留的内容，你输入「清除」后才删除：上面这些数据，
加上从中生成的所有副本（搜索和向量索引、任务、事件和审批记录、桌面端的历史缓存和粘贴过的图片）。
Astra 浏览器里的登录只有勾选后才删除。设置、模型、密钥、微信登录、邮件缓存和活动记录保留；Hindsight
服务器上的记忆要在那边清。桌面端会先关掉所有对话和后台进程，完成后重新载入窗口。在终端里对应
`astra data plan`、`astra data export --out 文件.zip` 和 `astra data wipe --confirm 清除 [--browser-data]`；
还有其他 Astra 在运行时不会清除。源码安装中被 git 跟踪的文件（例如自带的技能）永远不会删除。

<a id="memory-commands"></a>

## 记忆命令

```text
/memory
/memory remember <stable agent or environment fact>
/memory remember-user <stable user profile or preference>
/tasks [id]
/memory working temporary_constraints <temporary constraint>
/memory inspect [query|id]
/memory timeline <id>
/memory why
/memory correct <id> <replacement text>
/memory forget <id>
/memory clear-working
/memory proposals
/memory accept <n|all>
/memory dismiss <n|all>
/memory consolidation [off|review|auto|now]
```

模型可用 `memory` 工具维护相同存储。疑似凭据内容会被拒绝。`MEMORY.md` 上限 2,200 字符，`USER.md` 上限 1,375 字符。核心记忆的添加和删除都无需审批；模型被要求只在你要求时，或文件已满且条目已过时、重复或被取代时，才删除核心条目。已保存的记忆添加和修订都无需审批（读过外部内容的那一轮除外，见上文）；模型只有在你要求时才能删除，而且每次删除都要你确认。你也可以用 `/memory forget <id>`，或在 `/memory review` 中选定删除。

<a id="proactive-context-index"></a>

## 主动 Context Index

这是 Astra 原生且可选的记忆推荐流程。查询规划、RRF 排名融合、MMR 多样性选择、证据预算和反馈都在 Astra Python 组件中运行，不要求外部记忆框架、记忆服务、图数据库或辅助模型。Embedding 可选，功能默认关闭，普通核心 Markdown 不变。它是结构化记录唯一的自动召回：`session` 和 `all` 模式下由它把记录加进某一轮；关闭时，记录只通过已保存记忆的索引和模型自己的搜索到达模型。

```text
/context-index session   # Astra's local records and session history
/context-index all       # Also consider cached Activity history
/context-index off       # Restore the existing memory path
/context-index why       # Explain the last selection and its cost
/context-index feedback R1 useful      # Explicit feedback on a displayed entry
/context-index feedback R1 irrelevant  # Lower its priority for similar tasks
/context-index feedback R1 outdated    # Ranking feedback; does not rewrite facts
```

中文问题会在助词、代词和疑问词处切开（“牙医是哪天”按“牙医”检索），两字词保持完整；会话存档为此维护一个字符对索引，查找两字词不需要扫描全部历史。活动历史仍按三字片段检索。“上周”“三天前”“上个月”“9月27号”“last week”这类相对日期会变成日期范围，而不是要匹配的词。检索行为、预算、诊断、反馈和质量检查见[原生记忆推荐设计（英文）](../native-memory-recommendation.md)；可选 embedding 的设置见[平台配置](context-index-platforms.md)。初始预览默认 900 字符、最高 2,000 字符，并受 500-token 估算预算限制。系统可以不返回推荐；旧记录不代表当前指令或事实证明。

<a id="importing-hermes-history"></a>

<a id="evaluating-memory"></a>
## 评测记忆

`agent/evals/memory` 测量记忆实际送到模型面前的内容。它使用合成的中文、英文和混合语料，通过公开的记忆和归档接口写入，全部在临时状态里运行，不会碰你的安装数据：

```bash
python -m agent.evals.memory.run --mode off,session,all --out memory-eval.json
```

默认离线运行，不调用模型。它按后端的方式构造 agent，跑多轮对话，按问题类别报告：证据召回（最新上下文中和整体可见两种）、误注入与旧值注入、带日期的条目、每次请求和每个对话注入的 token 及其中核心记忆的部分、前缀稳定性和准备耗时。`--live --model <key>` 会调用真实模型，按关键词、日期和拒答给回答打分，并检查在一个会话里说过的偏好下个会话是否还记得。`--consolidation` 对每个设了预期修改的世界跑一次记忆回顾（故意少写入几条记录，让归档领先于记忆），报告预期的修改有没有被提出、过时的记忆是原地修订还是重复新增，以及其他建议有多少。

## 导入 Hermes 历史

默认从 `~/.hermes/state.db` 导入到源码安装的 `.astra/sessions.db`。写入前用明确路径预览。

Windows 读取 WSL 中的数据库：

```powershell
# Windows reading a Hermes database in WSL
python scripts/import_hermes_history.py --dry-run `
  --source-db "\\wsl.localhost\Ubuntu\home\your-name\.hermes\state.db" `
  --target-db ".astra\sessions.db"
```

macOS/Linux：

```bash
# macOS/Linux
python3 scripts/import_hermes_history.py --dry-run \
  --source-db "$HOME/.hermes/state.db" \
  --target-db ".astra/sessions.db"
```

核对预览后去掉 `--dry-run` 重复执行。`HERMES_DB` 和 `ASTRA_SESSIONS_DB` 为定时脚本提供相同路径覆盖，显式 `--source-db` / `--target-db` 优先。

<a id="optional-hindsight-service"></a>

## 可选 Hindsight 服务

Hindsight 使用自己的模型执行 retain、consolidation、reflect。Astra 的 `HINDSIGHT_PROCESSING_MODEL=deepseek-flash` 只是诊断标签，不会修改服务器。在实际运行 Hindsight 的 Windows/WSL 主机上，更新服务环境（例如 `profiles/main.env`）：

```dotenv
HINDSIGHT_API_LLM_MODEL=deepseek-flash
HINDSIGHT_API_RETAIN_LLM_MODEL=deepseek-flash
HINDSIGHT_API_CONSOLIDATION_LLM_MODEL=deepseek-flash
HINDSIGHT_API_REFLECT_LLM_MODEL=deepseek-flash
```

保留原 DeepSeek 提供方、端点、凭据和 bank 配置。用新环境重新加载或创建服务及独立 worker，并检查实际有效值；普通容器重启不会导入修改后的 Compose env 文件。各操作或 bank 的模型覆盖也应一致，参见 [Hindsight 配置参考（英文）](https://hindsight.vectorize.io/developer/configuration)。只更新 Astra 不会更新独立部署的 Hindsight。
