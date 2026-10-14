# Context Index on macOS and Windows

[Home](../README.md) · [Documentation](README.md) · [简体中文](zh-CN/context-index-platforms.md)

The shared code selects MLX on macOS and OpenAI-compatible llama.cpp embeddings
on Windows/Linux. An explicit `ASTRA_EMBEDDING_BACKEND` overrides detection.
macOS retains `mlx-community/Qwen3-Embedding-4B-mxfp8` and the legacy default
`.astra/context-vectors.db`. Install the `embedding` extra on either platform;
MLX is installed only on macOS, while numpy is available on both.

## macOS

Astra's native `embedding_worker.py` owns the MLX model. All local Astra clients
using the same runtime directory and model identity connect to that one worker;
opening another session does not load another copy of the 4B weights. The
worker uses only Astra code and the Python standard library for authenticated
loopback transport. It is not an external memory service or an installed daemon.
There is no extra package dependency.

Boot warmup or explicitly enabling recommendation may start it when a vector
file exists; explicit index preparation may start it without a vector file.
Query encoding never spawns it or waits for model loading. Busy/unavailable
workers return text fallback. Active clients send a heartbeat every 30 seconds;
closing them or using `/context-index off` ends their heartbeat. After about
90 seconds without clients or encoding, the worker exits and releases the model.
Clients recover a lost worker in the background, outside query handling.

The private endpoint/token file defaults to `~/.astra/embedding-runtime`, with
an override in `ASTRA_EMBEDDING_RUNTIME_DIR`. Using different directories creates
separate workers. Diagnose the shared runtime with:

```console
python -m agent.runtime.context_index.embedding_runtime status
```

This reports the owning PID, readiness, busy state and MLX active/cache/peak
allocation bytes without starting a worker. `stop` requests shutdown
of that exact authenticated worker; active clients can restart it on their next
heartbeat, so first close/pause clients when intentionally stopping the model.
These commands also accept `--directory PATH` for an override.

The model retains roughly 4 GiB; sharing eliminates repeated model copies, not
the weights themselves. Single-text, 512-token encoding and immediate scratch
cache release bound routine work. MLX's 6 GiB memory setting is a scheduling
guideline, not a hard OS cap. See the [resource probe](native-memory-recommendation.md#model-memory-and-multiple-sessions)
for actual measured footprint and a reproducible check. Restart Astra sessions
after updating: an already running old backend still owns its original model.

## Windows

For native foreground activity collection, see [Windows activity recorder](windows-activity.md)
and use the separate `.\scripts\activity-control.bat` menu. The embedding menu below only
controls the model service; it does not toggle recording.

Double-click `.\scripts\embedding-control.bat` for a Start / Stop /
Status / Exit menu. Exiting the menu leaves the background service running.
From a terminal, `.\scripts\embedding-control.bat start`, `stop`, or `status` performs one
action without pausing. This is an optional Windows-only helper; nothing in the
macOS startup path calls it.

The default installation paths are `D:\llama-cpp\llama-server.exe` and
`D:\llama-cpp\models\Qwen3-Embedding-4B-Q4_K_M.gguf`. For a different installation,
invoke `scripts/start-embedding-windows.ps1` with `-Server`, `-Model`, and `-Port`
and use the same parameters with `-Action stop` or `-Action status`. Stop checks
the executable, model, embedding arguments, port, and process creation time;
it does not kill every llama.cpp process or an arbitrary port owner.

Download the official [Qwen3-Embedding-4B GGUF](https://huggingface.co/Qwen/Qwen3-Embedding-4B-GGUF).
The provided PowerShell launcher defaults to Q4_K_M and can be given other local
executable/model paths. Run `scripts/start-embedding-windows.ps1`; it launches
a hidden, loopback-only server on port 8088. It is not an installed startup task;
run it again after Windows restarts. Do not point the embedding endpoint at a
chat-only llama.cpp server. The required flags are `--embedding --pooling last`.
Multiple Astra sessions already share this server. They do not start the macOS
MLX worker; the service remains under the existing Windows helper's control.

Set these values in the machine's untracked `.env`:

```dotenv
ASTRA_EMBEDDING_BACKEND=llamacpp
ASTRA_EMBEDDING_BASE_URL=http://127.0.0.1:8088/v1
ASTRA_EMBEDDING_MODEL=Qwen3-Embedding-4B-Q4_K_M
ASTRA_EMBEDDING_TIMEOUT=2
ASTRA_CONTEXT_INDEX_SESSION_MS=450
ASTRA_CONTEXT_INDEX_SOURCE_MS=700
```

The HTTP client reuses its connection, bypasses inherited proxy settings, validates
input order/vector shape, and degrades to lexical recommendations on failure.
For authenticated endpoints use `ASTRA_EMBEDDING_API_KEY`. The HTTP timeout is
independent of the broker's bounded wait; an unavailable embedding service must
not prevent normal conversation.

## Data and enabling

`/context-index session` enables Astra local-record and conversation recommendations.
Text retrieval works without embeddings; an optional prepared semantic index
adds paraphrase candidates through the same native components on both platforms.
`/context-index all` also
enables activity recommendations. `/context-index status` reports the backend and
whether the activity archive/vector file exists; this is not a service health probe.
`/context-index why` shows the latest actual source result and timeout information,
including separate semantic results when those channels ran.
The native `context_inspect` tool provides current-turn counts, budgets,
per-entry retrieval channels and exact current handles on both platforms,
without loading a model or reading archives. The model chooses this tool through
normal tool use; it reads the current recommendation without changing or rerunning
retrieval. Its separate previous-turn summary contains metadata only;
neither it nor the four-entry recommendation limit changes the three-handle
`context_open` limit. See [injection inspection](native-memory-recommendation.md#inspecting-an-injection).

Session readers remain read-only. Native partial-match queries rank by shared
query-character coverage; session and native Activity reuse trigram posting
lists and apply literal-target boundaries before limits. These paths use Python
and SQLite on both platforms, with no schema migration or added service.
Semantic channel ranks enter the existing RRF fusion.
Independent semantic tasks cannot discard completed text results on timeout.
The budget overrides above are local tuning; the shared defaults remain 75ms
for sessions and 200ms for the broker on both platforms. Activity reserves part
of its 75ms read allowance for independent later channels, retaining completed
results on interruption. Inspection exposes separate archive/semantic errors
and allowlisted phase timings; a read deadline does not establish that the
embedding backend was unavailable. Short read deadlines and phase timings use
`perf_counter`, whose resolution avoids early expiry from the coarse Windows
Python 3.11 monotonic clock.

Activity history is separate data. Git sync does not copy macOS activity archives,
model weights, local settings, or vector indexes. Without an activity archive,
the activity source reports `absent`; it does not start desktop recording.
Import a compatible archive or configure `ASTRA_CONTEXT_INDEX_ACTIVITY_DB` before
enabling this source. To build embeddings from summaries:

```console
python -m agent.runtime.context_index.vector_indexer --activity-db PATH_TO_ACTIVITY_DB
```

Session and structured-memory vectors have their own Astra table in the same
model-scoped vector file. They do not require an Activity archive or desktop
recording. With the configured backend available, run the same command on
Windows (activated `.venv`) or macOS:

```console
python -m agent.runtime.context_index.semantic_indexer
```

The default cap is 128 changed entries per source. Rerun while `pending` is
nonzero, or set `--max-encode 5000` for a larger explicit maintenance run.
`--sessions-db`, `--memory-db`, and `--vectors-db` override the canonical paths;
`--source session` or `--source memory` limits the scope. `--rebuild --max-encode
5000` re-encodes the bounded archive window. Model identity changes require a
separate vector file. A different checkout/archive path is a separate scope,
so copying a vector file alone does not make its evidence usable elsewhere.

On startup, an enabled broker with an existing vector file warms the configured
backend and maintains at most 16 changed entries per source every 30 seconds.
An Astra kernel lock permits one background writer per vector file at a time;
each encoding call processes one entry before committing its progress.
The same worker also fills this process's immutable session/record snapshot
cache outside the writer lease. Each source gets one cooperative 2-second
background attempt per cycle, with cancellation and version checks before
publication; this does not change the interactive 75ms or broker 200ms budget.
A cold query may use the text fallback until that work completes. Scheduling
and model readiness can add a maintenance cycle, so background load latency
is not a guarantee about time from startup to availability. The four-snapshot
cache limit remains unchanged; changed database/WAL versions invalidate it.
Only a ready backend is used by maintenance; a busy batch leaves queries on
their text fallback. Restart Astra after first building the file. Set
`ASTRA_CONTEXT_INDEX_EMBEDDING=off` for text-only operation. The session/record
cosine floor defaults to `ASTRA_CONTEXT_INDEX_MEMORY_MIN_SIMILARITY=0.75`;
this is calibrated on the Mac MLX fixture, not a universal probability or a
verified GGUF threshold. Use the public quality replay on Windows before
changing the local threshold. The Activity threshold is unchanged.

The indexer loads the project `.env`. HTTP model identities use separate default
vector filenames; stored metadata also rejects mismatched model identities even
if vector dimensions agree. Keep `ASTRA_EMBEDDING_MODEL` tied to the actual model
and quantization, and use a new identity/path when changing them. Existing MLX
indexes without metadata remain readable on MLX only. Explicit vector path
overrides must not point a GGUF backend at an old MLX index.

Restart Astra after editing `.env` so the process-wide backend and preferences
reload. macOS does not use the Windows PowerShell launcher or its local paths.

The recommendation pipeline itself is native Astra code on both platforms: no
external memory framework or memory server is required. Query-time retrieval
uses only an already warmed embedding instance and has bounded encoding
concurrency; it never loads a model as a side effect of a query. A cold or busy
backend uses the lexical path. See [native memory recommendation](native-memory-recommendation.md)
for shared budgets, explicit feedback, and regression commands.
