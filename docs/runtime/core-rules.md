# Astra core Skill: model-directed loading

The user approved this replacement on 2026-09-11. It supersedes the earlier
regex-based, session-sticky chat/work prompt projection.

## Runtime contract

- Keep persona source text, identity, hard rules and expression style verbatim.
  Add one short, stable instruction describing when and how to read core rules.
- Register the packaged `agent/runtime/astra.md` as the built-in `astra-core`
  Skill. List it before project skills and expose its version in the catalog and
  `skill_view` result. Its priority means workflow reading order, not authority
  above system or user instructions. Project skills cannot shadow or modify it;
  it remains readable outside trusted projects and from installed wheels.
- The model decides whether a request needs work. Ordinary conversation and
  conceptual explanation do not require loading the full rules. Before code,
  debugging, testing, review, file operations, browser or desktop execution,
  read `astra-core`, then the relevant specialist skills.
- Load the body only in response to the model's `skill_view` call. Do not classify
  user text in Python, change prompt mode after work, discard tool plans to force
  a reread, or automatically append/reinject the core body in turn context.
- Reuse a complete, current-version result still visible in context. A summary
  or a past claim of reading is insufficient. After compaction removes it, the
  model must read it again when work requires it. There is no durable "read"
  boolean and no automatic tool-dispatch gate.
- Keep other skills' existing refresh behavior, project trust, custom prompts,
  Minimal mode and restricted tool surfaces intact.
- A second built-in skill, `interactive-cards` (`agent/runtime/interactive_cards.md`),
  teaches the card format of the desktop. The catalog lists it only when the backend
  was started by the desktop (`ASTRA_UI_SURFACE=gui`), since no other client shows
  cards. It follows the ordinary rules: its name is reserved everywhere, it stays
  readable wherever a conversation that lists it is continued, and the catalog a
  conversation started with changes only at an explicit refresh.
- `ASTRA_CORE_RULES_MODE=skill` is the default. `full` restores the original
  complete core identity without changing persona text. The retired
  `ASTRA_LAZY_WORK_RULES` setting has no effect. Configuration changes take effect
  after restarting Astra; ordinary chat/work transitions never change the mode.

## Cache and migration

The default prompt, catalog and tool schemas remain stable across chat/work
transitions. A core read adds an ordinary tool result to history without editing
earlier messages. Its body remains in subsequent history, so returning to chat
does not remove its token cost. Migration/configuration changes need a new prefix;
provider retention and unrelated dynamic context may still affect cache hits.

Remove the rejected classifier and its session state. Ignore old `work_prompt`
headers, normalize known personas to the selected configuration, and clear old
core projection state during migration so it cannot keep an obsolete full/chat
head alive. Preserve custom prompt contents and the existing date-anchor and
general system-projection fixes.

## Verification and limits

Check original core/persona hashes in full mode, stable bootstrap assembly in
skill mode, read-only built-in precedence, project trust, wheel resources,
unchanged tool schemas, full tool results without duplicate core injection,
chat/work/chat prefixes, session restore and compaction. Run the relevant and
full Python gates plus static checks and wheel smoke.

Separately exercise real DeepSeek decisions on ordinary chat, unfamiliar casual
phrasing, direct engineering tasks, browser/desktop work, follow-up work and
compacted history. Record tool order, duplicate reads, latency and provider cache
usage. Use isolated fixtures and no live external writes. Unit tests establish
runtime behavior, not model compliance. Report observed misses honestly; if
model-directed reading is insufficient, the complete constant prompt remains
the explicit fallback.

Run the opt-in real-model probe with:

```sh
.venv/bin/python -m agent.evals.core_skill_probe --live --output .astra/validation/core-skill-live.json
```

It uses the configured official DeepSeek account and isolated read-only fixture
tools. Its report records the request containing each tool plan: reading a Skill
and planning a dependent action in the same request does not count as compliance.
The compaction scenario deliberately removes the body; it measures rereading,
not the quality of the summarizer. Browser/desktop fixtures test Skill selection,
not real GUI interaction. Existing specialist-Skill refresh and compaction can
still rewrite historical context even while the system/tool prefix stays stable.

For a manual check, restart Astra, open a fresh session, chat naturally, then ask
it to inspect a real project file. The first work sequence should show
`skill_view` for `astra-core`, followed by relevant specialist skills and file
tools. `/skills show astra-core` confirms availability only; manually displaying
the file is not evidence that the model chose to read it.
