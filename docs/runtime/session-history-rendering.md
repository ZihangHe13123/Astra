# Atomic session history rendering

The confirmed `/session` defect renders the restored assistant reply before the
user message, then repeats that reply. Saved session messages contain one copy.
Ink renders the replacement lines using the old Static cursor before a separate
state update remounts Static and prints the full history.

The fix keeps `historyGeneration` in `StreamingDisplayState` alongside
`historyLines`. Both `replaceHistory` and `clearHistory` advance the generation
in the same reducer transition that replaces the lines. App reads both from that
state. Normal streaming and activity appends keep the generation unchanged.

This preserves terminal scrollback and the existing bounded history restore.
Backend persistence and missing replies from older provider failures are outside
this change.

Validation uses the real App and non-debug Ink output with a fake backend:
restore a conversation after a session command, switch between histories of
different lengths, restore an empty session, and append messages after reset.
Each restored message must print once in canonical order. Run the regression
before and after the fix, related rendering tests, the full TUI suite, type
checking, and the production build.

## Validation (2026-09-20)

- The real App regression failed before the fix: `ASSISTANT_GREETING` appeared
  twice instead of once. It passes with atomic replacement, including multiline
  replies, repeated restores, empty history, and post-reset output.
- `node --import tsx --test src/*.test.ts src/*.test.tsx src/components/*.test.ts
  src/components/*.test.tsx` from `ui-tui`: 432 passed, none failed or skipped.
- `npm run build` from `ui-tui`: TypeScript checking and the production bundle
  succeeded. The render regression uses a fake backend and real Ink; it does not
  modify saved conversations or call a model service.
