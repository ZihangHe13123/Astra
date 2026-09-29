"""Opt-in, content-safe profiling for one provider request."""

from __future__ import annotations

from agent.runtime.paths import state_path

import hashlib
import json
import os
import threading
import time
from argparse import ArgumentParser
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .bounded_artifacts import append_bounded_text, positive_int_env
from .token_estimator import estimate_value_tokens


_TURN_CONTEXT_MARKER = "[SYSTEM-SUPPLIED TURN CONTEXT — "


def _truthy(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _digest(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def _usage_int(usage: dict[str, Any], key: str) -> int:
    try:
        return max(0, int(usage.get(key, 0) or 0))
    except (TypeError, ValueError):
        return 0


def _reported_usage(usage: dict[str, Any], key: str) -> int | None:
    value = usage.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _split_system_context(system: str) -> tuple[str, str]:
    """Split the stable system prefix from Astra's per-turn overlay."""
    marker = f"\n\n{_TURN_CONTEXT_MARKER}"
    if marker not in system:
        return system, ""
    stable, dynamic = system.split(marker, 1)
    return stable, f"{_TURN_CONTEXT_MARKER}{dynamic}"


def _request_segments(messages: list[dict], tools: list[dict]) -> list[dict[str, Any]]:
    """Return content-safe ordered hashes for cache-prefix comparison."""
    segments: list[dict[str, Any]] = []
    if tools:
        segments.append({
            "kind": "tools",
            "hash": _digest(tools),
            "tokens": estimate_value_tokens(tools),
        })
    for index, message in enumerate(messages):
        if message.get("role") == "system":
            stable, dynamic = _split_system_context(str(message.get("content") or ""))
            if dynamic:
                stable_message = {**message, "content": stable}
                segments.append({
                    "kind": "system_stable",
                    "message_index": index,
                    "hash": _digest(stable_message),
                    "tokens": estimate_value_tokens(stable_message),
                })
                segments.append({
                    "kind": "system_dynamic",
                    "message_index": index,
                    "hash": _digest(dynamic),
                    "tokens": estimate_value_tokens(dynamic),
                })
                continue
        segments.append({
            "kind": "runtime_context" if (
                message.get("role") == "user"
                and str(message.get("content") or "").startswith(_TURN_CONTEXT_MARKER)
            ) else str(message.get("role") or "message"),
            "message_index": index,
            "hash": _digest(message),
            "tokens": estimate_value_tokens(message),
        })
    return segments


def _common_prefix_metrics(
    previous: list[dict[str, Any]] | None,
    current: list[dict[str, Any]] | None,
) -> tuple[int, int]:
    if not previous or not current:
        return 0, 0
    count = 0
    tokens = 0
    for before, after in zip(previous, current):
        if before.get("hash") != after.get("hash"):
            break
        count += 1
        tokens += min(
            _usage_int(before, "tokens"),
            _usage_int(after, "tokens"),
        )
    return count, tokens


class PromptCacheTracker:
    """Attribute material cache-read drops without retaining prompt content."""

    def __init__(self) -> None:
        self._previous: dict[tuple[str, str], dict[str, Any]] = {}
        self._lock = threading.RLock()

    def observe(
        self,
        *,
        model: str,
        session_id: str,
        fingerprint: dict[str, Any],
        usage: dict[str, Any] | None,
        now: float,
    ) -> dict[str, Any]:
        usage = usage or {}
        cache_read = _reported_usage(usage, "prompt_cache_hit_tokens")
        cache_miss = _reported_usage(usage, "prompt_cache_miss_tokens")
        key = (model, session_id)
        current = {
            **fingerprint,
            "cache_read_tokens": cache_read,
            "cache_miss_tokens": cache_miss,
            "observed_at": now,
        }
        with self._lock:
            previous = self._previous.get(key)
            self._previous[key] = current
        if previous is None:
            return {
                "status": "baseline" if cache_read is not None else "unknown",
                "cache_read_tokens": cache_read,
                "reusable_prefix_segments": 0,
                "reusable_prefix_tokens_estimate": 0,
                "reusable_prefix_ratio_estimate": 0.0,
            }

        previous_read = previous.get("cache_read_tokens")
        if cache_read is not None and isinstance(previous_read, int):
            comparable_cache = True
            drop = previous_read - cache_read
            material = previous_read > 0 and drop >= 2_000 and cache_read < previous_read * 0.95
        else:
            comparable_cache, drop, material = False, 0, False
        causes: list[str] = []
        for field, label in (
            ("model_hash", "model"),
            ("system_hash", "system_prompt"),
            ("tools_hash", "tool_schemas"),
            ("skills_hash", "active_skills"),
            ("prefix_hash", "message_prefix"),
        ):
            if previous.get(field) != current.get(field):
                causes.append(label)
        gap_seconds = max(0.0, now - float(previous.get("observed_at", now)))
        reusable_segments, reusable_tokens = _common_prefix_metrics(
            previous.get("request_segments"),
            current.get("request_segments"),
        )
        current_tokens = sum(
            _usage_int(segment, "tokens")
            for segment in current.get("request_segments", [])
        )
        previous_segments = previous.get("request_segments", [])
        current_segments = current.get("request_segments", [])
        difference = None
        if reusable_segments < max(len(previous_segments), len(current_segments)):
            before = previous_segments[reusable_segments] if reusable_segments < len(previous_segments) else None
            after = current_segments[reusable_segments] if reusable_segments < len(current_segments) else None
            difference = {
                "segment": reusable_segments,
                "change": "appended" if before is None else "removed" if after is None else "changed",
                "previous_kind": before.get("kind") if before else None,
                "current_kind": after.get("kind") if after else None,
                "message_index": (after or before or {}).get("message_index"),
            }
        if material and not causes and gap_seconds >= 300:
            causes.append("possible_cache_ttl_or_server_eviction")
        return {
            "status": "material_drop" if material else "stable" if comparable_cache else "unknown",
            "cache_read_tokens": cache_read,
            "previous_cache_read_tokens": previous_read,
            "drop_tokens": max(0, drop),
            "gap_seconds": round(gap_seconds, 3),
            "causes": causes,
            "first_difference": difference,
            "reusable_prefix_segments": reusable_segments,
            "reusable_prefix_tokens_estimate": reusable_tokens,
            "reusable_prefix_ratio_estimate": round(
                reusable_tokens / current_tokens, 4
            ) if current_tokens else 0.0,
        }


_CACHE_TRACKER = PromptCacheTracker()


class QueryProfiler:
    """Collect phase timings and append one redacted JSON event when enabled."""

    def __init__(
        self,
        *,
        enabled: bool,
        session_id: str,
        request_id: str,
        step: int,
        model: str,
        root: Path,
        session_path: str | Path | None = None,
    ) -> None:
        self.enabled = enabled
        self.session_id = session_id
        from .session_identity import session_key
        self.session_key = session_key(session_path)
        self.request_id = request_id
        self.step = step
        self.model = model
        self.root = root
        self.started = time.perf_counter()
        self.request_sent_at: float | None = None
        self.first_event_at: float | None = None
        self.first_reasoning_at: float | None = None
        self.first_text_at: float | None = None
        self.first_tools_at: float | None = None
        self.phases_ms: dict[str, float] = {}
        self.fingerprint: dict[str, Any] = {}
        self._finished = False

    @classmethod
    def from_env(
        cls,
        *,
        session_id: str,
        request_id: str,
        step: int,
        model: str,
        root: Path,
        session_path: str | Path | None = None,
    ) -> "QueryProfiler":
        return cls(
            enabled=_truthy("ASTRA_PROFILE_QUERY"),
            session_id=session_id,
            request_id=request_id,
            step=step,
            model=model,
            root=root,
            session_path=session_path,
        )

    @contextmanager
    def phase(self, name: str) -> Iterator[None]:
        if not self.enabled:
            yield
            return
        started = time.perf_counter()
        try:
            yield
        finally:
            elapsed = (time.perf_counter() - started) * 1000
            self.phases_ms[name] = round(self.phases_ms.get(name, 0.0) + elapsed, 3)

    def observe_ms(self, name: str, value: float) -> None:
        if self.enabled:
            self.phases_ms[name] = round(max(0.0, value), 3)

    def set_request(
        self,
        messages: list[dict],
        tools: list[dict],
        active_skills: set[str] | None = None,
    ) -> None:
        if not self.enabled:
            return
        system = next((msg.get("content", "") for msg in messages if msg.get("role") == "system"), "")
        stable_system, dynamic_system = _split_system_context(str(system))
        # The most stable cacheable prefix is everything before the newest user
        # request. Store hashes and sizes only; never write prompt content.
        latest_user = max(
            (index for index, msg in enumerate(messages) if msg.get("role") == "user"),
            default=len(messages),
        )
        prefix = messages[:latest_user]
        request_segments = _request_segments(messages, tools)
        self.fingerprint = {
            "model_hash": _digest(self.model),
            "system_hash": _digest(system),
            "stable_system_hash": _digest(stable_system),
            "dynamic_system_hash": _digest(dynamic_system),
            "tools_hash": _digest(tools),
            "skills_hash": _digest(sorted(active_skills or set())),
            "prefix_hash": _digest(prefix),
            "message_count": len(messages),
            "tool_count": len(tools),
            "system_chars": len(str(system)),
            "stable_system_chars": len(stable_system),
            "dynamic_system_chars": len(dynamic_system),
            "request_segments": request_segments,
            "request_tokens_estimate": sum(
                _usage_int(segment, "tokens") for segment in request_segments
            ),
        }

    def request_sent(self) -> None:
        if self.enabled:
            self.request_sent_at = time.perf_counter()

    def first_event(self, kind: str = "") -> None:
        if self.enabled and self.first_event_at is None:
            self.first_event_at = time.perf_counter()
        if not self.enabled:
            return
        field = {"reasoning": "first_reasoning_at", "chunk": "first_text_at", "tool_calls": "first_tools_at"}.get(kind)
        if field is not None and getattr(self, field) is None:
            setattr(self, field, time.perf_counter())

    def finish(self, usage: dict[str, Any] | None = None, *, error: str = "") -> None:
        self._finish(usage, error=error, finished=time.perf_counter())

    async def finish_async(self, usage: dict[str, Any] | None = None, *, error: str = "") -> None:
        if not self.enabled or self._finished:
            return
        from .async_io import durable_io

        await durable_io(self._finish, usage, error=error, finished=time.perf_counter())

    def _finish(self, usage: dict[str, Any] | None, *, error: str, finished: float) -> None:
        if not self.enabled or self._finished:
            return
        self._finished = True
        request_start = self.request_sent_at or self.started
        first = self.first_event_at
        cache = _CACHE_TRACKER.observe(
            model=self.model,
            session_id=self.session_key or self.session_id,
            fingerprint=self.fingerprint,
            usage=usage,
            now=time.time(),
        )
        event = {
            "timestamp": time.time(),
            "session_id": self.session_id,
            "session_key": self.session_key,
            "request_id": self.request_id,
            "step": self.step,
            "model": self.model,
            "total_ms": round((finished - self.started) * 1000, 3),
            "request_to_first_event_ms": round((first - request_start) * 1000, 3) if first is not None else None,
            "request_to_first_reasoning_ms": round((self.first_reasoning_at - request_start) * 1000, 3) if self.first_reasoning_at is not None else None,
            "request_to_first_text_ms": round((self.first_text_at - request_start) * 1000, 3) if self.first_text_at is not None else None,
            "request_to_first_tools_ms": round((self.first_tools_at - request_start) * 1000, 3) if self.first_tools_at is not None else None,
            "pre_request_ms": round((request_start - self.started) * 1000, 3),
            "phases_ms": self.phases_ms,
            "fingerprint": self.fingerprint,
            "cache": cache,
            "usage": {
                key: _reported_usage(usage or {}, key)
                for key in (
                    "prompt_tokens",
                    "completion_tokens",
                    "prompt_cache_hit_tokens",
                    "prompt_cache_miss_tokens",
                )
                if _reported_usage(usage or {}, key) is not None
            },
            "error": error[:240],
        }
        path = state_path("query-profile.jsonl", root=self.root)
        from .latency import current_profiler

        runtime = current_profiler()
        if runtime is not None:
            metrics = {key: value for key, value in event.items() if key.endswith("_ms") and isinstance(value, (int, float))}
            runtime.record("llm", {**self.phases_ms, **metrics}, identity=f"{self.request_id}:{self.step}",
                           request_id=self.request_id, label="failed" if error else "completed")
        try:
            append_bounded_text(
                path,
                json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n",
                max_bytes=positive_int_env(
                    "ASTRA_QUERY_PROFILE_MAX_BYTES", 5 * 1024 * 1024
                ),
                backup_count=positive_int_env("ASTRA_QUERY_PROFILE_BACKUPS", 2),
            )
        except OSError:
            return


def analyze_profile(path: Path) -> dict[str, Any]:
    """Aggregate redacted profiler events without accessing prompt content."""
    events: list[dict[str, Any]] = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(event, dict):
                events.append(event)
    cache_hits = sum(_usage_int(event.get("usage", {}), "prompt_cache_hit_tokens") for event in events)
    cache_misses = sum(_usage_int(event.get("usage", {}), "prompt_cache_miss_tokens") for event in events)
    cache_input = cache_hits + cache_misses
    comparable = [
        event for event in events
        if event.get("cache", {}).get("status") != "baseline"
    ]
    reuse_values = [
        float(event.get("cache", {}).get("reusable_prefix_ratio_estimate", 0.0) or 0.0)
        for event in comparable
    ]
    dynamic_system_requests = sum(
        1 for event in events
        if int(event.get("fingerprint", {}).get("dynamic_system_chars", 0) or 0) > 0
    )
    return {
        "path": str(path),
        "events": len(events),
        "sessions": len({str(event.get("session_id") or "") for event in events}),
        "cache_hit_tokens": cache_hits,
        "cache_miss_tokens": cache_misses,
        "cache_hit_rate": round(cache_hits / cache_input, 4) if cache_input else 0.0,
        "comparable_requests": len(comparable),
        "mean_reusable_prefix_ratio_estimate": round(
            sum(reuse_values) / len(reuse_values), 4
        ) if reuse_values else 0.0,
        "dynamic_system_requests": dynamic_system_requests,
    }


def main() -> int:
    parser = ArgumentParser(description="Analyze Astra's redacted query profile")
    parser.add_argument(
        "path",
        nargs="?",
        type=Path,
        default=state_path("query-profile.jsonl"),
    )
    args = parser.parse_args()
    print(json.dumps(analyze_profile(args.path), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
