# Skill learning and manual review

[Home](../README.md) · [Documentation](README.md) · [简体中文](zh-CN/skill-learning.md)

Astra can save reusable methods directly as skills while working. It does not
require a candidate, an experiment and an activation decision for every lesson.
The model chooses when a lesson is worth saving; it is not required to create
one after every conversation.

## Use the library

```text
/skills
/skills show <name> [file]
/skills create [name] [description]
/skills create --template <name> <description>
```

`/skills create` drafts a complete skill in conversation before saving. Use
`--template` for the original empty template operation.

Skills contain `SKILL.md` and optional `references/`, `templates/`, `scripts/`
or `assets/`. The model saves its own summaries with `skill_manage(origin="auto")`
and uses `origin="user"` for content added at the user's request. Only the catalog
is injected; full instructions are read on demand. Packaged core rules have
their own [loading contract](runtime/core-rules.md).

## Skill ownership

| Source | Meaning | Included in `/learn review` |
| --- | --- | --- |
| `auto` | A method summarized and maintained by the model | Yes |
| `user` | A skill added, installed or edited on the user's behalf | No |
| `builtin` | Packaged core rules | No |

Automatic skills normally live in `.astra/skills/learned/<name>/`. New user skills
go under `user/`; existing categories are retained. `AGENT_SKILLS_PATH` can select
another library. Ownership comes from the writer's provenance record, not a
claim in the skill text or the directory name. Unknown ownership is protected
as user content. `/skills` shows ownership and category.

Skills should explain when to use a method, its steps, limitations and source.
They are listed in the normal skill catalog and read through `skill_view` when
relevant. A recorded experience does not establish that its commands still work
on another machine or version.

## Review a batch

```text
/learn
/learn review
/learn history
/learn history <run-id>
/learn undo <run-id>
```

`/learn review [skill-name]` starts an ordinary conversation using the current
model and reasoning effort. The first turn reads automatic skills and their
sources, proposes concrete changes, and describes useful verification. It is
read-only: the model cannot save edits or execute procedures in that turn.
User-owned skills remain protected review targets.

Reply with a question, a correction, “only change the second item”, or “continue”.
The selected changes use the existing skill journal and undo mechanism. “Continue”
covers the concrete editing and verification plan just presented. Content review,
saved edits, actual execution, and successful verification are reported separately.

Each snapshot includes at most four skills and 24,000 content/source characters.
The model may make multiple normal tool calls; the former separate 90-second,
4,096-token reviewer request is no longer used by the command. A successful apply
advances the shared batch cursor. Oversized and protected entries are reported as
not checked. A named review targets that skill without moving the batch cursor.

Snapshots are session-bound and kept in memory while you discuss the proposal.
A restart requires a fresh snapshot and reconciliation with the earlier proposal.
Changed files, invalid actions, and incomplete batch decisions are rejected. The
library is not locked while waiting for your reply. Applied versions and reasons
remain in `.astra/skills-learning/`; `/learn history` and `/learn undo` still work.
Undo refuses to overwrite subsequent edits. There is no background review.

Ctrl+C cancels the active conversation turn. A message sent during generation
steers that same turn and does not lift its read-only restriction; finish the
proposal before authorizing execution in a later turn. Already committed changes
remain in history when a turn is interrupted.

`/learn mode off` disables direct automatic saving. `/learn mode review` enables
it; the legacy name `review` does not turn on scheduled maintenance. Explicit
`/learn review` remains available while saving is off.

## Migrate an older library

```text
/learn migrate
```

Migration backs up the old SQLite store and imports eligible automatic skill
summaries using the existing file transaction and undo mechanism. It records
original IDs and destinations, so repeating migration does not duplicate skills.
Name collisions and old patches cannot overwrite user skills. Original database
records remain available as history. The old candidate queue itself (the
reviewer, trials and the `/learn legacy` commands) is retired; `/learn` shows how
many candidates are not yet migrated.

Observations and environment notes stay in the historical archive rather than
becoming skills. They can be retrieved through
[local history search](local-history-retrieval.md). A historical candidate count
therefore need not equal the automatic-skill count or the review batch size.

## Maintenance references

The conversation entry is in `agent/runtime/command_workflows.py` and its
operation adapters in `agent/cli/conversation_commands.py`. Storage, migration,
history and undo remain in `skill_learning.py`, `skill_curation.py`,
`skill_provenance.py`, `skill_migration.py` and `agent/cli/learning_commands.py`.
Tests cover ownership, cursor progress, invalid output, concurrent changes,
migration and undo. The optional `scripts/skill_curation_acceptance.py` exercises
copies of historical data; keep source records and model outputs out of Git.
