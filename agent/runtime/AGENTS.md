# Runtime contracts

Read the repository's `AGENTS.md` first. These rules also apply when changing the CLI and UI adapters that own runtime state.

- `AgentContext` holds canonical messages; provider request projections and display metadata remain separate. Never persist `api_content` or inject restored reasoning into model history merely to display it.
- `SessionStore` owns durable session storage. Preserve legacy reads, complete tool-call/result boundaries, cancellation draining, and branch-specific artifacts. Stage and validate session/branch changes before replacing the active context.
- `SessionLease` protects writable sessions. A preview or copied context must not release the live owner's lease. Use the existing ownership mechanism.
- Root and discovered project instructions share `ProjectInstructions`, its trust boundary, and its byte budget. Cached instructions never grant tool permissions. Minimal and Local modes retain their zero-injection behavior.
- Static guidance and skill catalogs are applied per conversation. Ordinary disk edits are pending until a new conversation or explicit application. Append new regional guidance through the existing projection/tool-result boundary; preserve already-sent history.
- Main agents, delegates, worker continuation, CLI, GUI, and channels must agree on the contract being changed. Inspect sibling entry points before calling a fix complete.
- A timed-out or disconnected write can have an unknown outcome. Observe and report it; do not replay it automatically.
- Persistent jobs use an explicit home, a fresh occurrence-specific session and kernel ownership locks. Commit schedule advancement and the occurrence before dispatch; keep terminal outcomes immutable. Inbox delivery is a separate transaction with a durable receipt. Never turn a recovered unknown execution into an automatic retry or adopt a GUI/channel conversation as a job context.

## Checks

From the repository root, use the configured virtual environment:

```sh
.venv/bin/python -m pytest tests/test_project_instructions.py tests/test_project_trust.py tests/test_prompt_cache_architecture.py tests/test_context_system_suffix.py tests/test_delegate_conversation_resume.py
.venv/bin/python -m ruff check agent/runtime agent/cli agent/ui
.venv/bin/python -m pyright
```

On Windows substitute `.venv\Scripts\python.exe`. Select additional behavioral suites for the paths changed, especially session restore, response branches, multimodal replay, and isolated modes. Use temporary `ASTRA_HOME` and project directories; test A-to-B-to-A when state is scoped by project or installation.
