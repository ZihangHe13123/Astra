# Working on Astra

These instructions apply to any coding assistant working in this repository. Follow the user's requested scope and preserve unrelated changes. More specific instructions in a subdirectory apply to that area.

## Repository map

- `agent/runtime/`: agent loop, model requests, tools, context, and persistence. Read `agent/runtime/AGENTS.md` before changing it.
- `agent/cli/` and `agent/ui/`: entry points, backend command/event handling, and session operations. Follow the runtime contracts when changing these adapters.
- `ui-core/`: shared frontend state and protocol. Read `ui-core/AGENTS.md`.
- `ui-gui/`: Electron and React desktop. Read `ui-gui/AGENTS.md`.
- `ui-tui/`: Ink terminal client, using the shared UI core.
- `native/`: platform helpers; validate native changes with the relevant platform's tools and a real application when needed.

## Development rules

- Read the implementation and relevant tests before proposing a change. Reuse existing loaders, projections, storage, and transport adapters.
- Keep model-facing core tools small. Prefer extending existing functionality, a CLI command with a skill, or an optional integration before adding a core tool.
- Preserve model-request prefixes within a conversation. Keep presentation-only information out of canonical model history. Explicit changes, context compression, and authority revocation must have documented application rules.
- Use an isolated checkout when the current checkout has unrelated edits. Do not reset, stage, or overwrite another task's changes.
- Store project-wide guidance here and area guidance in nested `AGENTS.md` files. Keep them concise, tool-neutral, and free of credentials, personal machine paths, or agent-specific persona instructions. `CLAUDE.md` files only import this shared source.

## Validation

From the repository root, run Python checks with `.venv/bin/python -m pytest <relevant tests>` and `.venv/bin/python -m ruff check <changed Python files>`. On Windows use `.venv\Scripts\python.exe` instead. Run `.venv/bin/python -m pyright` for runtime changes.

Run `npm test`, `npm run typecheck`, and the relevant build in `ui-core` or `ui-gui`. In `ui-tui`, run `node --import tsx --test src/*.test.ts src/*.test.tsx` and `npm run build` (its prebuild includes type checking). Desktop acceptance is `npm run test:e2e` in `ui-gui` after building.

Test observable behavior through real entry points and temporary state directories. Assert relationships such as preserved history, scope isolation, and exactly matched results; do not use source-text regexes or freeze incidental catalog counts. Mocked tests, package checks, native checks, and real-device acceptance are separate evidence. Report which were actually performed. Release work also verifies packaging and the applicable macOS/Windows CI.

## Delivery

Commit only the authorized changes. Keep private development material and machine-local configuration out of public adaptations. Merge, push, external messages, and deployment follow the user's authorization for the task; an instruction file does not grant extra authority.
