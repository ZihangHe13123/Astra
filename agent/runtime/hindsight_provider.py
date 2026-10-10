"""Optional Hindsight recall and durable Astra session synchronization."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from .memory_provider import (
    MemoryProvider,
    MemoryProviderCapabilities,
    MemoryProviderHealth,
)
from .memory_records import MEMORY_KINDS, MemoryRecord
from .session_store import SessionStore


_TRUTHY = {"1", "true", "yes", "on"}
_BUDGETS = {"low", "mid", "high"}
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(
        r"(?i)\b(api[_-]?key|access[_-]?token|auth[_-]?token|password|secret)"
        r"(\s*[:=]\s*)[^\s,;\"']{6,}"
    ),
)
_SESSION_ITEM_CHARS = 12_000
_SESSION_SYNC_FORMAT = 3


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value: Any) -> str:
    return str(value or "").strip()


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


class HindsightMemoryProvider(MemoryProvider):
    """Recall adapter plus a separate, append-only session transcript sink.

    Structured Astra memory remains authoritative. Hindsight retain/reflect
    are explicitly non-authoritative tools, while session synchronization
    writes sanitized user/final-assistant turns with its own durable cursor.
    """

    name = "hindsight"
    capabilities = MemoryProviderCapabilities(
        recall=True,
        retain=True,
        reflect=True,
        provenance=True,
        supersede=False,
    )

    def __init__(
        self,
        *,
        base_url: str,
        bank_id: str,
        budget: str = "mid",
        max_tokens: int = 800,
        timeout: float = 8.0,
        api_key: str = "",
        allow_remote: bool = False,
        session_sync: bool = False,
        session_sync_every_n_turns: int = 3,
        session_sync_async: bool = False,
        session_sync_timeout: float = 180.0,
        reflect_timeout: float = 120.0,
        processing_model: str = "deepseek-flash",
        client: Any | None = None,
    ):
        self.base_url = _text(base_url).rstrip("/")
        self.bank_id = _text(bank_id)
        self.budget = _text(budget).lower()
        self.max_tokens = max(64, min(int(max_tokens), 4096))
        self.timeout = max(0.5, min(float(timeout), 120.0))
        self.api_key = _text(api_key)
        self.allow_remote = bool(allow_remote)
        self.session_sync_enabled = bool(session_sync)
        self.session_sync_every_n_turns = max(1, min(int(session_sync_every_n_turns), 100))
        self.session_sync_async = bool(session_sync_async)
        self.session_sync_timeout = max(0.5, min(float(session_sync_timeout), 600.0))
        self.reflect_timeout = max(1.0, min(float(reflect_timeout), 600.0))
        self.processing_model = _text(processing_model) or "server-configured"
        self.last_sync_detail = "not attempted"
        self._sync_failures = 0
        self._sync_cooldown_until = 0.0
        self._client = client
        self._configuration_error = self._validate()
        initial_state = "unavailable" if self._configuration_error else "configured"
        initial_detail = self._configuration_error or (
            f"url={self.base_url}; bank={self.bank_id}; budget={self.budget}; not probed"
        )
        self.last_health = MemoryProviderHealth(initial_state, initial_detail, self.capabilities)

    @classmethod
    def from_env(cls) -> "HindsightMemoryProvider | None":
        if os.getenv("HINDSIGHT_ENABLED", "0").strip().lower() not in _TRUTHY:
            return None
        session_sync = _env_bool("HINDSIGHT_SESSION_SYNC", False)
        if os.getenv("PYTEST_CURRENT_TEST") and not _env_bool(
            "HINDSIGHT_SESSION_SYNC_IN_TESTS",
            False,
        ):
            session_sync = False
        return cls(
            base_url=os.getenv("HINDSIGHT_API_URL", "http://127.0.0.1:8888"),
            bank_id=os.getenv("HINDSIGHT_BANK_ID", "main-v2"),
            budget=os.getenv("HINDSIGHT_BUDGET", "mid"),
            max_tokens=_env_int("HINDSIGHT_RECALL_MAX_TOKENS", 800),
            timeout=_env_float("HINDSIGHT_TIMEOUT", 8.0),
            api_key=os.getenv("HINDSIGHT_API_KEY", ""),
            allow_remote=os.getenv("HINDSIGHT_ALLOW_REMOTE", "0").strip().lower() in _TRUTHY,
            session_sync=session_sync,
            session_sync_every_n_turns=_env_int("HINDSIGHT_SESSION_SYNC_EVERY_N_TURNS", 3),
            session_sync_async=_env_bool("HINDSIGHT_SESSION_SYNC_ASYNC", False),
            session_sync_timeout=_env_float("HINDSIGHT_SESSION_SYNC_TIMEOUT", 180.0),
            reflect_timeout=_env_float("HINDSIGHT_REFLECT_TIMEOUT", 120.0),
            processing_model=os.getenv("HINDSIGHT_PROCESSING_MODEL", "deepseek-flash"),
        )

    def _validate(self) -> str:
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return "HINDSIGHT_API_URL must be an absolute http(s) URL"
        if not self.allow_remote and parsed.hostname not in _LOCAL_HOSTS:
            return (
                "remote Hindsight is blocked by local-first policy; use localhost "
                "or explicitly set HINDSIGHT_ALLOW_REMOTE=1"
            )
        if not self.bank_id:
            return "HINDSIGHT_BANK_ID is required"
        if self.budget not in _BUDGETS:
            return "HINDSIGHT_BUDGET must be one of: low, mid, high"
        return ""

    def _client_or_raise(self):
        if self._configuration_error:
            raise RuntimeError(self._configuration_error)
        if self._client is None:
            try:
                # This optional SDK is deliberately absent from base installs.
                hindsight = importlib.import_module("hindsight_client")
                client_type = hindsight.Hindsight
            except (ImportError, AttributeError) as exc:
                raise RuntimeError(
                    "hindsight-client is not installed; install the 'hindsight' optional dependency"
                ) from exc
            kwargs: dict[str, Any] = {
                "base_url": self.base_url,
                # The SDK client has one transport timeout shared by recall,
                # retain and reflect. Keep it at the largest per-operation
                # budget; each operation still has its own outer asyncio.wait_for.
                "timeout": max(self.timeout, self.session_sync_timeout, self.reflect_timeout),
            }
            if self.api_key:
                kwargs["api_key"] = self.api_key
            self._client = client_type(**kwargs)
        return self._client

    async def recall(
        self,
        query: str,
        *,
        kinds: Iterable[str] = (),
        limit: int = 8,
    ) -> list[MemoryRecord]:
        del kinds  # Astra kinds and Hindsight observation types are not equivalent.
        clean_query = " ".join(_text(query).split())
        if not clean_query:
            return []
        requested = max(1, min(int(limit), 12))
        client = self._client_or_raise()
        response = await asyncio.wait_for(
            client.arecall(
                bank_id=self.bank_id,
                query=clean_query,
                budget=self.budget,
                max_tokens=self.max_tokens,
            ),
            timeout=self.timeout,
        )
        results = list(_field(response, "results", []) or [])[:requested]
        records = [
            record
            for rank, item in enumerate(results)
            if (record := self._record(item, rank)) is not None
        ]
        self.last_health = MemoryProviderHealth(
            "available",
            f"url={self.base_url}; bank={self.bank_id}; last_recall={len(records)} result(s)",
            self.capabilities,
        )
        return records

    def _record(self, item: Any, rank: int) -> MemoryRecord | None:
        content = _text(_field(item, "text"))
        if not content:
            return None
        metadata = dict(_field(item, "metadata", {}) or {})
        document_id = _text(_field(item, "document_id"))
        # A memory Hindsight gives no date for keeps empty date fields: the time
        # of this recall is not when it happened.
        occurred = _text(_field(item, "occurred_start")) or _text(_field(item, "mentioned_at"))
        raw_kind = _text(_field(item, "type")).lower()
        kind = raw_kind if raw_kind in MEMORY_KINDS else "observation"
        tags = tuple(dict.fromkeys([
            *(_text(value) for value in (_field(item, "tags", []) or []) if _text(value)),
            "external",
            "hindsight",
            "non-authoritative",
        ]))
        record_id = _text(_field(item, "id")) or f"hindsight-{rank}"
        return MemoryRecord(
            record_id=f"hs-{record_id}",
            kind=kind,
            content=content[:6000],
            source_session_id=_text(metadata.get("session_id")) or document_id or f"hindsight:{self.bank_id}",
            source_message_id=record_id,
            created_at=occurred,
            last_confirmed_at=occurred,
            valid_from=occurred,
            valid_until="",
            confidence=0.55,
            salience=max(0.35, 0.70 - rank * 0.05),
            status="active",
            supersedes_id="",
            tags=tags,
            metadata={
                **metadata,
                "provider": self.name,
                "bank_id": self.bank_id,
                "document_id": document_id,
                "hindsight_type": raw_kind,
                "authoritative": False,
                "rank": rank,
            },
        )

    async def retain(self, *, kind: str, content: str, **kwargs: Any) -> MemoryRecord:
        clean_content = _redact_session_text(str(content or "").strip())
        if not clean_content:
            raise ValueError("Hindsight retain content is required")
        clean_kind = _text(kind).lower()
        if clean_kind not in MEMORY_KINDS:
            clean_kind = "observation"
        document_id = _text(kwargs.get("document_id")) or f"astra-manual:{uuid.uuid4().hex}"
        raw_tags = kwargs.get("tags") or ()
        if isinstance(raw_tags, str):
            raw_tags = (raw_tags,)
        tags = [
            value
            for value in (_text(item) for item in raw_tags)
            if value
        ][:32]
        metadata = {
            "source": "astra-hindsight-tool",
            "kind": clean_kind,
            **dict(kwargs.get("metadata") or {}),
        }
        source_session_id = _text(kwargs.get("source_session_id"))
        if source_session_id:
            metadata["session_id"] = source_session_id
        client = self._client_or_raise()
        response = await asyncio.wait_for(
            client.aretain(
                bank_id=self.bank_id,
                content=clean_content,
                context=_text(kwargs.get("context")) or None,
                document_id=document_id,
                metadata={key: str(value) for key, value in metadata.items()},
                tags=tags or None,
                retain_async=False,
            ),
            timeout=self.session_sync_timeout,
        )
        if _field(response, "success", True) is False:
            raise RuntimeError(f"Hindsight rejected retain: {_field(response, 'message', response)}")
        now = _now()
        return MemoryRecord(
            record_id=f"hs-{document_id}",
            kind=clean_kind,
            content=clean_content,
            source_session_id=source_session_id or document_id,
            source_message_id=document_id,
            created_at=now,
            last_confirmed_at=now,
            valid_from=now,
            valid_until="",
            confidence=0.55,
            salience=0.6,
            status="active",
            supersedes_id="",
            tags=tuple(dict.fromkeys([*tags, "external", "hindsight", "non-authoritative"])),
            metadata={
                **metadata,
                "provider": self.name,
                "bank_id": self.bank_id,
                "document_id": document_id,
                "authoritative": False,
            },
        )

    async def reflect(self, query: str) -> str:
        clean_query = " ".join(_text(query).split())
        if not clean_query:
            raise ValueError("Hindsight reflect query is required")
        client = self._client_or_raise()
        response = await asyncio.wait_for(
            client.areflect(
                bank_id=self.bank_id,
                query=clean_query,
                budget=self.budget,
                max_tokens=self.max_tokens,
            ),
            timeout=self.reflect_timeout,
        )
        text = _text(_field(response, "text"))
        self.last_health = MemoryProviderHealth(
            "available",
            f"url={self.base_url}; bank={self.bank_id}; last_reflect_chars={len(text)}",
            self.capabilities,
        )
        return text

    async def supersede(self, record_id: str, *, content: str, **kwargs: Any) -> MemoryRecord:
        del record_id, content, kwargs
        raise NotImplementedError("Hindsight recall is non-authoritative and does not support supersede")

    async def health(self) -> MemoryProviderHealth:
        if self._configuration_error:
            return self.last_health
        try:
            client = self._client_or_raise()
            await asyncio.wait_for(
                asyncio.to_thread(client.get_bank_config, self.bank_id),
                timeout=self.timeout,
            )
            self.last_health = MemoryProviderHealth(
                "available",
                f"url={self.base_url}; bank={self.bank_id}; SDK and bank reachable",
                self.capabilities,
            )
        except Exception as exc:
            self.last_health = MemoryProviderHealth(
                "unavailable",
                f"{type(exc).__name__}: {exc}",
                self.capabilities,
            )
        return self.last_health

    async def sync_session(
        self,
        session_path: str | Path,
        messages: Iterable[dict[str, Any]],
        *,
        force: bool = False,
        reason: str = "turn",
    ) -> dict[str, Any]:
        """Append unsynchronized messages to one stable Hindsight session document."""
        if not self.session_sync_enabled:
            return {"decision": "disabled", "synced_messages": 0}
        if not force and time.monotonic() < self._sync_cooldown_until:
            wait_s = int(self._sync_cooldown_until - time.monotonic()) + 1
            self.last_sync_detail = (
                f"sync in cooldown ({wait_s}s left) after "
                f"{self._sync_failures} consecutive failure(s)"
            )
            return {
                "decision": "cooldown",
                "synced_messages": 0,
                "retry_in_s": wait_s,
                "failures": self._sync_failures,
            }
        clean_messages = [message for message in messages if isinstance(message, dict)]
        if not clean_messages:
            return {"decision": "empty", "synced_messages": 0}

        store = SessionStore(session_path)
        state = store.load_hindsight_sync_state()
        session_id = store.legacy_path.stem
        document_id = f"astra-session:{session_id}"
        current_turns = sum(message.get("role") == "user" for message in clean_messages)
        cursor = _env_int_from(state.get("message_count"), 0)
        previous_turns = _env_int_from(state.get("user_turn_count"), 0)
        rebuild = state.get("sync_format") != _SESSION_SYNC_FORMAT
        repair_existing = bool(state) and rebuild

        if rebuild or state.get("bank_id") != self.bank_id or state.get("document_id") != document_id:
            cursor = 0
            previous_turns = 0
            rebuild = True
            repair_existing = repair_existing or bool(state)
        if cursor > len(clean_messages):
            cursor = 0
            previous_turns = 0
            rebuild = True
            repair_existing = True
        if cursor:
            expected = _text(state.get("last_message_digest"))
            actual = _message_digest(clean_messages[cursor - 1])
            if not expected or expected != actual:
                cursor = 0
                previous_turns = 0
                rebuild = True
                repair_existing = True

        if cursor >= len(clean_messages):
            return {"decision": "current", "synced_messages": 0}
        pending_turns = max(0, current_turns - previous_turns)
        if not force and not repair_existing and pending_turns < self.session_sync_every_n_turns:
            return {
                "decision": "deferred",
                "synced_messages": 0,
                "pending_turns": pending_turns,
            }

        groups = _session_turn_delta(
            clean_messages[cursor:],
            cursor,
            session_id,
            rebuild=rebuild,
        )
        if not groups:
            return {
                "decision": "incomplete",
                "synced_messages": 0,
                "pending_turns": pending_turns,
            }
        client = self._client_or_raise()
        synced_messages = 0
        for item, next_cursor in groups:
            try:
                response = await asyncio.wait_for(
                    client.aretain_batch(
                        bank_id=self.bank_id,
                        items=[item],
                        document_id=document_id,
                        document_tags=["astra", "session-transcript"],
                        retain_async=self.session_sync_async,
                    ),
                    timeout=self.session_sync_timeout,
                )
            except Exception:
                self._register_sync_failure()
                raise
            if _field(response, "success", True) is False:
                self._register_sync_failure()
                raise RuntimeError(f"Hindsight rejected session batch: {_field(response, 'message', response)}")
            synced_messages = next_cursor - cursor
            state = {
                "sync_format": _SESSION_SYNC_FORMAT,
                "bank_id": self.bank_id,
                "document_id": document_id,
                "message_count": next_cursor,
                "user_turn_count": sum(
                    message.get("role") == "user"
                    for message in clean_messages[:next_cursor]
                ),
                "last_message_digest": _message_digest(clean_messages[next_cursor - 1]),
                "synced_at": _now(),
                "reason": reason,
                "retain_async": self.session_sync_async,
                "processing_model": self.processing_model,
            }
            store.save_hindsight_sync_state(state)

        self._sync_failures = 0
        self._sync_cooldown_until = 0.0
        self.last_sync_detail = (
            f"accepted {synced_messages} message(s) from {session_id}; "
            f"cursor={state.get('message_count', cursor)}; async={self.session_sync_async}"
        )
        return {
            "decision": "accepted",
            "synced_messages": synced_messages,
            "message_count": state.get("message_count", cursor),
            "document_id": document_id,
        }

    def _register_sync_failure(self) -> None:
        """Record a failed session sync and arm an exponential backoff cooldown.

        Consecutive failures back off 60s -> 120s -> 240s -> ... capped at 1h so a
        persistently failing Hindsight backend does not burn paid API calls on
        every turn.
        """
        self._sync_failures += 1
        wait_s = min(3600, 60 * (2 ** (self._sync_failures - 1)))
        self._sync_cooldown_until = time.monotonic() + wait_s
        self.last_sync_detail = (
            f"sync failed ({self._sync_failures}x consecutive); "
            f"cooldown {wait_s}s before next attempt"
        )

    async def aclose(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            close = getattr(client, "aclose", None)
            if close is not None:
                await close()

    def close_nowait(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            close = getattr(client, "close", None)
            if close is not None:
                close()


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    fallback = "1" if default else "0"
    return os.getenv(name, fallback).strip().lower() in _TRUTHY


def _env_int_from(value: Any, default: int) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _redact_session_text(text: str) -> str:
    redacted = text
    for pattern in _SECRET_PATTERNS:
        if pattern.groups:
            redacted = pattern.sub(r"\1\2[REDACTED]", redacted)
        else:
            redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def _message_text(
    message: dict[str, Any],
    *,
    include_tool_calls: bool = True,
) -> str:
    content = message.get("content", "")
    parts: list[str] = []
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text" and item.get("text"):
                parts.append(str(item["text"]))
            elif item.get("type") == "image_url":
                source = (
                    (item.get("metadata") or {}).get("source_path")
                    or item.get("source_path")
                    or "attached image"
                )
                parts.append(f"[Image: {Path(str(source)).name}]")
    elif content:
        parts.append(str(content))

    tool_calls = message.get("tool_calls") if include_tool_calls else None
    if isinstance(tool_calls, list) and tool_calls:
        names = []
        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            raw_function = call.get("function")
            function = raw_function if isinstance(raw_function, dict) else {}
            name = call.get("name") or function.get("name")
            if name:
                names.append(str(name))
        if names:
            parts.append("[Tool calls: " + ", ".join(names) + "]")
    return _redact_session_text("\n".join(part for part in parts if part).strip())


def _message_digest(message: dict[str, Any]) -> str:
    payload = {
        "role": _text(message.get("role")),
        "content": _message_text(message),
        "tool_call_id": _text(message.get("tool_call_id")),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _session_turn_delta(
    messages: list[dict[str, Any]],
    start_index: int,
    session_id: str,
    *,
    rebuild: bool,
) -> list[tuple[dict[str, Any], int]]:
    """Build Hermes-compatible user/final-assistant turns without tool traffic."""
    batch: list[dict[str, str]] = []
    next_cursor = start_index
    position = 0
    retained_at = _now()

    while position < len(messages):
        if messages[position].get("role") != "user":
            position += 1
            continue
        next_user = position + 1
        while next_user < len(messages) and messages[next_user].get("role") != "user":
            next_user += 1
        final_assistant: dict[str, Any] | None = None
        for candidate in messages[position + 1:next_user]:
            if candidate.get("role") != "assistant" or candidate.get("tool_calls"):
                continue
            if _message_text(candidate, include_tool_calls=False):
                final_assistant = candidate
        if final_assistant is None:
            if next_user < len(messages):
                # A cancelled/failed historical turn may have no final answer.
                # Skip it without retaining tool traffic and continue scanning.
                next_cursor = start_index + next_user
                position = next_user
                continue
            break
        user_text = _message_text(messages[position], include_tool_calls=False)[:_SESSION_ITEM_CHARS]
        assistant_text = _message_text(final_assistant, include_tool_calls=False)[:_SESSION_ITEM_CHARS]
        batch.extend([
            {
                "role": "user",
                "content": f"User: {user_text or '[empty]'}",
                "timestamp": retained_at,
            },
            {
                "role": "assistant",
                "content": f"Assistant: {assistant_text or '[empty]'}",
                "timestamp": retained_at,
            },
        ])
        next_cursor = start_index + next_user
        position = next_user

    if not batch or next_cursor <= start_index:
        return []
    return [(
        {
            "content": json.dumps(batch, ensure_ascii=False, separators=(",", ":")),
            "context": (
                f"conversation between Astra Agent and the User; session={session_id}; "
                f"turns={len(batch) // 2}"
            ),
            "metadata": {
                "source": "astra-session-sync",
                "session_id": session_id,
                "format": "hermes-turn-json-v3",
                "message_count": str(len(batch)),
                "turn_count": str(len(batch) // 2),
                "message_start": str(start_index),
                "message_end": str(next_cursor - 1),
            },
            "tags": ["astra", "session-transcript"],
            "update_mode": "replace" if rebuild else "append",
        },
        next_cursor,
    )]
