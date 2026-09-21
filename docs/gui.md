# Desktop GUI preview

[Home](../README.md) · [Documentation](README.md) · [简体中文](zh-CN/gui.md)

Astra's source desktop preview uses Electron and the existing Python backend.
It supports chat, model connections, independent background sessions, tool approvals,
questions, file changes and a searchable command panel. Full TUI parity remains
an active target; the [design and current coverage](design/gui.md) track the remaining work.

## Install and launch

Use a complete source checkout, Python 3.11+, Git and **Node.js 22.12+ with npm**.
In an installation with the `astra` command registered:

```text
astra setup --gui
astra --gui
```

Without a registered command, run from the checkout:

| Platform | Setup | Start |
| --- | --- | --- |
| macOS | `./astra.sh setup --gui --install-command` | `astra --gui` |
| Windows CMD / PowerShell | `.\astra.bat setup --gui --install-command` | `astra --gui` |

Exit running Astra sessions before setup. `--install-command` registers `astra` in
your user PATH; open a new terminal afterward, then start `astra --gui` from the
project directory you want to use. To launch directly without registration, use
`./astra.sh --gui` or `.\astra.bat --gui` from the checkout.

Plain `astra` continues to start Ink. A terminal-only setup does not download
Electron. `setup --gui` installs locked components and records the choice for
future updates. A standalone installer is not included. Linux desktop use is
experimental; existing Python and TUI platform support is unchanged.

The start command returns after opening the window. Closing the launching terminal
does not stop the desktop. A second launch for the same installation/data directory
focuses the existing window. Relaunching restores the last active conversation and
unsent draft without issuing a model request. The project directory is shown in the header; adding
a project starts its backend with that directory as the tool workspace.

## Model connection

Select **连接或选择模型** on the welcome page or the model button beside Send.
Select **ChatGPT / Codex**, click **登录 ChatGPT**, open the authorization page and
complete its device flow. After the connection is saved, select an available model.
The API route also accepts a key or environment-variable name and an optional endpoint.
See [ChatGPT login](codex-oauth.md) for account requirements and troubleshooting.

Credentials use Astra's existing authentication storage. Each computer connects
separately. Reasoning displays only what the provider returns, including summaries;
Astra does not generate missing private reasoning.

The model list distinguishes **未连接** (not connected), **已配置** (configured)
and an unknown connection state. Selecting a model without credentials first opens
its connection form; it does not repeatedly attempt a switch with a missing key.
“Configured” describes saved credentials, not a completed live authentication test.
Switch progress and failures stay in **模型与账号**, and a rejected switch keeps the
current model. A missing current connection also has a **连接模型** action above
the composer. This is useful for a separate checkout whose `.env` and login storage
have not been configured yet.

## Conversations and tools

- **新对话** returns to the current project's blank draft. Repeated clicks reuse
  that draft rather than filling the sidebar with empty sessions. Opening model
  settings or preparing capture may start a backend when needed; a backend used
  only for configuration remains a draft. Once a conversation starts, it has its
  own backend. Switching conversations keeps background work running; the dot
  in the sidebar shows activity or pending input.
- Historical conversations first open read-only. **继续此会话** opens a writer.
  If another TUI/GUI process owns the conversation, the backend refuses a second
  writer; close that writer before continuing. Opening model, permission or persona settings
  from a preview also continues that conversation, without affecting a hidden session. Work, Minimal and Bar
  retain separate namespaces.
- Text and ordinary attachment lists are saved per conversation. Sending waits for
  backend acceptance before clearing the submitted draft. If acceptance is unknown,
  keep the draft and check the outcome before sending again. Reconnection does not
  automatically repeat the model request.
- Tool approvals and structured questions appear inline. Send changes to Stop while
  work is active and the draft is empty; typing a new instruction allows steering.
- **执行详情** opens tools/processes, per-turn file diffs, files/images, context and
  Team state. Missing/expired snapshots and unconfirmed changes are labelled separately.
  Large text previews are bounded; use **系统打开** for the complete file.
- **命令与功能** exposes all current TUI command roots with optional parameters.
  `/tool N` and `/gallery N` locate a recorded result. Commands such as `/memory`,
  `/skills`, `/tasks` and `/diagnostics` currently use compatibility forms and the
  existing backend output, rather than complete dedicated management pages.

Markdown images and files can be previewed from the current workspace and Astra's
artifact storage. Use the file picker to explicitly open an external file. Raw HTML
and executable URL schemes are not rendered. These presentation rules do not change
the model's tool permissions.

## Command help while typing

The composer starts at one line, grows with its content, and scrolls internally
after 220 px. Type `/` at the start of a message for command suggestions. Filter
by command spelling or Chinese feature name, use the arrow keys to select, and
Tab, Enter or click to fill the draft. **Completion does not execute anything**;
press Send or Enter again after completion to run the command. Escape dismisses
help without clearing the draft. Shift+Enter still inserts a newline. Suggestions
stay hidden for multiline text or drafts with attachments.

The current mode's actions appear first. Enter its prefix for subcommand help:

| Mode | Continue latest | New isolated session | Browse history | Return to Work |
| --- | --- | --- | --- | --- |
| Bar | `/bar` | `/bar new` | `/bar sessions` | `/bar leave` |
| Minimal | `/minimal` | `/minimal new` | `/minimal sessions` | `/minimal leave` |

In the GUI, browsing mode history opens a filtered session picker. New sessions
preserve previous ones. `/bar sip` takes a sip; `/bar output atomic` displays a
validated reply together with its scene state, while `/bar output stream` streams
the text. Installed local modes contribute their own commands and descriptions.
Leave the current isolated mode before
entering another. The permissions/mode dialog also links to command help and
preserves any existing draft.

## Session menus

Use the **…** button beside an opened or historical session, or open
**设置 → 管理会话** to search and manage sessions.

| Action | Result |
| --- | --- |
| **重命名** | Changes the name shown in this GUI. The stored session ID and files keep their names. |
| **置顶 / 取消置顶** | Keeps the session in, or removes it from, the pinned section. |
| **导出 Markdown** | Opens a system save dialog and exports the full saved transcript, including existing tool output and subagent transcripts. Export does not alter the source session or depend on the visible history page. |
| **关闭会话后端** | Stops that session's local backend while retaining its saved history. Finish or stop current work before closing; reopening the saved session is separate from deleting it. |
| **删除会话** | Asks for confirmation, then removes the selected namespace's history, associated session artifacts and captured media. Project files the agent changed are retained. Active work or another writer prevents deletion. |

Work, Minimal and Bar sessions with the same ID are distinct. Session
menus act on the selected namespace; deleting one does not delete the others.

Optional installation-local modes are discovered from the running backend, keeping
their configured command and label. They can be entered through **权限与对话模式**
or **命令与功能**. Their live conversations support display names, pinning and
closing the backend, but offline history browsing, export and deletion are not yet
available in the GUI; manage those histories with the extension's own commands.
Public Astra does not bundle a private mode or private personas.

## Permissions and persona

The approval-mode button in the composer opens **权限与对话模式**. Choose
**按需审批** or **完全访问** explicitly, and use the conversation-mode choices for
Work, Minimal or Bar. Return to Work before switching between isolated
modes. These controls call the existing backend commands;
they do not add a model workflow or tool quota. Pending approvals continue to appear
inside the conversation.

Use **设置 → 人格设置** to choose from the current backend's available personas.
The model button, session menus and these settings provide direct controls for common
actions; **命令与功能** remains available for other commands and free-form parameters.

## Window capture and lifecycle

The GUI adapter uses the existing [Appshot](appshot.md) broker and verified capture
protocol. Enable it with `/appshot enable` after installing the native helper.
Captures attach to the selected conversation's draft; submitting remains a separate
user action. `/appshot pending status` queries an uncertain submission and
`/appshot pending discard` discards its local attachment state without cancelling
an already received request. Capture connectivity is shown as one current status
near the composer. A missing broker produces **窗口捕获暂不可用，不影响对话** with
**检查连接**, rather than appending the same notice to conversation history on every
retry. This status concerns capture availability, not the model connection.
Native GUI capture on macOS/Windows still needs the
platform acceptance matrix; transport fixtures are not a live permission check.

Closing a window with active work, pending input, Team activity or a session wakeup
offers **后台继续**, **停止并退出** or **返回**. Reopen hidden windows from the tray.
Backend restarts use `/restart` and its existing visible-acknowledgement protocol.
GUI and backend restarts are different: closing the desktop ends its owned backends.
Session wakeups still require a live backend and do not become offline schedules.

## Updates and diagnostics

Exit the GUI, including hidden tray instances, before `astra update`. A desktop
runtime holds the same installation lease as the terminal interface. Updates rebuild
enabled GUI components and the shared client; generated files and the component
selection participate in setup/update rollback.

```text
astra doctor --json
astra setup --gui --repair
```

Doctor reports the GUI component separately. Launch never installs missing packages
in the background. Run explicit setup/repair if Electron or built assets are missing.
If you update source manually with Git rather than `astra update`, run
`astra setup --gui --repair` afterward to rebuild the desktop assets.
GUI preferences live under the installation data directory's `gui/` folder. Backend
logs follow `AGENT_LOG_DIR`; detached launch diagnostics live under `gui/launches/`.

## Keyboard

| Action | macOS | Windows |
| --- | --- | --- |
| Send / newline | Enter / Shift+Enter | Enter / Shift+Enter |
| New conversation | Cmd+N | Ctrl+N |
| Command panel | Cmd+K | Ctrl+K |
| Cancel current task | Cmd+. | Ctrl+. |
| Tool details | Cmd+Shift+O | Ctrl+Shift+O |
| Context details | Cmd+Shift+L | Ctrl+Shift+L |
| Close dialog/details | Escape | Escape |

## Development checks

Run `astra setup --gui --extra dev` once, then from the source root:

```text
npm --prefix ui-core test
npm --prefix ui-tui run build
npm --prefix ui-gui test
npm --prefix ui-gui run build
npm --prefix ui-gui run test:e2e
```

`test:e2e` launches the real Python backend with a loopback model fixture and temporary
data. It does not copy credentials or invoke a paid model. Screenshots and results
are saved in ignored `output/playwright/gui/`. The manual **Desktop preview** workflow
runs build/unit checks and this fixture on macOS/Windows.

This preview has not completed full platform acceptance, real ChatGPT login through
the GUI, native capture competition, prolonged streaming/memory pressure or complete
screen-reader/IME checks. History uses measured-height virtualization with at most
100 mounted messages, pagination and reading-position preservation. Do not treat a passing
local fixture as completion of the design's A01–A16 acceptance matrix.
