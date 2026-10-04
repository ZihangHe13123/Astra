# Project instructions and conversation application

`AGENTS.md` is ordinary Markdown for any coding assistant. Root instructions apply throughout the project; nested files contain area-specific rules. Explicit user instructions take precedence. Astra accepts a directory's `AGENTS.override.md` instead of its `AGENTS.md`, and path-scoped rules under `.astra/rules` or `.agents/rules`. These optional extensions are not required by other agents.

The repository keeps one shared source. Its `CLAUDE.md` files use `@AGENTS.md` imports for Claude Code installations that require them. Modern Claude Code can also read AGENTS.md directly; consult its [instruction-file settings](https://code.claude.com/docs/en/memory#agentsmd). Tools that do not discover AGENTS.md automatically can be configured to read it; see [AGENTS.md](https://agents.md/). No global agent settings are changed by this repository.

Astra loads instructions only from trusted projects. Main agents and delegates share the same loader. Regional guidance is discovered on first access and shares the root instruction byte budget. Dependency/build directories, external paths, and symlinks escaping the project are excluded. Truncation and budget exhaustion are visible through guidance status. Instructions are context and do not grant tool permissions.

## Applied and saved state

A conversation records its applied static guidance, skill catalog, and stable tool declarations. Subsequent disk edits are saved normally and become available to a new conversation. Cold restore and response branches retain their relevant applied state. New regional instructions are appended at existing runtime-context or completed tool-result boundaries, leaving already-sent history intact.

Use these commands in CLI, TUI, or desktop:

```text
/guidance
/guidance refresh --now
/skills create --template example-skill "When to use this skill"
/skills create --template another-skill "When to use this skill" --now
```

`/guidance` reports applied files, applied skill count, and pending disk changes. `refresh --now` deliberately applies saved instructions and catalogs to the current idle conversation; the provider's request prefix may refresh. `--now` on direct template creation uses the same boundary. `/skills create` without `--template` retains the existing conversational draft-and-review workflow. Immediate application is rejected before mutation while a task or skill review is running. Ordinary skill saves, including reviewed learning, remain pending.

Workdir or installation/skill-store changes select another scope. Current trust and tool execution permissions remain authoritative. Minimal and Local modes do not inject project guidance or skill catalogs. Legacy conversations capture guidance once on their next new turn. Existing system-prompt and runtime-context projections continue to provide model-specific request handling.
