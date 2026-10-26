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

A check follows the default model, the one selected when it runs. `--model KEY`
pins a configured model instead; `astra jobs update ID --model KEY` changes it later
and `--model default` returns to following the default, as does the model menu on
the desktop's jobs page. `--web` explicitly enables existing search/fetch tools, and `--mail` lets the check read the
configured mailbox (read-only; see [mail](integrations.md#read-only-163-mail)). On macOS,
`--read-calendar` lets it read Calendar and Reminders, never change them
([calendar](integrations.md#calendar-and-reminders-macos)).
While today's model usage is over the daily budget (`/usage budget`), checks that come
due are recorded as skipped and not pushed; the next day they run as usual. Reminders and a
run you ask for still run.
Checks expose file reading/search and time tools, with no shell, writes, memory
providers, Computer Use, delegates or channel sends. Each execution has a fresh
session under `ASTRA_HOME/jobs/runs/ID/`, loading project guidance under current
trust rules. Credentials remain in the selected profile's existing configuration;
they are not stored in the job.

Run a check every weekday at a local time, and hear from it only when something changed:

```sh
astra jobs add rust-news --time 08:00 --days mon,tue,wed,thu,fri --web --notify on_change \
  --prompt "Look for new Rust 2026 edition discussions" \
  --request "Tell me each weekday morning if there are new Rust edition discussions"
```

`--time HH:MM` repeats on `--days` (default every day) in `--tz` (an IANA zone; default this
computer's), so 08:00 stays 08:00 across daylight-saving changes. With `--notify on_change` a
check is given the last result the person was told and answers `NO_UPDATE` when nothing is new;
that run is kept as a quiet result. Each run records its token usage.

In a conversation, Astra can manage the same jobs with its `jobs` tool when you ask for something
at a time or on a schedule ("remind me at 3pm", "every morning…"). Creating, changing, resuming,
removing or running a job now is shown to you for approval first, in the desktop or in WeChat; the
job records your own words as its authority: your message and the questions you answered after it.
Listing and pausing need no approval. Its `quiet` action sets the quiet hours, also with approval.

While the Astra desktop app is open (also from the tray), its background host runs due jobs
and pushes results: a desktop notification, and a WeChat message to the person who logged WeChat
in, which also joins that chat's conversation so a reply can refer to it. WeChat gets the whole
result, sent as several messages when it is long (past 8,000 characters the rest is in the inbox);
the desktop notification shows its start. `--targets` chooses
(`desktop,weixin` by default; empty for the inbox only). Every result stays in the inbox. Quiet
results and records of missed slots are not pushed. A push that fails is tried again after 1, 5,
15 and 30 minutes, then given up; one whose run started more than 12 hours ago is not sent.

Check results and pause notices wait out the quiet hours, 23:00–08:00 unless changed with
`astra jobs quiet START END` or `astra jobs quiet off`; a reminder is sent at the time it was set
for. The desktop starts the background host while a channel is switched on or a job may still run
or push (the job store keeps `jobs/active` for it).

The desktop's **定时任务** page shows the same jobs, results and quiet hours, with pause, resume,
run-once and remove buttons.

Without the desktop, start the independent scheduler in another terminal:

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
the same notification twice. `astra jobs serve` and `tick` fill the inbox; pushes are sent by the desktop's background host.

Schedules use `--after SECONDS`, `--every SECONDS` (minimum 60), `--time HH:MM`, or `--at` with an
ISO timestamp containing `Z` or a numeric offset. You can combine `--at`/`--after`
with `--every` for a different first instant. Intervals are elapsed-time periods;
`--every 86400` does not track daylight saving changes, `--time` does. For example, a one-shot time is `--at 2026-10-06T09:00:00+08:00`.

`--catch-up SECONDS` sets the maximum lateness (1–86400). The default is five
minutes for a one-shot and half the interval, capped at one hour, for repeats.
Missed ranges are explicitly recorded as `skipped` and delivered to the local inbox;
`list` and `show` also expose `last_outcome`, separately from the schedule state.
A completed one-shot schedule does not mean its last occurrence succeeded. At
most the latest eligible slot is executed, never a burst of past slots. Model-free
reminders are serviced while a check worker is running, so a slow model request
does not hold their delivery until that check completes. A running occurrence
still blocks another occurrence of the same job; later missed slots follow the
same accounting policy.

A job can also start on an event instead of a time. `--on-mail-from TEXT` and
`--on-mail-subject TEXT` (with `--on-mail-folder`, default the inbox) start it when new mail
arrives whose sender or subject contains the text; `--on-new-file PATTERN` starts it when a new
file matching the pattern (such as `'*.pdf'`) appears directly in `--workdir`. The desktop's
background host looks every five minutes; its first look only notes where things stand, so mail
and files already there never start a run, and one look starts at most five runs per job. A
reminder started this way delivers its text with the mail's sender and subject or the file's
name; a check is told which mail or file started it (and can read the mail). Results of
event-started jobs wait out the quiet hours like check results. The jobs tool's `on_new_mail` and
`on_new_file` do the same from a conversation.

Jobs stop themselves in two cases, and the reason reaches the inbox:

- A recurring job pauses after 3 failed or unknown runs in a row.
- When the desktop or a messaging channel has recorded no message from you for 7 days, recurring
  jobs pause instead of running on unattended. One-shot jobs still run.

Resuming starts again from the next scheduled time; slots that passed while a job was paused are
not caught up. `astra jobs run`, or the tool's `run`, adds one occurrence without moving the schedule;
`astra jobs run ID --queue` leaves it to the running scheduler, as the desktop page does.

`--idle SECONDS` limits inactivity, reset by actual model/tool events (default
120 seconds); `--timeout SECONDS` is a separate hard run limit (default 2700 for a
check with `--web`, otherwise 900).

Workers run in an owned process group on POSIX and a Windows Job on Windows.
On interruption, the scheduler stops that group/job and checks the execution
owner lease before recording an unknown outcome. If cleanup cannot be confirmed,
the active occurrence remains fenced until recovery observes that the owner has
exited; it is never automatically replayed. POSIX gives the worker a bounded
grace period to close detached provider processes before stopping its group.
`--iterations N` bounds the ReAct loop (default 30 for a check with `--web`,
otherwise 10; maximum 50). `astra jobs update ID --iterations N --timeout SECONDS`
changes them for later runs, and the tool's `update` takes `max_rounds` and
`time_limit_minutes`. A check that a safety guard stops still answers from what it
found: out of rounds, the same call a fourth time in a turn, a tool's calls for the
turn used up, or a tool failing again and again. That answer is the result, marked
"rounds used up" or "stopped early", maybe incomplete, in the inbox and the push,
and it does not count toward the failures that pause a job. If an execution
owner disappears or a running worker must be terminated, the occurrence becomes
`unknown` and is never automatically replayed. Inspect its saved session before
starting another check. A child still alive after its scheduler exits retains its
kernel ownership lock and may finish normally; the next tick delivers its result.

The scheduler serves one home per process. To use a different profile, launch a
separate process with that profile's `ASTRA_HOME` and configuration. A home-local
`.env`, when present, is loaded for jobs. Keep model/settings overrides within that
profile. Job parameters cannot change a worker's home, credentials or live session.
