# Loading tools on demand

Status: shipped and on by default (`ASTRA_TOOL_DISCLOSURE=deferred`; `full` sends every tool as
before). User-facing notes are in [docs/execution.md](../docs/execution.md). Canvases are offered the
same way ([canvas.md](canvas.md)).

## 1. Summary

An ordinary conversation is sent a small core set of tools. The others are listed by family in a
short catalog; when the model needs one, it fetches the definitions with `tool_search` and runs the
tool through `call_tool`. The tool list sent to the model never changes within a conversation, so
the cached prefix holds.

## 2. Why

Tool definitions open every request and sit at the front of every provider's cached prefix. With
every tool sent, they were most of the fixed prefix, and most conversations use only a few of them.
The earlier keyword-based exposure (`TOOL_PROGRESSIVE_EXPOSURE`, tool groups activated mid
conversation) saved tokens but changed the tool list, which invalidated the cache each time; it is
off by default for that reason. Skills already follow the pattern used here: a name and one line up
front, the full text when it is used. Fetched definitions are data, never instructions.

## 3. Measurements before the change

First request of a real backend with a local fake model, estimated with
`agent/runtime/token_estimator.py`:

| Part | Count | Estimated tokens |
|---|---|---|
| Tool definitions (every default tool) | 107 | ~28,900 |
| Tool definitions (deferred: core set + `tool_search` + `call_tool`) | 29 | ~11,300 |

Usage is concentrated: shell commands, reading and editing files, text search, skills, processes,
web search and session search account for most calls. Browser, computer use, agent teams,
documents, git, scheduling, peers and skill management are used in few conversations each, so they
are the deferred families.

Existing rules this design keeps:
- The tool list is frozen when a conversation starts and saved with it; it is rebuilt only on a
  model change, `/reload`, an explicit guidance refresh or a mode switch.
- Execution does not check the frozen list (`ToolRegistry.execute`), so a fixed dispatcher can run a
  tool whose definition was not sent.

## 4. Design

### 4.1 Tiers

| Tier | Definition sent with each request | Runs | Examples |
|---|---|---|---|
| core | yes | yes | files, shell, search |
| deferred | no; named by family in the catalog | through `call_tool`, once `tool_search` has handed it over | browser, computer use, git, teams |
| hidden | no, and not in the catalog | no (existing aliases aside) | existing hidden tools |

`CORE_TOOLS` in `agent/runtime/tools/disclosure.py` holds the core set; a conversation kind can add a
few (`ToolRegistry.add_core_tools`). In deferred mode every other default tool, MCP tools included,
is deferred. Whether a definition is sent (`ToolRegistry.is_deferred`) is decided apart from whether a
tool may run (`expose_by_default`).

### 4.2 Core set

`read_file` `write_file` `edit_file` `apply_patch` `search_text` `search_files` `stat_file`
`execute_shell` `execute_python` `process_poll` `process_read` `process_cancel` `process_list`
`ask_user_question` `plan_update` `goal` `memory` `skill_view` `session_search` `activity_search`
`context_open` `search_web` `web_extract` `fetch_url` `read_image` `current_time` `delegate_task`,
plus `run_code` and `read_image_tiles`, which follow their own rules (Code Mode, the vision-tile
preference).

### 4.3 Catalog

The catalog is part of `tool_search`'s own description, and the family names are the enum of its
`family` parameter, so it is frozen with the tool list and the system prompt is untouched. One line
per family says what it is for, without tool names: names are given only when tools are loaded, so
the model does not skip loading. A family appears only when one of its tools is registered (canvases
only on the desktop, computer use only on macOS). The whole catalog stays under the 2,048-character
description limit of the Claude Code provider.

```
- browser: drive the user's browser: open, read, click, fill and upload on web pages
- git: repository status, diffs, commits, branches and push
...
- mcp:<server>: tools of the <server> MCP server
```

### 4.4 The two dispatch tools (core)

- **`tool_search(family?, names?, query?)`** returns up to 20 full definitions (the JSON schemas as
  registered) as its result, which starts with `Loaded tools: ...`.
  - The loaded set is not stored separately: it is read back from that line in the results of
    `tool_search`, `call_tool` and deferred calls only, so page or file content cannot fake it.
    Restores and branches follow it; once compaction replaces those results with a summary, the
    loaded set is empty again.
- **`call_tool(name, arguments)`** runs a loaded deferred tool.
  - A tool not handed over in this conversation does not run: the result carries its definition
    and asks for the call again.
  - Arguments are validated against the definition, with errors per field.
  - A valid call takes the same path as a native one, resolved right after the assistant message is
    recorded: approvals, "always allow" rules, repeat guards, circuits, events, the interface and
    logs see the real tool name (for example `browser_click`). History keeps the `call_tool` call as
    sent; its arguments are redacted by the target tool's rules.
- A deferred tool called directly by name does not run either; the result hands over its definition
  with the `call_tool` form, which also loads it. A refused dispatch does not count as that tool's
  failure.

### 4.5 Cache and history rules

- Loading never changes the tool list. Definitions travel only in tool results, appended; history
  already sent is not rewritten (`agent/runtime/AGENTS.md`).
- After compaction the loaded set is empty and the model searches again when it needs a family.
- A model change, `/reload` or a mode switch rebuilds the list under the existing rules.

### 4.6 Providers

The two tools are ordinary function tools for OpenAI-compatible providers and Codex, and ordinary
bridged MCP tools for the Claude Code provider, whose own tool search stays off.

### 4.7 Core set per conversation kind (fixed when it starts)

- Personal thread: adds `jobs` and `peer_send`.
- Messaging channels: add `channel_send_file`.
- Minimal, Bar and Local modes and read-only command flows keep their existing allow-lists.
- Subagents keep their existing, already small, allow-lists.

## 5. Costs and risks

- The first use of a family in a conversation costs one extra model call and appends its
  definitions (1–3k tokens) to history, cached from then on.
- Arguments wrapped in `call_tool` are one level deeper; precise validation messages help smaller
  models recover.
- The model may overlook a deferred tool; each catalog line states its use plainly.
- Approvals and the interface must show the real tool name; this is tested.

## 6. Live comparison

DeepSeek Flash, five scripted tasks (averaging a CSV, creating and listing two scheduled jobs over
three turns, the latest git commit, checking a Markdown document's structure, listing other
conversations), each in a fresh isolated backend, once per mode. Browser, computer use, calendar
and mail were hidden in both modes, since they reach real accounts and screens, so full mode sent 81
tools here. Numbers are the provider's reported usage.

| Mode | Requests | Prompt tokens | Cache hits | Cache misses | Per request | Correct |
|---|---|---|---|---|---|---|
| full | 25 | 725,144 | 595,840 | 129,304 | 29,005 | 5/5 |
| deferred | 23 | 390,857 | 320,768 | 70,089 | 16,993 | 5/5 |

- Deferred mode used 46% fewer prompt tokens and 46% fewer cache-miss tokens, with every answer
  correct.
- Each task that needed a deferred family called `tool_search` once; the scheduling task loaded its
  family once and reused it in the later turns.
- No deferred tool was called directly or before it was loaded.
- One run per task and mode: request counts vary, and this is not a statistical result.

Not measured: the Claude Code and Codex providers, and the browser and computer-use families.

## 7. Open

- Shorter descriptions for the longest core tools (target: the core set under 8k tokens).
- Optionally, promote loaded tools into the native list at a compaction boundary, where the prefix
  is rebuilt anyway; use the same mechanism for subagents and channels.
