# 记忆、历史与学习

[首页](../../README.zh-CN.md) · [文档导航](README.md) · [English](../memory.md)

[核心记忆与任务状态](#core-memory-and-task-state) · [按需历史与自动技能](#on-demand-history-and-learned-skills) · [记忆命令](#memory-commands) · [主动 Context Index](#proactive-context-index) · [评测记忆](#evaluating-memory) · [导入 Hermes 历史](#importing-hermes-history) · [可选 Hindsight 服务](#optional-hindsight-service)

Astra 分别维护少量核心记忆、可检索历史和可复用技能，三者用途和生命周期不同。

<a id="core-memory-and-task-state"></a>

## 核心记忆与任务状态

核心记忆是全局且刻意保持精简的。`MEMORY.md` 和 `USER.md` 是始终注入的直接来源，不是 SQLite 的镜像。Work 模式下，它们以单独的“常驻记忆”副本发给模型：只发一次，任一文件变化时才再发，不会在每轮上下文里重复，修改在下一轮可见；受限模式仍加入当前轮上下文。已保存的用户消息不会被改写。目前核心 Markdown 不按项目文件夹分区。

持久执行以 `.astra/tasks.db` 中的 `TaskRun → Step/TaskEvent` 为准。每次请求独立记录检查点、工具结果、阻塞、取消和验证证据，`/tasks` 可查看。长任务通过 `/goal` 明确启用，增加独立验证和有限自动继续，不从普通对话猜测用户意图。

编码日志记录命令实际状态、退出码和有限输出。命令成功或文件回读不代表代码修改已验证；`/tasks` 会将这些回执标为 **UNVERIFIED**，等待评估。后台、被中断和已完成执行分别记录。模型应报告相关检查及限制，不为消除提醒而重复测试。Goal 验证包含命令参数和长输出首尾，没有证据的“完成”不能结束目标；验证器仍是模型判断，不保证正确。

普通请求不会重复注入任务 ID、`running` 标签和原问题。只有恢复、阻塞、调度或工具进度值得保留时，才加入最多 1,000 字符的 `<task-state>`；它省略模型调用记账，优先保留不确定工具结果。内容来自真实任务状态，不依赖输入关键词或额外模型调用。同一轮工具迭代使用冻结的上下文；完整目标和日志仍可通过 `/tasks`、检查点和显式恢复访问。

`.astra/memory.db` 中按会话隔离的工作记忆只作为小段 `<conversation-state>` 注入：临时约束、未决问题、假设和本轮备注。旧 goal/plan/progress/artifact 字段及计划 API 仍可兼容读取，但不再作为执行真相或注入来源。

始终可见的记忆只有两个 Markdown 文件：`MEMORY.md` 硬上限 2,200 字符，`USER.md` 硬上限 1,375 字符。超过限额的写入会被拒绝；读取侧还有保护，防止旧文件或人工编辑膨胀提示。没有 Auto Core 或额外可调 L0 预算。

结构化历史记忆保存在本地 `.astra/memory.db`，不要求 Hindsight。偏好、用户事实、经历、任务引用和观察保留来源、置信度与生命周期。相关时每轮最多召回 4 条、渲染总计 2,400 字符；项目/配置问题不必含“记住”也能匹配。简单对话、斜杠命令和无关问题跳过自动召回。可用时使用 SQLite FTS5，中文另有确定性的子串回退。目前除纠正外没有途径新增结构化记录；纠正会取代被纠正的记录，已有记录仍可检索。

个人事实和纠正通过模型记忆工具或明确的 `/memory` 命令写入，不用“我喜欢”“记住”等句式规则直接转成事实。工具提供证据的可选保留默认关闭（`MEMORY_AUTO_RETAIN=0`）；开启后只接受成功工具调用明确提供、可归因的证据；目前没有工具提供这种证据，所以开启后暂时不会记录任何内容。诊断显示有效设置。旧 `conservative` / `evolving` 配置仍能加载，但都使用这套受限证据策略。

记忆维护（由 `/compress` 和 Astra 退出时触发）只处理明确到期时间，并退休来源、范围、寿命均相同的完全重复观察。它保留原记录，不因重复提高置信度，不把临时经历升级成永久事实。升级不改写已有记忆；用 `/memory inspect`、`correct`、`forget` 管理。中文问题中的普通英文词是软匹配项，明确字面量和代码标识符仍约束结果。

Hindsight 是可选的联合语义召回增强。启用时与本地权威库结果合并；不可用时本地保留和召回继续工作。服务响应慢时，会在交互召回预算（`HINDSIGHT_INTERACTIVE_RECALL_TIMEOUT`，2.5 秒）内被截断，本地结果不受影响。当前用户陈述、核心 Markdown、实际 TaskRun 结果、Goal 和临时对话状态都优先于旧召回内容。

<a id="on-demand-history-and-learned-skills"></a>

## 按需历史与自动技能

模型可用 `session_search` 查找相关的旧对话、修复和环境观察。关掉终端后档案仍存在，不复制进始终可见的记忆。过滤、展开与过时证据处理见[本地历史检索](local-history-retrieval.md)。

可复用方法进入技能库，提示只带目录，`skill_view` 按需读全文。模型总结与用户技能归属分开，`/learn review` 手动检查下一批自动技能，不审用户技能，也不执行其中流程。迁移、游标、保护和撤销见[技能学习](skill-learning.md)。

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
```

模型可用 `memory` 工具维护相同存储。疑似凭据内容会被拒绝。`MEMORY.md` 上限 2,200 字符，`USER.md` 上限 1,375 字符。核心记忆的添加和删除都无需审批；模型被要求只在你要求时，或文件已满且条目已过时、重复或被取代时，才删除核心条目。结构化记录只能通过 `/memory forget <id>` 或你在 `/memory review` 中选定的删除来遗忘。

<a id="proactive-context-index"></a>

## 主动 Context Index

这是 Astra 原生且可选的记忆推荐流程。查询规划、RRF 排名融合、MMR 多样性选择、证据预算和反馈都在 Astra Python 组件中运行，不要求外部记忆框架、记忆服务、图数据库或辅助模型。Embedding 可选，功能默认关闭，普通核心 Markdown 不变。在 `session` 和 `all` 模式下，它还会接管内置路由对结构化记录的自动召回（包括 Hindsight 的自动合并）。

```text
/context-index session   # Astra's local records and session history
/context-index all       # Also consider cached Activity history
/context-index off       # Restore the existing memory path
/context-index why       # Explain the last selection and its cost
/context-index feedback R1 useful      # Explicit feedback on a displayed entry
/context-index feedback R1 irrelevant  # Lower its priority for similar tasks
/context-index feedback R1 outdated    # Ranking feedback; does not rewrite facts
```

检索行为、预算、诊断、反馈和质量检查见[原生记忆推荐设计（英文）](../native-memory-recommendation.md)；可选 embedding 的设置见[平台配置](context-index-platforms.md)。初始预览默认 900 字符、最高 2,000 字符，并受 500-token 估算预算限制。系统可以不返回推荐；旧记录不代表当前指令或事实证明。

<a id="importing-hermes-history"></a>

<a id="evaluating-memory"></a>
## 评测记忆

`agent/evals/memory` 测量记忆实际送到模型面前的内容。它使用合成的中文、英文和混合语料，通过公开的记忆和归档接口写入，全部在临时状态里运行，不会碰你的安装数据：

```bash
python -m agent.evals.memory.run --mode off,session,all --out memory-eval.json
```

默认离线运行，不调用模型。它按后端的方式构造 agent，跑多轮对话，按问题类别报告：证据召回（最新上下文中和整体可见两种）、误注入与旧值注入、带日期的条目、每次请求和每个对话注入的 token 及其中核心记忆的部分、前缀稳定性和准备耗时。`--live --model <key>` 会调用真实模型，按关键词、日期和拒答给回答打分，并检查在一个会话里说过的偏好下个会话是否还记得。

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
