# Everyday use

[Home](../README.md) · [Documentation](README.md) · [简体中文](zh-CN/usage.md)

Choose a model, adjust the interface and manage work in the terminal.

[Model connections](#model-connections) · [Model time context](#model-time-context) · [Reasoning intensity](#reasoning-intensity) · [Structured clarification](#structured-clarification) · [TUI themes](#tui-themes) · [Durable tasks](#durable-tasks)

## Conversational commands

These commands start normal model turns, keeping their evidence available for
follow-up questions and selected actions:

| Command | Conversation |
| --- | --- |
| `/learn review [skill-name]` | Read-only skill review, then user-selected edits and verification. |
| `/doctor [section or symptom]` | Explain fresh diagnostic evidence and propose checks or repairs. |
| `/diagnostics [section]` | Explain the current runtime snapshot. |
| `/conclave <question>` | Research using the existing tool and discuss its findings. |
| `/skills create [name] [description]` | Draft a useful user skill before saving. |
| `/memory review [query]` | Review current memory IDs and sources, then apply selected corrections. |
| `/handoff [notes]` | Prepare and save a redacted handoff; follow up to refine it. |

Review, skill drafting, memory review and diagnosis begin with read-only turns.
Once the proposal is complete, reply to choose the changes or execution checks.
Prepared handoffs use a distinct filename so an automatic exit snapshot cannot
overwrite them. Commands require a connected model in the work conversation.

Use `/doctor --raw [section]`, `/diagnostics --raw [section]`, `/diagnostics json`,
or `/handoff --raw [notes]` for direct output. `/skills create --template <name>
<description>` creates an empty template. Status, settings, history, undo and
cancellation commands still execute directly.

## Model connections

Run `/connect` to choose a provider, its API route, and an API key or environment
variable. Astra requests that endpoint's model list, then opens the searchable
model menu. No existing key is needed to open setup. Connecting saves the provider;
selecting a model makes it the startup default.

```text
/connect
/model
/model deepseek::deepseek-flash
/doctor
```

In `/model`, choose a connected provider with Enter, then type to filter its
models. Recent selections appear on the first level. The provider submenu also
has **Refresh model list** and **Back to providers**. To use an unlisted model,
type its exact ID after `provider::` and choose **Use model ID**.

Astra fetches only the provider you open, uses a one-hour cache, and offers manual
refresh. Nothing polls in the background. Rows distinguish `live`, `cache`,
`stale-cache`, `preset`, and manually entered models. An error remains visible
when cached or preset entries are shown. A successful empty response is not replaced by an old list; the current selection
may remain visible with a `selected` label.
A list response does not verify chat access, billing entitlement, or tool support.

The connection panel supports DeepSeek, Qwen / Model Studio, Zhipu, Hunyuan,
OpenRouter, and custom OpenAI-compatible endpoints. Regular API, Coding / Token
Plan and region routes stay separate; Astra never retries a plan key on a
regular API. For workspace-specific Qwen endpoints, paste the base URL from the
provider console. Existing local discovery, including oMLX, remains available.
Native non-compatible protocols need their own adapters.

Bundled per-model settings in `config/models.yaml` and machine-specific overrides
in `.astra/models.yaml` remain supported. Known models keep their own settings;
new model IDs use advertised metadata when available, otherwise a conservative
32K context and 4K output budget with no assumed vision or reasoning support.
Edit the model override if the provider requires specific limits or parameters.

Keys entered in the panel are masked, travel through a dedicated control message,
and are saved only in `.astra/connections/*.json`, outside Git and conversation
history. POSIX files use owner-only permissions; Windows uses the installation's
user-directory permissions. Environment references store only the variable name.
Model metadata is cached separately in `.astra/model-cache`, scoped to endpoint
and credential. Both live under the installation state (or `ASTRA_HOME`) and are
preserved by `astra update`. They are local private files, not an encrypted vault.

The legacy command is still accepted:

```text
/connect my-local http://127.0.0.1:8084/v1 LLM_API_KEY
```

Its third argument is an environment-variable **name**, never the key value.
In the plain CLI, `/connect` offers numbered choices and a hidden key prompt;
`/model <provider>::` lists that provider's models.

### DeepSeek model migration

Astra uses `deepseek-flash` (DeepSeek V4.1 Flash) for its built-in DeepSeek
profile, including native image input, thinking controls, and the `flash` /
`fast` worker aliases. On the official `api.deepseek.com` endpoint, saved V4
Flash, Vision Exp, and temporary V4.1 selections resolve to this profile;
retired names are not offered as separate models. `deepseek-v4-pro` remains a
selectable text-only profile of its own. Custom and local endpoints keep their
own model IDs. The 1M context and 384K output allowances are unchanged.

The [September 10 announcement](https://api-docs.deepseek.com/zh-cn/updates/)
retires V4 Flash and Vision Exp immediately; the scheduled September 14 V4 Pro
redirect was later reversed — DeepSeek keeps serving `deepseek-v4-pro` with
unchanged billing. Astra keeps V4 Pro selectable as its own model. Existing
Astra processes need a restart to load the updated adapter.

### DeepSeek vision tiling

The `deepseek-flash` profile preserves detail in large local or
base64 images with original-pixel tiling. The preference defaults to ON, applies
immediately, and is saved as `vision_tiles_enabled` in `.astra/settings.json`:

```text
/vision-tiles
/vision-tiles on
/vision-tiles off
```

When enabled for that configured DeepSeek vision model, Astra creates lossless
768 x 768 detail crops with 64 px overlap plus an overview. Requests are capped
at 32 images; if every crop does not fit, the model selects additional crops
through the request-scoped tile tool. Generated tiles are cached under
`.astra/image-cache/tiles` with bounded cleanup.

Original-pixel tiling is guaranteed only for local paths and base64 image data.
External image URLs are sent directly because Astra does not download them, and
small animated GIFs are sent directly without tiling. An animated GIF that
requires tiling fails closed with a recoverable error instead of silently
sending a provider-downscaled image.
`/vision-tiles off` restores direct image sending, so DeepSeek may downscale
large images and lose detail. Other model profiles remain unaffected.

## Model time context

User messages carry a model-only timestamp such as
`<message_time>2026-09-14T00:30:00+08:00 周一</message_time>`. The weekday comes from
the same local date as the timestamp; replaying history keeps the original date.
Relative-date anchors also include a weekday derived from their saved date.
The visible time rail is unchanged, and output/history filters recognize both
old ISO-only markers and the weekday form. Existing sessions need no migration.
Use `current_time` when an exact current time or a different timezone is needed.

## Reasoning intensity

`/mode` controls reasoning intensity independently of persona, tools and output budgets.

```text
/mode
/mode low
/mode high
/mode xhigh
/mode max
```

The default is `high`. `xhigh` reasons more deeply than `high` at a higher token
cost, still well below `max`, which costs the most and can overthink. The
selected `reasoning_effort` is saved in `.astra/settings.json` and applies to the
next model request. The DeepSeek, Codex and Claude adapters send it: DeepSeek
runs `xhigh` as `high`, and a Codex model without `xhigh` runs at its highest
level below it. Other adapters keep their existing behavior and report that the
preference is not applied. Model configuration continues to own the output
budget. `/think` controls reasoning visibility separately.

Old saved `agent_mode` values migrate as `coding` → `max`, `chat` → `high`.
The coding/chat commands and `/mode code` selector are retired. Normal sessions
use native tools; tool exposure is no longer a user mode setting.
The status bar retains `TOOLS NATIVE` as a read-only runtime indicator after the
effort label, followed by the model, context usage and latest tool result.
The model stays in the summary while tools run. `Ctrl+L` opens session and
context details without repeating the model, reasoning indicator or latest
tool result. Narrow windows shorten secondary labels to keep the model,
context usage and expansion shortcut visible; Computer Use control state
takes priority in very small windows.

After each successful model request, the status bar shows its average output
speed in `tok/s` (`t/s` in compact layouts). This uses the provider's
`completion_tokens` divided by that request's elapsed time, including the
first-token wait and excluding tool execution. DeepSeek's count includes
reasoning tokens. The expanded `SPEED` row shows the token count and duration;
this is the latest completed request's average, not a live token estimate.
Missing usage does not produce an estimated rate. Switching models or sessions
clears the previous measurement.

## Structured clarification

During non-trivial coding work, Astra may pause to show a question card with
selectable options and a custom answer. The answer resumes the same agent turn.
Once the request is clear, Astra can populate the Working Plan panel, establish
Goal Mode, and begin work when the original request authorized implementation.

A choice is not a security approval. Filesystem, shell, MCP, workflow, and
host-execution operations still use their existing approval checks. On
messaging channels the interactive question tool returns a recoverable failure,
so the model can ask again in ordinary text instead of opening a hidden wait.

## Other Astra sessions

Astra sessions open on the same computer can hand each other work. Ask one in
plain words, for example "ask the session doing the slides which figure it
used", and Astra calls `peer_list` to find the other session and `peer_send` to
open a task for it. When the other session is idle it starts a turn on its own
with the request, marked as coming from another session rather than from you,
and reports back with `peer_task_update`: `working`, `input-required` to ask a
question, then `completed`, `failed` or `rejected`. The reply starts a turn in
the session that asked. A session that is busy reads its mail when its current
turn ends.

```text
/peers
/peers name <new name>
```

`/peers` lists the open sessions and this session's open tasks with them.
A session is named after the start of its first request; `/peers name` renames
it, and the name survives reopening the session. Mail waits for a closed session
until it is opened again.

Each session works with its own tools and permissions, so a session in YOLO mode
does the requested work without approvals, as it would for your own request. A
request from another session never changes what you asked this one to do. To
keep two sessions from talking forever, a task holds at most 20 messages, a
finished task takes no more, and a session opens at most 20 tasks an hour. The
directory, mailbox and task board are one SQLite file, `.astra/peers.db`.

## TUI themes

Use `/theme` to list the Ink TUI themes. `/theme hermes`, `/theme classic`,
`/theme nord`, `/theme dracula`, `/theme solarized`, and `/theme gruvbox`
switch immediately. `hermes` is the default warm gold-and-cream skin modeled
after Hermes CLI; `classic` preserves the original bright ANSI styling. The
selection is saved separately in `.astra/tui-settings.json`.

## Durable tasks

Every user turn is journaled to `.astra/tasks.db` using SQLite WAL.
LLM/tool boundaries create checkpoints, completed tool results are reused after
a restart, and tools left in an uncertain in-flight state are never replayed
automatically.

```text
/tasks
/tasks <task-id>
/resume <task-id>
/cancel [task-id]
```

The Ink TUI keeps reading commands while a task runs. The first `Ctrl+C`
requests a durable cancellation; pressing it again forces the process to exit.
Resume is restricted to the original session so its conversation checkpoint is
available.

YOLO can be changed while a reply is streaming or tools are running:
`/yolo` toggles it, `/yolo on` and `/yolo off` set it explicitly, and
`/yolo status` reports the current setting. `Ctrl+Y` also toggles it from
approval panels, questions, and tool details without submitting the input draft.
The input bar displays `YOLO` while enabled. Turning it on releases pending
approvals that support YOLO with a one-time decision; mandatory permission
boundaries still apply. Turning it off restores approval checks at subsequent
tool boundaries, including running Team members. It does not cancel a tool
that has already been authorized or erase separately granted session permissions.
YOLO is available in Work and Minimal modes. Commands that cannot run during a
reply show an explanation; `/help` lists the available controls.

Verbose tool output is collapsed in terminal history by default. Run
`/tool <id>` to open a numbered result, or press `Ctrl+O` for the latest result.
The detail panel supports arrow keys and PageUp/PageDown; press Escape or
`Ctrl+O` to close it. `TUI_TOOL_COLLAPSE_CHARS`, `TUI_TOOL_COLLAPSE_LINES`, and
`TUI_MAX_TOOL_RESULTS` control the thresholds and retained detail records.
Slash-command suggestions use a fixed-height scrolling window so filtering does
not leak old menu rows into terminal scrollback. `TUI_COMMAND_MENU_ROWS` defaults
to 8; arrow keys still traverse every matching command.

Pasting an image file path into the composer preserves text already typed before
or after the cursor. Image paths stay separate from the question when converted
to `[Image #N]` placeholders and when submitted. Pasting does not send the message;
press Enter when the draft is ready.
