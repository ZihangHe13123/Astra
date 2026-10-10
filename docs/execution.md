# Tool execution and filesystem access

[Home](../README.md) · [Documentation](README.md) · [简体中文](zh-CN/execution.md)

Configure where tools run, which files they can access and how execution is approved.

## Team status and retained workers

Teams in the same session can be inspected with `team(action="status", team_id=...)`
without a separate resume. Sending work or configuring a Team automatically adopts
an inactive earlier turn using the same ownership and workspace checks as explicit
`resume`; an active parent cannot be displaced. Reads never transfer control.

Status is compact by default. `detail=true` on `team` or `team_wait` includes full
spawn context, task text and the available episode history. `team(action="create",
keep_alive_limit=5)` or `team(action="configure", team_id=..., keep_alive_limit=5)`
sets this Team's idle capacity immediately and durably. Omission inherits
`ASTRA_TEAM_KEEP_ALIVE_LIMIT` (default 2); zero disables new idle admissions.
Reducing capacity does not evict members already idle.

Worker `lifecycle` records identify the completion reason, actual limits, last idle
entry/deadline, active and idle monotonic seconds, and wall elapsed seconds.
`observed_at` identifies the diagnostic snapshot; active/idle snapshots are saved
at transitions. Host sleep can advance wall time without advancing monotonic time.
After a crash, `recovered_at` is the detection time and timings remain the last
durable observation, not an invented time of death. Reasons survive process-log
pruning. `team_restart` clears old decisions and starts a new conversation from a
bounded checkpoint; it does not restore the old model conversation.

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
the limit. A failure whose text is longer than the inline limit (a failing test
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

- `TOOL_MAX_INLINE_CHARS=12000` keeps a bounded head/tail preview in durable
  history and writes the complete result to `TOOL_RESULT_DIR`. Fresh text up to
  `TOOL_MAX_FRESH_RESULT_CHARS=100000` is shown in full to the immediately
  following model iteration, then falls back to the durable preview. Opaque
  base64/data-URL payloads are never inlined as text noise.
- `TOOL_FAILURE_THRESHOLD=3` opens a per-tool circuit after equivalent
  consecutive failures, then performs one final synthesis with tools disabled.
- `PROMPT_CACHE_STABLE_TOOLS=1` keeps one provider-visible tool manifest for
  the work session. Tool budgets and circuits remain enforced at execution
  time without deleting schemas between ReAct iterations.
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
