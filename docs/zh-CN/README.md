# Astra 中文文档

[首页](../../README.zh-CN.md) · **简体中文** · [English](../README.md)

第一次使用可先看[中文首页](../../README.zh-CN.md)的快速开始。常用指南均有中文版，下面按任务选择；底层设计和历史验收材料暂保留英文，链接已标明语言。

## 安装与日常使用

| 我想要…… | 查看 |
| --- | --- |
| 安装、更新、保留本地改动或恢复中断 | [安装、启动与更新](launcher-update.md) |
| 使用源码桌面预览版、模型登录和文件差异 | [桌面 GUI](gui.md) |
| 选择模型、调整推理、使用终端命令 | [日常使用](usage.md) |
| 受控重启、会话内稍后继续或定期检查 | [会话生命周期](session-lifecycle.md) |
| 配置沙箱、主机文件访问和工具权限 | [工具执行](execution.md) |
| 选择 Lyra 或定制本地人格、了解酒吧模式 | [Lyra 人格](persona.md) |
| 添加搜索、MCP、QQ 消息、图片工具或 163 邮箱 | [可选集成](integrations.md) |
| 查找并实际查看图片参考 | [图片搜索](search-images.md) |
| 执行 Notebook 并保存结果 | [Notebook 执行](notebook-execution.md) |

## 电脑操作与窗口捕获

| 我想要…… | 查看 |
| --- | --- |
| 操作浏览器页面和表单 | [浏览器操作](browser-interaction.md) |
| 操作选定的 Mac 应用窗口 | [macOS Computer Use](macos-computer-use.md) |
| 把窗口截图加入对话 | [Appshot](appshot.md) |

## 记忆、技能与活动历史

| 我想要…… | 查看 |
| --- | --- |
| 了解哪些内容被保存、注入或按需检索 | [记忆概览](memory.md) |
| 积累技能、检查下一批、迁移旧候选或撤销 | [技能学习与检查](skill-learning.md) |
| 检索旧对话和保存的观察 | [本地历史检索](local-history-retrieval.md) |
| 查询已记录的电脑活动 | [活动历史](activity-history.md) |
| 配置活动记录 | [macOS 浏览器网址](macos-browser-activity.md) · [Windows 活动记录](windows-activity.md) |
| 配置本地嵌入模型与索引 | [Context Index 平台配置](context-index-platforms.md) |

## 排查、开发与维护

- [GUI 设计与完整功能对照（中文）](../design/gui.md)：桌面界面、共享后端、源码启动与逐项验收；含当前预览版实现和剩余工作。
- [运行耗时分析](runtime-responsiveness.md)：区分模型等待、工具执行、存储与界面耗时。
- [开发与维护](development.md)：锁定依赖、本地检查、发布检查、回放评估和生成文件清理。

## 设计与验收参考（英文）

以下材料保留英文，便于与实现、协议及原始验收记录对照。历史记录中的日期、版本和通过数量只代表当时的验证范围，不能直接作为当前版本或所有应用的兼容保证。

| 内容 | 英文参考 |
| --- | --- |
| 记忆选择、检索预算和质量回放 | [原生记忆推荐（英文）](../native-memory-recommendation.md) |
| 核心 Skill 加载和提示词边界 | [核心规则（英文）](../runtime/core-rules.md) |
| 工具顺序、轮次时限和故障回放 | [轮次可靠性（英文）](../runtime/turn-reliability.md) |
| 模型流、提示词复用和客户端恢复 | [模型服务可靠性（英文）](../provider-reliability.md) |
| 捕获逻辑及平台适配器 | [Appshot 架构（英文）](../appshot-architecture.md) |
| 委派、Team 预算和生命周期 | [Worker 运行时（英文）](../design/worker-runtime-v1.md) |
| 隔离的 Minimal 会话 | [Minimal 模式设计（英文）](../design/minimal-mode.md) |
| 程序化工具调用中的审批 | [PTC 审批桥接（英文）](../design/ptc-approval-bridge.md) |
| 配置能力、样本结果与实机结果的区别 | [Computer Use 证据规则（英文）](../computer-use-evidence.md) |
| 可复现的 Mac 实机检查 | [实机测试说明（英文）](../macos-computer-use-real-machine-test-runbook.md) |
| WPS 文本观察实验 | [Smart Snapshot 测量（英文）](../macos-smart-snapshot-runbook.md) |
| Windows 辅助程序构建、测试与限制 | [Windows helper（英文）](../../native/windows-computer-helper/README.md) |
| 应用状态事务的本地验收 | [空白验收模板（英文）](../macos-app-state-acceptance.md) |
| 历史兼容结果及证据边界 | [脱敏兼容摘要（英文）](../macos-computer-compatibility-evidence.md) |

## 文档维护

中文页优先链接中文版，英文页继续链接英文版，每篇常用指南顶部可切换语言。更新指南时同步对应译文，保持命令、参数名、错误码和示例一致；英文设计与验收链接继续明确标注。

此目录保留当前命令、行为、限制、设计决策与可复现测试步骤。移动文档时同步更新代码、Skills 和测试中的链接。实现计划、任务对话、交接和单次报告放入被忽略的 `output/`、`.astra/artifacts/` 或 `docs/superpowers/`，不强制加入 Git。清理历史报告前，把仍有用的结论整理到当前指南；已提交的旧版本仍可从 Git 历史读取。
