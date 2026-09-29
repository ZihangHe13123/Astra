"""Bounded read-only projection of redacted request metrics; never starts a model."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path

from agent.runtime.paths import state_path
from agent.runtime.session_identity import session_key

_MAX_SCAN_BYTES = 4 * 1024 * 1024
_MAX_LINE_BYTES = 64 * 1024
_TIMINGS = ("timestamp", "step", "total_ms", "request_to_first_event_ms", "request_to_first_text_ms",
            "request_to_first_reasoning_ms", "request_to_first_tools_ms", "pre_request_ms")
_USAGE = ("prompt_tokens", "completion_tokens", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens")
_FINGERPRINT = ("message_count", "tool_count", "system_chars", "stable_system_chars", "dynamic_system_chars",
                "request_tokens_estimate")


def _number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def _numbers(value: object, keys: tuple[str, ...]) -> dict:
    return {k: value[k] for k in keys if _number(value.get(k))} if isinstance(value, dict) else {}


def _project(record: dict) -> dict:
    result = _numbers(record, _TIMINGS)
    for key in ("request_id", "model"):
        result[key] = str(record.get(key, ""))[:200]
    result["usage"] = _numbers(record.get("usage"), _USAGE)
    result["fingerprint"] = _numbers(record.get("fingerprint"), _FINGERPRINT)
    phases = record.get("phases_ms")
    result["phases_ms"] = {str(k)[:80]: v for k, v in list(phases.items())[:40] if _number(v)} if isinstance(phases, dict) else {}
    result["failed"] = bool(record.get("error"))
    cache = record.get("cache")
    if isinstance(cache, dict):
        result["cache"] = _numbers(cache, ("cache_read_tokens", "reusable_prefix_tokens_estimate", "reusable_prefix_ratio_estimate"))
        result["cache"]["status"] = cache.get("status") if cache.get("status") in {"baseline", "stable", "material_drop"} else "unknown"
        causes = cache.get("causes", [])
        result["cache"]["causes"] = [x for x in causes if x in {"model", "system_prompt", "tool_schemas", "active_skills", "message_prefix", "possible_cache_ttl_or_server_eviction"}] if isinstance(causes, list) else []
    return result


def request_timeline(session_path: Path, *, limit: int = 100) -> dict:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise ValueError("Timeline limit must be between 1 and 200")
    identity = session_key(session_path)
    path = state_path("query-profile.jsonl")
    available = False
    truncated = False
    unscoped = 0
    records: dict[tuple[str, object], dict] = {}
    # Oldest first: the current file supersedes a duplicated rotated record.
    for source in (path.with_name(path.name + ".2"), path.with_name(path.name + ".1"), path):
        try:
            with source.open("rb") as stream:
                stream.seek(0, 2)
                size = stream.tell()
                stream.seek(max(0, size - _MAX_SCAN_BYTES))
                if size > _MAX_SCAN_BYTES:
                    stream.readline(_MAX_LINE_BYTES)
                    truncated = True
                payload = stream.read(_MAX_SCAN_BYTES)
                available = True
        except FileNotFoundError:
            continue
        for line in payload.splitlines():
            if len(line) > _MAX_LINE_BYTES:
                truncated = True
                continue
            try:
                record = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(record, dict):
                continue
            if record.get("session_key") != identity:
                if not record.get("session_key") and record.get("session_id") == session_path.stem:
                    unscoped += 1
                continue
            if not isinstance(record.get("request_id"), str) or not _number(record.get("step")):
                continue
            projected = _project(record)
            records[(projected["request_id"], projected["step"])] = projected
    ordered = sorted(records.values(), key=lambda r: (r.get("timestamp", 0), r["request_id"], r["step"]), reverse=True)
    return {"available": available, "records": ordered[:limit], "has_more": len(ordered) > limit,
            "recording_enabled": os.environ.get("ASTRA_PROFILE_QUERY", "1").lower() in {"1", "true", "yes", "on"},
            "scan_truncated": truncated, "unscoped_records": unscoped}
