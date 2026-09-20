# Astra ✦

[English](README.md) · **简体中文**

**面向日常任务的个人 AI Agent，具备电脑操作、记忆和终端交互能力。**

Astra 主要通过模型 API 帮你查资料、处理文件和操作应用。对话和运行状态保存在你的机器上，也支持连接可选的本地模型服务。

[快速开始](#快速开始) · [常用命令](#常用命令) · [更新与数据](#更新与数据) · [文档导航](#文档导航)

## 可以做什么

| 能力 | 用途 |
| --- | --- |
| **电脑操作（Computer Use）** | 操作浏览器页面和选定的 macOS 应用窗口，详见[浏览器操作](docs/zh-CN/browser-interaction.md)和 [Mac 指南](docs/zh-CN/macos-computer-use.md)。 |
| **把窗口带入对话** | [Appshot](docs/zh-CN/appshot.md) 可在 macOS 和 Windows 上将窗口截图及可用的界面文字加入草稿，补充问题后再发送。 |
| **资料研究与日常工作** | 搜索网页、读写文件、运行代码和执行 Notebook，并按需配置工具权限。 |
| **记忆与技能** | 保存偏好、检索相关历史、积累可复用的方法；手动检查自动总结的技能，用户加入的技能不参与这类检查。 |
| **继续已有工作** | 查看任务记录、取消执行，并通过保存的检查点恢复符合条件的任务。 |
| **自选模型与工具** | 切换模型配置、连接本地服务，或启用 MCP、QQ 消息等可选集成。 |

Lyra 是 Astra 的默认人格。想体验一个终端小彩蛋，可以试试 `/bar`。

## 快速开始

**当前为预发布版本：**请从 Git 仓库安装。暂未提供完整的独立安装包；仅安装 Python wheel 不包含完整终端界面和原生辅助程序。

### 1. 安装

需要 **Python 3.11+**、**Node.js 18+（含 npm）** 和 **Git**。

**Windows — CMD 或 PowerShell**

```powershell
git clone https://github.com/ZihangHe13123/Astra.git
cd Astra
.\astra.bat setup --install-command
```

**macOS 或 Linux**

```bash
git clone https://github.com/ZihangHe13123/Astra.git
cd Astra
./astra.sh setup --install-command
```

安装会准备 Python 环境和终端界面，将 `astra` 注册到用户 PATH，并在缺少 `.env` 时创建它。已有配置会保留。

### 2. 连接模型

编辑安装目录中的 `.env`。例如，使用内置 DeepSeek 配置时，将已有的对应条目设为以下值，并填入你自己的 API Key：

```dotenv
DEEPSEEK_API_KEY=your-api-key
LLM_MODEL=deepseek-flash
LLM_BASE_URL=https://api.deepseek.com
```

其他 API 服务和本地模型的配置方式见[模型配置与 `/connect`](docs/zh-CN/usage.md#model-connections)。

也可以运行 `astra auth login`，或在 `/connect` 选择 **ChatGPT / Codex**，
使用 ChatGPT 订阅登录，再通过 `/model` 选模型。需先在 ChatGPT「设置 → 安全」
启用 Codex 设备代码授权；可显示服务端返回的推理摘要。详见[登录说明](docs/codex-oauth.md)。

### 3. 启动

打开一个**新终端**，进入准备工作的文件夹，再运行：

```text
astra doctor
astra
```

进入 Astra 后，用 `/connect` 添加提供商，用 `/model` 浏览其模型，用 `/help` 查看命令。当前文件夹会作为工作区，除非设置了 `SANDBOX_WORKDIR`。

已经装过旧版？请看[旧版本升级指南](docs/zh-CN/launcher-update.md#upgrade-an-older-checkout)。

## 常用命令

以下命令在 **Astra 内部**输入：

| 命令 | 用途 |
| --- | --- |
| `/help` · `/doctor` | 查看命令和运行时连接状态。 |
| `/model` · `/mode high` | 选择模型，调整受支持的推理强度。 |
| `/memory` · `/skills` | 查看记忆和技能库。 |
| `/learn review` | 讨论自动技能的改进建议，再选择修改和验证范围。 |
| `/tasks` · `/resume <task-id>` | 查看任务记录，或恢复符合条件的任务。 |
| `/restart` · `/restart cancel` | 等当前工作完成后重启本次后端，或取消等待。 |
| `/wakeup` · `/wakeup cancel` | 查看或停止[会话内唤醒](docs/zh-CN/session-lifecycle.md)。 |
| `/appshot enable` | 安装原生辅助程序后，启用窗口捕获。 |

`Ctrl+L` 打开活动详情，`Ctrl+O` 打开最近一次工具结果，`Ctrl+C` 请求取消执行。更多操作见[日常使用指南](docs/zh-CN/usage.md)。

## 更新与数据

以下命令在**终端中**运行。应用更新前先退出交互式 Astra 会话；更新器会自动暂停并恢复已识别的配套服务。

```text
astra update --check
astra update
```

更新后重新运行 `astra` 即可使用新版本。更新跟随当前仓库的 Git 上游。源码安装默认将配置保存在 `.env`、私人运行状态保存在 `.astra/`、对话保存在 `.sessions/`，更新时会保留这些数据。处理本地改动、备份或恢复中断的更新，请看[更新指南](docs/zh-CN/launcher-update.md)。

本地存储不代表本地推理：请求中包含的内容会发送给所选模型服务。电脑操作需要相应辅助程序、模型能力和系统权限。Windows 的 Appshot 辅助程序目前不提供通用原生鼠标键盘控制，详见[平台限制](docs/zh-CN/appshot.md)。

## 文档导航

常用指南均提供中文版本；底层设计和历史验收材料保留英文，并在文档索引中标注。

| 我想要…… | 查看 |
| --- | --- |
| 安装、更新或恢复 | [启动与更新](docs/zh-CN/launcher-update.md) |
| 配置模型、使用终端界面 | [日常使用](docs/zh-CN/usage.md) |
| 操作浏览器或桌面应用 | [浏览器操作](docs/zh-CN/browser-interaction.md) · [macOS 电脑操作](docs/zh-CN/macos-computer-use.md) · [Appshot](docs/zh-CN/appshot.md) |
| 了解记忆和学习机制 | [记忆概览](docs/zh-CN/memory.md) · [技能检查](docs/zh-CN/skill-learning.md) |
| 配置搜索、MCP、消息或绘图服务 | [可选集成](docs/zh-CN/integrations.md) |
| 配置沙箱和文件访问范围 | [工具执行](docs/zh-CN/execution.md) |
| 开发、测试或维护 Astra | [开发指南](docs/zh-CN/development.md) |

架构、活动历史、Notebook 和故障排查等内容，可继续浏览[完整文档索引](docs/zh-CN/README.md)。

## 许可证

Astra 使用 [MIT 许可证](LICENSE)。第三方依赖与评测资料保留各自的许可，见[评测来源说明](evals/coding/README.zh-CN.md#sources-and-licenses)。
