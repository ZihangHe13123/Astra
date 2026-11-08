# Turn ordering, deadlines and fault replay

Astra keeps model-issued tool calls in their original dependency order. Adjacent
eligible reads may overlap up to `tool_concurrency`; writes and other exclusive
operations wait for earlier work and block later reads. Queued calls are checked
against the current registry again before admission. Cancellation drains work
already started and stops further dispatch. The conversation keeps the whole
call chain: a call that returned keeps its result, a call that started without
returning is recorded as outcome unknown and is not replayed, and a call that
never started is recorded as not started. A call starts when its tool function
is entered, after its checks and approval; a synchronous tool starts only when
a worker thread runs it, not while it waits in the thread pool's queue. The
task store records the same outcome; a not-started step may run later as a
first execution.

## Using a turn budget

`/budget 60` sets a shared 60-second budget for subsequent turns in the current
Astra process. `/budget` reports the setting; `/budget off` disables it. The
startup setting is `ASTRA_TURN_TIMEOUT_SECONDS=0` (off by default), with a maximum
of 86400 seconds. The command is available in normal, Bar and Minimal modes.
It does not change the selected model or reasoning effort.

The monotonic timer starts when ReAct begins a turn. It includes prompt/image
preparation, model requests and their retries, tools, approvals and question
waits. Startup and post-completion bookkeeping are outside that timer. Existing
per-request/per-tool limits still apply; retrying never resets the turn timer.

A reserve of `min(5 seconds, 10% of the budget)` lets an already admitted tool
finish its own action/readback. New provider work and tool attempts cannot start
in this reserve. Cancellation is cooperative: blocking native calls, external
processes and synchronous worker threads are not forcibly killed by the timer.
A timeout therefore does not prove that an external action was undone.

On expiry, the task becomes interrupted with a `time_budget` checkpoint. Saved
results and prior checkpoint details survive; an action whose outcome was not
durably recorded becomes unknown. Explicit `继续`, `continue`, or `/resume <id>`
renews the budget. Existing durable tool-claim rules block automatic replay of
an unknown original invocation. The agent must inspect uncertain effects before
issuing a new action.

## Running offline fault replay

From a development checkout with Python development dependencies and TUI
packages installed:

```sh
python -m agent.evals.turn_replay
python -m agent.evals.turn_replay --browser --output .astra/validation/turn-replay
python -m pytest -q tests/test_turn_budget.py tests/test_turn_replay.py
ASTRA_BROWSER_REPLAY_E2E=1 python -m pytest -q tests/test_turn_replay.py
```

`--output` preserves a fresh `run-*` directory per invocation containing reports,
sessions, task databases and fixture state. Without it, artifacts are temporary.
The browser case needs installed Chrome/Edge and Astra's CDP dependencies; it
opens a separate headless profile and a loopback-only page, then closes both.
It neither attaches to an existing browser profile nor submits a real form.

The version-1 fixture in `evals/turn_faults.json` contains synthetic parsed stream
frames. It passes them through the real OpenAI-compatible streaming adapter,
LLMClient, ReAct, ToolRegistry and session/task storage. The fixture loader is
closed and bounded; it accepts no scripts, credentials or live provider setup.
Only named fixture tools and three isolated browser operations are allowed.

Cases cover ordered read/write/read, missing completion signals, bounded
reasoning-only recovery, malformed JSON rejection, truncated tool recovery,
stalls after progress, expiry after a completed write, expiry/cancellation during
a write, and stale browser references followed by batch selection and repetition.
The oracle checks actual file contents, tool/request counts, error identities,
saved session tool pairing and exactly one terminal event. The browser oracle
independently reads DOM checked state and the page's click/submit counters.

`ui-tui/src/turn-replay-render.test.tsx` launches a real Python replay subprocess
through the real Ink App. The subprocess uses production tool-event projection,
pending-tool finalization, event envelopes and JSON serialization. It checks
normal completion, budget expiry and recovery progress, then submits another
turn to check that input is usable. Full backend startup, slash-command routing,
task status and explicit budget continuation are separately exercised by
`tests/test_backend_task_protocol.py` against a local SSE server.

This suite checks Astra's execution and recovery behavior. Synthetic streams
cannot establish live DeepSeek latency, model answer quality, Windows GUI
behavior, or success on a real Canvas assessment.
