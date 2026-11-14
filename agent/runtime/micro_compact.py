"""Clearing old tool results, for a conversation rebuilt to fit a budget.

A conversation in use never clears them: a clear rewrites an earlier message, and a provider
then reads everything after it again uncached (AgentContext.compress_if_needed).
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass

from .token_estimator import estimate_value_tokens

_ARTIFACT_RE = re.compile(
    r"(?:artifact(?:_path)?|Full output artifact)\s*[:=]\s*([^\]\n,}]+)",
    re.IGNORECASE,
)
_HASH_RE = re.compile(r"(?:sha256|hash)\s*[:=]\s*([a-f0-9]{12,64})", re.IGNORECASE)
_ERROR_RE = re.compile(
    r"(?:\bstatus\s*[:=]\s*(?:error|failed)|\berror\s*[:=]|traceback|exception|"
    r"permission denied|approval required|exit code\s*[1-9])",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class MicroCompactStats:
    cleared_results: int = 0
    saved_chars: int = 0
    shadowed_tokens: int = 0
    replacement_tokens: int = 0

    @property
    def saved_tokens(self) -> int:
        return max(0, self.shadowed_tokens - self.replacement_tokens)


def _positive_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


def _process_payload(content: str, tool_name: str) -> dict | None:
    if tool_name not in {"process_poll", "process_read"}:
        return None
    try:
        value = json.loads(content)
    except (ValueError, RecursionError):
        return None
    if not isinstance(value, dict):
        return None
    process_id = value.get("process_id")
    if not isinstance(process_id, str) or not 0 < len(process_id) <= 256:
        return None
    return value


def _process_stamp(content: str, tool_name: str, payload: dict) -> str:
    # Historical observations, never a new claim about the live process.
    stamp = {
        "tool": tool_name,
        "process_id": payload["process_id"],
        "original_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "original_chars": len(content),
        "note": "Historical process observation; full result remains in the durable session. "
                "Use reread for the original page, next_byte_offset for continuation. "
                "Do not rerun the process merely because its output was compacted.",
    }
    for key in ("status", "exit_code", "stream", "artifact_path", "byte_offset",
                "next_byte_offset", "offset", "next_offset", "eof",
                "total_bytes", "output_chars", "completed_at"):
        value = payload.get(key)
        if key in payload and (value is None or type(value) in (bool, int, float)
                               or isinstance(value, str) and len(value) <= 4096):
            stamp[key] = value
    if stamp.get("artifact_path"):
        stamp["artifact_stream"] = "combined"
    output = payload.get("content")
    if isinstance(output, str):
        stamp["output_excerpt"] = (output if len(output) <= 480 else
                                   output[:160] + "\n[... omitted ...]\n" + output[-320:])
        stamp["excerpt_complete"] = len(output) <= 480
        start = payload.get("byte_offset")
        stream = payload.get("stream")
        if type(start) is int and start >= 0 and stream in {"combined", "stdout", "stderr"} and output:
            stamp["reread"] = {
                "tool": "process_read",
                "arguments": {"process_id": payload["process_id"], "stream": stream,
                              "byte_offset": start, "max_chars": min(len(output), 100000)},
            }
    return "[Historical process result compacted; status and read position retained]\n" + json.dumps(
        stamp, ensure_ascii=False, separators=(",", ":"))



def _placeholder(content: str, tool_name: str) -> str:
    payload = _process_payload(content, tool_name)
    if payload is not None:
        return _process_stamp(content, tool_name, payload)
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    artifact = _ARTIFACT_RE.search(content)
    existing_hash = _HASH_RE.search(content)
    details = [
        "[Old tool result content cleared from the active prompt]",
        f"tool={tool_name or 'unknown'}",
        f"sha256={existing_hash.group(1) if existing_hash else digest}",
        f"original_chars={len(content)}",
    ]
    if artifact:
        details.append(f"artifact={artifact.group(1).strip()}")
    details.append(
        "The durable session still contains the bounded original result. "
        "Re-read its artifact or rerun the read-only tool if exact details are needed."
    )
    return "; ".join(details)


def micro_compact_tool_results(
    messages: list[dict],
    *,
    tool_risk: Callable[[str], str] | None = None,
    char_budget: int | None = None,
    keep_recent: int | None = None,
    clear_batch: int | None = None,
) -> tuple[list[dict], MicroCompactStats]:
    """Clear old compactable results while preserving protocol and active work.

    Only results before the latest real user request are eligible. Error,
    write, execute, and secret-bearing tool results are never cleared.

    The budget is for the results before that request, and they are cleared
    ``clear_batch`` at a time, oldest first, once that many have to go to meet
    it. A clear changes the request at the oldest result it touches, and the
    provider reads everything after that again at the uncached price; cleared
    one at a time, that is paid on nearly every turn of a long conversation.
    Nothing after that request counts, so what is cleared stays the same from
    one model request to the next while a turn adds results.
    """
    budget = char_budget or _positive_env("TOOL_RESULT_PROMPT_BUDGET_CHARS", 24_000)
    keep = keep_recent or _positive_env("TOOL_RESULT_KEEP_RECENT", 5)
    batch = clear_batch or _positive_env("TOOL_RESULT_CLEAR_BATCH", 10)
    if budget <= 0 or len(messages) < 3:
        return messages, MicroCompactStats()

    latest_user = max(
        (
            index
            for index, message in enumerate(messages)
            if message.get("role") == "user"
            and message.get("provenance") not in {"notification", "delegate", "synthetic"}
            and not str(message.get("content") or "").startswith("[SYSTEM-SUPPLIED")
        ),
        default=-1,
    )
    if latest_user <= 0:
        return messages, MicroCompactStats()

    call_names: dict[str, str] = {}
    for message in messages:
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            function = call.get("function") if isinstance(call, dict) else None
            name = (
                function.get("name") if isinstance(function, dict)
                else call.get("name") if isinstance(call, dict)
                else ""
            )
            call_id = str(call.get("id") or "") if isinstance(call, dict) else ""
            if call_id:
                call_names[call_id] = str(name or "")

    candidates: list[tuple[int, str, str]] = []
    total_chars = 0
    for index, message in enumerate(messages[:latest_user]):
        if message.get("role") != "tool":
            continue
        content = str(message.get("content") or "")
        total_chars += len(content)
        if content.startswith(("[Historical process result compacted;", "[Old tool result content cleared")):
            continue
        if len(content) < 512 or _ERROR_RE.search(content):
            continue
        name = str(message.get("name") or call_names.get(str(message.get("tool_call_id") or ""), ""))
        payload = _process_payload(content, name)
        if payload is not None and (
            payload.get("status") in {"failed", "error", "interrupted", "cancelled"}
            or (type(payload.get("exit_code")) is int and payload["exit_code"] != 0)
        ):
            continue
        risk = tool_risk(name) if tool_risk is not None else "read"
        if risk not in {"read", "network"}:
            continue
        candidates.append((index, name, content))

    if total_chars <= budget or len(candidates) <= keep:
        return messages, MicroCompactStats()

    # The oldest safe results that have to go to get under budget; a recent
    # working tail always stays, even if the configured budget is unusually small.
    due: list[tuple[int, str, str]] = []
    for index, name, content in candidates[:-keep]:
        replacement = _placeholder(content, name)
        if len(replacement) >= len(content):
            continue
        due.append((index, content, replacement))
        total_chars -= len(content) - len(replacement)
        if total_chars <= budget:
            break
    # Whole batches only. As the history grows the number due never falls, so
    # the cleared results stay cleared and the next batch goes all at once.
    due = due[:len(due) // batch * batch]
    if not due:
        return messages, MicroCompactStats()

    prepared = copy.deepcopy(messages)
    saved = 0
    shadowed_tokens = 0
    replacement_tokens = 0
    for index, content, replacement in due:
        prepared[index]["content"] = replacement
        saved += len(content) - len(replacement)
        shadowed_tokens += estimate_value_tokens(content)
        replacement_tokens += estimate_value_tokens(replacement)
    return prepared, MicroCompactStats(
        cleared_results=len(due),
        saved_chars=saved,
        shadowed_tokens=shadowed_tokens,
        replacement_tokens=replacement_tokens,
    )
