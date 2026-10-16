# Memory, history and learning

[Home](../README.md) · [Documentation](README.md) · [简体中文](zh-CN/memory.md)

Astra keeps a small core memory, searchable history and reusable skills. Each has a different purpose and lifecycle.

[Core memory and task state](#core-memory-and-task-state) · [On-demand history and learned skills](#on-demand-history-and-learned-skills) · [Memory commands](#memory-commands) · [Proactive Context Index](#proactive-context-index) · [Evaluating memory](#evaluating-memory) · [Importing Hermes history](#importing-hermes-history) · [Optional Hindsight service](#optional-hindsight-service)

## Core memory and task state

Memory has two layers. Core memory is global and intentionally small: `MEMORY.md`
and `USER.md` are pinned entries you can edit directly, with fixed hard limits.
Saved memories are the growing layer: records in `.astra/memory.db` that Astra
writes with its `memory` tool (`remember`), each with a one-line summary, the
conversation it came from, a date, and global or project scope (the current
checkout). An index of saved memories, one dated line each, newest first, at most
30 lines and 2,400 characters, is always visible after the core entries; Astra
reads a memory's full text when it needs more than the summary. Only memories
for all projects and for the current one are listed; superseded and expired ones
are not. Records from before saved memories are listed only when they are
preferences or facts about you. When more memories qualify than fit, the ones
saved or used most recently are listed. Used means recalled into a turn, found
by a search, or read in full; listed lines stay in date order, so the index changes only when
a memory enters or leaves it. `read` shows how often a memory was used, and
`/memory` counts saved memories unused for 90 days.

In Work mode the core entries and the index reach the model as a separate
standing-memory copy, sent once and again only when they change, so they are
not repeated in every turn's context and a change shows on the next turn;
restricted modes add them to the current turn's context. The persisted user
message remains unchanged.

Durable execution uses `TaskRun → Step/TaskEvent` in `.astra/tasks.db`.
Every request receives an independent TaskRun with checkpoints, tool results,
blocking state, cancellation, and verification evidence. Use `/tasks` to inspect
the execution journal. Long-horizon work is opt-in through `/goal`, which adds
independent verification and bounded automatic continuation without guessing
intent from ordinary conversation.

Coding journals record what commands actually did: terminal status, exit code,
and bounded output. A successful command or file readback does not certify a
source change; `/tasks` shows these receipts as **UNVERIFIED** until assessed.
Background and interrupted executions remain distinct from completed commands.
The model should report relevant checks and their limits without repeating checks
just to dismiss a reminder. Goal verification includes command arguments and
both ends of long output; a completion verdict with empty evidence cannot finish
a goal. The verifier remains a model judgement, not a guarantee of correctness.

Routine requests do not inject a duplicate task ID, `running` label, or original
question. A compact `<task-state>` is included only when the journal contains
recovery, blocking, scheduling, or tool progress worth carrying forward. It is
capped at 1,000 characters, omits LLM bookkeeping, and prioritizes uncertain tool
outcomes over recent successes. This uses actual task state, not input keyword
matching or an extra model call. The turn's context stays frozen across tool
iterations; `/tasks`, checkpoints, and explicit resume retain the full journal
and original objective.

Session-isolated working memory in `.astra/memory.db` is now injected as
small `<conversation-state>` only: temporary constraints, open questions,
assumptions, and turn notes. Legacy goal/plan/progress/artifact fields and plan
APIs remain readable for compatibility, but are no longer execution truth and
are not injected into the prompt.

Always-visible memory has exactly two direct Markdown sources: `MEMORY.md`
(hard limit 2,200 characters) and `USER.md` (hard limit 1,375 characters).
Writes that exceed either fixed limit are rejected, and a second read-side guard
prevents legacy or manually edited oversized files from expanding the model
prompt. There is no Auto Core and no configurable extra L0 budget.

Structured historical memory is stored locally in `.astra/memory.db` and does
not require Hindsight. Evidence-backed preferences, user facts, episodes, task
references, and observations retain provenance, confidence, and lifecycle
metadata. Core Markdown and the saved-memory index are always visible. Other
records reach the model in two ways: automatically only through the Context
Index (`session` or `all` mode, below), and on demand when the model calls
`memory(action="search")`. With the Context Index off, nothing is recalled into
a turn. Search uses the Context Index's query rules (conversational words
dropped, short Chinese words kept whole, a relative date such as 上周 read as a
time window) and lists up to eight current memories for all projects and the
current one, best match first, each with its date. When no memory contains two
of the query's words, memories containing any one of them are listed.

Personal facts and corrections are written through the model's memory tools or
explicit `/memory` commands. When something changes, Astra revises the memory:
the new one supersedes it, and the old one is kept, marked superseded with an
end date, so its history stays readable with `/memory timeline`. The conversation
shows each change as it happens (“已记住”, “已更新记忆”, “已忘记”, with the id), and
a turn whose Context Index recall includes saved memories says how many. If a turn has read a web
page, a browser page, an MCP result, or content delivered by a subagent, another
session or a background run, saving a memory in it needs your approval: such
text can carry instructions written to plant themselves in memory. Astra does not turn conversation sentences into facts
using patterns such as “I like”, “remember”, or “change to”. Optional tool-authored
retention is off by default (`MEMORY_AUTO_RETAIN=0`); enabling it admits only tools
that explicitly provide attributable evidence from successful calls, and no current
tool provides it, so enabling it records nothing yet. Diagnostics
show the active runtime setting. Old `conservative` / `evolving` mode values still
load, but both use this restricted tool-evidence policy.

Memory maintenance, run by `/compress` and when Astra exits, only expires explicit deadlines and retires exact
observation duplicates with matching provenance, scope and lifetime. It preserves
the original records, does not increase confidence for repetition, and does not
promote temporary episodes into permanent facts. Existing memories are not
rewritten on upgrade; use `/memory inspect`, `correct` and `forget` to review them.

Memory consolidation reviews new conversations while Astra is idle and proposes
changes. When the desktop or terminal app has
had no turn for 10 minutes and you have sent at least four messages since the
last review (at most every 30 minutes), DeepSeek Flash reads those messages
(your own in full, the assistant's shortened) with what memory already holds,
and proposes up to five changes: remember a lasting fact or preference you
stated, revise a memory a later statement changed, or forget one you withdrew.
Each proposal quotes the message it rests on; only your own messages count as
evidence, and secret-looking text is dropped. A notice says how many are
waiting. `/memory proposals` lists them, `/memory accept <n|all>` applies them
as ordinary saved-memory changes with that message as the source, and
`/memory dismiss <n|all>` drops them. The first review reads only the last
three days. Conversations are sent to DeepSeek and need a DeepSeek key; the
archive holds only your messages and the assistant's replies, never tool
output, and Minimal, bar and installed local modes are not recorded. Consolidation has
three modes. `review`, the default, only proposes: nothing changes until you
accept. `auto` applies remember and revise proposals at once; each change shows
as an ordinary memory notice marked 记忆整理, a revised memory keeps its old
version for `/memory timeline`, and `/memory forget <id>` undoes a new one.
Forget proposals still wait for you. `off` stops the reviews.
`/memory consolidation off|review|auto` sets the mode (an earlier `on` reads as
`review`), and `now` runs a review at the next idle moment.
Semantic recall treats ordinary English words in Chinese questions as soft terms;
explicit literals and code identifiers still constrain matching results.

Hindsight is an optional extra source for search; nothing from it is added to
a turn automatically. When it is enabled, `memory(action="search")` sends it the
same words and lists its results after Astra's own, marked historical and
non-authoritative, without the ones that repeat a memory already listed. A
service that fails or takes more than five seconds leaves Astra's own results
and a note saying so. Search is the only way to read Hindsight's memories; its
`hindsight_reflect` (a synthesized answer) and `hindsight_retain` tools remain
available. Current user statements, Core
Markdown, concrete TaskRun results, Goal state, and temporary conversation state
always take precedence over recalled records.

## On-demand history and learned skills

The model can use `session_search` to find earlier conversations, fixes and
environment observations when relevant. Archives persist after the terminal
closes. They are not copied into always-visible memory. See
[local history retrieval](local-history-retrieval.md) for filters,
bounded expansion and stale-evidence handling.

Reusable procedures use the skill library. Only its catalog is injected;
`skill_view` reads full instructions on demand. The model's own summaries and
user-added skills have separate ownership. `/learn review` starts a read-only discussion of the next automatic-skill batch;
user-selected edits and optional execution checks happen on a subsequent turn. See [skill learning](skill-learning.md) for migration,
cursor progress, protections and undo.

## Memory commands

```text
/memory
/memory remember <stable agent or environment fact>
/memory remember-user <stable user profile or preference>
/tasks [id]
/memory working temporary_constraints <temporary constraint>
/memory inspect [query|id]
/memory timeline <id>
/memory why
/memory correct <id> <replacement text>
/memory forget <id>
/memory clear-working
/memory proposals
/memory accept <n|all>
/memory dismiss <n|all>
/memory consolidation [off|review|auto|now]
```

The model can use the `memory` tool to maintain the same stores. Credential-like
content is rejected. `MEMORY.md` is limited to 2,200 characters and `USER.md` to
1,375 characters. Core additions and removals do not require approval; the model
is told to remove a core entry only when you ask, or when a file is full and the
entry is obsolete, duplicated or superseded. Saved memories are added and revised without
approval (except after outside content, as above); the model may forget one only
when you ask, and you approve each forget. You can also use `/memory forget <id>`
or select a deletion in `/memory review`.

## Proactive Context Index

The Proactive Context Index is Astra's **native, optional memory recommendation
pipeline**. Query planning, reciprocal-rank fusion (RRF), diversity selection
(MMR), evidence budgets, and feedback all run in Astra's Python components.
It requires no external memory framework, memory server, graph database, or
auxiliary LLM. Existing embedding backends remain optional. It is **off by
default**, and ordinary Markdown core memory stays unchanged. It is the only
automatic recall of structured records: in `session` and `all` modes it adds
them to a turn, and with it off, records reach the model only through the
saved-memory index and the model's own searches.

```text
/context-index session   # Astra's local records and session history
/context-index all       # Also consider cached Activity history
/context-index off       # Restore the existing memory path
/context-index why       # Explain the last selection and its cost
/context-index feedback R1 useful      # Explicit feedback on a displayed entry
/context-index feedback R1 irrelevant  # Lower its priority for similar tasks
/context-index feedback R1 outdated    # Ranking feedback; does not rewrite facts
```

Chinese questions are split at particles, pronouns and question words, so a
two-character word stays whole (牙医是哪天 searches 牙医). Session history keeps a
character-pair index for these words, so finding one does not scan the whole
archive; Activity history still searches three-character pieces. Relative
dates such as 上周, 三天前, 上个月, 9月27号 or "last week" become a date range
instead of words to find.

Use [native memory recommendation](native-memory-recommendation.md) for
retrieval behavior, budgets, diagnostics, feedback and quality checks, and
[platform configuration](context-index-platforms.md) for optional embeddings.
The initial preview defaults to 900 characters (at most 2,000) and a 500-token
estimate. Context Index can return no recommendation; retrieved notes remain
historical evidence, not current instructions or proof of a fact.

## Evaluating memory

`agent/evals/memory` measures what memory puts in front of the model, using
synthetic worlds (Chinese, English and mixed) seeded through the public memory
and archive APIs, in temporary state that never touches your installation:

```bash
python -m agent.evals.memory.run --mode off,session,all --out memory-eval.json
```

The default offline run uses no model. It builds the agent as the backend does,
runs multi-turn conversations, and reports evidence recall (in the newest
context and anywhere visible), false and stale injection, dated items,
injected and core-memory tokens per request and per conversation, prefix
stability and preparation latency, by probe category. `--live --model <key>`
asks a real model and scores its answers by keywords, dates and abstention,
including whether a preference stated in one session is remembered in the next.
`--consolidation` runs one memory review per world that has expected changes,
with some records left out so the archive is ahead of memory, and reports
which expected changes were proposed, whether stale memories were revised in
place rather than duplicated, and how many other proposals were made.

## Importing Hermes history

The importer defaults to `~/.hermes/state.db` and the repository-local
`.astra/sessions.db`. Preview an import with explicit paths before writing:

```powershell
# Windows reading a Hermes database in WSL
python scripts/import_hermes_history.py --dry-run `
  --source-db "\\wsl.localhost\Ubuntu\home\your-name\.hermes\state.db" `
  --target-db ".astra\sessions.db"
```

```bash
# macOS/Linux
python3 scripts/import_hermes_history.py --dry-run \
  --source-db "$HOME/.hermes/state.db" \
  --target-db ".astra/sessions.db"
```

After reviewing the preview, repeat without `--dry-run`. `HERMES_DB` and
`ASTRA_SESSIONS_DB` provide the same path overrides for scheduled runs; explicit
`--source-db` and `--target-db` arguments take precedence.

## Optional Hindsight service

Hindsight runs its own LLM for retain, consolidation and reflect. Astra's
`HINDSIGHT_PROCESSING_MODEL=deepseek-flash` is a diagnostic label only; it does
not change the server. On the Windows/WSL host running Hindsight, update its
actual service environment (for example `profiles/main.env`):

```dotenv
HINDSIGHT_API_LLM_MODEL=deepseek-flash
HINDSIGHT_API_RETAIN_LLM_MODEL=deepseek-flash
HINDSIGHT_API_CONSOLIDATION_LLM_MODEL=deepseek-flash
HINDSIGHT_API_REFLECT_LLM_MODEL=deepseek-flash
```

Keep the existing DeepSeek provider, endpoint, credentials and bank settings.
Reload/recreate the service with this environment, including any separate
workers, and check its effective configuration. A plain container restart does
not import changes to a Compose env file. Per-operation or bank-level model
overrides must also agree; see the [Hindsight configuration reference](https://hindsight.vectorize.io/developer/configuration).
Pulling Astra alone does not update a separately deployed Hindsight service.
