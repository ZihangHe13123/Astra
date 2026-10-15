# Native memory recommendation

Astra implements this pipeline in `agent/runtime/context_index/`. There is no
new package dependency, external memory framework, memory server, or auxiliary
chat-model call. Optional semantic retrieval reuses Astra's existing embedding
backends. RRF and MMR are established retrieval algorithms implemented locally;
compact previews and on-demand evidence are implemented through Astra's existing
Context Index and `context_open` contracts.

## Components

| Component | Responsibility |
| --- | --- |
| `query.py` | Bounded task queries, CJK word splitting and character grams, shared lexical coverage, trivial-turn abstention, resume context, local-date windows |
| `lexical.py` | Literal-target constraints, output-destination recognition, and shared query-character coverage in SQL and fusion |
| `session_source.py` | Read-only FTS/LIKE and session-window retrieval, current-message exclusion |
| `activity_source.py` | Independent text and warm-vector channels over cached Activity |
| `record_source.py` | Read-only access to Astra's structured records; pinned/conflicted records are excluded |
| `semantic_index.py` / `semantic_indexer.py` | Model/archive-scoped vectors and bounded incremental background maintenance |
| `semantic_reader.py` | Warm vector recall followed by canonical revision, lifecycle and query-window checks |
| `query_embedding.py` | One shared query encoding within the broker deadline |
| `embedding_runtime.py` / `embedding_worker.py` | One Astra-owned MLX process shared by local clients, authenticated loopback IPC, idle shutdown |
| `selection.py` | RRF fusion, scoped recency, MMR diversity, cross-source selection |
| `ranking.py` | Opaque handles and bounded, escaped previews; the old fixed-slot selector remains a regression reference |
| `broker.py` | Parallel source deadlines, per-turn snapshots, shared evidence budget, diagnostics |
| `feedback.py` | Explicit, version-scoped utility statistics in local SQLite |

Ordinary Markdown core memory and current working/task context remain separate
from historical evidence. When native recommendation is enabled, dynamic local
records enter the same selection and budget as session/activity candidates;
`MemoryRouter` does not additionally recall through its provider. Existing
optional integrations are not dependencies of this pipeline.

## Retrieval and budgets

Native lexical queries remove common conversational search phrases before
generating character terms. Whole, simple arithmetic such as `2+2` skips
retrieval instead of searching historical numbers; dates, versions and
substantive requests such as `2+2 报错` remain eligible. Chinese runs are then
split at particles, pronouns, copulas and question words (的、是、哪、吗、在…),
and common words that would match most history (觉得、问题、多少…) are dropped,
so the words of a question stay whole: 牙医是哪天 searches 牙医, not 牙医是 / 医是哪
/ 是哪天. Characters that often begin or end content words (都、就、有) do not
split. A remaining two-character word is one term; a three- or four-character
run, usually two words, becomes overlapping character pairs (课程讲师 → 课程、程讲、讲师);
longer runs use overlapping trigrams, matching the FTS5 trigram index. Literal
ASCII identifiers remain searchable. Terms are unique and capped at 12; explicit
literal targets have priority over long Chinese gram runs. No tokenizer package
or dictionary service is required.

A date in the question becomes a local calendar window that filters every
source, and its words leave the search terms (上周体重 searches 体重 within last
week). Covered: ISO dates; 今天, 昨天, 前天, 大前天; N天前, N周前, N个月前, in Arabic or
Chinese numerals; 上周, 这周, 上周三; 上个月, 本月; 前几天; 最近一周, 最近一个月;
M月D号; and the English forms (today, yesterday, N days/weeks/months ago, last
week, last Monday, this month, past week, Sep 27). Weeks start on Monday. A
month-day later than today means last year's. Compounds that only contain a
date word, such as 晚上周末 and "may 5 people", are not windows. A windowed
question also considers what happened in the window, except when a window
longer than a day names a topic that matched nothing there: that question
abstains instead of filling the slots with unrelated items.

Session Recall keeps a second FTS5 table for two-character terms,
`messages_cjk`: each message's distinct CJK character pairs. It is written in
the same transaction as the message, and `recall_meta.cjk_pairs_through`
records the highest indexed message id. The first open after an upgrade indexes
the existing archive for at most half a second, and each later message indexes
up to 500 more. Readers use the index up to that id and check later messages
literally, so messages written by an older Astra or an import are found before
they are indexed. SQL triggers remove a deleted message's pairs; an edit removes
them and moves the watermark back so the message is indexed again. SQLite
before 3.43 cannot delete from a contentless table, so there the table stores
its pair text (about twice the size). Native Activity has no pair index; it
keeps the unsplit trigram terms, so short words cannot force full scans of its
much larger archive.

Session and record lookups accept partial matches: multi-term queries require
at least two distinct query terms; a single-term query requires one. This is a
lexical eligibility floor, not a semantic confidence score. Coverage is computed
before candidate limits, so weak newer matches cannot crowd out stronger older
ones. When every term is at least three characters, native Session and Activity
combine one indexed posting list per term into a bitmask; Session also reads
two-character Chinese terms from its pair index. Each term contributes
once, and coverage uses integer operations without repeatedly decoding long
bodies. One or two indexed targets restrict postings before common grams are read;
broad target lists use the shared masks and a single boundary check instead
of repeating a large target disjunction for every term. Mixed short-term queries retain literal scans and lossless indexed target
prefilters; there is no early recency-only truncation. Session scoring is shared
across global and workspace pools, and workspace checks probe matching sessions
directly.
Fully attributed archives skip legacy workspace inference. The exact current question is
excluded before those pools are limited; a self-hit never disables partial
recall. The old reader path without a query plan remains available for legacy
callers.

Literal-target constraints recognize ASCII terms in mixed Chinese queries,
acronyms/camel case, and code identifiers. A candidate must mention at least one
such target with ASCII token boundaries: `NUS` matches `nus`, but not `sinus`
or `NUS2`. Multiple targets use OR, allowing complementary evidence for a
comparison. When every allowed target is at least three characters, Session
and Activity can use the existing trigram index to restrict work to those targets
before evaluating common query terms, including Activity's short-term fallback.
If a target is shorter, that extra prefilter is omitted so alternatives such
as `AI` are not lost. Ordinary English request words and explicit output destinations
(for example, “把今天的经验更新进 skill” or “summarize today's notes into README”)
are not required historical subjects. These are bounded syntax heuristics,
not a general entity recognizer; aliases and paraphrases can still be missed.
Output destinations can be lists of bare or quoted file paths joined by
commas, `、`, `和`, `以及`, `and`, or `or`. Later items do not become alternate
subjects; a new instruction such as “再检查 NUS” or “then inspect NUS” ends
the list grammar and retains its named subject. The existing bounded lookback
still applies, so unusually long or ambiguous output instructions can be missed.

Session, Record and native Activity rank eligible matches by independent query-character
coverage. Each unique CJK gram marks its first span in the normalized query;
overlapping spans share credit. Two adjacent trigrams cover four characters,
not six. Each ASCII term contributes three units. SQL first computes a term
bitmask, then uses integer operations to count covered positions before pool
limits. The existing one/two-term admission floor is separate and unchanged:
overlap affects ranking without making a four-character Chinese phrase
unsearchable. Fusion uses the same covered-position fraction instead of raw
bigram overlap; MMR still uses text features to measure duplicate evidence.

All native lexical channels apply literal-target constraints before SQL limits.
Fusion checks them again for lexical, vector, recent, and habit candidates, so
extra channel votes cannot override an explicitly named subject. Activity uses
up to 6,000 redacted characters of searchable evidence privately for ranking;
the compact preview and model-visible budgets remain unchanged. A target beyond
the bounded evidence, an unrecognized output instruction, or an ambiguous
mixed-language word can still cause a miss. This policy does not guarantee
semantic relevance, and long text is not penalized solely for being long.

Activity's native FTS and short-term fallback both apply the same coverage floor
and query-character ranking. Workspace priority, time and import guards apply
before limits; only selected rows receive full evidence enrichment. The legacy
reader without a query plan retains its term-pair query and BM25 ranking;
vector candidates retain their existing semantic eligibility.
These heuristics can still miss paraphrases and can match broadly worded
questions; nonempty results alone are not evidence of useful answers.

Source channels keep separate ranks. RRF uses `sum(1 / (60 + rank))`, counting a
candidate at most once per channel. Selection also considers task-term overlap
and workspace proximity, then penalizes redundant text with MMR. It returns
zero to four entries by default. Recency does not fill unused slots on an
independent query. Habit evidence requires both existing support thresholds and
a matching preference query.

Source databases are read-only. Query time bounds are applied before SQL pool
limits, and evidence windows respect the same bounds. Activity also checks when
a record was imported, so later imports cannot enter an earlier replay. Records
that have been superseded or changed after selection cannot be opened through
their old native-record handles. Historical replay of mutable archives is
conservative: missing old versions cannot be reconstructed. Use historical
snapshots when an exact past version is needed.

The initial preview uses the existing saved character limit, capped at 2,000,
and a 500-token local estimate. Long previews retain at least 64 characters or
are dropped; naturally short facts can be shown in full. Trace rows contain the
actual rendered preview, not a longer pre-truncation candidate. Shadow mode uses
the same rendering and budgets but does not inject the result or enable opens.

`context_open` remains request-local: two calls per turn, up to three handles
per call, a 6,000-character per-call cap, and a shared 2,000-token estimate for
details. Multi-handle opens share the remaining budget so one long record cannot
consume every requested section. The returned content remains historical evidence, not instructions.
Opening evidence does not cause it to be retained as a newly learned fact.

The arguments to `context_open` (handles and window) are saved verbatim in tool
history and the argument audit. Expanded results keep their request-local
persistence policy. Saving a handle does not extend its lifetime or enable an
old-turn open. Before sending history to the model, Astra omits legacy redacted
`context_open` calls and their paired results, while retaining surrounding text
and sibling tool calls. The saved archive is unchanged. A newly submitted
placeholder is rejected before execution and the per-tool call budget, with a
bounded retry hint to use `context_inspect` and copy current handles exactly.

### Inspecting an injection

`context_inspect` is a read-only Astra tool available on both platforms. It
reads the broker's current state without archive queries, embedding, or expanded
evidence. It reports the default four-entry selection limit separately from
`context_open`'s three-handle limit, pre-fusion candidate count, selected count,
visible count, submission status, actual channel names, and per-slot character
or token budget omissions. `related` alone does not establish semantic recall.
The candidate count can include multiple channel hits for one item.

The model selects `context_inspect` through normal tool use when a user asks
about the current injection. There is no self-inspection phrase matcher or
special inspection intent in the query planner. The turn follows the normal
recommendation pipeline; the tool reads the results already prepared for that
turn, without rerunning retrieval or changing the injection. Counts reflect
actual selection and submission, so a self-inspection request can have zero or
nonzero injected entries. Tool selection needs no separate classifier or model
call before the normal agent turn.

Current visible previews and exact handles are request-local tool output.
The previous completed turn is labeled separately and retains only bounded,
content-free metadata in this broker's session. No previous previews or handles
are revived; reset, mode change or another session cannot expose them. Core MD,
working memory and task state are outside these counts. Restarting loses this
in-memory summary, so it cannot reconstruct a historical injection.

Version 3 `context_index_built` and `context_index_submitted` audit events add
the same content-free `diagnostics` object. Empty/gated/error decisions also
produce a built event in active modes; shadow/off still emit no impressions.
Counts and channel labels are recorded, but query text, previews, paths and
exception messages are not. Existing event row metadata is unchanged. Old
events without diagnostics must be treated as unknown, not as zero candidates.
`/context-index why` exposes selection counts, channels and render omissions
without a model call. Invalid open arguments direct the model back to
`context_inspect`; handles must be copied exactly and old-turn handles still
expire normally.

Query-time embedding uses an already warmed Astra backend. Session, record and
Activity workers share one encoding per request. Shared encoding owners are
bounded to two outstanding jobs; the real backend also serializes encoding and
returns immediately when busy. Peers wait only within the broker deadline.

Cold vector snapshots use contiguous float32 buffers directly. Expanding each
stored float into a Python object used to hold the GIL long enough to exhaust
another source's SQLite deadline. The local performance gate now loads 5,000
session vectors, 32 memory vectors and 1,200 activity vectors concurrently at
2,560 dimensions; an allocation regression test also bounds activity snapshot
memory relative to its binary payload. This changes matrix loading, with the
stored values, vector dimensions, ranking rules and deadlines retained.

A timed-out job may finish in the background but cannot change the frozen
prompt. Session/record vector jobs have independent tasks, so their timeout
cannot discard completed lexical results. The original source deadlines and
lexical fallback remain.
No accuracy, latency improvement, or token saving is assumed from these limits;
measure actual provider usage, including tool-driven model continuations.

Activity divides one 75ms SQLite read allowance among independent phases: habit
uses at most 20% of the remaining time, summary relevance 75%, then events use
the remainder (reserving 20% when recency is requested). Completed channels
survive a later interruption. The existing bounded embedding wait pauses SQL
accounting; canonical vector validation receives only the unused read time,
not a new 75ms allowance. The broker still bounds the complete source work at
200ms. SQLite progress checks and Python callbacks make these cooperative
limits, not real-time scheduling guarantees.

Inspection and content-free audit events include `source_stages`,
`semantic_stages` and `semantic_errors`. Stage names, elapsed milliseconds and
error categories are allowlisted; SQL, query text, paths and exception messages
are not recorded. Archive `deadline` and semantic `deadline` are separate
failures. A broker timeout can identify the unfinished channel but cannot
retrospectively identify a stage that had not returned. Older logs have none
of this extra stage evidence.

## Semantic index maintenance

The new `context_memory_vectors` table shares the existing model-scoped vector
file; the Activity table and its encoding are unchanged. Each row stores source,
hashed canonical archive path, item ID, exact content revision, timestamps,
encoding layout and a vector. It does not duplicate memory bodies. Readers
never create or migrate this table, and validate model identity and dimensions.
They cache at most four immutable archive matrices and invalidate snapshots on
database/WAL changes. Source SQL has its own deadline.

Each source indexes at most its 5,000 latest eligible items. Embeddings use the
first 900 redacted characters, so facts beyond that prefix may still need text
retrieval. This is a bounded first semantic channel, with no automatic summaries
or chunking model. Named-target, time-window, active-context and current-message
filters still apply; a cosine match cannot bypass them. Candidates are checked
against their canonical source revision before selection. Record status, pinned
and conflict metadata, and validity intervals remain authoritative. Changed or
deleted entries cannot be used through stale vectors, even before maintenance
prunes them. Opens also reject changed session/record versions, and fusion does
not combine votes from two different versions of the same item.

Bootstrap explicitly with `python -m agent.runtime.context_index.semantic_indexer`.
It uses the configured backend and processes at most 128 changed entries per
source, reporting the remaining `pending` count. Repeated calls continue the
backlog; `--max-encode 5000` raises the maintenance cap. Use
`--rebuild --max-encode 5000` for a full re-encode of the bounded source window,
or a separate vector path after a model identity change. Explicit bootstrap may
load/download the configured model; ordinary query readers never do.

After restart, an enabled broker with an existing vector file starts the normal
backend warmup and a daemon which checks source versions every 30 seconds.
It uses only a ready backend and processes at most 16 changed entries per source
per cycle. One entry is encoded and committed at a time, so a busy model can
defer the next entry without losing progress. A kernel-held Astra instance lock
allows one background maintainer per vector file; other clients retry later.
Unchanged archives are skipped; deletes are pruned after a successful
canonical scan. If the backend is busy or unavailable, maintenance retries on a
later cycle and queries retain their text fallback. No Activity recording or
source-archive synchronization is started.

The session/record minimum cosine defaults to 0.75, adjustable with
`ASTRA_CONTEXT_INDEX_MEMORY_MIN_SIMILARITY` (bounded to 0.3–1.0). It was selected
on the development split below to improve recall without increasing negative
injection. Cosine is model-dependent, not a probability. The existing Activity
threshold is separate. See [platform setup](context-index-platforms.md) for Mac
and Windows backend commands; Windows GGUF quality requires its own replay.

## Model memory and multiple sessions

The macOS backend is a small client of Astra's own shared worker. The worker owns
one MLX model per local runtime directory and model identity; multiple sessions
and checkouts use the default per-user directory. A kernel lock is acquired
before loading, so racing clients cannot create multiple live model owners.
The rendezvous file is private and carries a random authentication token;
requests stay on `127.0.0.1`, bypass proxies and have bounded bodies. Neither
memory text nor credentials are logged. This adds no third-party service or
package. Windows retains its existing shared llama.cpp HTTP backend.

Boot/explicit preparation may start the worker; query encoding never does.
Encoding is serialized across clients, with no queue of model jobs when busy.
Astra then retains available text results. Each text uses at most 512 model
tokens. MLX results are materialized with `tolist()` instead of retaining scalar
views, and the worker synchronizes and clears scratch buffers even on failure.
[MLX's cache limit](https://ml-explore.github.io/mlx/build/html/python/_autosummary/mlx.core.set_cache_limit.html)
is set to zero. The 6 GiB `set_memory_limit` is a scheduling guideline that MLX
can exceed, not a hard memory guarantee.

Active clients check the worker every 30 seconds, but only an encode request
counts as use: after `ASTRA_EMBEDDING_IDLE_SECONDS` (default 900, at least 60)
without one, the worker exits and releases the model, also while Astra keeps
running. Loading never counts as idle. The next encode request (a query, or the
indexer after new messages) starts it again in the background; that request,
like any request when no endpoint is ready, falls back to text immediately.
Restart pre-update Astra sessions to release their old process-local models.
See [platform runtime controls](context-index-platforms.md#macos) for status and
explicit stop commands.

Latency alone does not establish a small memory footprint. On 2026-09-09, an
offline Mac probe ran 24 alternating short queries and 900-character synthetic
passages through the original Qwen3 model. MLX active allocations returned to
3.86 GiB after encoding, with 5.00 GiB peak active allocations and zero scratch
cache. `vmmap` reported about 4.2 GiB physical footprint and 5.4 GiB peak. A
second independent Python client used the same worker PID; neither client
imported MLX. These are separate allocator/OS measurements, not interchangeable
RSS totals or a guarantee for every workload. The earlier 17–28 GiB live
backend observation was not a paired run of this synthetic workload.

Run the opt-in probe with the embedding extra and an already cached model:

```console
python -m agent.evals.context_index_benchmark.resources --output output/embedding-resources.json
```

It uses offline model loading, synthetic inputs and a private temporary runtime,
then stops its own worker. It never stops a user's normal worker. Checks cover
two-process sharing, no MLX in clients, released caches, return to baseline and
an 8 GiB peak-active acceptance budget (`--max-peak-gib` overrides the budget;
it does not change the model's allocator settings). This is intentionally
separate from ordinary unit tests and the model-free latency gate. Run it
separately from native builds or other GPU benchmarks. The 4B weights still
cost roughly 4 GiB even with one shared owner.

## Feedback and observability

`/context-index why` explains the latest intent, decision, source availability,
selected entries, submission state, and estimated cost. Structured events are
kept outside normal model-visible session history:

- `context_index_built`: an index was prepared, not necessarily sent;
- `context_index_submitted`: the final prompt included that index at the
  provider-call boundary; this is a submission attempt, not proof of attention;
- `context_index_open`: bounded evidence was requested and its result recorded.

These events do not train ranking. Only explicit feedback changes utility:

```text
/context-index feedback R1 useful
/context-index feedback R1 irrelevant
/context-index feedback R1 outdated
```

Use the slot shown by `/context-index why`. Feedback applies only to the latest
submitted selection, including after its request-local handles expire. A repeat
for the same entry/request updates the same event rather than adding votes.
Feedback never modifies Markdown memory or replaces a fact; use normal memory
correction commands for that.

The store defaults to `context-index-feedback.db` beside the canonical Session
Recall database. `ASTRA_CONTEXT_INDEX_FEEDBACK_DB` can override it. It is created
only when explicit feedback is saved; recommendation reads do not create it.
It stores hashed workspace/intent and item/version identifiers and numeric
feedback, not queries, paths, or evidence bodies. Up to 5,000 feedback events
are retained. Each adjustment is `0.1 * sum(feedback) / (count + 5)`, so cold
records are neutral and feedback cannot dominate relevance. A changed version
has a new key; another workspace or intent does not inherit its votes. A
missing or unavailable feedback database leaves ordinary ranking operational.

## Reproducible checks

Run from the repository root with the project's development environment:

```bash
python -m pytest -q tests/test_context_index_native.py tests/test_context_index_runtime.py
python -m pytest -q tests/test_context_index_lexical.py tests/test_context_index_precision.py
python -m pytest -q tests/test_context_index_session_source.py tests/test_context_index_activity_source.py
python -m pytest -q tests/test_context_index_broker.py tests/test_context_index_eval.py tests/test_context_index_benchmark.py
python -m pytest -q tests/test_context_index_semantic.py tests/test_context_index_quality.py
```

Performance checks are opt-in. Run the release gate with
`python scripts/phase_t_gate.py --context-index-performance`, or set
`ASTRA_RUN_CONTEXT_INDEX_PERF=1` when running
`python -m pytest -q -s tests/test_context_index_performance.py` directly.
Without that switch, skipped performance tests are not a latency acceptance.

The native tests exercise task-aware querying, shared source slots, RRF channel
votes, duplicate suppression, time filtering before pool truncation, bounded
evidence, record supersession, explicit feedback isolation, and the real ReAct
prompt path without external-provider recall. Lexical regressions cover long
Chinese queries with a current-question hit, partial English matches, short
Chinese terms, single-term noise, workspace coverage, literal identifiers, and
Activity time/import boundaries. The optional performance suite also checks
nonempty long-Chinese Session recall on a 100,000-message synthetic archive
within the existing 75 ms source budget. These are behavioral regressions,
not downstream answer-quality scores.

The fourth performance case measures 5,000 fixed 4,096-dimensional vectors with
canonical Session revalidation and a 75 ms warm-search p95 bound. It does not
load a model and measures neither embedding accuracy nor encoding latency.
Semantic contract tests separately exercise cold/busy fallback, shared encoding,
source timeouts, stale and deleted rows, version-bound opens, archive/model
isolation, malformed vectors, and bounded incremental maintenance.

The precision regressions use synthetic positive/negative pairs, including
equally long course history versus fictional roleplay, 80 newer distractors
before source limits, literal identifier boundaries, complementary named
subjects, overlapping versus independent Chinese spans, output destinations,
and Activity evidence beyond a compact preview. SQLite instruction budgets
also check that a rare named target is found among 5,000 common-term distractors
without scanning all of them in Session and both Activity lexical paths.
Phase-budget regressions simulate summary interruption and confirm that events
can use the reserved time without renewing the total allowance. They also
check numeric abstention, exact workspace/anchor boundaries and content-free
semantic failure diagnostics.
These work-budget checks are independent of machine clock speed and do not
replace the opt-in latency suite. The tests use real SQLite
sources and fusion without a model judge or private user transcripts. Passing
these cases establishes those behaviors, not an aggregate accuracy score.

The existing benchmark loader now preserves source session/message identity;
replay excludes the test question itself and applies the historical cutoff.
The judge receives only previews that survived real rendering. Ranking overrides
implement `select_rows(session, activity, plan, memory)`; rendering stays fixed
across comparison arms. Public evaluation should use authorized or synthetic
data and report empty results, errors, tokens, and cold/warm latency alongside
task quality. Historical aggregate scores from the old fixed-slot policy are
not evidence of this policy's quality.

## Quality replay

`agent/evals/context_index_benchmark/data/quality-v1.json` is a public, synthetic
gold-labelled fixture. Its 24 scenarios are split into 16 development and 8
holdout scenarios. Each has a direct wording, a paraphrase, an unrelated query
and a near-topic query whose requested fact is absent, replayed through both
Session and Record sources: 192 cases, not 192 independent scenarios. Each
archive contains a relevant document and three distractors, including long
fictional text. Labels are independent of the returned panel: missing the gold
document lowers Recall@4 and nDCG@4; showing anything for a negative query counts
as unwanted injection. The real broker, selection, rendering and budgets run in
temporary synthetic archives. No private histories, model judge or chat calls
are used. These tests do not evaluate fact extraction or downstream answers.

```console
python -m agent.evals.context_index_benchmark.quality --output output/quality-lexical.json
python -m agent.evals.context_index_benchmark.quality --semantic --split development --output output/quality-development.json
python -m agent.evals.context_index_benchmark.quality --semantic --split holdout --live-query-encoding --output output/quality-holdout.json
```

The semantic run explicitly uses the configured embedding backend. By default,
it caches actual backend outputs before replay to isolate retrieval cost;
`--live-query-encoding` includes real warm query encoding in measured latency.
Model loading and document-index construction are excluded in both modes.
Reports include gold recall/ranking, displayed precision, empty cases, negative
injection, source failures, estimated preview tokens and p95 latency. Unit-test
vectors validate wiring only and are never used for the reported quality score.

Keep development and holdout splits fixed and report both positive retrieval and
unwanted injection. These small synthetic archives use manually specified binary
labels; they do not establish downstream answer quality or another platform's
embedding latency. Store measured scores with each run instead of treating old
aggregate results as the quality of the current implementation.
