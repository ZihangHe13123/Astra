# Shared UI contracts

Read the repository's `AGENTS.md` first. This package is shared by the desktop and terminal clients.

- Protocol types, event normalization, and reducer state belong here when both clients need them. Keep Electron, DOM, Ink, and platform-specific side effects in their adapters.
- Preserve conversation, task, response, and branch identities. Reject stale events rather than letting one session's response alter another session.
- Distinguish unexecuted tool preparation from calls and results. Display updates cannot imply execution, completion, or durable saving.
- Reducers preserve canonical event ordering and each response version's continuation. Retry and reconnect behavior must work through the same normalized event path in both clients.
- Validate protocol additions with real event fixtures and both clients. Assert state transitions and identity relationships instead of testing source text.

## Checks

From `ui-core` run `npm test`, `npm run typecheck`, and `npm run build`. When shared state or protocol changes, also run the relevant `ui-gui` and `ui-tui` tests and type checks. Run desktop Electron acceptance when session or retry behavior is affected.
