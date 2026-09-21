# 开发、测试与维护

[首页](../../README.zh-CN.md) · [文档导航](README.md) · [English](../development.md)

[可复现的开发环境](#reproducible-development-and-maintenance-setup) · [维护入口](#maintenance-entry-points) · [确定性回放评估](#deterministic-replay-evaluations) · [验证](#verify)

以下命令从源码仓库运行。单次测试报告应保存在被 Git 忽略的本地目录。

Pytest 会隔离继承的 Astra 安装、工作区和模型设置，使代理内运行测试与干净 Shell 使用相同的测试配置。
各测试仍可用 `monkeypatch` 设置自己的环境；确需读取外部配置的真机验收可标记 `allow_application_environment`，普通测试使用临时状态。

<a id="reproducible-development-and-maintenance-setup"></a>

## 可复现的开发环境

仓库提交了 `uv.lock` 以及 `ui-core`、`ui-tui`、`ui-gui` 的 npm 锁文件。安装 Python 3.11+、Node.js 18+ 和 [uv（英文文档）](https://docs.astral.sh/uv/) 后，在仓库根目录运行：

```text
uv sync --locked --python 3.11 --extra dev --extra mcp --extra tracing --extra server --extra notebook
npm --prefix ui-core ci
npm --prefix ui-core run build
npm --prefix ui-tui ci
```

`dev` extra 固定 pytest、Ruff 和 Pyright 的版本。需要本地嵌入后端时添加 `--extra embedding`；模型下载和原生辅助程序安装另行处理。已有环境可使用 `uv sync --inexact` 保留额外安装的包。

统一入口 `astra setup` 使用 `uv sync --locked --inexact`，记录启用的 extras 和依赖是否需要更新，执行锁定依赖安装及界面构建；仍会检查依赖冲突。上述手动命令适合开发与发布检查，之后可运行 `astra setup` 记录验证过的环境。

桌面开发使用 Node.js 22.12+，运行 `astra setup --gui --extra dev`。
GUI 验证命令见[桌面开发检查](gui.md#development-checks)。普通发布检查会先构建和测试 `ui-core` 再检查 TUI；GUI 验证单独启用。

Python wheel 包含 Agent、内置模型配置和 Session Recall。Ink 和 Electron 界面、仓库内技能和 macOS 原生辅助程序仍需要源码安装及各自的准备步骤。

<a id="maintenance-entry-points"></a>

## 维护入口

| 操作 | Windows | macOS/Linux |
| --- | --- | --- |
| 启动 Astra | `astra.bat` | `./astra.sh` |
| 迁移旧数据 | `.\scripts\astra-migrate.bat` | `./scripts/astra-migrate.sh` |
| 运行发布检查 | `scripts\phase_t_gate.cmd` | `./scripts/phase_t_gate.sh` |
| 构建沙箱镜像 | `scripts\build-sandbox-image.ps1` | `./scripts/build-sandbox-image.sh` |

在 TUI 中，`/maintenance preview 30` 列出过期的生成文件，`/maintenance apply 30` 删除符合条件的文件并执行被动 SQLite WAL checkpoint，`/maintenance checkpoint` 只对已配置的数据库执行 checkpoint。

嵌套临时文件只在明确配置的生成目录内处理；根层 `.astra/*.tmp` 仍按超过 24 小时的规则处理。会话、记忆、技能、任务和任务检查点均排除在外，即使名称带 `.tmp` 也不删除。不跟随符号链接，计划生成后被改动的文件会跳过。Windows 还核验文件内容，因为创建时间不能反映所有改写。数据库迁移、备份和 checkpoint 的连接在替换/删除文件前关闭。这些操作不会清理对话或活动历史。


<a id="deterministic-replay-evaluations"></a>

## 确定性回放评估

无需启动模型服务即可检查人格与上下文不变量：

```powershell
python -m agent.evals.replay
python -m agent.evals.replay --category context-compression
python -m agent.evals.replay --case legacy-work-session-migrates --json
```

可编辑的 JSONL 样本位于 `evals/persona_invariants.jsonl`。每次运行会在 `.astra/evals/runs/` 下写入回放会话和结构化 `report.json`。可编辑安装还提供等价的 `agent-lab-eval` 入口。

<a id="verify"></a>

## 验证

先安装上述锁定的开发依赖。发布检查包括 Ruff、Pyright、全部 TUI 测试、TUI 类型检查/构建和 Python 测试。Ruff 使用 `pyproject.toml` 中偏重正确性的规则集，格式规则不是发布要求。可选验收项另行启用：

```text
uv run --locked --extra dev --extra mcp --extra tracing --extra server --extra notebook python scripts/phase_t_gate.py --wheel-smoke
```

开发中先运行相关检查，最终云端验收前完成本机 macOS 检查：

```bash
./scripts/phase_t_gate.sh --keep-going --wheel-smoke --native --context-index-performance
```

这会包含 Swift 测试和接近生产数据量的 Context Index 文本检索延迟/覆盖率测试。后者禁用嵌入、隔离向量数据库路径，与普通测试分开运行，并保留原有阈值。

`--provider-smoke` 会显式发送真实模型请求。自动检查通过不能证明桌面动作真实生效、Appshot 捕获权限可用或真实模型服务验收通过，这些仍需有针对性的手动检查。

最终跨平台验收时，在最终 commit 上分别运行 Ubuntu、macOS、Windows 检查。可以使用对应系统的本地机器，也可以在 **Actions → Maintenance → Run workflow** 手动运行一次。每个平台保留 commit、操作系统、命令和结果；没有可用机器的平台应标为未运行。失败时先在本地修复和验证，再重新运行。手动 Maintenance 工作流包含三个平台的普通检查和 wheel smoke，以及 macOS 原生/性能验收。

独立的 [Launcher compatibility 工作流](../../.github/workflows/launcher.yml) 在 push 或 pull request 涉及其列出的启动器文件时自动运行，也支持 **Run workflow**。它在三个平台检查源码发现、原生命令转发、真实 Git 更新、进程排除和恢复。本机 macOS 通过不能替代 Windows CMD、PowerShell 或 Linux 验收，应检查目标 commit 对应任务的最终结果。

私有仓库运行消耗账号的 GitHub Actions 配额。公开仓库使用标准 GitHub 托管 runner 免费，较大型 runner 单独计费，详见 [Actions 计费说明（英文）](https://docs.github.com/en/actions/concepts/billing-and-usage)。私有仓库额度用完时，可以保留本地验收记录，将暂时无法运行的平台检查留待补验；购买额外额度是可选项。如果 GitHub 同时提示付款失败，应先检查账户账单状态。被拦截而未启动的任务不能算测试已执行。

Maintenance 使用 `--keep-going` 在一次运行中收集相互独立的失败；任一项失败，整体仍以失败退出。本地默认遇到首个失败即停止。

macOS/POSIX 原生权限测试需要对应主机能力，Windows 仍会执行可移植的拒绝路径和协议测试。沙箱镜像测试需要 Linux Docker 引擎。Appshot 集成会把 Swift 依赖/构建耗时与有时限的端到端测试分开。

Windows 上的专项命令：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check agent tests scripts
.\.venv\Scripts\python.exe -m pyright agent scripts/phase_t_provider_smoke.py
npm --prefix ui-tui run build
```

macOS/Linux 上的专项命令：

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check agent tests scripts
.venv/bin/python -m pyright agent scripts/phase_t_provider_smoke.py
npm --prefix ui-tui run build
```
