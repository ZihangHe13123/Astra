# Process protection, restart and session wakeups

**English** · [简体中文](zh-CN/session-lifecycle.md)

## Avoid stopping the host by mistake

Local execution tools check recognizable termination commands before starting
them, including common `kill`, `pkill`, `killall`, `taskkill`, `Stop-Process`
commands and literal Python `os.kill`/`os.killpg` calls. They reject targets that
identify the running backend, verified TUI/launcher ancestors, or services
identified as owned by this installation. Approving a tool or enabling YOLO does
not override this check. Normal cleanup of unrelated processes remains available.

These checks are a best-effort guard against mistakes, not isolation for arbitrary
code. Indirect scripts, dynamically constructed commands and unknown process
identities can fall outside the check. Windows host PIDs are not matched against
WSL's Linux PIDs; recognizable Windows interop commands still receive host checks.
Internal cleanup of the tool's own child processes uses the existing process manager.

## Restart the current backend

In a local Work session, enter `/restart`. The current reply, tool work and saving
finish first. Astra then waits for the TUI to acknowledge delivery, exits the
backend and launches it again with the same session. `/restart cancel` stops the
request. A 120-second timeout cancels the restart instead of interrupting work.
If the TUI could not display an update, it cancels the restart and shows a diagnostic.

New work and session/connection changes wait until the restart is cancelled or
complete. Approval responses and cancellation remain available. An agent can
request this operation with `request_restart`; this requires explicit confirmation,
including in YOLO mode. The command does not update dependencies, reload the TUI
bundle or restart other services. A crash does not trigger automatic respawning.

## Check back while this session stays open

Ask Astra, for example: “Check this CI run every five minutes. Stay quiet until it
changes, and stop when it finishes.” It can create a plan with `schedule_wakeup`.
The plan records the current session, original request, check, next time and expiry.
It grants no additional tool permissions.

You can also use commands directly:

```text
/wakeup after 300 Check the CI run URL from this conversation once
/wakeup every 300 Check the CI run URL; stop when it passes or fails
/wakeup
/wakeup cancel
```

There is one active plan per backend. Creating another replaces it. Delays and
repeat intervals are at least 60 seconds. Plans expire after one hour by default;
the tool accepts an explicit lifetime up to 12 hours, longer than the first delay.
The direct commands use the one-hour default.

When Astra is busy, one due check waits until it is idle. Missed intervals do not
accumulate. Each check uses the ordinary agent and tool policies, with at most ten
iterations and five minutes (or the shorter remaining plan/turn limit). A new user
request during a check cancels that check and its repetition, then starts the
user's work. Cancellation and replacement discard late results from the old plan.

Checks report one outcome using `report_wakeup`: `unchanged` stays quiet, `changed`
displays a useful update, and `completed`/`failed` displays a result and stops.
Missing outcomes and execution failures stop repetition. Internal check messages
remain auditable in session storage but are not restored as user messages or
silent assistant chatter; useful notifications remain in visible history.

Switching sessions, closing Astra or restarting the backend stops the plan.
Restarting Astra never catches up on missed checks. These features are available
in the local Work TUI, not isolated Bar/Minimal/local modes or remote channels.

## When background work finishes

In a Work session Astra is told when work it left running ends, so it carries on
without you asking. This covers commands started with `background=true` or still
running after `foreground_yield_ms`, waits written as a command (for example
`until curl -fs http://localhost:3000; do sleep 2; done`), and background
subagents and Team members.

- While Astra is still working, what ended arrives before its next step.
- Once it has answered, the backend starts a turn for it. The conversation shows
  “后台任务完成，继续处理：…” and Astra reports the outcome. Work that ends within
  a second is one turn.
- A command whose end Astra has already seen through `process_poll`,
  `process_read`, `process_list` or `process_cancel` is not reported again.
- With subagents in the background, Astra answers at once instead of waiting for
  them, so you can keep talking to it while they work. Their reports, and what
  they send to it, start a turn when they arrive. `delegate_poll` with `wait_ms`
  and `team_wait` still wait when Astra asks for them. Later turns of the session
  can poll, read and cancel subagents that earlier turns started; Team members are
  reached through their Team.

No turn starts while a reply is running, while a question waits for you, during a
restart, in Bar/Minimal/local modes or in channels; what ended waits until a
turn can start, and what ends in another session waits until you return to it.
A message you send during such a turn steers it like any other turn. After eight automatic turns in a row without a message from you
(`ASTRA_BACKGROUND_WAKE_LIMIT`), results wait for your next message and the
conversation says so. Stopping a turn keeps what its commands report for your
next message without starting a turn. The notice grants no new permissions.

This lives in the running backend. Restarting Astra delivers nothing it missed;
subagent reports left from before a restart arrive with your next message. With a
model on this computer, Astra and its subagents share that model's request slots
(`LLM_MAX_CONCURRENT_REQUESTS`, default 1), so a reply can wait for a subagent's
request to finish.
