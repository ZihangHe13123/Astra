# Provider completion and client recovery

These adaptations use Astra's existing runtime and session/event stores. They
were informed by dsh 0.1.5-rc.2 at
[`fb2c4b9`](https://github.com/deepseek-ai/deepseek-harness/tree/fb2c4b9e698e30edb738bca4cf0618587db7d203).

## Completion and tools

OpenAI-compatible streams need an explicit completion signal. Empty or
reasoning-only responses get one visible recovery request with concrete guidance.
It shares the original overall deadline and has no additional transport retries.
SDK retries are disabled because Astra already owns the retry budget.

Text already delivered is never automatically replayed after a broken connection.
After a completion signal, the adapter allows at most one second for a usage
trailer (and respects shorter configured deadlines). Duplicate terminal frames
cannot append tool arguments twice. Calls are assembled in wire-index order;
empty/null identity fields do not overwrite known values, while conflicting
identities fail explicitly. Truncated batches, duplicate IDs and unclosed JSON
are rejected before execution, including nested JSON that a repair might otherwise
misinterpret. Existing bounded tool recovery and failure circuits remain active.

Usage from a successful semantic recovery includes both requests for accounting,
while context occupancy and token calibration use only the last request size.

## System prompt prefix reuse

The official `api.deepseek.com` route with `deepseek-flash` enables system updates
in history. Custom routes require the explicit `system-prompt-in-history`
capability. Set `ASTRA_SYSTEM_PROMPT_HISTORY=0` to disable this optimization.

A changed prompt appends a complete system message after the existing request
history. Equal prompts add nothing. The optional, versioned
`system_prompt_projection` session metadata records the original system text,
update boundaries and prefix digests; canonical user/tool history is unchanged.
Rewritten history, compaction, a changed model/credential/tool schema, cleared
prompt, unsupported route or excessive update growth starts a new prefix. If
retaining old system text would exceed the input budget, the request uses the
current leading system prompt instead. Request-local content is not stored in
this metadata, and image byte hashes are not recorded in session history.

This saves repeated prefill work when a prompt changes. It does not shorten the
model's reasoning output. The benefit depends on the provider's cache state and
the stability of the rest of the request.

## Image references

Official DeepSeek vision requests can reuse identical preprocessed image bytes
through its [Files API](https://api-docs.deepseek.com/guides/files_api/). The local
index defaults to `~/.cache/astra/deepseek-files.db`, with file mode 0600. It stores
only content/credential-scope digests, file IDs and expiry. Override the path with
`ASTRA_DEEPSEEK_FILE_CACHE`, or disable reuse with `ASTRA_DEEPSEEK_FILES=0`.

The image batch has a three-second upload deadline. Failure falls back to the
original inline request and disables uploads for 120 seconds for that provider
instance. Invalid file references rejected before streaming get one inline
fallback. Concurrent requests on one provider share upload work; separate
instances reuse completed entries through SQLite. Entries refresh within 60
seconds of expiry, local storage is bounded to 2,048 records, and uploaded files
expire after one hour. Astra never deletes unrelated remote files.

The existing pixel preprocessing, tile selection, image limits and conservative
token estimates are retained. Official `high`, `original` and `auto` detail all
keep original pixels; `low` continues inline to preserve server downsampling.
External image URLs are not downloaded by the upload cache.

## Model catalog and context settings

The Codex account catalog filters models by the client version Astra reports.
Refreshing a provider updates the active model's context budget as well as its
menu entry. An unlisted model entered manually uses the provider's conservative
capacity until discovery supplies metadata.

Per-model entries in `.astra/models.yaml` may set `context_window` to explicitly
override the discovered default and `auto_compact_token_limit` to choose when
history compaction starts. For example, `512000` and `450000` use a 512K window
and compact at 450K. These settings survive catalog refreshes and apply only to
that model; output space is always reserved. `CTX` measures occupancy against
the compaction budget. Choose a window supported by the account/model: local
configuration does not increase the server's maximum. Neither setting is sent
as an API generation parameter.

## Client and MCP recovery

The backend sends an IPC hello before optional initialization. A silent process
gets a notice after three seconds and a deadline after fifteen; this deadline
ends on the first valid response and does not limit subsequent model loading.
Recovery does not automatically spawn repeated replacement processes.

The TUI retains an event-delivery ledger across backend reconnections. Only
applied journal events acknowledge replay cursors; volatile text cannot advance
them. Duplicate/overlapping replay is idempotent. A completed turn supersedes its
old transient controls. Sequence gaps request at most one pending replay and
hold the applied cursor before the gap until the final replay page arrives. Live
events received during recovery cannot make missing events look like duplicates.
Each TUI instance has its own replay scope so another instance's terminal events
cannot clear its active task. Event handler and diagnostic failures are isolated.
The event database adds a scope column without rewriting existing rows. Model
text and raw tool output remain outside the lifecycle journal; replay restores
control state, while session history remains the source of completed content.

Text bursts are combined for at most 32 ms before Markdown parsing. Role changes,
tools and terminal events flush synchronously. History output is memoized and
keeps Ink's append-only contract. Mutable previews render a bounded tail based on
terminal height; the full text still commits to terminal scrollback.

MCP listings collect all pages before atomically replacing a server's tool group.
Repeated/invalid cursors, duplicate or colliding names, malformed pages and the
100-page limit fail explicitly. Refresh has a shared timeout and preserves the
previous valid tools and connection on failure. Invocation failures still use
the established reconnect path. Missing tools and common file/schema failures
include concrete recovery instructions rather than an unchanged-call retry.

## Verification

Deterministic tests cover stream completion and deadlines, tool assembly, prompt
updates, image scope/expiry/fallback, MCP pagination, event replay and rendering.
Use isolated provider requests to measure cache reuse or transfer savings; record
request conditions and cleanup with the result. Such checks do not establish
general latency gains or replace acceptance on a specific site or application.
