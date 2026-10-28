---
name: dispatching-parallel-agents
description: Use when two or more concrete tasks are independent, can run concurrently, and do not require shared mutable files or sequential results.
---

# Dispatching Parallel Agents

## Decide Whether Work Is Independent

Parallelize by problem domain only when each task can be understood and
completed without another task's result. Related failures, exploratory root
cause work, shared files, shared external state, and the root agent's critical
path remain local or sequential.

## Dispatch Contract

Use one `delegate_task` batch with bounded items. Every item states:

- one concrete goal and task-specific context;
- `mode=explorer` for read-only investigation or review, or `mode=worker` for
  authorized edits and tests;
- `isolation=worktree` for worker changes unless shared mode is explicitly safe;
- disjoint file ownership for parallel workers;
- constraints, required evidence, and a compact result contract.

Do not paste unrelated conversation history or ask two agents to solve the same
problem. Name the items you may follow up on. Give agents that must agree on an
interface the same `group` so they can write to each other; leave the others
without one. Do useful non-duplicating local work while background tasks run;
results arrive by themselves, and `delegate_wait` waits for them. Collect every
result before final synthesis, and send follow-up questions to the agent that
did the work with `delegate_send`.

## Integration

Read each result and inspect every changed file. Check that workers did not
cross ownership boundaries, conflict with user changes, or weaken platform and
permission constraints. Resolve overlaps deliberately, review the combined
diff, and run integrated tests from the root workspace.

An agent's success report is not completion evidence. Before making any claim,
call `skill_view` for `verification-before-completion` and verify independently.
