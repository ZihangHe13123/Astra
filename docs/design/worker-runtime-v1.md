# Worker runtime and Team lifecycle

Astra delegates bounded work through `delegate_task` and coordinates named Team
members through `team_spawn`, messaging and the task board. The current contracts
live in `agent/runtime/worker.py`, `tools/delegate.py`, `agent_team.py` and
`team_budget.py`; this guide replaces the earlier phase-by-phase implementation
plan.

## Capabilities and isolation

`mode="explorer"` exposes the permitted read-only tool surface.
`mode="worker"` may edit workspace files and run checks through the existing
sandbox and approval policy. Explicit tool subsets cannot enlarge that surface.
Eligible CodeGraph tools can be included when configured; they are not a required
service. Policy, approval scopes and YOLO state remain shared with the parent.

Shared workers operate in the selected workspace. `isolation="worktree"` creates
an isolated Git checkout; changed work must be reviewed and integrated. Parallel
workers should own disjoint files. A worktree is file isolation, not a separate
OS security boundary.

## Run contract

`WorkerSpec` fixes the goal, supplied context, allowed tools, model overrides,
the time limit and optional Team/workspace identity. Its public metadata omits
the full supplied context.

`WorkerRun` accompanies the existing process result with a run ID, outcome,
timing, turns used and error code. Completion, partial work,
failure, cancellation and timeout are distinct outcomes. Poll/read/cancel refer
to that existing run; they do not launch its goal again. Saved output is evidence
of what the worker reported, not independent verification of its conclusions.

A run has no turn limit and no budget for its output. It ends when the model
answers without calling a tool, when it is stopped, or when its time limit
passes: 1,800 seconds by default, 14,400 at most, counted from when the run
holds a slot. Sixteen subagents may run at once, per parent and in all
(`ASTRA_DELEGATE_CONCURRENCY` changes it, up to 64); waiting for a slot does
not count toward a run's time. Every turn, the last included, has the model's
normal output allowance and reasoning.

A reply with neither a tool call nor an answer (nothing visible, or tool-call
markup the provider did not parse), or a run whose time is nearly spent, hands
over to a tool-free report written from the evidence collected, with one retry
for each way that can fail; a report put together from evidence alone is
marked partial.

## Context compaction

A subagent's history is compacted the way the main agent's is: at the same
threshold (`prompt_token_budget`: the model's window less its output reserve,
its own compaction setting, and for most models half the window), by the same
compressor, and the result replaces the history and is saved, so the summary
is kept and updated rather than written again for every request.
`ASTRA_SUBAGENT_MAX_PROMPT_TOKENS` lowers the threshold when set.

Kept as they were: the system prompt, the latest request and the last four
complete steps, which also hold any call still waiting for its result. Earlier
completed steps are summarised, those of the current assignment as well, as
the main agent summarises the start of a long turn. A write among them goes
into the summary, which records the actions taken and the files changed;
conversation recovery used to keep such writes verbatim, which made a long
implementation history impossible to compact. A step whose outcome is unknown
is always among the recent steps, and the read-only lock it sets is saved
state, not history. A compaction that cannot keep all of this, or whose
summary fails, is not applied; each request is still checked against the
budget before it is sent.

## Team continuity and budgets

A Team member is meant to be reused for the whole conversation: related
follow-up work and corrections go to the member that already holds the
context. One requested with `keep_alive=true` waits idle between assignments.
The initial result may report pending readiness; use the later Team status to
establish that it is actually available. Each assignment has its own time
limit; idle time does not count.

An idle episode publishes its report once to the parent's durable delegate
inbox. The parent can finish its turn while that member remains alive; queued
messages and active episodes still count as work to join. `delegate_poll` waits
for current work to report and returns `episode_result` with worker status
`idle`, while the process status remains `running`. Normal idle expiry does not
repeat a delivered report. Cancellation still reaps idle members, and a later
failure remains observable. A new parent turn can resume the Team and assign
work to the same retained member with its existing conversation history.

The task board and episode records keep what each assignment took and how it
ended, by task, for later planning.

## Talking with members

A message reaches a working member before its next step, so the lead can
redirect work in progress. A member writes to `lead` to ask for a decision or
to report news; that message arrives in the parent's conversation between its
steps, as a subagent's result does, and wakes a parent that is waiting for its
subagents, so it can answer before the member finishes. It is delivered once
and is then read in `team_inbox` too.

A member stays available after it stops running. An idle member ends after
`ASTRA_TEAM_IDLE_TIMEOUT_SECONDS` (default 1,800) or when its Team's idle
capacity is full, and a backend restart ends every member; none of this ends
its conversation. A message from the lead to a member that is no longer
running picks it up again from its saved conversation, before the message is
delivered, adding nothing, so its history and request prefix are as they were.
That needs a conversation that stopped at a clean boundary: a member whose last
step has an unknown outcome is brought back only by `team_restart`, read-only.
Members live as long as the session; they are not shared with other sessions.

Worktree startup needs a verified tool root and usable project dependencies.
Missing dependencies are environment failures, not proof of a code regression.
Use the repository's Team and worktree Skills for the operational workflow.

## Cancellation and recovery

Cancellation stops further work and requests cleanup of the owned process.
Results from already dispatched external actions may still need inspection;
process termination does not prove that those actions were undone. Persisted
process/Team records support inspection and recovery, but do not serialize a
live model coroutine for automatic continuation after a host restart.

`team_restart` brings back any terminal member, including a completed one. A
member with a saved conversation goes on from it with the restart instruction
as its next message; one whose last step's outcome is unknown can then only
read and report. A member without one is re-seeded as a new worker from its
durable spawn spec and transcript tail. A `keep_alive` request recorded at
spawn is re-applied, so a revived retained member returns to idle after its
recovery episode.

Relevant regressions cover delegation, Team state and budgets, process lifecycle,
approval propagation, workspace isolation, mailbox delivery and interrupted
results. Use fresh evidence when validating another platform or provider.
