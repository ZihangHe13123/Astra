# 安装、启动与更新

[首页](../../README.zh-CN.md) · [文档导航](README.md) · [English](../launcher-update.md)

[首次安装](#first-installation) · [升级旧安装](#upgrade-an-older-checkout) · [日常启动](#daily-use) · [更新源码安装](#update-from-a-source-checkout) · [看懂更新结果](#reading-update-results) · [恢复与修复](#recovery-and-repair) · [数据位置与包安装](#data-and-installed-distributions) · [命令参考](#command-reference) · [故障排查](#troubleshooting) · [跨平台移动源码目录](#moving-a-checkout-between-platforms) · [验收边界](#acceptance-boundary)

本指南介绍预发布版本的源码安装流程。安装器已有包归属和私有数据路径的隔离机制，但目前尚无完整公开安装包或自动稳定版下载服务。

<a id="first-installation"></a>

## 首次安装

先安装 Python 3.11+、终端界面所需的 Node.js 18+（含 npm）和 Git。可选桌面 GUI 需要 **Node.js 22.12.0+**；启用 GUI 后，更新和修复也使用这个要求。安装器会在安装依赖或暂停配套服务之前检查版本。版本不足时，升级到受支持的 Node.js LTS，打开新终端（Windows 尤其需要），用 `node --version` 确认后重试 `astra setup --gui --repair`。

安装使用仓库锁定的 Python 与 Node 依赖；缺少 uv 时会安装私有副本，不向全局 Python 环境安装包。Windows 启动器可在 CMD 或 PowerShell 中运行，Agent 的 [Bash 工具](execution.md#minimal-bash-environment)另行使用 WSL。

Windows：

```powershell
git clone https://github.com/ZihangHe13123/Astra.git
cd Astra
.\astra.bat setup --install-command
```

macOS/Linux：

```bash
git clone https://github.com/ZihangHe13123/Astra.git
cd Astra
./astra.sh setup --install-command
```

Setup 创建 `.venv`、安装并构建终端界面，将 `astra` 注册到用户 PATH。已有 `.env` 会保留；缺少时从 `.env.example` 创建。在该文件中配置模型，或启动后使用[模型连接命令](usage.md#model-connections)。安装检查本身不需要 API Key 或模型连接。

打开新终端使 PATH 生效，进入准备工作的项目文件夹，再运行：

```text
astra doctor
astra
```

除非明确设置 `SANDBOX_WORKDIR`，当前目录就是工作区。启动器独立定位自己的安装目录，不依赖当前项目。日常启动只检查环境，不拉取代码或安装依赖。

<a id="upgrade-an-older-checkout"></a>

## 升级旧安装

旧版安装需要先手动拉取一次，才能获得新启动器。先关闭使用该安装的 Astra 会话和服务，然后在原源码目录执行。

Windows：

```powershell
git pull --ff-only
.\astra.bat setup --install-command
```

macOS/Linux：

```bash
git pull --ff-only
./astra.sh setup --install-command
```

拉取失败时先处理 Git 报告的问题。原有配置、对话和记忆保留。打开新终端运行 `astra doctor`，此后使用 `astra update` 更新。

<a id="daily-use"></a>

## 日常启动

在工作项目目录运行 `astra`；当前目录及文件权限、`SANDBOX_WORKDIR` 决定工作区，不决定安装位置。源码包装脚本和已注册命令使用同一套标准库参数解析器：`astra` 启动 Ink，`astra --cli` 显式启动旧文本 CLI，`astra --tui` 保留旧 Textual 入口，`agent-lab` 保留原兼容行为。

正常启动不联网取代码或安装软件。若手动改过锁文件或绕过更新器拉取源码，环境显示未验证时运行 `astra setup`。

<a id="update-from-a-source-checkout"></a>

## 更新源码安装

```text
astra update --check
astra update
```

更新跟随当前分支配置的 Git 上游，适用于 Mac 提交、Windows 更新等工作流，不会擅自指定别的分支。没有 `.git` 的下载 ZIP 不能使用 Git 更新，需要克隆仓库。

`--check` 拉取到临时 Git 引用、比较固定提交后删除该引用，可能增加 Git 对象，但不替换工作文件、不同步依赖、不重启会话。Astra 运行期间也能检查，`--json` 提供结构化输出。

应用更新要求上游可快进。本地修改的文件若未被上游触碰，或已与传入版本相同，会自动保留。双方都改了同一文件时，交互终端列出文件并提供三种选择：

1. **Keep local files（保留，默认）**：整份保留本地文件，更新其他文件。
2. **Back up and overwrite local files（备份后覆盖）**：备份所有列出的本地文件，然后使用上游版本。
3. **Cancel（取消）**：维持当前安装。

保留是按整文件处理，不会合并文件中的不同文本片段。原始字节、删除状态、可执行权限，以及暂存/未暂存的区别都会保留。因此更新成功后仍可能显示 `dirty: True`。

脚本可明确指定策略：

```text
astra update --keep-local
astra update --overwrite-local
```

`--json` 不弹交互提示，非交互环境的重叠修改必须先明确策略。策略参数不能与 `--check`、`--repair`、`--recover` 混用。已是最新且健康的安装没有传入更新，即使要求覆盖也不会丢弃本地编辑。

无关的未跟踪或忽略文件保留；若即将被上游文件替换，同样加入选择。私人配置、状态及生成环境不属于源码覆盖对象。不能明确保留的文件/目录冲突、子模块冲突，以及受影响路径的 intent-to-add 或隐藏索引标记，须先人工处理。

更新器不会自动 stash、重置本地提交或切换分支，也不会修改由别的工作树共享或通过符号链接指向的生成环境。

应用前关闭交互式 Astra 会话。启动器和后端持有进程生命周期租约，安装锁避免更新期间另起启动或维护。已识别的配套服务由更新器暂停和恢复；占用安装的未知进程仍会阻止更新，不会批量终止 Python/Node。

<a id="companion-service-lifecycle"></a>

### 配套服务生命周期

先完成本地文件选择及工具检查，再暂停服务。启用状态写入 `services.json`，每次原生停止调用前先持久化意图。暂停后再次确认安装空闲，才改动代码或依赖。取消、仅检查、已最新且健康时都不重启服务。

| 平台 | 自动管理范围 | 恢复内容 |
| --- | --- | --- |
| macOS | 可确认属于此安装的浏览器桥接、活动摘要和同步 LaunchAgent，以及本安装拥有的稳定活动记录器 | 原先加载的任务，保留 plist、录制设置、排除规则和配对数据；常驻任务需运行，周期任务需恢复调度。 |
| Windows | 此安装启动的可选 Python 活动记录器及其摘要子进程 | 通过停止标记正常退出，再按相同参数后台启动；保留暂停状态并验证新心跳。 |
| Linux | Astra 当前不安装对应服务 | 不调用服务管理器。 |

macOS 还管理本安装启动的共享 MLX embedding worker：检查准确的解释器、模块和运行目录，等待正在进行的编码完成，再使用经过认证的回环停止端点。周期客户端重启前先恢复原 worker 目录；控制端点就绪后，模型可继续后台准备。回执不包含连接令牌或向量内容。

原本关闭的服务保持关闭，其他安装的 LaunchAgent 不动。恢复前复核加载身份、保存定义和指纹；维护期间发生的编辑会保留并报告。稳定原生程序按已有安装重启，不隐式重建或重签名。Appshot 随下次会话启动连接；外部模型和 MCP 服务不在管理范围内。

`services_status: restored` 表示恢复成功。代码已装好但服务恢复失败时，仍报告 `outcome: applied`，同时给出 `restart_failed`、下一步说明及非零退出码，并继续尝试其他服务。运行 `astra update --recover` 重试剩余服务，不回滚已成功安装的代码，也不重复恢复同一事务中已恢复的服务。JSON 标准输出与进度错误输出分开。

维护程序使用基础 Python，从源码目录外的临时副本运行。Windows 批处理在预解析命令块中结束，更新自身文件不会改变后续执行；若从即将被替换的虚拟环境调用，会提示改用 `astra.bat`。

只备份受影响的生成目录：Python 锁文件或包元数据变更触发 Python 环境，npm 锁文件变更触发 Node 依赖，界面变更触发构建输出。未验证环境和明确修复执行全量同步。扩展组会保存，接管旧环境时也识别常见已装扩展；`--inexact` 保留额外包，但不放过依赖冲突。

更新器先保存旧环境，再快进到固定提交、按锁同步、检查 Python 导入/语法和界面，最后写回执。`applied` 表示下次启动已准备好，不表示已有进程已换上新代码。

<details>
<summary>旧更新器的一次性浏览器服务迁移</summary>

旧更新器可能仍把后台浏览器记录器视为阻塞进程，退出 TUI 不会停止它。仅在升级这类旧安装时，临时卸载已经安装的桥接服务：

```bash
launchctl bootout "gui/$(id -u)/com.astra.activity-browser-bridge"
```

维护或恢复完成后，用原保存配置加载：

```bash
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.astra.activity-browser-bridge.plist"
```

这些步骤保留配置与配对数据，只适用于已安装的浏览器服务。其他服务按自己的停止/启动流程处理。

</details>

<a id="reading-update-results"></a>

## 看懂更新结果

| 输出 | 含义 |
| --- | --- |
| `outcome: available` | 上游有更新，仅检查，尚未应用。 |
| `outcome: applied` | 更新完成，重新启动 Astra 后使用。 |
| `outcome: current` | 当前源码已匹配所检查的上游。 |
| `outcome: cancelled` | 工作文件未改变。 |
| `local_changes: True` / `dirty: True` | 仍有本地差异，可以与成功保留本地文件的结果同时出现。 |

<a id="recovery-and-repair"></a>

## 恢复与修复

```text
astra doctor
astra setup --repair
astra update --repair
astra update --recover
```

- `setup --repair` 同步当前源码配置，包括主动编辑的开发元数据。
- `update --repair` 修复当前干净提交，不拉取新版本。
- `update --recover` 在再次启动前恢复中断的安装或更新。

服务恢复在运行时恢复之后：回滚成功则按旧运行时重启，回滚未完成则继续暂停。服务日志覆盖代码事务开始前和提交后的中断。只恢复服务时返回 `outcome: services_recovered`；`astra doctor` 用 `pending_services` 提示尚需处理。

失败会恢复原提交、本地暂存/未暂存文件和受影响生成目录，不会把旧对话数据库覆盖到新用户数据上。虚拟环境快照只适用于同机原路径，不能当作跨平台备份。

其他进程改了源码、HEAD 或 Windows 文件锁阻止替换时，恢复会停止并保留日志，不强制重置新工作。检查错误、关闭明确的占用者后再重试。

源码安装的控制文件位于 `.astra/launcher/`：

| 文件或目录 | 用途 |
| --- | --- |
| `installation.json` | 安装身份和所选扩展组 |
| `environment.json` | 最近验证的锁文件指纹和运行时版本 |
| `pending.json` | 中断操作的权威日志 |
| `services.json` | 原服务状态及暂停/恢复意图，完成后才清除 |
| `transactions/` | 完成或恢复前保留的生成目录 |
| `receipts/latest.json` | 最近一次应用、已最新或恢复回执 |
| `instances/` | 进程租约与启动时版本 |
| `local-changes/<id>/` | 本地文件选择的原文件、索引快照和清单 |

不要删除 `pending.json` 或 `services.json` 绕过恢复。人工恢复前先保留日志与目录快照；服务定义在维护中变化时，先与记录核对再重试。

成功覆盖后仍保留本地文件备份，输出中给出路径。`files/` 按原相对路径保存文件，`manifest.json` 记录类型、权限和删除状态。恢复前先检查备份及后续编辑。备份中的 `index` 属于旧提交事务恢复，不要复制进已经更新的 Git 工作树。

<a id="data-and-installed-distributions"></a>

## 数据位置与包安装

| 内容 | 源码安装 | 包安装 |
| --- | --- | --- |
| 程序文件 | 实际源码根目录 | 原安装器管理的位置 |
| 私有状态 | `<source>/.astra` | 平台用户数据目录 |
| 会话 | `<source>/.sessions` | `<user data>/sessions` |
| 模型配置 | `<source>/.env` | `<user data>/.env` |

`ASTRA_HOME` 可覆盖私有状态目录；已有数据库、设置和会话的专用覆盖仍优先。`.env` 中可识别的旧 `.astra/...` 路径相对所选数据目录解析。项目规则、文件检查点等工作区内容仍归项目所有，源码 `.env` 仍放在源码旁。

包安装的数据默认位于 Windows `%LOCALAPPDATA%\Astra`、macOS `~/Library/Application Support/Astra`、Linux `$XDG_DATA_HOME/astra`（通常 `~/.local/share/astra`）。即使共享数据配置，各安装也有独立控制身份。

注册命令默认在 Windows `%LOCALAPPDATA%\Astra\bin` 或 POSIX `~/.local/bin`。Setup 只修改用户 PATH，新终端才生效，不覆盖别的安装器拥有的命令。

`astra version` 和 `astra doctor` 区分源码/包归属。包安装需通过原安装器升级，不受 Git、uv 项目同步或 npm 修改。当前 Python wheel 缺少完整 Ink 和原生辅助组件，默认启动会明确报缺失，不静默降级到旧 CLI。

未来完整发行包可在不改变命令名和数据归属的前提下提供资源及版本化激活适配器；当前流程不发布稳定版源，不升级外部模型/MCP 服务，也不提供对话内重启调度。

<a id="command-reference"></a>

## 命令参考

这些命令在终端执行，与对话中的斜杠命令不同。例如 `astra doctor` 查安装健康，`/doctor` 查运行时连接。

| 命令 | 用途 |
| --- | --- |
| `astra` | 在当前项目启动 Ink |
| `astra version --json` | 查看安装、提交、路径和运行实例版本 |
| `astra doctor` | 不依赖模型 Key 或网络的本地安装检查 |
| `astra setup` | 准备或验证源码依赖 |
| `astra setup --install-command` | 准备依赖并注册命令 |
| `astra setup --command-only` | 只注册命令和 PATH |
| `astra setup --extra notebook` | 启用并记住所选扩展，可重复 `--extra` |
| `astra update --check` | 拉取比较上游，不应用 |
| `astra update` | 应用源码更新并验证 |
| `astra update --keep-local` | 原样保留本地文件，更新其他文件 |
| `astra update --overwrite-local` | 备份后使用传入版本 |
| `astra setup --repair` | 按当前源码重建依赖 |
| `astra update --repair` | 修复当前干净提交，不取新版本 |
| `astra update --recover` | 恢复中断的安装/更新 |
| `astra --cli` | 显式启动旧文本 CLI |

`setup`、`update`、`version`、`doctor` 支持 `--json`。不改 PATH 时用 `astra setup`；自管命令目录可用 `astra setup --command-only --bin-dir PATH --no-path`。注册前，在源码根目录用 `.\astra.bat`（Windows）或 `./astra.sh`（POSIX）替换 `astra`。

源码包装脚本支持 `PYTHON` 解释器覆盖，例如 `PYTHON=python3.11 ./astra.sh setup`。`--setup-only` 保留为 setup 别名。Agent 连接配置好的模型端点，不管理其服务器进程。


<a id="troubleshooting"></a>

## 故障排查

| 现象 | 处理方式 |
| --- | --- |
| 找不到 `astra` 或启动旧副本 | 开新终端，用 `where.exe astra` / `command -v astra` 确认路径，再从目标源码执行 `setup --command-only`。 |
| 依赖缺失或未验证 | 关闭使用安装的实例，执行 `astra setup`；损坏时用 `astra setup --repair`。 |
| 进程仍占用安装 | 关闭列出的交互会话；已识别服务自动处理，未知进程由其拥有者停止。 |
| 代码更新但服务未恢复 | 用 `astra update --recover` 重试服务恢复。 |
| 本地文件需要选择 | 在交互终端选保留/覆盖/取消；脚本明确指定策略。 |
| 分支分叉 | 在源码目录检查 `git status --short` 和 `git branch -vv`，解决历史分叉。覆盖文件不会删除本地提交。 |
| 无跟踪上游或 Git 历史 | 配置预期的上游；ZIP 安装改用 Git 克隆。 |
| Windows 提示更新中的 Python 环境被占用 | 在源码目录调用 `.\astra.bat update`。 |
| 安装/更新中断 | 关闭残余占用者，运行 `astra update --recover`，再运行 `astra doctor`；保留事务日志和快照。 |
| 移动目录后环境过时 | 按下一节重建生成依赖并注册新路径。 |

<a id="moving-a-checkout-between-platforms"></a>

## 跨平台移动源码目录

Windows 虚拟环境和原生 Node 模块不能直接复用于 macOS，反之亦然。绝对路径改变也需重建。先关闭使用该安装的会话与服务，只删除生成依赖，再在新位置安装和注册：

```powershell
# Moving to Windows
Remove-Item -Recurse -Force -ErrorAction SilentlyContinue .venv, ui-tui\node_modules
.\astra.bat setup --install-command
```

```bash
# Moving to macOS/Linux
rm -rf .venv ui-tui/node_modules
./astra.sh setup --install-command
```

保留 `.env`、`.astra` 和 `.sessions`，其中存放配置、任务、记忆及对话。注册后开新终端，检查 `.astra/settings.json`、`.astra/filesystem.json`、`.astra/models.yaml`，替换属于旧平台的绝对路径。旧 `.agent_system` 数据可通过 `.\scripts\astra-migrate.bat` 或 `./scripts/astra-migrate.sh` 合并。

<a id="acceptance-boundary"></a>

## 验收边界

自动检查覆盖无站点包的管理命令、各主机命令转发、临时 Git 远端、脏/分叉仓库、文件冲突、更新中断与恢复、数据保留、进程租约及 wheel 导入隔离。维护检查另含 Python/TUI 测试、类型、lint、构建及可选 wheel smoke。

macOS 的通过结果不算原生 Windows 验收；CMD、PowerShell、平台辅助程序及真实模型交互须分别报告。
