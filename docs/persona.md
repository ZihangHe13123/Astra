# Public Lyra persona

[Home](../README.md) · [Documentation](README.md) · [简体中文](zh-CN/persona.md)

Astra bundles one public persona, `lyra`, for both work and everyday conversation.
Lyra is a warm, direct AI collaborator: match the user's language, use evidence,
complete authorized work and state uncertainty. The preset does not assign the
user a name, biography or private relationship, or invent a shared history.

Use `/persona` to inspect the available profile and `/persona lyra` to select it.
The saved selection takes precedence over `AGENT_PERSONA`, then Astra uses the
default `lyra` profile. `/mode` controls reasoning effort separately; `/think`
controls the display of reasoning. The public persona leaves temperature to the
provider unless an active runtime mode supplies its own sampling settings.

## Private local customization

An optional `persona.local.json` in Astra's private state directory supplies local
profiles or creative-mode prompts. For a source installation the default is
`.astra/persona.local.json`; `ASTRA_HOME` moves the state directory, and
`ASTRA_PERSONA_FILE` selects an explicit file. Astra never discovers this file in
an unrelated working project. Keep it outside Git and public artifacts.

```json
{
  "schema": 1,
  "profiles": [
    {
      "name": "personal-lyra",
      "description": "My local collaboration style",
      "persona": "Use concise replies and explain important tradeoffs.",
      "version": 1,
      "persona_layer": "style"
    }
  ]
}
```

The optional `mode_prompts` object accepts complete prompts for `bar_atomic` and
`bar_stream`. These replace only the relevant prompt text; the
controllers still enforce session isolation and tool access. Bar overrides must
retain the corresponding scene-tool contract. Invalid configuration reports an
error instead of silently switching persona and overwriting saved settings.

An explicitly supplied local profile takes precedence over the public migration
alias of the same name. This allows a private installation to preserve its own
profile and session state when updating public code. New installations without
this file receive only public Lyra. Editing a prompt does not update an already
running session automatically; select the profile or reopen the mode to load it.

## Definition and session state

The persona framework separates a versioned stable definition from session state.
State can record a working mode and context supplied for that session; the
bundled definition has no private relationship baseline. Persona style does not
change tool permissions, factual standards or the meaning of task completion.
The [core rules](runtime/core-rules.md) remain a separate part of the contract.

When there is no explicit local profile for a retired preset ID, that ID
resolves to public Lyra. Loading a session with one of those IDs rebuilds its system
prompt, clears that preset's old mode/relationship/affect state and discards its
cached system projection. An exact fingerprint also recognizes the bundled
pre-versioning prompt forms without shipping their retired prose. The next
session save persists the normalized form; conversation messages are preserved.
Arbitrary custom prompts are not identified by broad text matching or rewritten.

This compatibility behavior is not a privacy export tool: it does not redact
conversation messages, personal memory, custom prompts or Git history. Those
materials must remain private when preparing a public source repository.

## Bar easter egg

`/bar` opens an original fictional cafe/bar scene suitable for a general audience.
It uses its own session namespace and does not draw on work-session memory.
Its prompts distinguish fiction from the user's real identity and experiences.
Scene tools affect only the local visualization. Exit with `/bar leave` to return
to ordinary work.

## Optional local mode

An installation can keep a private mode in `.astra/local_mode.py` (or an explicit
`ASTRA_LOCAL_MODE_FILE`). Nothing is bundled or enabled by default. The module is
trusted local Python code, loaded once at backend startup, never discovered in a
working project. Keep it outside Git. Invalid modules stop with a configuration
error rather than silently changing behavior.

The module exports `API_VERSION = 1` and `create_mode(agent)`, returning a
`LocalMode` from `agent.runtime.local_mode`. Supply a controller, a unique slash
command, label, description, private session namespace and status template.
Optional UI text supplies the header, status and input placeholder. The controller
contract is `ModeController` in that module: enter/switch/leave plus undo/retry.
It must park and restore the work context, set `agent.local_mode` before lifecycle
callbacks, disable tools and all memory/task/context providers, and preserve
session data on failures. The host supplies command routing, cancellation,
separate session discovery and guards against work-memory/task exports.

A controller that replaces agent settings can also return the work agent's own
values, by attribute (`yolo` included), from an optional `work_settings()`. A
WeChat or QQ turn that borrows the agent while the mode is open then runs with
them, as it does during Bar and Minimal; without that method it runs with the
mode's settings.

Source updates preserve ignored local modules. Controller code stays local and
must remain compatible with this interface; changes are picked up on restart.
Private mode-prompt keys in `persona.local.json` are retained as opaque local data.

## Validation

Persona regression tests cover saved selections, restored state, cached prompt
projection, sampling precedence and replay/compression. Replay fixtures use
synthetic conversations and test runtime contracts, not live model quality.
`scripts/persona_ab.py` can compare two prompt layouts using a configured model;
it uses a fixed synthetic memory fixture and does not read the personal memory
store. A live run still sends those fixtures to the selected provider.
