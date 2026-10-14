---
name: team-tdd-development
description: Use when executing any approved multi-step development plan; coordinate a retained Team worker and reviewer through test-driven implementation and bounded review cycles.
---

# Team TDD Development

## Preconditions

Require an approved specification, a complete multi-step implementation plan,
permission for its writes, and an isolated feature worktree. Read
`executing-plans`, `using-git-worktrees`, and `test-driven-development` with
`skill_view` before changing production behavior. Run the relevant baseline
tests and use `plan_update` as the authoritative plan state.

The lead owns scope, shared Team tasks, commits, verification, and lifecycle.
Do not use this workflow for one bounded change or for explicitly disposable
one-shot delegation.

Retained members have no turn limit and keep their context across assignments, so
one worker and one reviewer can carry the whole plan, up to three worker-reviewer
rounds per task. What bounds a member is the time limit of each single assignment:
`timeout` (default 1800 seconds, at most 14400), counted while it works; idle time
and waiting for a free slot do not count. Before spawning, set `timeout` to cover
the longest single assignment you expect, including its investigation, tools,
corrections and report. If one assignment would need longer than the maximum,
do not spawn for it: split that task first.

Use recorded `episodes` in Team status and task-board reads to see what each
assignment actually took. Compare the same role, model and episode kind; include
startup and correction costs. Keep giving related follow-up work to the member
that already holds its context instead of replacing it.

## Create the Team

Create one Team for the implementation plan. Resolve the lead's isolated
worktree to one absolute path and pass that exact `workspace_root` to both
`team_spawn` calls. Spawn exactly two retained teammates:

- one `mode=worker`, `keep_alive=true`, `isolation=shared` worker for edits and
  tests;
- one `mode=explorer`, `keep_alive=true`, `isolation=shared` reviewer for
  read-only review.

Spawn and admit them sequentially: wait until the worker is idle and
`effective` before spawning the reviewer, then wait until the reviewer is idle
and `effective` before assigning the first task. This keeps the shared-worktree
rule of one active teammate at a time true even during setup.

Give each teammate a standing role and ask it to report readiness without
starting unassigned work. Keep this handshake to a short report: actual Git root,
the absolute path returned by `stat_file` for one known project file, and ready
or a concrete environment problem. Budget for the tool check and final report,
not a broad repository investigation. The lead supplies the preflight result and
verified interpreter/test command. `team_spawn` initially reports
`keep_alive_state=pending`; use `team_wait` or Team status until both members
become `effective`. Never treat pending as admission. If either member becomes
`quota_rejected`, terminal, or timed out, do not silently replace Team
continuation with `delegate_task`.

After each admission, call `team(action=status)` and require its
`workspace_root` to equal the lead's absolute worktree and the teammate's
reported actual Git root. If any value differs, do not assign work; apply the
controlled-abort cleanup rule.

Only one teammate should be active at a time. The reviewer must not edit files
or wake the worker directly.

## Execute One Plan Task

Select one dependency-ready plan task and mirror it on the shared board with
`team_task(action=create, ...)`. The lead must immediately assign ownership
with `team_task(action=claim, team_task_id=..., agent=worker)` before waking the
worker. Wake it with `team_send(kind=task_assignment, to=worker, ...)`, carrying
the task id, exact files, acceptance criteria, constraints, and relevant review
history. Require the worker to follow `test-driven-development` and return:

- the RED command, observed failure, and why it proves missing behavior;
- the GREEN command, pass count, and exit status;
- changed files, relevant regression evidence and any shared behavior affected;
- remaining risks or unverified behavior;
- confirmation that the lead still owns the commit.

Before every worker or reviewer assignment, read the member's status from
`team(action=status)`: assign only to an idle member, or send a correction to a
working one, which it receives before its next step.

Pass the board id as structured `team_task_id` and `episode_kind=implementation`,
`review` or `revision` on `team_send(kind=task_assignment, ...)`. These fields
drive durable accounting; a task id written only in prose cannot be joined
reliably to the board.

Keep message bodies at or below 3500 characters (the hard limit is 4000). Include
exact files/line ranges and the relevant revision; place longer reference material
in an artifact. The worker runs the relevant task tests and a focused regression;
the lead runs broad regression at integration gates. Do not duplicate the same
full suite at every handoff, but retain tests needed for changes to shared logic.

Inspect the diff and evidence before waking the reviewer. Keep the worker-owned
shared task `running` throughout review. Wake the reviewer with
`team_send(kind=task_assignment, to=reviewer, ...)`, including the task
contract, current diff, worker evidence, and prior findings. Require `APPROVE`
or `CHANGES_REQUIRED`, findings grouped as Critical, Important, and Minor,
tight file locations, impact, and completion conditions.

State the review scope and estimate explicitly. Use a short checklist for bounded
presentation-only edits, and deeper review for state, concurrency, permissions,
focus or lifecycle behavior. TUI files are not automatically low risk. The reviewer
retains independent evidence checks and needed reproductions; an incomplete or
budget-limited review must state its missing coverage and cannot count as approval.

The reviewer reports only to the lead and returns idle. On approval, the lead
runs fresh task verification, commits the slice, records the accepted evidence
with `team_task(action=update, status=completed, result=...)`, and updates the
full `plan_update`. On rejection, the lead validates the findings and wakes the
same worker with another `team_send(kind=task_assignment, ...)` containing only
the actionable corrections.

When recording acceptance, refer to the board's runtime `episodes` and `turns_used`
alongside the evidence. Keep actual response counts separate from estimates, token
cost and elapsed time. A `reported` episode means a report was produced, not that
the task passed review or was accepted.

A forced-finalization report is a lead to evidence, not proof of task
completion. Cross-check it against the shared task board, current diff, exact
test commands, and transcript before using it. A readiness report or a report
about the standing spawn goal cannot satisfy the reviewer gate: only an
assignment-specific `APPROVE` or `CHANGES_REQUIRED` report can do so. If that
report is unavailable, the lead performs the bounded takeover path instead of
claiming reviewer approval.

Allow at most three worker-reviewer rounds for one plan task. If Critical or
Important findings remain after round three, the lead takes over diagnosis and
either performs one bounded correction or reports a genuine design blocker.
Do not create replacement teammates merely to reset the round count.

## Failure and Recovery

`team_restart` is checkpoint restart and never continuation. After an
interruption or backend restart, state that live idle context was lost. Resume
from the durable checkpoint only when the specification, diff, task board, and
transcript provide enough evidence to continue safely; otherwise stop and ask
for direction.

Do not blindly retry quota rejection, active-time exhaustion, cancellation, or
an unexpected terminal state. Inspect Team status, preserve completed work,
and report the concrete boundary. Before leaving an unaccepted mirrored task,
update it with a useful result: use `status=failed` when attempted work or
verification cannot meet its acceptance criteria, and `status=cancelled` for a
lead/user cancellation or when safe continuation is declined before
acceptance.

Treat cleanup as a `finally` lifecycle rule on every exit path, not only normal
completion. This includes a blocker, `quota_rejected`, timeout, cancellation,
unsafe recovery, active-budget exhaustion, and unexpected terminal status.
For every still-active retained member, send
`team_send(kind=shutdown_request, to=..., ...)` and wait for terminal status
with `team_wait` or Team status before returning control. Use
`team(action=stop)` only for forced cancellation when graceful shutdown cannot
finish or immediate cancellation is required.

## Completion

After all plan tasks, inspect the complete branch range against the approved
specification and run integrated verification from the lead workspace. Read
`verification-before-completion` before any success claim and
`finishing-a-development-branch` before merge, push, or cleanup.

Apply the same `finally` cleanup rule to both retained teammates and ensure no
idle Team member remains. The lead alone performs final commits, integration,
push, and worktree cleanup within the user's authority.
