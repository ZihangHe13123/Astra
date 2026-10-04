# Persistent reminders and scheduled checks

`astra jobs` stores jobs, execution history and delivery receipts in the selected
`ASTRA_HOME`, independently of the in-memory `/wakeup` feature. GUI and TUI users
can manage the same jobs with `/jobs`; this does not change the current chat or
its model-request prefix. Skill interoperability is not part of this feature.

Create a reminder without a model:

```sh
astra jobs add stretch --after 600 --reminder --prompt "Take a short break"
```

Create a recurring read-only check, recording the original authorization:

```sh
astra jobs add project-status --every 3600 --workdir /path/to/project \
  --prompt "Read the project status and summarize anything that needs attention" \
  --request "Check this project each hour and leave results in my local inbox"
```

The current configured model is captured at creation. Use `--model KEY` to pick
another configured model. `--web` explicitly enables existing search/fetch tools.
Checks expose file reading/search and time tools, with no shell, writes, memory
providers, Computer Use, delegates or channel sends. Each execution has a fresh
session under `ASTRA_HOME/jobs/runs/ID/`, loading project guidance under current
trust rules. Credentials remain in the selected profile's existing configuration;
they are not stored in the job.

Start the independent scheduler in another terminal:

```sh
astra jobs serve
```

This is a foreground process, stopped with Ctrl+C. No OS service is installed
automatically. It continues across GUI restarts if its terminal remains open.
You can instead use your OS scheduler to invoke `astra jobs tick` periodically;
kernel locks prevent competing tickers from dispatching the same occurrence.
When all schedulers are stopped, jobs remain saved and execute on the next tick
only if they are within their catch-up window.

```sh
astra jobs list
astra jobs show JOB_ID
astra jobs history JOB_ID
astra jobs inbox
astra jobs inbox --read OCCURRENCE_ID
astra jobs pause JOB_ID
astra jobs resume JOB_ID
astra jobs remove JOB_ID
astra jobs run JOB_ID
```

`run` explicitly creates a new manual occurrence; it does not retry an old one or
advance the original recurring schedule. Pause/remove affect future scheduled
occurrences; they do not cancel a running check. Removal preserves audit history.
Completed one-shot jobs cannot be resumed; create a new job for another reminder.
Local inbox insertion and its receipt commit atomically, so restart cannot insert
the same notification twice. External platform delivery is not included yet.

Schedules use `--after SECONDS`, `--every SECONDS` (minimum 60), or `--at` with an
ISO timestamp containing `Z` or a numeric offset. You can combine `--at`/`--after`
with `--every` for a different first instant. Intervals are elapsed-time periods,
not local wall-clock cron expressions; `--every 86400` does not track daylight
saving changes. For example, a one-shot time is `--at 2026-10-06T09:00:00+08:00`.

`--catch-up SECONDS` sets the maximum lateness (1–86400). The default is five
minutes for a one-shot and half the interval, capped at one hour, for repeats.
Missed ranges are explicitly recorded as `skipped`; at most the latest eligible
slot is executed, never a burst of past slots. A running occurrence blocks another
occurrence of the same job; later missed slots follow the same accounting policy.

`--idle SECONDS` limits inactivity, reset by actual model/tool events (default
120 seconds); `--timeout SECONDS` is a separate hard run limit (default 900).
`--iterations N` bounds the ReAct loop (default 10, maximum 50). If an execution
owner disappears or a running worker must be terminated, the occurrence becomes
`unknown` and is never automatically replayed. Inspect its saved session before
starting another check. A child still alive after its scheduler exits retains its
kernel ownership lock and may finish normally; the next tick delivers its result.

The scheduler serves one home per process. To use a different profile, launch a
separate process with that profile's `ASTRA_HOME` and configuration. A home-local
`.env`, when present, is loaded for jobs. Keep model/settings overrides within that
profile. Job parameters cannot change a worker's home, credentials or live session.
