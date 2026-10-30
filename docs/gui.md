# Desktop GUI preview

[Home](../README.md) · [Documentation](README.md) · [简体中文](zh-CN/gui.md)

Astra's source desktop preview uses Electron and the existing Python backend.
It supports chat, model connections, independent background sessions, tool approvals,
questions, file changes and a searchable command panel. Full TUI parity remains
an active target; the [design and current coverage](design/gui.md) track the remaining work.

## Install and launch

For a self-contained local application, see [standalone desktop builds and recovery](desktop-distribution.md).
Those builds include Python and Electron; the following instructions are for source installations.

Use a complete source checkout, Python 3.11+, Git and **Node.js 22.19+ with npm**.
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
does not stop the desktop. If the window has not opened within 90 seconds, the
command ends the desktop and everything it started, and names the startup log. A second launch for the same installation/data directory
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

## Personal thread

**个人对话** at the top of the sidebar is one long-lived conversation, apart from your
project sessions. The desktop's background host holds it, so it keeps running with the
window closed (from the tray), and a paired phone opens on it. Every scheduled job's
result arrives in it as Astra's message, so you can ask about it right there. It works
in its own folder (`personal` in Astra's data folder) rather than in a project, and its
model is switched like any conversation's. It reads as a chat: your messages and Astra's in
bubbles, a time heading after each pause, the tool calls of a step folded into one line that
opens on a click, and job results as cards naming the job. It cannot be closed or deleted;
`/new` in it starts afresh. WeChat keeps its own conversations. The design is in
`specs/personal-thread.md`.

Work in a project goes to a conversation there. Ask the thread ("in astra-master, find out
why this test fails") and it opens a conversation in that project (a folder path, or a
project's name as the sidebar lists it) and hands it the task through the local mailbox.
That conversation works with its own tools, permissions and approvals, and shows
**个人对话正在管理这个任务** with **接管**: take it over and the thread stops following
it. While it works, the thread lists it with whatever it waits on: its approvals and
questions can be answered right there, on the phone too. When it is done the thread hears
back and tells you, and the desktop and a paired phone are notified.

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
  Optional questions can use a timed wait: the question remains answerable while
  independent work continues. Required decisions retain blocking waits; a timeout
  grants no approval. See [asynchronous questions](async-user-questions.md).
- **执行详情** opens tools/processes, subagents, per-turn file diffs, files/images and
  context (including the agent's current plan). Missing/expired snapshots and unconfirmed
  changes are labelled separately. Large text previews are bounded; use **系统打开** for
  the complete file.
- **子代理** in that panel holds every subagent of the conversation by name, each on its
  latest run (status, time, the tool it is calling, its result and full record) with earlier
  runs folded; those of one group, which may write to each other, are shown together, and an
  ended one is marked 已结束. Agent Teams of earlier conversations and runs without a name
  follow. The subagent summary in the conversation opens it, and a dot on the tab means one
  is still going.
- **命令与功能** exposes all current TUI command roots with optional parameters.
  `/tool N` and `/gallery N` locate a recorded result. Commands such as `/memory`,
  `/skills`, `/tasks` and `/diagnostics` currently use compatibility forms and the
  existing backend output, rather than complete dedicated management pages.

Markdown images and files can be previewed from the current workspace and Astra's
artifact storage. Use the file picker to explicitly open an external file. Raw HTML
and executable URL schemes are not rendered. These presentation rules do not change
the model's tool permissions.

PDF files have a local page reader. DOCX/PPTX previews convert a private snapshot
locally and report missing fonts; XLSX previews show saved cell values and clearly
label missing formula caches. Refresh a preview after its source changes. Cancelling
or leaving the preview cancels its pending conversion. `doc_check` reports package
structure separately from visual inspection and formula recalculation. See
[document preview and limits](desktop-document-preview.md).

The Context panel includes a request timeline with preparation time, first-response
latency, provider usage, cache metrics and separately labelled client estimates.
Missing measurements stay uncollected. Desktop backends enable redacted metrics by
default; `ASTRA_PROFILE_QUERY=0` disables collection. Records contain hashes, counts
and timings, not prompt bodies, and use bounded log rotation. The viewer is read-only
and never starts a model request; old unscoped records are not attributed to a session.

## Interactive cards

A reply can contain a small interactive display: a calculator with sliders, a stepper through
a process, a comparison with toggles. The model writes it as a fenced code block whose language
is `card`, holding an HTML fragment with its own `<style>` and `<script>`:

````markdown
```card
<h3>Compound interest</h3>
<label>Years <input id="years" type="range" min="1" max="40" value="20"></label>
<p>1,000 grows to <strong id="total"></strong></p>
<script>
  const years = document.getElementById("years"), total = document.getElementById("total");
  const show = () => { total.textContent = Math.round(1000 * 1.05 ** years.value).toLocaleString(); };
  years.addEventListener("input", show); show();
</script>
```
````

The desktop runs a finished block in place; while the block is still being written, and
whenever it cannot run, its source is shown instead. **查看源码** switches between the two.
The terminal and message channels show the block as code, so a reply should still make sense
without the card.

What a card can and cannot do:

- It runs in a frame of its own with no access to Astra: no bridge, no conversation, no files,
  no storage. It receives its own source and the theme colours, and reports its height.
- It loads nothing and reaches nothing. Scripts, styles, images and fonts must be inline
  (`data:` images are allowed); requests, WebSockets, WebRTC, forms, pop-ups and navigation are
  refused. Links inside a card do not open. A card that navigates its frame is stopped and its
  source shown.
- It may use the theme through the CSS variables `--bg`, `--panel`, `--soft`, `--hover`, `--line`,
  `--text`, `--muted`, `--accent`, `--on-accent` and `--error`, and inherits plain styles for
  text, buttons, inputs and tables, so it follows light and dark themes.
- It keeps its state only while it is on screen in an open conversation: scrolling it far out
  of view, switching to the source, or reopening the conversation starts it again.
- A block over 200,000 characters is shown as source.

The model learns the format from the built-in `interactive-cards` skill. Only a conversation
started in the desktop lists it, because only the desktop shows cards; the model reads it when
it decides a card would help, or when you ask for one. The skill catalog is applied per
conversation: a conversation started before this skill existed, or in the terminal, gets it
after `/guidance refresh --now` in the desktop, and a desktop conversation continued in the
terminal keeps it.
`/skills show interactive-cards` displays the text.

## Pages

A card is a small display inside a reply. For something to look at as a whole (a design draft, a
prototype, a report, a long interactive walk-through) the model writes a page: a complete HTML
document saved in the workspace under a name ending in `.page.html`. When the write finishes the
Files panel opens on it beside the conversation, and each later change to that file shows the
new version. An ordinary HTML file of a project being worked on is not opened this way, though
any `.html` file can be previewed as a page from the Files panel.

- **源码** / **预览** switch between the page and its source. **放大** gives the page the
  conversation's room as well; **还原** or Escape gives it back.
- Once you close the page or leave the Files panel, later writes to that same file stay out of
  your way; a different page the model writes opens again. Every page written in the
  conversation stays listed under Files.
- A page runs under the same rules as a card: a frame of its own with no access to Astra, and no
  network. Scripts, styles, fonts and images must be inline; requests, WebSockets, WebRTC,
  storage, forms, pop-ups and navigation are refused. A link to a place inside the page scrolls
  there; any other link does not open. A page that navigates its frame is stopped.
- Unlike a card, a page is a document of its own: its doctype, head, body and styles are what it
  wrote, and nothing of the app's is laid over it. The app's colours are available to a page
  that wants them as `--astra-bg`, `--astra-panel`, `--astra-soft`, `--astra-hover`,
  `--astra-line`, `--astra-text`, `--astra-muted`, `--astra-accent`, `--astra-on-accent` and
  `--astra-error`.
- A file over 1 MiB is shown as source.
- The model can look at a page as well as write it. When it reads the page file with its image
  tool, the desktop draws the page out of sight, in the same isolated frame, and hands the model
  the picture: 1000 px wide, from the top, up to three screens. A longer page is reported with
  its full height, and an error raised by the page's script is reported with it. The picture is
  of the page as it loads; the model cannot click in it.
- **发给 Astra 看** attaches that same picture to the message you are writing, for when you want
  the model to look at what you are looking at. Pictures are kept in the desktop's own data
  folder (`gui/page-snapshots`), the newest forty.

Pages are a desktop feature. The model learns of them from the same `interactive-cards` skill as
cards, so the same notes on the skill catalog apply.

## Canvases

A canvas is a work shown beside the conversation, drawn by Astra's own template from what the model
writes: a slide deck or a document. Every change to its content is kept as a version.

### Slide decks

A slide deck is a canvas: a folder `<name>.canvas` in the workspace with `canvas.json` (its type
and title), `slides.html` and `assets/` (specs/canvas.md). The model makes one with the `canvas`
tool, which hands it the deck's format, then writes the slides with the ordinary file tools: one
`<section class="slide">` per slide, a 1920 x 1080 page each, in a few layouts (title, section,
columns, image, quote) with a theme of CSS variables. The deck's look, the list beside the
conversation, presenting and printing are Astra's own code; the model writes only the slides.

- When the deck is made, and after each change to any of its files, the Files panel shows it as a
  list of its slides with their numbers. Click a slide to present: one slide at a time, the arrow
  keys and Space move, Escape goes back. **放大** gives the deck the whole window first.
- **源码** shows `slides.html` as the model wrote it.
- Images go in `assets/` and are named `assets/<file>` in the slides: PNG, JPEG, GIF, WebP or SVG,
  up to 8 MiB each. Nothing loads from the network. An image that cannot be shown is listed above
  the deck.
- The model looks at a slide with `canvas_look`: the desktop draws that slide at its own size, out
  of sight, in the same isolated frame, and hands the model the picture at 1280 x 720. Looking
  needs no approval.
- `canvas` with `action="export"` prints the deck to `<name>.canvas/exports/<name>.pdf`: one
  16:9 page per slide, with selectable text. The print runs in a window nobody sees, as a
  document of its own under the frame's policy and in an opaque origin, with WebRTC removed.
- With `format="pptx"` it writes `<name>.pptx` there instead: one full-slide picture per slide at
  1920 x 1080, each slide's heading as the picture's description for screen readers, and the
  slide's `<aside class="notes">` as its speaker notes. The text cannot be edited in PowerPoint.
  This needs python-pptx (the `documents` extra, included in the desktop package).
- Slides run under the same rules as a page: no access to Astra, no network, no storage, no
  pop-ups, no navigation.
- Everything of a canvas is read and written inside its folder, where that really is: content,
  images, versions or exports reached through a link out of the folder are refused.

### Documents

A document canvas holds `doc.md` instead of the slides: an ordinary Markdown document that the model
writes section by section with the `doc_*` tools (the "documents" family of `tool_search`).

- The Files panel shows it rendered, with its outline above: click a heading to go to it. Open
  review comments are listed on top.
- Select a passage and write what should change: **写进文档** writes the comment into `doc.md` as
  `<!-- @astra: ... -->` right after the passage (at the end of its line when the Markdown spells
  it differently, at the end of the document when it cannot be found; never inside a heading or a
  code block). Ask Astra to handle the comments; `doc_comments` lists them and the model resolves
  each one with `doc_edit`.
- Export with `doc_export` (Word, PDF or HTML), as for any Markdown document. `canvas_look` and the
  canvas's own export are for decks.

### Versions

The content file (`slides.html` or `doc.md`) is copied to `.versions/NNNN/` with a `meta.json`
(time, what made it, its SHA-256) when the canvas is made, after each successful write into it
by a tool, after a comment and after a restore. A version identical to the last one is not kept
again; the 30 newest are kept. **版本** lists them, newest first; click one to see it in place
of the current content, then **恢复成这个版本** or **回到现在**. A restore writes the old content back
as a new version, after keeping the current content if no version holds it yet, so nothing is lost
by restoring. Images in `assets/` are not versioned.

Canvases are a desktop feature; the `canvas` and `canvas_look` tools are offered only to the
desktop's backends, as tools loaded on demand (the "canvas" family of `tool_search`). On a paired
phone a canvas is not shown yet, and comments and versions are desktop-only.

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

## Scheduled jobs and activity

**定时任务** in the sidebar lists the jobs Astra scheduled for you (see
[persistent jobs](persistent-jobs.md)): when each runs next, how it reports, and
whether it was paused and why. Pause, resume, run once now or remove one there, and
choose the model a check runs with (following the default, or one of yours);
these are your own changes and ask for no approval. The quiet hours are set on the
same page, and the inbox below keeps every result, unread ones marked, with where
each was pushed and its token usage.

**活动** lists what Astra did in the last 7 days, newest first: conversation turns
from the desktop, the terminal and WeChat with the files they changed, the commands
they ran and the approvals you gave, and job runs. Turns that only talked are hidden
until asked for. **打开这次对话** opens the turn's conversation. While the window is
in the background (or the Mac has been idle or locked for two minutes), a waiting
approval or question, a reply that took 20 seconds or more, and job results arrive as
system notifications, and on a paired phone that turned notifications on
([phone companion](phone.md#notifications)).

Its top shows **今日用量**: today's tokens for every model request Astra made (desktop
and terminal conversations, WeChat and QQ chats, scheduled jobs, and the work behind them
such as delegates and context compaction), by model and by source. Enter a model's prices
per million tokens there (or `/usage price MODEL INPUT OUTPUT [CACHED]`) and its cost is
shown too. A daily budget, in tokens (`500000`) or money (`¥5`, which needs prices), is set
there or with `/usage budget`; once set, the composer's toolbar shows today against it (a phone shows it only once over).
The first request that goes over the budget says so once in the conversation (and as a
notification); scheduled checks then wait until the next day, while conversations, reminders
and runs you ask for go on. `/usage` shows today and the current conversation in any client.
A tool that keeps failing with different arguments is flagged to the model on the third
failure in a row and stopped at the fifth.

**记忆** shows and edits what Astra keeps about you; see [the memory page](memory.md#the-memory-page).

## Permissions and persona

The approval-mode button in the composer opens **权限与对话模式**. Choose
**按需审批** or **完全访问** explicitly, and use the conversation-mode choices for
Work, Minimal or Bar. Return to Work before switching between isolated
modes. These controls call the existing backend commands;
they do not add a model workflow or tool quota. Pending approvals continue to appear
inside the conversation.

An approval card answers the same way on the desktop, a paired phone, the terminal and in
WeChat: **允许一次** (this call), **本任务允许** (until the work of your current message is
done), **本会话允许** (until this conversation ends) or **拒绝**. Each conversation has its
own grants: a WeChat chat never uses what the desktop session allowed, nor the other way round.
Paying, sending, deleting and publishing (a browser button that submits, pays or deletes;
`git push`, a hard reset, `rm` and publish commands; an MCP tool named for sending, deleting or
paying; an upload) are asked every time with only allow-once and deny, and no earlier grant
covers them; they are also not answered by the window-wide keys. When such a step would send
text, the card shows that text as a draft with **发送** and **丢弃**. After you have approved
the same ordinary request five times on two different days and never refused it, its card also
offers **以后都允许**; those standing rules are listed under **权限与对话模式 → 以后都允许的操作**,
where you can remove them.

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
Application-menu quit, macOS Cmd+Q and tray exit use the same task guard. Startup and
unconfirmed submissions also require a decision; returning from the dialog retains
the backend. Repeated quit requests share one confirmation.

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
For self-contained local builds with a bundled Python runtime and explicit offline
update/rollback, see [desktop distribution](desktop-distribution.md). Signing,
notarization and platform acceptance are separate release checks.

## Keyboard

| Action | macOS | Windows |
| --- | --- | --- |
| Send / newline | Enter / Shift+Enter | Enter / Shift+Enter |
| New conversation | Cmd+N | Ctrl+N |
| Command panel | Cmd+K | Ctrl+K |
| Cancel current task | Cmd+. | Ctrl+. |
| Tool approval: allow once / allow for the session / deny | Cmd+Enter / Cmd+Shift+Enter / Cmd+Delete | Ctrl+Enter / Ctrl+Shift+Enter / Ctrl+Backspace |
| Tool details | Cmd+Shift+O | Ctrl+Shift+O |
| Context details | Cmd+Shift+L | Ctrl+Shift+L |
| Close dialog/details | Escape | Escape |

The approval keys answer the oldest pending approval from anywhere in the window. In a
field that holds text they keep sending and deleting, so a draft is never taken for an
answer, and they are off while a dialog is open. A request scrolled out of sight is
shown by the first press and answered by the next. An approval card never takes the focus
by itself; after a click or Tab into it, Y or Enter allows once, A allows for the
session, and N or Escape denies, as in the terminal.

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
