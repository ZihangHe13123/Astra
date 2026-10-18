"""Each model-facing tool schema is pinned by a reviewed digest.

Tool schemas open every request and stay fixed for a conversation, so changing one
changes what the model is told and the cached prefix of every new conversation. The
digests make such a change deliberate: a failure names the tool and its new digest,
and the fixture records why each schema last changed. A tool the fixture does not
know fails; a tool this platform does not offer is not required, and a platform
whose schema text legitimately differs may list its own digest next to the others.

To accept a reviewed change: UPDATE_TOOL_DIGESTS=1 pytest tests/test_tool_schema_digests.py,
then write the reason for every entry it left without one.
"""

import hashlib
import json
import os
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

from test_backend_task_protocol import _SlowOpenAIHandler, _start_question_protocol_backend, _stop_protocol_backend
from test_session_lifecycle_protocol import send

FIXTURE = Path(__file__).parent / "fixtures" / "tool_schema_digests.json"


def digest(tool: dict) -> str:
    text = json.dumps(tool, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def sent_tools(tmp_path) -> list[dict]:
    """The tool schemas of a real backend's first model request in an ordinary conversation."""
    seen: list[list[dict]] = []

    class Handler(_SlowOpenAIHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
            if "messages" not in payload:
                self._json({"total_tokens": 100})
                return
            if payload.get("stream") and payload.get("tools"):
                seen.append(payload["tools"])
            chunk = {"id": "fixture", "object": "chat.completion.chunk", "model": "Qwen3.6-35B-A3B",
                     "choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}]}
            if not payload.get("stream"):
                self._json({"id": "fixture", "object": "chat.completion", "model": "Qwen3.6-35B-A3B",
                            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}]})
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    workdir = tmp_path / "work"
    workdir.mkdir()
    proc, _, _, wait_for = _start_question_protocol_backend(
        tmp_path, server.server_port, "schema_digest_session", workdir=workdir,
        env_overrides={"ASTRA_CHANNEL_CONFIG": str(tmp_path / "no-channels.json"),
                       "ASTRA_CONTEXT_INDEX_EMBEDDING": "off"},
    )
    try:
        wait_for(lambda e: e.get("type") == "startup_banner", timeout=30)
        send(proc, {"type": "message", "text": "hello"})
        wait_for(lambda e: e.get("type") == "done", timeout=30)
    finally:
        _stop_protocol_backend(proc)
        server.shutdown()
        server.server_close()
    assert seen, "the backend sent no request with tools"
    # A description may name the workspace (apply_patch does); that path is this test's own.
    text = json.dumps(seen[0], ensure_ascii=False)
    for path in sorted({str(workdir.resolve()), str(workdir)}, key=len, reverse=True):
        text = text.replace(json.dumps(path)[1:-1], "<workspace>")
    return json.loads(text)


def test_every_tool_schema_the_model_receives_matches_its_reviewed_digest(tmp_path):
    tools = sent_tools(tmp_path)
    current = {tool["function"]["name"]: digest(tool) for tool in tools}
    pinned = json.loads(FIXTURE.read_text(encoding="utf-8")) if FIXTURE.exists() else {}

    if os.getenv("UPDATE_TOOL_DIGESTS") == "1":
        updated = dict(pinned)
        for name, value in current.items():
            entry = pinned.get(name)
            if entry is None or value not in entry["sha256"]:
                updated[name] = {"sha256": [value], "reason": ""}
        FIXTURE.write_text(json.dumps(dict(sorted(updated.items())), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        pinned = updated

    changed = {name: value for name, value in current.items() if name in pinned and value not in pinned[name]["sha256"]}
    unknown = sorted(set(current) - set(pinned))
    unexplained = sorted(name for name, entry in pinned.items() if not entry.get("reason", "").strip())
    assert not changed and not unknown and not unexplained, (
        "Model-facing tool schemas changed. Review the change, then run with UPDATE_TOOL_DIGESTS=1 "
        "and give each updated entry a reason.\n"
        f"changed: {changed}\nnew: {unknown}\nwithout a reason: {unexplained}"
    )
