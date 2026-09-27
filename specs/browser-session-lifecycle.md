# Browser ownership and MCP recovery

Approved scope (2026-09-16): bind browser resources to a live conversation,
provide explicit release, distinguish uncertain MCP writes from safe reads, and
limit the environment inherited by browser children. Keep the native CU helper.

## Browser lifecycle

- Each activation owns a fresh browser session and its logical tab handles.
  Persisted SQLite rows are history, not authorization to reuse a connection.
- Serialize browser operations. On session end or `/browser stop`, invalidate
  handles immediately, reject queued work from the old activation, drain the
  dispatched operation, then release connections and the extension endpoint.
- Cleanup is an owned, shielded task. Cancelling the caller cannot admit new
  work early. Failed cleanup keeps admission closed; explicit stop can retry.
- Release attached browser connections without terminating the user's browser.
  Close Astra-owned CDP resources. Lazily reconnect with fresh handles.
- All local frontends support `/browser [status|stop]`; the agent has a matching
  `browser_stop` tool. No idle timeout, lock stealing, or implicit process kill.
- Amended 2026-09-27: a runtime that cannot take the endpoint files a release
  request and waits briefly; the owner releases through the same stop path, only
  while it has no turn or browser operation running. Still no idle timeout,
  stealing, or kill.

## MCP recovery

- Only configured reads or read-only annotated network tools get read recovery
  hints. Missing annotations are conservative; approval rules remain intact.
- Bound remote calls to the configured timeout. A dispatched write with no
  response has an unknown outcome and must not be repeated automatically.
- Propagate user cancellation, discard failed connections, and preserve the
  no-replay instruction in connection diagnostics. Reconnection is independent
  of invocation replay. Queued definitions from a replaced connection fail
  before dispatch; callers must rediscover tools and obtain fresh observations.
- An opaque remote reference remains the remote server's responsibility; MCP
  transport alone cannot prove that a UI selector is fresh or an action undone.

## Child environments

Use an explicit allowlist for OS paths, temporary directories, locale, GUI/DBus,
Windows/WSL interoperability, and proxy/certificate settings. Exclude model and
mail credentials and code-injection variables. Apply only to browser/relay
children, including discovery/launch helpers, not general shell or MCP servers.

## Validation

Exercise session switch/reopen, queued and in-flight work, cancelled cleanup,
cleanup failure, and real endpoint release/reacquisition with temporary sockets.
Use fake remote calls to prove timeout/disconnect never cause a second write,
read recovery works, and replaced connections reject old definitions. Verify
launch paths pass a scrubbed environment and required platform keys survive.
Run existing browser/MCP/command tests, Python checks, and TUI typecheck/build.
Real desktop actions and native Windows/WSL behavior require separate acceptance.
