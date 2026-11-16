# 启动与跨平台环境

只在首次启动、依赖或连接出错时读取。CLI 固定为已经验收的 `@playwright/cli@0.1.24`；更新版本要同时检查命令帮助并重新跑冒烟验证，不使用浮动 `latest`。

## 定位启动器

启动器是此技能目录下的 `scripts/playwright_cli.py`。`skill_view` 返回内容，不附加绝对路径；不要把目标项目的工作目录当成技能目录。标准 Astra 启动器会传入技能根路径，可在 `execute_python` 中运行下面的只读定位代码，只输出需要的两个路径：

```python
import json, os, sys
from pathlib import Path
configured = os.environ.get("AGENT_SKILLS_PATH", "").strip()
home = os.environ.get("ASTRA_HOME", "").strip()
if not configured and not home:
    raise RuntimeError("未提供技能根路径；先确认本次 Astra 安装的 skills 目录")
root = Path(configured).expanduser() if configured else Path(home).expanduser() / "skills"
if not root.is_absolute():
    raise RuntimeError("技能根路径仍为相对路径；先确认 Astra 实际加载目录的绝对路径，不按测试项目目录猜测")
script = (root / "development" / "web-product-debug" / "scripts" / "playwright_cli.py").resolve()
if not script.is_file():
    raise FileNotFoundError(f"此技能资源未安装在当前技能根目录：{script}")
print(json.dumps({"python": sys.executable, "launcher": str(script)}, ensure_ascii=False))
```

自定义根目录使用绝对路径 `AGENT_SKILLS_PATH`，源码安装通常是安装目录的 `.astra/skills`；标准启动器会解析旧的 `.astra/...` 配置。其他相对路径配置需要先确认实际加载位置。若只复制了 SKILL.md，需补全此技能整个目录。不要打印其他环境变量、猜测其他用户路径或扫描个人浏览器数据。

选择当前环境可运行的 Python 3.11+。Astra 虚拟环境的 Python 可直接使用；Windows 常见命令为 `python` 或 `py -3`，macOS/Linux 常见为 `python3`。命令名只是候选，先确认实际可用路径。Node.js 使用受支持的 LTS 版本并带 npm/npx。

```text
<python> <launcher绝对路径> --workspace <项目绝对路径> --session <任务名> --check
```

检查不启动浏览器，也不下载依赖。Windows 启动器使用 Node 执行 npm 的 npx JavaScript 入口，避免 `.cmd` 对空格、引号和特殊字符二次解释；macOS/Linux 使用 npx 可执行文件。缺失时按实际报错修复 Node/npm 安装，不把“命令缺失”报告成页面故障。

## 一次任务，一个会话

session 使用简短的英文字母、数字、下划线、连字符名称，并带任务唯一后缀，例如 `ui-review-a83f`。同一任务的每条命令保持相同 workspace 和 session；多任务不共用 session。

以下用 `PW` 表示完整前缀 `<python> <launcher> --workspace <项目> --session <名称> --`，它不是已安装的命令。真正执行时替换为该前缀，或使用当前 shell 正确引用的参数数组。

```text
PW --version
PW open http://127.0.0.1:实际端口/
PW snapshot
PW close
```

先检查项目实际监听地址，不猜端口。启动器默认允许 npx 获取固定版本 CLI；无网络任务或已有缓存时在分隔符前加 `--offline`。缓存缺失会明确失败，不会自动退出离线模式。首次下载 CLI 不等于浏览器已安装。

跨平台调用优先在 `execute_python` 中用参数数组，替换为刚确认的实际路径和参数，例如：

```python
import subprocess
result = subprocess.run([
    "<python绝对路径>", "<launcher绝对路径>",
    "--workspace", "<目标项目绝对路径>", "--session", "ui-review-a83f",
    "--", "snapshot",
], shell=False)
print("Playwright exit code:", result.returncode)
```

检查子进程退出码和原始输出，不能只看 execute_python 自身是否执行成功。Astra 的 Windows `execute_shell` 通常是 cmd，不要直接套用 PowerShell 的 `&` / `$env:`；在明确使用 PowerShell 时才用相应语法。shell 模式下正确引用完整路径和每个参数，尤其是引号、换行、`$`、反引号等。启动器内部不会修复调用它之前已经发生的 shell 展开。

## 浏览器与显示

- Windows/macOS/Linux 可使用本机受支持的 Chrome；Windows 上已有 Edge 时可传 `open <URL> --browser=msedge`。独立会话不要求安装 Astra Browser Control 扩展。
- 缺少浏览器时先看 `PW install-browser --help`，只安装任务需要且环境允许的浏览器。启动器不会替用户静默安装浏览器或操作系统依赖。
- Linux 没有桌面显示服务时用默认 headless。缺少共享库、权限或浏览器二进制时报告具体缺项；需要管理员权限的系统安装由用户处理。
- `--headed` 需要有效图形会话；macOS 的 WebKit 测试不等于控制用户的 Safari。不要把 Windows 的通过结果描述成 macOS/Linux 实机通过。
- 自定义配置使用 `PW open <URL> --config=<配置绝对路径>`，需要时查当前版本帮助。启动器不自动读取目标项目根目录的 Playwright 配置，也不复制已有个人浏览器 profile。

## 产物与恢复

所有命令在 `<workspace>/output/playwright/<session>/` 执行，截图、日志、trace 默认放在其中的 `.playwright-cli/`。CLI 返回相对文件路径时，以这个目录解析，再交给 `read_file` / `read_image`。上传、配置和脚本输入也相对于此目录，所以本地输入统一使用绝对路径。

连接失败先用同一 session 的只读状态/快照确认，不接管别的浏览器会话；关闭自己的会话使用 `close`，不要用 `close-all` 或 `kill-all`。中断正在执行的写操作不证明它没有生效。恢复后重新观察，只有确认结果和任务仍需要时再继续。

若启用了 Windows Job containment，工具返回时可能回收它创建的浏览器守护进程；一次 open 成功不保证下次调用仍能连接。先用独立的 snapshot 调用验证会话延续，失败时报告当前执行环境不支持此持久会话方式，不自行关闭进程隔离设置或重复提交。

参考：[Playwright CLI 配置](https://playwright.dev/agent-cli/configuration)、[浏览器依赖](https://playwright.dev/docs/browsers)。具体选项以固定版本的 `--help` 为准。
