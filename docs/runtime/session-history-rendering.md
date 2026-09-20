# Atomic session history rendering

The confirmed `/session` defect renders the restored assistant reply before the
user message, then repeats that reply. Saved session messages contain one copy.
Ink renders the replacement lines using the old Static cursor before a separate
state update remounts Static and prints the full history.

The approved fix keeps `historyGeneration` in `StreamingDisplayState` alongside
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
