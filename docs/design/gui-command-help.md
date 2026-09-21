# Composer command help

The user requested a compact composer and command help like the supplied Codex
menu, including discoverable Bar, Minimal and installation-local mode actions. Extend the
approved GUI interaction design with an anchored slash menu. The existing full
command palette remains useful for forms; a separate help page would interrupt
typing, so the composer uses the same command catalogue directly.

- Typing `/` at the beginning of a single-line draft shows Chinese feature names,
  descriptions and command spellings. Filter locally by spelling or description;
  a known command exposes its subcommands. No model or backend request is made
  while browsing suggestions.
- Prioritize the current isolated mode's actions and explain how to return to
  Work before entering another mode. Use only modes available in this install.
  Saved mode sessions open the existing history picker with the correct namespace.
- Arrow keys select; Tab, Enter or clicking completes the draft. Completion never
  executes an operation. A subsequent Enter or Send executes a completed command
  through the existing dispatcher. Escape keeps the draft and dismisses help.
  IME composition, Shift+Enter, attachments, arbitrary arguments and ordinary
  multiline messages retain their normal behavior.
- Keep the popup above the composer, scroll its list and preserve input focus.
  The empty input is one line, grows with content to 220 px, then scrolls.
- Validate matching and namespace routing with focused tests, then use the
  existing isolated Electron acceptance harness to check keyboard/pointer use,
  no accidental dispatch, mode history, narrow/dark layouts and draft sizing.

This changes presentation and navigation only. Command effects, permissions,
conversation isolation and model behavior remain owned by the existing backend.

## Verification — 2026-09-21

- GUI unit tests: 47 passed. TypeScript and production GUI build passed.
- Existing Electron acceptance harness: 34 scenarios passed using isolated data,
  the real Python backend and a loopback model fixture. Completion made no model
  calls or session writes; Bar/Minimal history kept the correct namespace.
- Inspected the compact list in light/dark themes and at 1000/1320 px. Verified
  empty-input height below 105 px, multiline growth and the 220 px scroll bound.
- The delegate acceptance waits for all three projected rows before asserting
  their labels; receiving the first backend status does not imply all rows have
  reached the renderer yet.

Windows and real provider authentication were not part of this UI-only check.
