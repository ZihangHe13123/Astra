"""MCP stdio server that shows Astra's tools to the Claude Code CLI and never runs them.

The CLI lists these tools to the model like its own, so Claude calls them natively. Astra stops
the CLI after that first response, runs the calls itself and replays the result next time; the
CLI's permission mode denies every call before it could reach here, and a call that does arrive
is answered with an error instead of any effect.
"""
from __future__ import annotations

import json
import sys


def respond(message_id, result=None, error=None) -> None:
    payload = {"jsonrpc": "2.0", "id": message_id}
    payload.update({"error": error} if error is not None else {"result": result})
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main(tools_path: str) -> None:
    with open(tools_path, encoding="utf-8") as handle:
        tools = json.load(handle)
    for line in sys.stdin:
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if not isinstance(message, dict) or "id" not in message:
            continue  # Notifications need no answer.
        method = message.get("method")
        if method == "initialize":
            requested = (message.get("params") or {}).get("protocolVersion") or "2025-06-18"
            respond(message["id"], {"protocolVersion": requested, "capabilities": {"tools": {}},
                                    "serverInfo": {"name": "astra", "version": "1"}})
        elif method == "tools/list":
            respond(message["id"], {"tools": tools})
        elif method == "tools/call":
            respond(message["id"], {"content": [{"type": "text", "text": "Astra runs this tool itself."}],
                                    "isError": True})
        elif method == "ping":
            respond(message["id"], {})
        else:
            respond(message["id"], error={"code": -32601, "message": "Method not found"})


if __name__ == "__main__":
    main(sys.argv[1])
