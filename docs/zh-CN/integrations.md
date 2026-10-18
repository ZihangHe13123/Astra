# 可选集成

[首页](../../README.zh-CN.md) · [文档导航](README.md) · [English](../integrations.md)

[OpenAI 兼容 API](#optional-openai-compatible-api) · [原生消息渠道](#native-messaging-channels) · [MCP](#mcp) · [网页搜索](#web-search) · [Conclave 多专家研究](#conclave-multi-expert-research) · [图片工具](#image-tools) · [163 邮件只读集成](#read-only-163-mail)

按需启用服务。Shell 示例从安装目录执行，提供方凭据放在本地 `.env`。

<a id="optional-openai-compatible-api"></a>

## OpenAI 兼容 API

API 与 TUI 分开运行，需要 `server` 扩展：

```text
uv run --locked --extra server python -m agent.cli.api_server
```

默认监听 `127.0.0.1:8900`，通过 `ASTRA_API_HOST` / `ASTRA_API_PORT` 覆盖。环境或安装目录 `.env` 中的 `ASTRA_API_KEY` 为所有端点（含 `/health`、`/v1/models`）启用 `Authorization: Bearer <key>`，与上游模型 Key 分开。

绑定非回环地址必须设置此 Key，否则启动被拒绝。携带浏览器 `Origin` 的请求即使访问回环地址，也要求配置 bearer 认证。

`POST /v1/chat/completions` 支持流式和非流式响应。已消费字段格式错误时，在创建会话或执行 Agent 前返回 HTTP 400。该桥接把 Astra 本地工具开放给认证调用方，远程访问应使用可信网络或 TLS 反向代理。原生消息渠道是另一项独立集成。

<a id="native-messaging-channels"></a>

## 原生消息渠道

Ink 后端在同进程中管理可选消息适配器。消息直接进入现有 ReAct Agent，不经过 AstrBot 或实验性 API 桥接。每个私聊和群聊在 `.sessions/channel_*` 中有独立持久会话；渠道与 TUI 共享生命周期锁，避免有状态 Agent 同时使用两个上下文。

QQ 目前采用原生 OneBot v11 反向 WebSocket。NapCat 仍负责 QQ 协议并直接连接 Astra。启用前复制示例：

Windows：

```powershell
# Windows
New-Item -ItemType Directory -Force .astra | Out-Null
Copy-Item config/channels.example.json .astra/channels.json
```

macOS/Linux：

```bash
# macOS/Linux
mkdir -p .astra
cp config/channels.example.json .astra/channels.json
```

设置 `qq.enabled=true`，默认监听 `127.0.0.1:2280`，NapCat 反向 WebSocket 地址为 `ws://127.0.0.1:2280`。若 NapCat 配有 access token，将相同值放入 `ASTRA_QQ_ACCESS_TOKEN`，不写入 JSON。默认接收私聊，群消息需 @提及或配置的唤醒前缀（默认 `/`）。

2280 端口只能有一个服务占用，启用前先停用或改配 AstrBot。适配器启动失败会报告但不关闭 TUI；后端退出时关闭全部适配器。消息渠道中需要交互授权的操作会被拒绝，不等待不可见提示。

QQ 图片经公开 URL 检查与大小限制后下载，再作为原生多模态内容传给视觉模型。发本地文件需启用 `send_files_enabled`、用 `file_allow_users`（或 `allow_users`）列出可信 QQ ID，并把 `send_file_roots` 限定为可分享的输出目录。`channel_send_file` 拒绝越界路径、隐藏文件和目录、应用数据和常见凭据文件。

### 微信

微信走腾讯官方的 iLink Bot API：用微信扫一次二维码登录，之后通过 HTTPS 长轮询收消息。不操作微信桌面客户端，也不需要屏幕，所以息屏、锁屏时照样回复，只要电脑不睡眠。

运行 `/weixin login`（或桌面端 设置 → 微信 → 扫码登录），用微信扫码。终端按深色背景画出二维码，桌面端显示为图片。显示二维码需要 `segno`（`pip install 'segno>=1.6,<2'`，或安装 `weixin` 可选依赖，它同时带上读取图片要用的 `cryptography`）。登录成功会在 `.astra/channels.json` 里设置 `weixin.enabled`，之后用 `/weixin status`、`/weixin off`、`/weixin on`、`/weixin logout` 管理。

- **回复谁**：只回复扫码登录的那个微信号；`weixin.allow_users` 可以再列出其他人。群消息忽略。
- **只有一个在收**：每个 Astra 后端都可以运行这个渠道，但同一时间只有一个在长轮询，其他的待命，它退出后自动接手。
- **睡眠**：正在收消息的后端会阻止电脑空闲睡眠：macOS 用 `caffeinate -i`（后端退出即释放），Windows 设置执行状态，Linux 用 `systemd-inhibit`。显示器照常关闭。MacBook 合盖仍会睡眠，除非接着电源和外接显示器。不想管睡眠就把 `weixin.keep_awake` 设为 `false`。
- **能收到什么**：文字、引用、微信对语音的转写，以及最多 `max_images` 张图片（从微信 CDN 下载后解密）。文件和视频只收到一个占位说明。
- **怎么回**：微信会话会被告知按聊天的方式说话：简短、口语、不用 Markdown、一条说一件事。回复像人发微信那样拆成几条短消息依次发出（最多 `max_bubbles` 条；`chat_bubbles: false` 则整段发送，只在超过 `max_output_chars` 时分段），每条之间稍作停顿；从收到消息到最后一条回复发出，微信里一直显示"对方正在输入"。
- **发文件**：你要的时候，Astra 用 `channel_send_file` 发本地文件：图片和视频按图片、视频发，其他按文件发。只发给扫码的人（或 `weixin.file_allow_users` 列出的人），只从 `weixin.send_file_roots`（默认是用户主目录）里取，不超过 `max_send_file_bytes`；隐藏文件和目录（包括 Astra 自己的 `.astra`）、应用数据（`Library`、`AppData`）和凭据文件一律拒绝。`send_files_enabled: false` 可以关掉。
- **语音**：你让 Astra 说话时，它用 `channel_send_file` 的 `speak` 代替 `path`：用朗读功能配置的声音（见[语音](voice.md)）念出来，作为音频文件发送，手机上点一下就能听（macOS 上是 M4A，其他平台是 WAV），可以再配几句短文字。iLink 收下 bot 的原生语音消息却不会投递，所以微信里显示的是文件，不是语音条。
- **隐私**：bot token 存在状态目录的 `channels/weixin/account.json`，只有本人可读，不出现在事件和日志里。另一个程序（比如 Hermes）若用同一个登录长轮询，会抢消息，请分开登录。

协议细节参考 Hermes Agent 的适配器（MIT 许可，Nous Research）。

<a id="mcp"></a>

## MCP

安装 `mcp` 扩展，并创建 `.astra/mcp.json`：

```json
{
  "servers": {
    "filesystem": {
      "transport": "stdio",
      "command": "python",
      "args": ["path/to/server.py"]
    }
  }
}
```

工具名为 `mcp__<server>__<tool>`；超过 64 个字符或会与别的工具重名时，改为以一段短哈希结尾。名字为空或参数 schema 不是对象的工具会被单独略过，服务器的其余工具照常可用。`/mcp` 查看服务并列出每个被改名或略过的工具及原因，`/doctor` 排查连接错误。支持 stdio 和 streamable HTTP，可分别停用服务；`${ENV_NAME}` 从环境解析凭据，避免将密钥存入 JSON。见[完整配置示例](../../config/mcp.example.json)。

<a id="web-search"></a>


MCP 派发后的失败会区分读取与动作：可能有副作用的调用在超时或应答丢失时返回 `mcp_unknown_outcome`，不得自动重放。重连后先观察目标、取得新引用，再决定下一步。配置 `tool_risks` / `risk` 为 `read`，或 network 工具声明 `readOnlyHint=true`，才提供读取恢复提示；没有声明时保守处理。这些提示不会降低原有审批要求，旧连接的工具定义不能在替换后的连接上派发。

## 网页搜索

`search_web` 支持 `provider=auto|exa|searxng|deepseek`。`/search` 查看默认值，`/search auto|exa|searxng|deepseek` 持久切换。auto 对新闻、研究、模型发布和基准问题优先 Exa，普通问题先用 SearXNG；翻页（page 大于 1）、指定 `engine`、science/news 以外的类别只有 SearXNG 支持，auto 会把这类调用交给 SearXNG，由 Exa 返回的结果会注明未生效的参数。SearXNG 每个站点最多列 2 条，查询含 `site:` 时不限。结果为空或失败时，同一次工具调用内相互回退。不设固定每轮搜索次数，但重复调用和 ReAct 上限仍防止无限循环。不使用 DuckDuckGo。

直接配置 `EXA_API_KEY`，或用 `EXA_ENV_FILE` 指向另一份包含该 Key 的 dotenv，以复用凭据而不复制。`/doctor` 只报告是否配置和来源，不打印密钥。

DeepSeek Search 与 Fetch API 是可选项，不会被自动选择：`auto` 既不用 DeepSeek 搜索也不用它提取。用 `provider=deepseek`、`/search deepseek` 或 `WEB_SEARCH_PROVIDER=deepseek` 选择它搜索；它从环境变量或 `.env` 读取 `DEEPSEEK_API_KEY`（只粘贴在 `/connect` 里的 Key 不会被使用），只发送查询和结果数（`max_results`，1–50）。查询最长 128 个字符，支持 DeepSeek 的运算符：`site:`（多个用 `OR` 连接）、`-site:`、`-词`、`"精确短语"`、`-"短语"`、`lang:zh,en`，以及带年份或日期的 `after:`/`before:`。翻页、引擎、类别、语言和内联正文在这条路线上不生效，结果会注明。每条结果给出 DeepSeek 面向模型阅读的 summary（没有时用简短 snippet），有日期和站点名时一并列出。Astra 不发送可选的 `ori_question`（用户原问题）和 `localization`（IP 地址），查询以外的文本不会到达该服务；以后可作为显式选项再加。请求发往 `DEEPSEEK_TOOLS_URL`（默认 `https://api.deepseek.com/v1/tools`；搜索超时 `DEEPSEEK_SEARCH_TIMEOUT`，15 秒），不跟随重定向；HTTP 408、429、5xx 稍等后重试一次，超时只报告不重发。Key 被拒（401）、余额不足（402）和限流（429；每账号每秒 5 次，Fetch 还按目标域名限制）会在失败信息中写明。DeepSeek 自 2026-11-01（北京时间）起对两个 API 各按每 1,000 次成功请求 5 元计费。`/doctor` 和 `search_status` 只报告是否配置了 Key，不调用该服务。

`web_extract` 有独立回退顺序。`WEB_EXTRACT_PROVIDER=auto` 依次尝试已配置的 `Tavily -> Exa -> Parallel`，再到本地/云 Firecrawl，最后 HTTP 与正文 HTML-to-Markdown 提取。逐 URL 回退，成功页面不重复获取。单次工具 `provider` 或 `WEB_EXTRACT_PROVIDER=tavily|exa|parallel|firecrawl|http` 可明确首选。DeepSeek Fetch 只在同样明确指定（`provider=deepseek` 或 `WEB_EXTRACT_PROVIDER=deepseek`）时使用：先试它，读不到的页面再按常规顺序回退。它返回 Markdown 但不报告最终 URL，结果保留请求的 URL。超过 15,000 字符时返回 75/25 首尾窗口，完整 Markdown 保存到 `.astra/cache/web`。

浏览器提取器优先使用 `BROWSER_EXTRACT_ARGV` JSON 数组，将可执行文件和固定参数分开，例如 `["C:\\Program Files\\Browser Extract\\extract.exe","--render"]`。兼容 Shell 字符串时 `BROWSER_EXTRACT_CMD` 优先于 `WSL_EXTRACT_CMD`，旧变量在所有平台仍作为回退。

Windows 通过 `cmd.exe`，POSIX 通过 Bash 执行旧字符串。Windows 给每个子进程独立 URL 环境值，使用带引号的 `%ASTRA_BROWSER_EXTRACT_URL%` 且关闭延迟展开；POSIX 将 URL 作为独立位置参数。Windows 旧模式拒绝 URL 中原始双引号、CR、LF、NUL，应百分号编码或使用 argv。`$HOME` 等 Shell 展开仍可用，但不把 URL 文字拼进 Shell 程序。

`BROWSER_EXTRACT_STATUS_CMD` 可配置独立就绪检查；未设时只查可执行文件或包装器，不伪造 URL 或 status 参数。没有自定义设置时，仅 Windows Python（`sys.platform=win32`）走内置 WSL 提取；macOS 和所有 Linux Python（含 WSL 中 Python）走原生 Bash。macOS 能发现标准 Chrome、Edge 和 Chromium 安装位置。

把 Windows 源码拷到 macOS/Linux 后，应取消 `WSL_EXTRACT_CMD`，或改成原生有效的 argv/命令。旧变量在每个平台都会读取，残留以 `wsl` 开头的值会被错误尝试。

代理动态读取 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY`。`ASTRA_PROXY_MODE=auto` 只在端点可达时使用代理，重启代理无需重启 Astra；启动器默认选 `off`，需明确选 `auto` 才探测，`always` 跳过探测。私有/本地 URL 及 `NO_PROXY` 主机始终直连，可同时使用本地 SearXNG/Firecrawl 与需代理的 API。

<a id="conclave-multi-expert-research"></a>

## Conclave 多专家研究

Conclave 编排 12 类领域专家的并行 SearXNG 搜索，再综合结构化报告。可直接提问：

```text
/conclave Compare current options for running LLMs locally
```

配置命令支持终端补全：

```text
/conclave config
/conclave config chairperson <profile>
/conclave config expert_list
/conclave config sources 5
```

`chairperson` 选择综合模型，`active` 使用当前模型；`sources` 限制每位专家的来源数量。专家菜单支持多选：Tab 追加，Enter 确认，使用菜单提供的名称。配置自动保存到 `~/.config/hermes/conclave.json`。

<a id="image-tools"></a>

## 图片工具

公开版提供 `read_image`、`read_image_tiles` 和 `inspect_image_metadata`。
读图、查看局部图块和读取 PNG 生成元数据都不依赖生图服务。

公开版已移除内置 ComfyUI 生图工具及其生命周期管理；原有生图环境配置不会再注册工具或启动服务。
如需生图，可通过 [MCP](#mcp) 配置外部服务，个人工作流和模型配置应保存在仓库之外。
Windows 图片路径和 Shell 命令统一使用 `AGENT_WSL_DISTRO` 选择 WSL 发行版。

<a id="read-only-163-mail"></a>

## 163 邮件只读集成

可选邮件集成增量缓存邮件，用于本地检索。在安装目录 `.env` 填写账号和客户端授权码（不是网页登录密码）：

```dotenv
ASTRA_163_EMAIL=your-account@163.com
ASTRA_163_AUTH_CODE=your-client-authorization-code
# ASTRA_163_MAX_BODY_BYTES=5242880
```

不带子命令时，`checkmail` 刷新默认 `INBOX` 和 `已发送`，然后显示收件箱最近 30 封。还支持明确同步、缓存搜索、稳定 UID 读取、文件夹列表和状态。对 `recent`、`search`、`read`、`folders` 加 `--offline` 可只读已有缓存；刷新失败且缓存存在时返回明确标注的缓存结果，离线访问缺失缓存会失败且不新建数据库。

Windows：

```powershell
.\scripts\checkmail.bat
.\scripts\checkmail.bat sync --json
.\scripts\checkmail.bat search "invoice" --from billing@example.com --window 200 --json
.\scripts\checkmail.bat read 352 --folder INBOX --json
.\scripts\checkmail.bat recent --offline --recent 10 --json
```

安全的本地附件写入目前只支持 POSIX；Windows 明确下载附件会在联系服务器获取附件前返回 `unsupported_platform` 和退出码 `2`。同步、搜索、读取、文件夹、状态和离线缓存功能仍可用。

macOS/Linux：

```bash
./scripts/checkmail.sh
./scripts/checkmail.sh sync --json
./scripts/checkmail.sh search "invoice" --from billing@example.com --window 200 --json
./scripts/checkmail.sh read 352 --folder INBOX --json
./scripts/checkmail.sh recent --offline --recent 10 --json
./scripts/checkmail.sh attachment --folder INBOX --uidvalidity 77 --uid 352 --part 2 --json
```

同步把头部和解码文字存入 `.astra/mail/163.sqlite3`，不下载附件载荷；只有明确的 `attachment` 命令把选定附件写入 `.astra/mail/attachments/`。邮箱以只读方式选择，不标记已读，不移动、删除、回复或发送邮件。

源码安装附有[163 邮件技能](../../.astra/skills/operations/163-email-sync/SKILL.md)，通过项目信任检查发现，Setup 不负责安装或更新该技能。凭据、缓存和其他私有 `.astra` 状态保持本地且被 Git 忽略。
