# Open design questions

This directory contains proposals that still need design decisions and validation.
It is not a list of shipped features or permission to start implementation.

- [Peer Agent workspace](peer-agent-workspace.md): collaboration across explicitly
  paired installations; transport, wake-up and file acceptance remain open.
- [Phone companion](phone-companion.md): the optional remote interface for a paired
  phone; push notifications, voice input and a native app remain open.
- [Personal thread](personal-thread.md): one long-lived conversation shared by the
  desktop and a paired phone, home of proactive messages and of work handed to project
  conversations; other computers as peers remain open.
- [Loading tools on demand](tool-disclosure.md): a small core set in every request, the
  other tools found by family with `tool_search` and run through `call_tool`; shorter core
  descriptions remain open.
- [Subagents](subagents.md): one kind of subagent, named and kept for the conversation;
  Agent Teams merged into `delegate_*`, with optional groups that write to each other.
- [Canvases](canvas.md): slide decks and documents beside the conversation, with export,
  comments and kept versions; the phone view and more templates remain open.
- [Outbound request byte budget](llm-request-byte-budget.md): measure serialized
  requests independently of the existing token budget before choosing a policy.

Implemented Computer Use constraints live in the [current guide](../docs/macos-computer-use.md).
The standalone helper commands are documented [with their source](../scripts/cu-tools/README.md).
Keep personal environment inventories, discussion transcripts and execution logs
in ignored local artifacts rather than this directory.
