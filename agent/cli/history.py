"""Restore terminal tool details from the active conversation and saved results."""

import math
import re
from collections import defaultdict, deque


_RESULT_HEADER = re.compile(r"^\[Tool result: ([^\n|]+) \| status: (success|error)\]\n")
_RESULT_END = "\n[End tool result. Continue the current user request from this result; do not restart from assumptions made before the tool call.]"
# Model-facing notices the result envelope may add after the tool's own text.
_RESULT_NOTICES = re.compile(r"(?:\n(?:Repeated call|Repeat limit|Result completeness): [^\n]*)+\Z")


def history_tool_results(messages: list[dict], saved_results: list[dict], limit: int = 100) -> list[dict]:
    """Use durable timing/output when available, or a known saved result envelope.

    Only calls still belonging to this conversation are included. This avoids
    reviving cleared activity or displaying another session's tool results.
    """
    saved_by_id: dict[str, list[tuple[int, dict]]] = defaultdict(list)
    for order, result in enumerate(saved_results):
        if result.get("id"):
            saved_by_id[str(result["id"])].append((order, result))
    calls = {
        str(call.get("id")): call.get("function", {}).get("name", "")
        for message in messages if message.get("role") == "assistant"
        for call in message.get("tool_calls") or []
    }
    recent = deque((m for m in messages if m.get("role") == "tool"), maxlen=max(1, limit))
    restored = []
    for message in reversed(recent):
        call_id = str(message.get("tool_call_id") or "")
        content = str(message.get("content") or "")
        header = _RESULT_HEADER.match(content)
        candidates = saved_by_id.get(call_id, [])
        saved_order, saved = candidates.pop() if candidates else (None, None)
        if saved is not None:
            result: dict[str, object] = {key: str(saved.get(key) or "") for key in ("name", "output", "error")}
            duration = saved.get("duration_ms")
            if isinstance(duration, (int, float)) and not isinstance(duration, bool) and math.isfinite(duration) and duration >= 0:
                result["duration_ms"] = duration
            if saved.get("artifact_path"):
                result["artifact_path"] = str(saved["artifact_path"])
            if saved.get("output_truncated"):
                result["output_truncated"] = True
        elif header:
            body = _RESULT_NOTICES.sub("", content[header.end():].removesuffix(_RESULT_END))
            failed = header[2] == "error"
            result = {"name": header[1], "output": "" if failed else body, "error": body if failed else ""}
        elif content == "[Atomic interaction committed successfully.]" and calls.get(call_id):
            result = {"name": calls[call_id], "output": content, "error": ""}
        else:
            # Older arbitrary tool text has no reliable completion status.
            continue
        if result["name"]:
            if call_id:
                result["call_id"] = call_id
            restored.append((saved_order, result))
    # Parallel calls are appended to conversation history in call order, while
    # LAST follows their actual completion order whenever the journal has it.
    if all(order is not None for order, _ in restored):
        restored.sort(key=lambda item: item[0])
    else:
        restored.reverse()
    return [result for _, result in restored]
