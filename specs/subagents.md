# Subagents

Status: shipped. Agent Teams were merged into the `delegate_*` tools: every subagent has a name
and stays for the whole conversation, and subagents given the same group may write to each other.
User-facing notes are in [docs/execution.md](../docs/execution.md#subagents).

## 1. Why one kind of subagent

Both features ran the same worker (the same compaction, no turn limit, the same concurrency),
wrapped in two sets of tools:

- `delegate_task` (with poll, read, list and cancel): a subagent ended with its report and could
  not be given more work.
- Agent Teams (`team`, `team_spawn`, `team_send`, `team_wait`, `team_inbox`, `team_task`,
  `team_restart`): named members that could be reused, write to each other, and share a task board.

The model had to decide up front whether it would need a subagent again, Teams spent many calls on
bookkeeping (status reads, inbox reads, task board updates), and twelve tools described two ways of
doing one thing.

## 2. Shape

| Tool | What it does |
|---|---|
| `delegate_task` (core) | Starts one subagent or a batch; optional `name`, `role` and `group`; a name is generated (`explorer-1`, `worker-2`) when none is given |
| `delegate_send` (core) | More work, an answer or a correction for a subagent: a working one reads it before its next step, an idle one goes on from its saved conversation; `recover=true` brings back read-only one whose last step's outcome is unknown. A subagent uses it to write to `parent` or to its group |
| `delegate_wait` | Waits for a report or a message (replaces `delegate_poll`, `team_wait` and `team_inbox`) |
| `delegate_list` | The subagents by name: running, idle or closed, group, first goal, workspace |
| `delegate_read` | A subagent's transcript, by name or process id |
| `delegate_cancel` | Stops its current work, keeping its conversation; `close=true` ends it for the conversation |

- **Lifecycle:** running, report, idle (the process ends and its conversation is saved; it holds no
  memory or slot), then more work through `delegate_send`. There is no keep-alive capacity or idle
  expiry.
- **Groups (agent to agent):** subagents with the same `group` may write to each other by name;
  one without a group writes only to its parent. A subagent reads messages only while it works, so
  a message to an idle member of the group is refused and its parent gives it work instead. The
  parent can write to any subagent.
- **Worktrees:** a worker with `isolation=worktree` has a fixed path,
  `.astra/delegate-worktrees/<agent id>`. One with changes is kept and used for the next
  assignment; a clean one is removed and made again at the same path. Its conversation is saved too.

## 3. Implementation

- Every conversation has one hidden crew built on the Team store (`agent_teams.kind = 'crew'`; earlier
  rows read as `team`), whose lead is the parent. Names, mailboxes, sender identity, going on from a
  saved conversation and taking over across turns are the Team machinery as it was.
- With a durable task store and a parent task, `delegate_task` names the subagent in the crew before
  starting it; elsewhere a subagent runs once, as before.
- Crew members get `delegate_send`; members of an Agent Team from an earlier conversation keep that
  Team's tools and instructions. Messages arrive as `[SYSTEM-DELIVERED SUBAGENT MESSAGE ...]`, the
  lead shown as `parent`.
- The Team tools and `delegate_poll` stay registered, hidden and executable: a conversation whose
  frozen tool list has them still runs them; new conversations are not offered them.
- Events: `team_created` carries `team_kind="crew"` (the event envelope already has `kind`), member
  events carry `group`, and `team_agent_closed` marks a closed subagent. The desktop's subagent page
  lists subagents by name and group; the terminal status bar counts them as `SUB running/total`.
- The development skills (`team-tdd-development` and the delegation skills) use the merged tools.

## 4. Validation

- `tests/test_delegate_subagents.py`: a named subagent goes on in a later turn with its earlier
  requests as the start of the next one; names are unique and generated; group members write to
  each other and others only to their parent; one stopped mid-write comes back only read-only, and a
  closed one not at all; new conversations are offered the six tools while the Team tools still run;
  a worktree worker goes on in the same worktree. Removing the group check, the crew, or the fixed
  worktree name each fails a test.
- The Agent Team tests still pass: they cover the path earlier conversations keep.
- A live run with DeepSeek Flash: a batch of two named explorers; a follow-up to one of them, which
  went on from its saved conversation; two workers in one group, where one asked the other for a
  function's signature, got two replies, and wrote documentation that matched the code.
- Not covered: going on after a backend restart in a live run (covered by tests), a live worktree
  worker, and the Claude Code provider.
