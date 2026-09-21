# GUI usability and performance follow-up

Approved scope: the user accepted the review of `ec190615f` and asked to implement the proposed reliability, performance and visual improvements. Continue on `codex/astra-gui`; no merge or push is requested.

Use existing backend behavior and model freedom. Do not reduce model context, add workflow requirements, cap autonomous workers, or rewrite conversation storage incompatibly. Keep unrelated changes in the main checkout intact.

## Implementation boundaries

- Repair lifecycle and projected state: scheduled/running wakeups must prevent silent exit; explicit rejection differs from unknown delivery; tool result indices remain stable and unique. OAuth request state survives closing and reopening its panel. Shared activity checks govern close/delete/window exit.
- Improve navigation: initial keyboard focus belongs to the relevant input, command search supports arrows/Enter, and simple navigation commands open their panels directly. Model selection uses a small searchable picker with configuration available as a second step. Keep provider billing routes and custom endpoints intact.
- Isolate message rendering from draft input and background state. Cache completed Markdown rows, render only the changing tail, bound mounted history while keeping all messages reachable, and preserve the scroll anchor when loading older messages. Keep unknown submissions from being resent automatically.
- Read historical messages by page using a rebuildable index and reuse the read-only query worker. The index must handle legacy JSON and event/snapshot storage correctly, invalidate after rewrite/append, and avoid unbounded memory both while building and serving pages. Original storage remains authoritative.
- Release closed backend state without losing partial visible output. Keep active jobs, agents, approvals and reminders alive. Use ordered asynchronous preference/log writes and bounded file reads. Reclaim only GUI-owned temporary media with known absence of draft/history references.
- Deduplicate sidebar sessions, add concise status/labels, make details responsive at the supported minimum width, remember adjustable panel width, and show friendly execution/context summaries before raw diagnostic events. Let the composer grow within a bounded viewport area and expose jump-to-latest/copy feedback.

## Acceptance

- Focused Python and TypeScript regressions cover history format changes, worker recovery, lifecycle states, projection, OAuth remount, and failed delivery.
- Electron tests use isolated data and local model fixtures: reopen login UI, reminder close handling, keyboard command navigation, history pagination/scroll position, model picker/configuration, multiple sessions, explicit close/reopen and persistence.
- Repeat the previous 62-character typing benchmark with 100 mounted Markdown messages (baseline CPU 1.9–2.1 s, wall time 2.65–2.83 s). Target substantial reduction of renderer CPU without changing model behavior.
- Repeat the 100 MiB historical-query measurement, distinguishing first index build from warm pages. Warm page work must depend on page size rather than full transcript size.
- Inspect light/dark layouts at 1000/1320/1600 px; narrow details must not partly cover an apparently available send control. Build GUI and TUI; keep the product worktree clean after local commits.

The existing macOS fixture does not certify Windows, real OAuth or native capture permissions. Record those boundaries explicitly. Implement and validate the approved scope without another design approval gate.

## Implemented and verified — 2026-09-21

- Lifecycle uses one activity predicate for close/delete/window close: tasks, background processes, Teams, approvals, questions and scheduled/running wakeups. Explicit Quit still stops work. Successful close removes Runtime/Appshot state; unexpected disconnect preserves visible partial output and remains selectable. Closing the selected backend clears automatic restoration.
- Message delivery distinguishes pending, accepted, rejected and unknown. Unnumbered tool receipts receive distinct indices. OAuth progress belongs to the runtime and survives modal closure; only public challenge metadata is projected, never API keys/tokens. Actual account authorization was not performed in this validation.
- Model selection opens a searchable compact picker; missing credentials open the matching billing route. Command search receives focus and supports arrow keys/Enter; navigation commands open their existing controls directly.
- Markdown rows are memoized and a measured viewport window mounts at most 100 messages. This changes presentation only, not the conversation or model budget. Earlier pages preserve the reading anchor, expanded reasoning survives virtualization, and externally rewritten history replaces the displayed generation. Draft height grows up to 220 px; copy feedback and jump-to-latest are visible.
- Sidebar groups are disjoint (pinned/open/recent), include status and readable fallback titles, and search still matches original IDs. Details use readable summaries with lazy diagnostic JSON. At <=1100 px details replace the conversation area; wider windows support a remembered, keyboard/pointer-adjustable panel width.
- History uses a rebuildable private SQLite display index, with streaming legacy/snapshot/JSONL replay. Warm pages read metadata and only their page. A persistent query worker avoids per-click Python startup and caches static commands. Damaged SQLite pages rebuild once; timeouts discard the stuck worker and allow an immediate retry. Session deletion removes the display cache.
- Preference writes are asynchronous, serialized and coalesced; the final unload snapshot cannot be overwritten by an earlier write. Logs use a backpressured stream, text previews read at most 256 KiB, and clipboard cleanup only removes files created in this launch with no known sent/draft references.

### Measurements

macOS, isolated data and loopback model fixtures; results are local measurements, not cross-platform guarantees.

| Scenario | Before | After |
| --- | --- | --- |
| 62 characters typed into a 200-message Markdown conversation, renderer CPU | 1,906–2,108 ms | 154–168 ms |
| Same typing sequence, wall time including 15 ms/key injection delay | 2,650–2,833 ms | 1,175–1,176 ms |
| Same history loaded, DOM messages at the sampled bottom viewport | 100 | 2; other rows remain reachable by scrolling |
| 50,000 messages / 99.58 MiB, warm page computation | 285–315 ms (old query, excluding startup) | 1.79–1.97 ms (worker IPC roundtrip median) |
| First display-index build | No index | 544–576 ms; first legacy request includes worker startup |
| Historical query memory | 492–725 MiB old process peak RSS | About 32 MiB worker RSS after query |
| 10,000-message preview, complete click-to-render in Electron | — | 163 ms cold; 32–36 ms warm |

The same input fixture is used for the typing comparison; DOM mount count intentionally differs because of virtualization. Memory figures distinguish old peak RSS from new sampled worker RSS. New indexing memory scales with the largest individual value, not the whole transcript. Any source change currently rebuilds the index; incremental append indexing is deferred to preserve replay correctness.

### Acceptance evidence

- Python GUI/history/session-action tests: **73 passed**.
- Shared UI core tests: **13 passed**; GUI TypeScript tests: **38 passed**.
- Electron real-backend/local-provider acceptance: **28 scenarios passed**, including scheduled wakeup close/delete protection, narrow-window scroll restoration, reasoning disclosure persistence, history rewrite/pagination, file approvals, mode isolation, drafts and full app/backend restart.
- GUI and TUI builds passed; affected Python Ruff/Pyright and Git whitespace checks passed.
- Light/dark layouts inspected at 1000/1320/1600 px. At 1000 px the conversation is fully hidden while details are open; send controls are not partially covered. Tests verify the reading position when returning.

Local reproducible evidence (ignored artifacts): `output/playwright/gui/result.json`, `output/playwright/gui-optimized/probe.mjs`, `output/playwright/gui-optimized/review.json`, and `output/gui-history-review/benchmark.py` / `benchmark.json`. Baseline remains under `output/playwright/gui-review/`.

Windows, real OAuth authorization and native Appshot capture permissions remain separate acceptance items. Model configuration still uses a reusable full draft backend; splitting out a lightweight auth/config service is deferred. Unexpected-disconnect snapshots are retained until explicit close. Clipboard files from previous launches or any uncertain/sent reference are intentionally retained. No model-context limits, tool-choice rules or workflow restrictions were added.
