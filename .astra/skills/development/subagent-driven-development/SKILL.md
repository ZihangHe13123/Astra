---
name: subagent-driven-development
description: Use when the user explicitly requests disposable one-shot delegates for a written implementation plan and does not need a retained worker and reviewer.
---

# Subagent-Driven Development

## Preconditions

Use this workflow only for an explicit one-shot delegation request. Multi-step
development defaults to `team-tdd-development`; do not select this skill merely
because a plan has several tasks.

Require an approved specification, a complete implementation plan, permission
for its writes, and an isolated feature branch. Read the plan once and resolve
contradictions before dispatching work. Use `plan_update` as the authoritative
task state.

## Task Cycle

1. Select the next dependency-ready task. Keep implementation tasks sequential
   unless `dispatching-parallel-agents` confirms disjoint ownership.
2. Call `delegate_task` with `mode=worker`, preferably
   `isolation=worktree`, and only the task's requirements, interfaces,
   constraints, exact files, and expected test evidence. Start a fresh
   subagent for each task; a correction to the task just reported goes to the
   same subagent with `delegate_send`.
3. Require the worker to follow `test-driven-development`, inspect existing
   code first, preserve unrelated changes, and report changed files, commands,
   results, concerns, and commit state.
4. Inspect the returned diff and evidence. Use a separate bounded
   `mode=explorer` review when the task's risk or size justifies it; otherwise
   review locally.
5. Resolve important findings, rerun the task checks, and update the complete
   `plan_update` list only after the task meets its contract.

Do not blindly retry a blocked worker. Supply missing context, reduce scope, use
a more suitable configured model only when necessary, or ask the user when the
plan itself is wrong. Never let a worker's report replace root-level judgment.

## Completion

After all tasks, review the complete branch range against the specification and
run integrated verification from the root workspace. Then call `skill_view`
for `verification-before-completion` and `finishing-a-development-branch`.
Merge, push, or cleanup only within the user's existing authorization.
