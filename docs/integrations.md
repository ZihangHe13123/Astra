# Optional integrations

[Home](../README.md) · [Documentation](README.md) · [简体中文](zh-CN/integrations.md)

Enable only the services you need. Run shell examples from the installation checkout. Provider credentials belong in your local `.env`.

[Optional OpenAI-compatible API](#optional-openai-compatible-api) · [Native messaging channels](#native-messaging-channels) · [MCP](#mcp) · [Web search](#web-search) · [Conclave multi-expert research](#conclave-multi-expert-research) · [Image tools](#image-tools) · [Read-only 163 mail](#read-only-163-mail) · [Calendar and Reminders (macOS)](#calendar-and-reminders-macos)

## Optional OpenAI-compatible API

The API runs separately from the TUI and requires the `server` extra:

```text
uv run --locked --extra server python -m agent.cli.api_server
```

It defaults to `127.0.0.1:8900`. `ASTRA_API_HOST` and `ASTRA_API_PORT` override
the binding. Set `ASTRA_API_KEY` in the environment or repository `.env` to
require `Authorization: Bearer <key>` on **every endpoint**, including `/health`
and `/v1/models`. The API key is separate from the upstream model API key.
Binding to a non-loopback address requires this key; startup rejects an
unauthenticated public binding. Requests carrying a browser `Origin` header
also require configured bearer authentication, even on loopback.

`POST /v1/chat/completions` supports streaming and non-streaming responses.
Malformed consumed fields return HTTP 400 before session or Agent work. This
bridge exposes Astra's local tools to authenticated callers; use a trusted
network or a TLS reverse proxy for remote access. Native messaging channels
remain a separate integration.

The endpoint serves the configured model (`astra` is an alias); unknown model
names return 400. Stateless requests use submitted system/developer and
user/assistant text history, plus user `image_url` parts (including image-only
messages). Client tool-call/result history and unknown content parts return
400. With Astra's `session_id` extension, send only one new user message; full
client history and stored session history cannot be combined implicitly.
Responses identify the actual model and aggregate available provider usage;
unknown usage is `null`. Streaming clients can request
`stream_options: {"include_usage": true}` for the final usage chunk.

## Native messaging channels

The Ink backend owns optional messaging adapters in the same process. Incoming
messages go directly to the existing ReAct agent; AstrBot and the experimental
OpenAI-compatible API bridge are not involved. Each private chat and group gets
an isolated persistent session under `.sessions/channel_*`, and channel turns
share one lifecycle lock with the TUI so a stateful agent is never used
concurrently with two different contexts.

QQ currently uses a native OneBot v11 reverse-WebSocket adapter. NapCat remains
the QQ protocol client and connects directly to Astra. Copy the example before
enabling it:

```powershell
# Windows
New-Item -ItemType Directory -Force .astra | Out-Null
Copy-Item config/channels.example.json .astra/channels.json
```

```bash
# macOS/Linux
mkdir -p .astra
cp config/channels.example.json .astra/channels.json
```

Then set `qq.enabled` to `true`. The safe default listens on
`127.0.0.1:2280`; configure NapCat's reverse WebSocket URL as
`ws://127.0.0.1:2280`. If an access token is configured in NapCat, put the same
value in `ASTRA_QQ_ACCESS_TOKEN`—the token itself is never stored in the JSON
file. Private messages are accepted by default. Group messages require either
an @mention or a configured wake prefix (default `/`) to avoid unsolicited
replies.

Configure `qq.allow_users` with the QQ IDs permitted to chat; an empty list no
longer accepts everyone. These contacts receive public web/chat tools only,
without local files, history, memory, project instructions or delegation.
For private remote control, explicitly list trusted owner IDs in
`qq.owner_users`; owners need not also appear in allow_users. Group messages
always use chat capabilities, including those sent by an owner. Revoking owner
access starts a separate restricted conversation; old owner history remains on
disk and is never loaded into that chat.

Only one service may own port 2280. Stop or reconfigure AstrBot before enabling
the native QQ adapter. Adapter startup failure is reported without taking down
the TUI, and all adapters are closed when the Astra backend exits. Tool
operations that would normally require an interactive approval are denied on
messaging channels rather than waiting forever for a hidden prompt.

QQ image segments are downloaded with public-URL checks and bounded size, then
passed to vision-capable models as native multimodal content. Local file sending
is deliberately more restrictive: enable `send_files_enabled`, list trusted
sender QQ IDs in `file_allow_users` (or `allow_users`), and restrict
`send_file_roots` to output directories that are safe to share. The
`channel_send_file` tool rejects paths outside those roots, hidden files and
folders, application data and common credential files even when requested by
the model.

### WeChat

WeChat uses Tencent's iLink Bot API: Astra logs in once with a QR code scanned
in WeChat, then long-polls for messages over HTTPS. It does not drive the WeChat
desktop client and needs no screen, so it keeps answering with the display off
or the screen locked, as long as the computer does not sleep.

Run `/weixin login` (or Settings → WeChat → Scan to log in) and scan the code
with WeChat. The terminal draws the code for a dark background; the desktop
shows it as an image. The QR code needs the `segno` package
(`pip install 'segno>=1.6,<2'`, or the `weixin` extra, which also brings the
`cryptography` used to read images). A successful login sets `weixin.enabled`
in `.astra/channels.json`. `/weixin status`, `/weixin off`, `/weixin on`,
`/weixin logout` and `/weixin model` manage it afterwards.

- **Who is answered.** Only the WeChat user who scanned the code, unless
  `weixin.allow_users` lists others. Group messages are ignored.
- **One poller, the desktop first.** Every Astra backend may run the channel,
  but only one polls a login at a time; the others wait and take over when it
  exits. While the desktop is open, its channel host has precedence: a
  backend that polls (an old terminal session, say) answers what it already
  received, then hands the channel over within a few seconds and waits until
  the desktop closes. The long poll it gives up moves no cursor, so no message
  is lost.
- **Sleep.** While a backend polls, it keeps the computer from idle sleep:
  `caffeinate -i` on macOS (released when the backend exits), an execution
  state on Windows, `systemd-inhibit` on Linux. The display still turns off.
  A closed MacBook lid still sleeps unless an external display and power are
  connected. Set `weixin.keep_awake` to `false` to leave sleep alone.
- **What arrives.** Text, quoted messages, WeChat's own transcript of a voice
  message, and up to `max_images` images (decrypted from WeChat's CDN). Files and
  videos arrive as a placeholder.
- **How it answers.** WeChat conversations are told to talk like a chat: short,
  casual, no Markdown, one thought per message. A reply goes out as several
  short messages the way people text (at most `max_bubbles`; `chat_bubbles:
  false` sends whole replies split only at `max_output_chars`), with a pause
  before each, and WeChat shows "typing…" from the message until the last reply.
  A persona may have a WeChat version, written for chat (tone rather than
  described actions); WeChat conversations then use it instead of the persona
  itself. It is not offered as a persona of its own. A conversation that was
  on another persona switches when it is next opened, which rebuilds its
  system prompt once.
- **A new conversation.** Each chat keeps one conversation, across restarts.
  Send `/new` (or `/reset`) on its own to start a new one: the next message
  begins with an empty history, while the old conversation stays saved and
  remains reachable through memory and session recall. The command is not
  passed to the model. It works the same on QQ.
- **Which model.** WeChat follows the default model: the one last chosen with
  `/model` in any window, read for each message. `/weixin model <key>` (a key
  from `/model`, such as `deepseek::deepseek-flash`, or Settings → WeChat →
  model) gives WeChat a model of its own without changing any window's;
  `/weixin model default` follows the default again, and `/weixin model` shows
  the current choice. The setting is `weixin.model`; it applies from the next
  message, and a model that is no longer in the catalog or has no key falls
  back to the default.
- **Approvals.** A step that needs approval (a command on this computer, a
  file outside the workspace) is sent to the chat with what it will do,
  credentials hidden, and waits for a reply: 同意 runs it once, 本任务 allows
  it for the rest of this request, 本会话 for this chat, 拒绝 does not run it
  (the prompt lists only what that request offers; a send shows its text and
  takes 发送 or 丢弃). The chat's grants are its own: what the desktop allowed
  never applies here. Only the person whose message started the turn can
  answer, only while a request waits, and only with a message that is nothing
  but one of those short answers (同意, 可以, 好 / 拒绝, 不行, 取消 and so on);
  anything else is an ordinary message. No answer within five minutes counts as 拒绝, and the
  chat is told the step did not run. An open desktop window showing the same
  request can answer first. Other channels get the same five-minute limit
  without the chat prompt, so a request nobody can see never holds the
  channel.
- **Files.** When asked, Astra sends a local file with `channel_send_file`: an
  image or a video arrives as one, anything else as a file. Only the person who
  scanned the code receives files (or `weixin.file_allow_users`), only from
  `weixin.send_file_roots` (default: the home folder), at most
  `max_send_file_bytes`; hidden files and folders (including Astra's own
  `.astra`), application data (`Library`, `AppData`) and credential files are
  always refused. `send_files_enabled: false` turns it off.
- **Voice notes.** When asked to speak, Astra calls `channel_send_file` with
  `speak` instead of `path`: the words are spoken in the voice set up for spoken
  replies ([voice](voice.md)) and sent as an audio file the phone plays with a
  tap (M4A on macOS, WAV elsewhere); short text may go with it. iLink accepts a
  bot's native voice message but does not deliver it, so WeChat shows a file
  rather than a voice bubble.
- **Privacy.** The bot token lives in the state directory's
  `channels/weixin/account.json`, readable by the owner only, and never appears
  in events or logs. Another program that polls the same login (for example
  Hermes) would compete for messages; log in separately.

Protocol details follow Hermes Agent's adapter (MIT License, Nous Research).

## MCP

MCP operations without an explicit local `risk: "read"` / `tool_risks` read-only
classification require approval, even in permissive mode. The default network
label, tool names and a server's `readOnlyHint` do not grant permission. Without
an approval handler, unapproved operations fail before dispatch. Explicit
read-only configuration retains approval-free reads, while consequential
operations such as sending or deleting still require their action approval.

Install the `mcp` extra and create `.astra/mcp.json`:

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

MCP tools are exposed as `mcp__<server>__<tool>`. A name over 64 characters, or
one that several tools would share, ends in a short hash instead. A tool with an
empty name or a schema that is not an object is left out while the server keeps
its other tools. `/mcp` lists every renamed or skipped tool with the reason; use
`/doctor` for connection failures.

Both stdio and streamable HTTP transports are supported; servers can be disabled
individually. `${ENV_NAME}` placeholders resolve environment variables without
storing credentials in JSON. See [the complete example](../config/mcp.example.json).


MCP failures after dispatch distinguish reads from actions: a lost response or timeout
from a potentially mutating tool returns `mcp_unknown_outcome` and forbids automatic
replay. Reconnect, observe the target, and obtain fresh references before deciding
what to do next. Only explicit local `tool_risks` / `risk` set to `read` allow read recovery hints.
Server `readOnlyHint` annotations do not establish permission or replay safety.
These hints do not reduce configured approval requirements. Old tool definitions
cannot dispatch on a replacement connection.

## Web search

Untrusted page downloads validate each redirect and resolved address before
requesting it, pin DNS for the connection, and bound wire/decompressed content.
Public pages require no approval; `fetch_url` requests explicit approval for a
private origin. This grant does not cover another redirect origin, and private
responses are not put in the shared URL cache. Configured model/search/MCP
service endpoints keep their separate connection configuration. Missing the
required `httpx` dependency fails page fetching closed; service diagnostics may
still use their own fallback transports.

`search_web` supports `provider=auto|exa|searxng|deepseek`. Use `/search` to inspect the
default and `/search auto|exa|searxng|deepseek` to switch it persistently. In `auto`
mode, news, research, model-release and benchmark queries prefer Exa; ordinary
queries start with SearXNG. A page above 1, an `engine` or a category other than
science/news is only honoured by SearXNG, so `auto` sends such a call there; an
Exa answer names any parameter it did not apply. SearXNG lists at most two
results per site unless the query contains `site:`. Empty results or provider
failures fall back between Exa and SearXNG inside the same tool call. Search has no fixed per-turn call
limit; the general repeated-call and ReAct iteration guards still prevent true
infinite loops. DuckDuckGo is not used.

Set `EXA_API_KEY` directly, or set `EXA_ENV_FILE` to another dotenv file that
contains `EXA_API_KEY`. The latter reuses a credential without copying the
secret into this project. `/doctor` reports only whether Exa is configured and
which source was used; it never prints the key.

DeepSeek's Search and Fetch APIs are optional and never chosen automatically:
`auto` does not search or extract through DeepSeek. Search through it with
`provider=deepseek`, `/search deepseek` or `WEB_SEARCH_PROVIDER=deepseek`. It
reads `DEEPSEEK_API_KEY` from the environment or `.env` (a key pasted only into
`/connect` is not used) and sends only the query and the result count
(`max_results`, 1–50). The query may have at most 128 characters and accepts
DeepSeek's operators: `site:` (several joined with `OR`), `-site:`, `-word`,
`"exact phrase"`, `-"phrase"`, `lang:zh,en`, and `after:`/`before:` with a year
or date. Page, engine, category, language and inline content are not applied on
this route, and the answer names them. Each result shows DeepSeek's summary
written for model reading (its short snippet when there is no summary), with the
date and site name when present. Astra does not send the optional
`ori_question` (the user's question) or `localization` (an IP address), so no
text beyond the query reaches the service; either could become an explicit
option later. Requests go to `DEEPSEEK_TOOLS_URL` (default
`https://api.deepseek.com/v1/tools`; search timeout `DEEPSEEK_SEARCH_TIMEOUT`,
15 seconds), never follow a redirect, and are repeated once after a short wait
on HTTP 408, 429 or 5xx; a timed-out request is reported rather than repeated.
A rejected key (401), an exhausted balance (402) and the rate limit (429; 5
requests per second per account, and per target domain for Fetch) are named in
the failure. DeepSeek bills each API at 5 CNY per 1,000 successful requests from
2026-11-01 (Beijing time). `/doctor` and `search_status` report only whether the
key is set; neither calls the service.

`web_extract` has an independent provider waterfall. `WEB_EXTRACT_PROVIDER=auto`
tries configured APIs in `Tavily -> Exa -> Parallel` order, then local/cloud
Firecrawl, and finally direct HTTP with main-content HTML-to-Markdown conversion.
Failures fall through per URL, so one successful page is not fetched again by
later providers. Set `provider` on one tool call, or set
`WEB_EXTRACT_PROVIDER=tavily|exa|parallel|firecrawl|http`, to choose the first
rung explicitly. DeepSeek Fetch is used only when named the same way
(`provider=deepseek` or `WEB_EXTRACT_PROVIDER=deepseek`): it is tried first and
the usual order follows for pages it cannot read. It returns Markdown without a
final URL, so results keep the requested URL. Pages over 15,000 characters
return a 75/25 head-tail window and save the full Markdown under
`.astra/cache/web`.

The optional browser extractor is most safely configured with
`BROWSER_EXTRACT_ARGV`, a JSON array containing the executable and each fixed
argument separately. For example, Windows can use
`["C:\\Program Files\\Browser Extract\\extract.exe","--render"]`. Legacy shell
strings remain supported: `BROWSER_EXTRACT_CMD` is preferred and takes
precedence over `WSL_EXTRACT_CMD`. The backward-compatible `WSL_EXTRACT_CMD`
remains a legacy fallback on every platform. Astra runs those strings through
`cmd.exe` on Windows or Bash on POSIX. On Windows, each extractor subprocess
receives the URL through its own child-only environment and a fixed quoted
`%ASTRA_BROWSER_EXTRACT_URL%` placeholder with delayed expansion disabled; on
POSIX it is a separate positional argument. Raw double quotes, CR, LF, and NUL
are rejected in Windows legacy-mode URLs—percent-encode them or use
`BROWSER_EXTRACT_ARGV`. Shell expansion such as `$HOME` remains available
without interpolating URL text into the shell program. Set
`BROWSER_EXTRACT_STATUS_CMD` to an independent readiness command for a custom
extractor; otherwise Astra checks only that its executable or shell wrapper is
ready and does not send a synthetic URL or status argument.

When no extractor setting is present, Windows Python (`sys.platform=win32`)
invokes the built-in extractor through WSL. macOS and all Linux Python
processes—including Python running inside WSL—use native Bash. Standard Chrome,
Edge, and Chromium application locations are discovered on macOS in addition
to the existing Windows/Linux candidates.

When moving a checkout copied from Windows to macOS/Linux, unset
`WSL_EXTRACT_CMD` or replace it with a valid native `BROWSER_EXTRACT_ARGV` or
`BROWSER_EXTRACT_CMD`. The legacy `WSL_EXTRACT_CMD` fallback is read on every
platform, so a Windows value that starts with `wsl` would otherwise be attempted
on macOS/Linux.

Proxy routing is process-dynamic. Astra reads `HTTP_PROXY`, `HTTPS_PROXY`, and
`ALL_PROXY`. In `ASTRA_PROXY_MODE=auto` it uses the configured proxy only while
that endpoint is reachable, so stopping or restarting a local proxy does not
require restarting Astra. The launchers select `off` unless you explicitly set
`ASTRA_PROXY_MODE`; use `auto` to enable reachability probing or `always` to skip
the probe. Local/private URLs and hosts listed in `NO_PROXY` always connect
directly, which lets local SearXNG or Firecrawl coexist with search APIs that
need a proxy.

## Conclave multi-expert research

Conclave orchestrates parallel SearXNG searches across 12 domain-specialist
experts, then synthesises findings into a structured report. Use the slash
command to ask a question directly:

```text
/conclave Compare current options for running LLMs locally
```

Configuration commands are auto-completed in the TUI menu:

```text
/conclave config
/conclave config chairperson <profile>
/conclave config expert_list
/conclave config sources 5
```

Use `chairperson` to select the synthesis model (`active` uses the current
model) and `sources` to limit sources per expert. The expert menu supports
multi-select: press Tab to append an expert, then Enter to confirm the list.
Use the names offered by the menu.

Configuration is persisted to `~/.config/hermes/conclave.json` automatically.

## Image tools

The public distribution includes `read_image`, `read_image_tiles`, and
`inspect_image_metadata`. Image reading and PNG generation-metadata inspection
work independently of any image-generation service.

The bundled ComfyUI generator and its lifecycle tools have been retired from
the public distribution. Existing generator environment settings no longer
register tools or start a service. Configure an external image provider through
[MCP](#mcp) if needed, and keep personal workflows and model settings outside the
repository. Windows image paths and shell commands use `AGENT_WSL_DISTRO` for
WSL selection.

## Read-only 163 mail

Astra reads the configured mailbox itself with its read-only `mail` tool: recent
mail (or only unread), search, and one message's text, in any conversation or a
scheduled check given `--mail`. Mail is treated as outside text, like a web page:
a memory saved in a turn that read mail needs your approval. Any IMAP account
works, not only 163: set `ASTRA_MAIL_ADDRESS` and `ASTRA_MAIL_PASSWORD` (an app
password or authorization code) instead of the 163 names below. Gmail, QQ,
iCloud and 163/126/yeah are recognised from the address; for another provider
also set `ASTRA_MAIL_IMAP_HOST` (and `ASTRA_MAIL_IMAP_PORT` or
`ASTRA_MAIL_SENT_FOLDER` when they differ). A folder's first sync caches its
newest 100 messages (`ASTRA_MAIL_SYNC_LIMIT`); later syncs fetch what arrived
since and never older history.

The optional 163 integration incrementally caches mail for local queries. Put
your account and a 163 client authorization code (not the web password) in the
repository `.env`:

```dotenv
ASTRA_163_EMAIL=your-account@163.com
ASTRA_163_AUTH_CODE=your-client-authorization-code
# ASTRA_163_MAX_BODY_BYTES=5242880
```

With no subcommand, `checkmail` refreshes the default `INBOX` and `已发送`
folders and then shows the 30 newest Inbox messages. Subcommands provide an
explicit `sync --json`, cached `search`, stable-UID `read`, folder listing, and
status. Add `--offline` to `recent`, `search`, `read`, or `folders` to avoid a
server connection and use only an existing cache. A failed refresh returns
clearly labelled cached results when available; offline access to a missing
cache fails without creating a database.

```powershell
.\scripts\checkmail.bat
.\scripts\checkmail.bat sync --json
.\scripts\checkmail.bat search "invoice" --from billing@example.com --window 200 --json
.\scripts\checkmail.bat read 352 --folder INBOX --json
.\scripts\checkmail.bat recent --offline --recent 10 --json
```

Explicit attachment download currently fails closed on Windows because secure
local attachment writes are available only on POSIX. Windows synchronization,
search, reading, folder listing, status, and offline cache queries remain
supported. A direct Windows attachment request returns the stable
`unsupported_platform` classification with exit code `2` before contacting the
mail server for attachment data.

```bash
./scripts/checkmail.sh
./scripts/checkmail.sh sync --json
./scripts/checkmail.sh search "invoice" --from billing@example.com --window 200 --json
./scripts/checkmail.sh read 352 --folder INBOX --json
./scripts/checkmail.sh recent --offline --recent 10 --json
./scripts/checkmail.sh attachment --folder INBOX --uidvalidity 77 --uid 352 --part 2 --json
```

Synchronization stores headers and decoded text in `.astra/mail/163.sqlite3`.
It does not download attachment payloads during synchronization; only the
explicit `attachment` command writes one requested payload below
`.astra/mail/attachments/`. The integration selects mailboxes read-only and
does not mark messages read, move, delete, reply to, or send mail.

Source checkouts also include the repository-local
[163 mail skill](../.astra/skills/operations/163-email-sync/SKILL.md). It is discovered
through the normal project-trust checks; setup does not install or update it.
Mail credentials, cache and other private `.astra` state remain local and ignored.

## Calendar and Reminders (macOS)

On macOS, the `calendar` tool reads the person's Calendar and Reminders apps
through the computer helper's EventKit mode (build the helper with
`scripts/build_macos_computer_helper.sh`): what is on between two times, open
reminders, and the names of calendars and lists. Adding an event or a reminder,
or ticking a reminder off, shows an approval card every time; nothing is ever
deleted. A reminder with a time gets an alert, so Reminders notifies the person
on their other devices too. Use the `jobs` tool instead when Astra itself should
push a reminder.

The helper answers calendar requests as its own process, so the first use makes
macOS ask whether **AstraMacComputerHelper** may access Calendars or Reminders;
the switches are in System Settings > Privacy & Security. The helper is ad-hoc
signed, so a rebuilt helper may be asked again. A scheduled check given
`--read-calendar` (or `read_calendar` in the `jobs` tool) can read the calendar
but never adds, changes or completes anything, and never makes macOS ask.
Invitations and shared lists can carry other people's text, so a memory saved in
a turn that read the calendar needs approval, as with mail. Windows and Linux
have no calendar tool.
