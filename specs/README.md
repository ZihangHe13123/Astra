# Open design questions

This directory contains proposals that still need design decisions and validation.
It is not a list of shipped features or permission to start implementation.

- [Peer Agent workspace](peer-agent-workspace.md): collaboration across explicitly
  paired installations; transport, wake-up and file acceptance remain open.
- [Phone companion](phone-companion.md): the optional remote interface for a paired
  phone; push notifications, voice input and a native app remain open.
- [Outbound request byte budget](llm-request-byte-budget.md): measure serialized
  requests independently of the existing token budget before choosing a policy.

Implemented Computer Use constraints live in the [current guide](../docs/macos-computer-use.md).
The standalone helper commands are documented [with their source](../scripts/cu-tools/README.md).
Keep personal environment inventories, discussion transcripts and execution logs
in ignored local artifacts rather than this directory.
