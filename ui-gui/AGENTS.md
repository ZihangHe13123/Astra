# Desktop contracts

Read the root `AGENTS.md` and `ui-core/AGENTS.md` before editing shared behavior.

- The desktop consumes backend events and shared UI state. Keep transport/session rules in their existing owners; do not implement a second runtime in React.
- Reasoning, tool preparation, rich Markdown, and navigation are display concerns. Rendering or restoring them must not modify the model's canonical conversation or trigger tool execution.
- Retry and response-version selection keep the user in the current conversation. Each version owns its continuation; retain the selected branch and reject stale events after a switch.
- Session previews and history inspectors are read-only. Validate source identity, revision, and branch before applying navigation or replacement state.
- Virtualized rows need measured, stable geometry before scrolling. Test both ordinary and narrow windows, and wait for asynchronous state with bounded polling.
- Use existing content sanitization, protocol types, and session ownership checks. A disconnect or auxiliary-panel failure should preserve the conversation view.

## Checks

From `ui-gui` run `npm test`, `npm run typecheck`, and `npm run build`. Run `npm run test:e2e` for changes to session selection, retry, history, transport, or rendering. The Electron smoke uses an isolated backend and state directory; do not point fixtures at a user's active Astra installation.

Check the backend suites for any changed event shape. Keep screenshots and build outputs outside tracked source. Report macOS checks separately from Windows CI or device validation.
