---
name: team-tdd-development
description: Use when executing any approved multi-step development plan; coordinate one retained worker subagent and one reviewer subagent through test-driven implementation and bounded review cycles.
---

# Team TDD Development

## Preconditions

Require an approved specification, a complete multi-step implementation plan,
permission for its writes, and an isolated feature worktree. Read
`executing-plans`, `using-git-worktrees`, and `test-driven-development` with
`skill_view` before changing production behavior. Run the relevant baseline
tests and use `plan_update` as the authoritative plan state.

The lead owns scope, task state, commits, verification, and lifecycle. Do not
use this workflow for one bounded change or for explicitly disposable one-shot
delegation.

Subagents have no turn limit and keep their context for the whole conversation,
so one worker and one reviewer can carry the whole plan, up to three
worker-reviewer rounds per task. What bounds a subagent is the time limit of
each single assignment: `timeout` (default 1800 seconds, at most 14400), counted
while it works; idle time and waiting for a free slot do not count. Set
`timeout` when starting each subagent to cover the longest single assignment you
expect, including its investigation, tools, corrections and report. If one
assignment would need longer than the maximum, split that task first.

Keep giving related follow-up work to the subagent that already holds its
context instead of starting another: `delegate_send` gives an idle subagent its
next assignment, and it goes on from its saved conversation.

## Start the Pair

Resolve the lead's isolated worktree to one absolute path and pass that exact
`workspace_root` to both `delegate_task` calls. Start exactly two named
subagents, each with `background=true`:

- `name=worker`, `mode=worker`, `isolation=shared` for edits and tests;
- `name=reviewer`, `mode=explorer`, `isolation=shared` for read-only review.

Give neither a `group`: they report to the lead and never write to each other.
Start them one after the other: wait for the worker's readiness report with
`delegate_wait(subagents=["worker"])` before starting the reviewer, then for the
reviewer's before assigning the first task. This keeps the shared-worktree rule
of one active subagent at a time true even during setup.

The starting goal of each is its standing role plus a short readiness
handshake, without unassigned work: actual Git root, the absolute path returned
by `stat_file` for one known project file, and ready or a concrete environment
problem. Budget for the tool check and report, not a broad investigation. The
lead supplies the preflight result and verified interpreter/test command.

After each readiness report, read `delegate_list` and require the subagent's
`workspace_root` to equal the lead's absolute worktree and the actual Git root
it reported. If any value differs, do not assign work; apply the cleanup rule.

## Execute One Plan Task

Select one dependency-ready plan task and mark it in progress in
`plan_update`. Before every worker or reviewer assignment, read the subagent's
status from `delegate_list`: assign only to an idle subagent, or send a
correction to a working one, which it receives before its next step.

Give the worker the task with `delegate_send(to="worker", ...)`, carrying the
plan task, exact files, acceptance criteria, constraints, and relevant review
history. Require it to follow `test-driven-development` and return:

- the RED command, observed failure, and why it proves missing behavior;
- the GREEN command, pass count, and exit status;
- changed files, relevant regression evidence and any shared behavior affected;
- remaining risks or unverified behavior;
- confirmation that the lead still owns the commit.

Keep messages focused on one assignment: exact files and line ranges and the
relevant revision; put long reference material in a file and name it. The
worker runs the task tests and a focused regression; the lead runs broad
regression at integration gates. Do not repeat the same full suite at every
handoff, but keep the tests needed for changes to shared logic.

Inspect the diff and evidence before assigning the review. Give the reviewer
the task contract, current diff, worker evidence, and prior findings with
`delegate_send(to="reviewer", ...)`. Require `APPROVE` or `CHANGES_REQUIRED`,
findings grouped as Critical, Important, and Minor, tight file locations,
impact, and completion conditions.

State the review scope explicitly. Use a short checklist for bounded
presentation-only edits, and deeper review for state, concurrency, permissions,
focus or lifecycle behavior. TUI files are not automatically low risk. The
reviewer keeps independent evidence checks and needed reproductions; an
incomplete or time-limited review must state its missing coverage and cannot
count as approval.

On approval, the lead runs fresh task verification, commits the slice, and
marks the task completed in `plan_update` with the accepted evidence. On
rejection, the lead validates the findings and sends the same worker only the
actionable corrections with `delegate_send(to="worker", ...)`.

A forced-finalization report is a lead to evidence, not proof of task
completion. Cross-check it against the plan, current diff, exact test commands,
and the subagent's transcript (`delegate_read`) before using it. A readiness
report or a report about the starting goal cannot satisfy the reviewer gate:
only an assignment-specific `APPROVE` or `CHANGES_REQUIRED` report can do so.
If that report is unavailable, the lead performs the bounded takeover path
instead of claiming reviewer approval.

Allow at most three worker-reviewer rounds for one plan task. If Critical or
Important findings remain after round three, the lead takes over diagnosis and
either performs one bounded correction or reports a genuine design blocker. Do
not start replacement subagents merely to reset the round count.

## Failure and Recovery

A subagent whose last step's outcome is unknown, such as an interrupted write,
is not resumed by an ordinary `delegate_send`. `delegate_send(recover=true)`
brings it back read-only, with the message as its instruction; inspect the diff
before giving it write work again. After a backend restart, work in flight was
lost, but each subagent goes on from its saved conversation when given work.

Do not blindly retry an exceeded time limit, a cancellation, or an unexpected
failure. Inspect `delegate_list` and the transcript, preserve completed work,
and report the concrete boundary. Before leaving an unaccepted task, record in
`plan_update` why it failed or was cancelled.

Treat cleanup as a `finally` rule on every exit path, not only normal
completion: a blocker, an exceeded timeout, a cancellation, unsafe recovery, or
an unexpected failure. Stop every subagent still working with `delegate_cancel`
and confirm with `delegate_list` that none is running before returning control.
Idle subagents cost nothing; close both with `delegate_cancel(close=true)` once
the plan is finished or abandoned, so neither is given work by mistake.

## Completion

After all plan tasks, inspect the complete branch range against the approved
specification and run integrated verification from the lead workspace. Read
`verification-before-completion` before any success claim and
`finishing-a-development-branch` before merge, push, or cleanup.

Apply the same `finally` cleanup rule to both subagents. The lead alone
performs final commits, integration, push, and worktree cleanup within the
user's authority.
