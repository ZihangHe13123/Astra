# 工具执行与文件访问

[首页](../../README.zh-CN.md) · [文档导航](README.md) · [English](../execution.md)

[Minimal 模式的 Bash 环境](#minimal-bash-environment) · [主机文件访问](#host-filesystem-access) · [工具策略与追踪](#tool-policy-and-tracing)

配置工具运行位置、文件访问范围及审批方式。

## Team 状态与待命成员

同一 session 可以直接用 `team(action="status", team_id=...)` 查看旧回合的 Team，无需先 resume。
发送任务或配置 Team 时，会复用原有的所有权和工作区检查，自动接管已结束的旧回合；仍在运行的父任务不能被抢占，查看状态不会转移控制权。

状态默认精简返回；`team` 和 `team_wait` 可加 `detail=true` 展开完整初始上下文、任务文字和可用的 episode 历史。
创建时指定 `keep_alive_limit=5`，或调用 `team(action="configure", team_id=..., keep_alive_limit=5)`，即可即时、持久地设置该 Team 的待机容量。
未设置时继承 `ASTRA_TEAM_KEEP_ALIVE_LIMIT`（默认 2）；零表示不再接纳待机成员，降低容量不会强退已经待机的成员。

成员的 `lifecycle` 记录退出原因、实际限额、最近待机起点与截止值，以及单调时钟的活跃/待机时长和实际经过的时间。
`observed_at` 标识诊断快照时间，运行和待机期间在状态切换时保存。主机睡眠可能让实际时间继续经过，而单调时钟暂停。
异常恢复后的 `recovered_at` 是发现时间，计时保留最后一次持久记录，不推测实际死亡时间。清理进程日志不会删除这些诊断。
`team_restart` 清空旧轮的运行决定，并从有限检查点建立新对话，不等于恢复原模型的完整会话。

## Shell 结果与完整输出

主机 `execute_shell`（包括 WSL）使用开启 `pipefail` 的 Bash，`pytest | tail -n 30` 仍会保留测试失败的管道退出码。
需要末条命令决定管道状态时，可显式执行 `set +o pipefail`。不启用 `errexit`；Docker 和 Minimal 持久 Bash 的 Shell 选项保持各自配置。

进程回执提供 `output_reader`，其中含 `process_read`（子代理为 `delegate_read`）及可直接调用的参数。
前台输出被截断时也保留进程读取句柄，无需为了读日志扩大文件工具的允许目录。

<a id="minimal-bash-environment"></a>

## Minimal 模式的 Bash 环境

Minimal Mode 的 `bash` 工具保留一个持久交互式 Shell，工作目录和导出的变量会跨调用保留。Windows 使用 WSL Bash，macOS/Linux 使用原生 Bash，并报告中性的 `posix` 环境。

每条命令默认超时 300 秒。跨平台配置 `ASTRA_PERSISTENT_BASH_TIMEOUT` 优先；只有未设置它时才读取旧 `ASTRA_WSL_PERSISTENT_TIMEOUT`。后者虽然名称含 WSL，仍兼容 WSL 和原生主机。

<a id="host-filesystem-access"></a>

## 主机文件访问

Python 和 Shell 默认使用 Docker 隔离。`/sandbox` 查看当前后端，`/sandbox off` 切到受保护的主机执行（如运行 `wsl.exe`），`/sandbox on` 切回 Docker。设置立即生效并保存到 `.astra/settings.json`。主机执行仍有超时、输出限额和危险命令检查。

`read_file`、`search_files`、`write_file`、`edit_file` 使用单独的主机文件策略，可访问明确允许的 Windows 盘符和 WSL UNC 路径，无需将这些路径暴露给任意沙箱命令。

当前工作区始终可读写。将 `config/filesystem.example.json` 复制为 `.astra/filesystem.json`，把 `shared-files` 换成实际要开放的主机路径。额外根目录设为 `ro` 或 `rw`，外部目录通常保持只读。`AGENT_FILESYSTEM_CONFIG` 可指定另一份策略文件。

默认 Docker 镜像为精简的 `python:3.12-slim`。需要项目依赖或 `pytest` 的 Minimal `run_code` 检查可构建并启用开发镜像。

Windows：

```powershell
.\scripts\build-sandbox-image.ps1
$env:ASTRA_DOCKER_IMAGE = "astra-sandbox:agent-system-dev"
```

macOS/Linux：

```bash
./scripts/build-sandbox-image.sh
export ASTRA_DOCKER_IMAGE=astra-sandbox:agent-system-dev
```

开发镜像包含 `pyproject.toml` 的基础依赖、`mcp`、`server`、`tracing`、`dev` 扩展，以及 Git、ripgrep 和 curl。不提供 Docker CLI、Node.js/npm 命令、浏览器或 GUI；Pyright 单独使用 Python 包提供的私有 Node 运行时，TUI 检查仍在主机执行。包含 curl 不代表开放网络，容器默认仍使用 `--network none`。

根目录 `.dockerignore` 仅让沙箱 Dockerfile 和依赖文件进入构建上下文，配置、状态、环境和无关源码不会传给 Docker。镜像首次构建需要联网；运行容器时，除非明确配置，否则仍断网。源码挂载在 `/workspace`，检查使用当前工作树，不是烘焙进镜像的旧副本。镜像设置也可写入 `.env`。

Windows 文件根目录示例：

```json
{
  "roots": [
    {"path": "D:\\shared", "mode": "ro"},
    {"path": "\\\\wsl.localhost\\Ubuntu\\home\\user", "mode": "ro"}
  ]
}
```

macOS 示例：

```json
{
  "roots": [
    {"path": "/Users/your-name/shared-files", "mode": "ro"}
  ]
}
```

`.astra/channels.json` 的 `send_file_roots` 遵循同样原则，例如 Windows 的 `D:\\allowed\\outputs` 或 macOS 的 `/Users/your-name/allowed/outputs`。仓库示例使用可移植的相对目录 `outputs`。

配置范围之外的路径会被拒绝，文件工具不会写入或编辑只读根目录。

<a id="tool-policy-and-tracing"></a>

## 工具策略与追踪

`AGENT_TOOL_POLICY` 支持 `permissive`、`safe` 和 `locked`。在 safe/locked 模式下，可用 `/permissions allow <tool-name>` 为当前进程批准被拒绝的工具。

- `TOOL_MAX_INLINE_CHARS=12000` 限制持久历史中的首尾预览，完整结果保存到 `TOOL_RESULT_DIR`。新结果在紧接着的一次模型迭代中最多完整展示 `TOOL_MAX_FRESH_RESULT_CHARS=100000` 字符，之后回到预览。base64/data URL 等不透明载荷不会作为大段文字内联。
- `TOOL_FAILURE_THRESHOLD=3` 在等价错误连续出现后打开单工具熔断，并禁用工具做最后一次综合回答。
- `PROMPT_CACHE_STABLE_TOOLS=1` 在工作会话中保持一份稳定的模型工具清单；预算和熔断仍在执行阶段检查，不在 ReAct 迭代间删除 schema。
- 设为 `PROMPT_CACHE_STABLE_TOOLS=0` 后，可用 `TOOL_PROGRESSIVE_EXPOSURE=1` 恢复旧核心/相关工具组及 `activate_tool_group`，以缩小首次未缓存提示，但会增加前缀缓存失效。
- `AGENT_TOOL_EXPOSE` 和 `AGENT_TOOL_HIDE` 接受逗号分隔的工具名。前者把默认不开放的工具加入清单：`conclave`（不开放也能用 `/conclave` 命令）、`notebook_execute`、`skills_list`（提示中的 `<available-skills>` 已列出技能）、`project_verifier_init`、`git_pull`、`git_reset` 和 `git_revert`；后者从清单中去掉默认工具。同一工具同时出现在两者中时保持隐藏。明确列出这些工具的模式和工作流仍可使用它们。
- `AGENT_MAX_REACT_ITERATIONS=50` 为整轮设置粗粒度上限；`0` 表示无固定次数上限，重复调用、熔断、提示预算和取消检查仍有效。

`AGENT_TRACE_ENABLED=1` 启用 OpenTelemetry spans。安装 SDK 和 OTLP exporter 后，`OTEL_EXPORTER_OTLP_ENDPOINT` 指向 Phoenix 或其他兼容收集器。
