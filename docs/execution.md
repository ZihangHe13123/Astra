# Tool execution and filesystem access

[Home](../README.md) · [Documentation](README.md) · [简体中文](zh-CN/execution.md)

Configure where tools run, which files they can access and how execution is approved.

## Subagents

`delegate_task` starts subagents, one or a batch (`tasks`). Each has a name in its
conversation, given with `name` or generated (`explorer-1`, `worker-2`), and
stays for the whole conversation: after its report it waits idle, holding no
memory or slot, and `delegate_send(to=<name>, message=...)` gives it more work,
which it does from its saved conversation, so its earlier requests start the next
one unchanged. A message to a working subagent reaches it before its next step.
One whose last step's outcome is unknown, such as an interrupted write, is not
picked up by a message; `recover=true` brings it back read-only. `delegate_cancel`
stops its current work and `close=true` ends it for the conversation;
`delegate_list` shows them all and `delegate_wait` waits for a report or message (given
`subagents`, for those subagents' reports only, while a message from any subagent still comes
through; other reports stay for normal delivery). A subagent can be closed after a backend restart
too, by name, without being run again.
Later turns of the session reach the subagents earlier turns started.

A subagent writes to its parent with `delegate_send(to="parent")`; that arrives
in the parent's conversation between its steps and wakes a parent waiting for
it. Subagents given the same `group` may also write to each other by name; one
without a group writes only to its parent, and a subagent reads messages only
while it works. A worker with `isolation=worktree` keeps its own worktree,
`.astra/delegate-worktrees/<id>`, from one assignment to the next: a clean one is
removed after an assignment and made again at the same path.

Subagents have no turn limit. A run ends when its model answers without a tool
call, when it is stopped, or when its time limit passes (`timeout`, 1,800
seconds by default, 14,400 at most, per assignment, counted while it holds a
slot). Sixteen run at once (`ASTRA_DELEGATE_CONCURRENCY`). A subagent's history
is compacted like the main agent's. In a Work session the parent does not wait
for them at the end of its turn; see
[When background work finishes](session-lifecycle.md#when-background-work-finishes).

Agent Teams of earlier versions (`team`, `team_spawn`, `team_send`, `team_wait`,
`team_inbox`, `team_task`, `team_restart`) and `delegate_poll` were merged into
these tools. A conversation whose tool list has them still runs them; new
conversations are not offered them.

Worker `lifecycle` records identify the completion reason, actual limits, last idle
entry/deadline, active and idle monotonic seconds, and wall elapsed seconds.
`observed_at` identifies the diagnostic snapshot; active/idle snapshots are saved
at transitions. Host sleep can advance wall time without advancing monotonic time.
After a crash, `recovered_at` is the detection time and timings remain the last
durable observation, not an invented time of death. Reasons survive process-log
pruning. A subagent without a saved conversation starts again from a bounded
checkpoint of its transcript (`delegate_send` with `recover=true`).

## Shell results and full output

Host `execute_shell` uses Bash with `pipefail`, including WSL. A command such as
`pytest | tail -n 30` retains pytest's failing pipeline status. An explicit
`set +o pipefail` restores last-command pipeline semantics when needed. This does
not enable `errexit` or change Docker and persistent Minimal Bash shell options.

Process receipts contain `output_reader` with a callable `process_read` (or
`delegate_read`) argument object. Truncated foreground output also retains a
process handle. Use it to page the full output without adding artifact directories
to the file tool's allowed roots.

A foreground run that is stopped at the sandbox time limit returns what it wrote
before the stop, marked as a partial result, together with how to rerun it past
the limit. A run stopped at the limit, by `process_cancel` or by stopping the
turn is stopped together with the processes it started (its process group on
macOS and Linux, its process tree on Windows). A process that moved to its own
session is not reached, and a timeout result says when one still holds the run's
output. A Minimal `bash` command that exceeds its limit returns what it printed;
the shell is reset, as it is when a `bash` call is cancelled, and the next result
says so. A failure whose text is longer than the inline limit (a failing test
run, for example) is shortened to its start and end, and the full text is saved
with its path in the result.

[Minimal Bash environment](#minimal-bash-environment) · [Host filesystem access](#host-filesystem-access) · [Tool policy and tracing](#tool-policy-and-tracing)

## Minimal Bash environment

Minimal Mode's `bash` tool keeps one persistent interactive shell, so working
directory and exported variables survive between calls. On Windows that shell
runs as WSL Bash; on macOS/Linux it runs as native Bash and reports the neutral
`posix` environment.

Each command has a 300-second timeout by default. `ASTRA_PERSISTENT_BASH_TIMEOUT`
is the preferred cross-platform override and takes precedence.
`ASTRA_WSL_PERSISTENT_TIMEOUT` is the legacy fallback, consulted only when the
preferred variable is unset; despite its name, it remains accepted for backward
compatibility on both WSL and native hosts.

## Host filesystem access

Python and shell execution use Docker isolation by default. Use `/sandbox` to
inspect the active backend, `/sandbox off` for guarded host execution (needed
for commands such as `wsl.exe`), and `/sandbox on` to return to Docker. The
choice is applied immediately and saved in `.astra/settings.json`.
Host execution still keeps the local timeout, output limit, and dangerous-command
checks; it is not equivalent to unrestricted shell access. The
`read_file`, `search_files`, `write_file`, and `edit_file` tools instead use an
explicit host-filesystem policy, so approved Windows drives and WSL UNC paths
can be accessed without exposing them to arbitrary sandboxed commands.

The current workspace is always read-write. Copy
`config/filesystem.example.json` to `.astra/filesystem.json` and replace its
portable `shared-files` placeholder with the host path you intend to expose.
Add extra roots as `ro` or `rw`; external roots should normally stay read-only.
`AGENT_FILESYSTEM_CONFIG` can select a different policy file.

The default Docker image is the small `python:3.12-slim` base image. For
Minimal `run_code` checks that need project dependencies or `pytest`, build the
shared development image and opt into it:

```powershell
.\scripts\build-sandbox-image.ps1
$env:ASTRA_DOCKER_IMAGE = "astra-sandbox:agent-system-dev"
```

On macOS/Linux, use the matching POSIX wrapper:

```bash
./scripts/build-sandbox-image.sh
export ASTRA_DOCKER_IMAGE=astra-sandbox:agent-system-dev
```

The development image mirrors the base dependencies and the `mcp`, `server`,
`tracing`, and `dev` extras from `pyproject.toml`. It also includes Git,
ripgrep, and curl. It exposes no Docker CLI, Node.js/npm commands, browsers, or
GUI tools; Pyright alone uses a private Node runtime supplied by its Python
package, and TUI checks remain host-side. Containing curl does not enable
networking: Astra still launches the sandbox with `--network none` by default.
A strict root `.dockerignore` sends only the sandbox Dockerfile and requirements
file to Docker, so project secrets, state, virtual environments, dependencies,
and unrelated source never enter the build context.

The same setting can be added to `.env`. Image building needs network access
once; containers still run with `--network none` unless the active sandbox is
explicitly configured otherwise. The project source remains mounted at
`/workspace`, so checks use the current working tree rather than a copy baked
into the image.

Windows example:

```json
{
  "roots": [
    {"path": "D:\\shared", "mode": "ro"},
    {"path": "\\\\wsl.localhost\\Ubuntu\\home\\user", "mode": "ro"}
  ]
}
```

macOS example:

```json
{
  "roots": [
    {"path": "/Users/your-name/shared-files", "mode": "ro"}
  ]
}
```

The same rule applies to `send_file_roots` in `.astra/channels.json`: use a
Windows path such as `D:\\allowed\\outputs`, or a macOS path such as
`/Users/your-name/allowed/outputs`. The committed channel example uses the
portable repository-relative `outputs` placeholder.

Paths outside the configured roots are rejected. A read-only root can never be
written or edited through the file tools.

## Tool policy and tracing

`AGENT_TOOL_POLICY` accepts `permissive`, `safe`, or `locked`. In safe/locked
mode a denied tool can be approved for the current process with
`/permissions allow <tool-name>`.

Tool execution uses the following guards and controls:

- `TOOL_MAX_INLINE_CHARS=12000` is the inline size of a tool's text output. An
  output up to twice that size, and no longer than
  `TOOL_MAX_FRESH_RESULT_CHARS`, stays in the conversation whole. A longer one
  is written in full to `TOOL_RESULT_DIR` and kept in durable history as a
  bounded head/tail preview of the inline size; up to
  `TOOL_MAX_FRESH_RESULT_CHARS=100000` characters of it are shown in full to
  the immediately following model iteration, and later ones get the preview.
  That fallback rewrites an earlier message, and a provider reads everything
  after a rewritten message again at the uncached price; below twice the
  inline size the preview saves too little to pay for that. A tool with its
  own limit follows the same rule. Opaque base64/data-URL payloads are never
  inlined as text noise.
- Old tool results stay in a conversation in use as they were read: a model
  request carries each one whole, or as the preview the history keeps. Clearing
  one would rewrite an earlier message, and a provider with a prompt cache reads
  everything after the first message that changed again at the uncached price.
  Live, until 2026-11-13, clears made 44% of what DeepSeek Flash read uncached
  and nearly a third of what Claude Code did, about 21k tokens re-read for every
  2k a clear took off. A long conversation is made shorter by history compaction
  alone, a summary in place of its older part. When no summary can shorten a
  conversation over its threshold, the superseded runtime and memory snapshots,
  kept only so that the cached prefix stays the same, are retired as a last
  resort; the conversation itself is untouched. The first request of a turn
  compacts at the model's compaction line. A request inside a turn compacts
  there too when the summary would take off at least a quarter of the line, by
  the compressor's plan and without writing it; what a compaction takes off is
  no longer read in each later request. When it would take off less, the
  request waits until the conversation is a third past the line, within what
  the model's window takes with room for the answer, so a turn over a history
  that was summarized already does not rewrite it each time it reaches the line;
  the next turn compacts at the line. A request that comes after a
  provider with a known cache lifetime has let the conversation go (Claude Code:
  an hour) compacts at the line, which then costs nothing more. A subagent's
  conversation rebuilt to fit a smaller budget has nothing cached to keep: there
  completed read steps are dropped and old read results cleared first, oldest
  first, down to `TOOL_RESULT_PROMPT_BUDGET_CHARS=24000` characters, keeping the
  `TOOL_RESULT_KEEP_RECENT=5` most recent.
- Idle compaction: a long Claude Code conversation is summarized before its
  cache goes. Between turns the backend checks the desktop's conversation and
  each chat's. One whose last turn has finished, whose last request carried
  `ASTRA_IDLE_COMPACT_MIN_TOKENS=100000` tokens or more, and whose cache nobody
  has read for 54 minutes (`ASTRA_IDLE_COMPACT_AFTER_SECONDS=3240`) gets a
  checkpoint summary. The summary is asked for by repeating that last request,
  as history compaction does, so the CLI reads it from the cache; never from a
  transcript, which would be read whole. A model change since that request
  skips it. The summary is kept beside the history, which does not change, and
  that request keeps the cache another hour. The next turn uses the summary
  only if it starts an hour or more after the last request that read the cache,
  the summary request included, and the history still begins with what was
  summarized: the summary then replaces that part and the rest stays word for
  word. Otherwise the summary is dropped and the conversation goes on in full.
  One summary per pause; a restart drops it. A turn that starts while it is
  being written interrupts it, since the model client may have one request
  slot, and one that has not come back in ten minutes is given up. If the CLI
  could not take the request turn by turn with Astra's cache breakpoint, it is
  not sent at all. It is sent only if the model's window holds it with room
  for the answer, sized as the provider reported the last request plus what it
  adds; it may pass the compaction line. When no summary is written for one of
  these reasons, a warning in the backend's log says why. A summary that came
  back counts as a read of the cache when it was asked for.
  `ASTRA_IDLE_COMPACT_MIN_TOKENS=off` turns this off. The text CLI and scheduled checks do not take part.
- `TOOL_FAILURE_THRESHOLD=3` opens a per-tool circuit after equivalent
  consecutive failures, then performs one final synthesis with tools disabled.
- `PROMPT_CACHE_STABLE_TOOLS=1` keeps one provider-visible tool manifest for
  the work session. Tool budgets and circuits remain enforced at execution
  time without deleting schemas between ReAct iterations.
- `ASTRA_TOOL_DISCLOSURE=deferred` (the default) sends a small core set (`CORE_TOOLS` in
  `agent/runtime/tools/disclosure.py`, plus a few per conversation kind) with
  `tool_search` and `call_tool`; `full` sends every default tool, as before.
  The other tools stay registered and run as before. `tool_search` lists them by
  family and returns full definitions as its result; `call_tool` then runs one
  under its own name, so approvals, repeat guards, events and the interface see
  that tool. Calling a tool whose definition was not handed over in this
  conversation does not run it; the result hands the definition over instead.
  The tool list never changes within a conversation (specs/tool-disclosure.md).
- With `PROMPT_CACHE_STABLE_TOOLS=0`, `TOOL_PROGRESSIVE_EXPOSURE=1` restores
  the legacy core/request-relevant group routing and `activate_tool_group`
  behavior. This reduces the first uncached prompt but causes more prefix-cache
  invalidations.
- `AGENT_TOOL_EXPOSE` and `AGENT_TOOL_HIDE` take comma-separated tool names.
  The first adds opt-in tools to the manifest: `conclave` (the `/conclave`
  command works without it), `notebook_execute`, `skills_list` (the prompt
  already lists skills in `<available-skills>`), `project_verifier_init`,
  `git_pull`, `git_reset`, `git_revert`, `extract_url` and `browser_extract`
  (`fetch_url` and `web_extract` read pages; the browser tools render them) and
  `search_status`. The second removes default tools.
  A tool named in both stays hidden. Hidden tools remain available to modes
  and workflows that list them explicitly.
- `AGENT_MAX_REACT_ITERATIONS=50` sets a coarse total-turn safety ceiling. Set
  it to `0` for no fixed ceiling; repeated-call, failure-circuit, prompt-budget,
  and cancellation guards remain active.

Set `AGENT_TRACE_ENABLED=1` to emit OpenTelemetry spans. When the SDK and OTLP
exporter are installed, `OTEL_EXPORTER_OTLP_ENDPOINT` selects a Phoenix or other
OTLP-compatible collector.
