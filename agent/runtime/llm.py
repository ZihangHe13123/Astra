"""LLMClient — LLM API 封装"""

import asyncio
import importlib
import inspect
import json
import logging
import os
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field, replace
from pathlib import Path

from .deepseek import DEEPSEEK_FLASH, canonical_deepseek_model, is_deepseek_model
from .harness import default_llm_timeout
from .network import active_proxy_for_url
from .provider_errors import summarize_provider_error
from .providers import DEFAULT_PROVIDER_REGISTRY, ProviderRegistry
from .token_estimator import estimate_messages_tokens, messages_to_tokenize_text
from .tracing import trace_span
from .vision_policy import VisionPreprocessPolicy

try:
    from openai import (
        APIError,
        APITimeoutError,
        AsyncOpenAI,
        BadRequestError,
        DefaultAsyncHttpxClient,
        RateLimitError,
    )
except ModuleNotFoundError:
    class _OpenAIUnavailableError(Exception):
        pass

    APIError = APITimeoutError = BadRequestError = RateLimitError = _OpenAIUnavailableError
    AsyncOpenAI = DefaultAsyncHttpxClient = None

logger = logging.getLogger(__name__)


def _transport_request_error_types() -> tuple[type[BaseException], ...]:
    """Return request-error bases from the installed HTTP compatibility layers."""
    found: list[type[BaseException]] = []
    for module_name in ("httpx", "httpx2"):
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError:
            continue
        candidate = getattr(module, "RequestError", None)
        if (
            isinstance(candidate, type)
            and issubclass(candidate, BaseException)
            and candidate not in found
        ):
            found.append(candidate)
    return tuple(found)


TRANSPORT_REQUEST_ERRORS = _transport_request_error_types()


def _accepts_keyword(callable_obj, name: str) -> bool:
    """Return whether a provider callable accepts one optional keyword."""

    try:
        parameters = inspect.signature(callable_obj).parameters.values()
    except (TypeError, ValueError):
        # Some extension callables do not expose a Python signature. Preserve
        # the new protocol for them instead of silently dropping the override.
        return True
    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        or (
            parameter.name == name
            and parameter.kind
            in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
        )
        for parameter in parameters
    )


def _usage_dict(usage) -> dict | None:
    """Normalize provider usage, including automatic prefix-cache telemetry."""
    if usage is None:
        return None
    prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
    completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
    total_tokens = int(
        getattr(usage, "total_tokens", 0)
        or (prompt_tokens + completion_tokens)
    )
    details = getattr(usage, "prompt_tokens_details", None)
    has_native_hit = hasattr(usage, "prompt_cache_hit_tokens")
    cache_hit = int(
        getattr(usage, "prompt_cache_hit_tokens", 0)
        or (getattr(details, "cached_tokens", 0) if details is not None else 0)
        or 0
    )
    raw_miss = getattr(usage, "prompt_cache_miss_tokens", None)
    has_cache_telemetry = raw_miss is not None or has_native_hit or details is not None
    cache_miss = int(
        raw_miss
        if raw_miss is not None
        else (max(0, prompt_tokens - cache_hit) if has_cache_telemetry else 0)
    )
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "prompt_cache_hit_tokens": cache_hit,
        "prompt_cache_miss_tokens": cache_miss,
    }


class LLMIdleTimeout(TimeoutError):
    pass


class LLMOverallTimeout(TimeoutError):
    pass


class LLMResponseError(ValueError):
    """A completed transport did not deliver a usable, unambiguous response."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")

    @property
    def public_message(self) -> str:
        messages = {
            "stream_closed": "The model connection ended without a completion signal. Partial output remains visible; no tool in that response was executed.",
            "empty_response": "The model returned an empty response, including after one recovery attempt. Retry the request or change the model.",
            "reasoning_only_response": "The model ended with thinking but no answer or tool call, including after one recovery attempt. Retry the request or change the model.",
            "response_truncated": "The output limit was reached. Partial output remains visible; request a shorter answer or continue explicitly.",
        }
        return messages.get(self.code, "The model returned an invalid or incomplete response. No tool in that response was executed; retry with a smaller request.")

    @property
    def public_code(self) -> str:
        allowed = {"stream_closed", "empty_response", "reasoning_only_response", "response_truncated",
                   "invalid_tool_index", "invalid_tool_arguments", "conflicting_tool_identity", "incomplete_response"}
        return self.code if self.code in allowed else "invalid_response"


class _RequestCapTimeout(TimeoutError):
    """An internal per-operation cap expired before the overall deadline."""


def _retry_backoff_seconds(attempt: int) -> float:
    """Return the deterministic delay after one-based transport attempt."""

    return float(min(8, 2 ** max(0, attempt - 1)))


@dataclass
class _RequestBudget:
    """Wall-clock and transport-attempt budget for one logical request."""

    timeout: float
    max_attempts: int
    started_at: float = field(default_factory=time.monotonic)
    attempts: int = 0
    turn_deadline: float | None = None

    @classmethod
    def from_policy(
        cls,
        timeout: float | None,
        max_retries: int,
    ) -> "_RequestBudget":
        from .turn_budget import current_turn_budget, check_work_budget
        check_work_budget()
        turn = current_turn_budget()
        return cls(
            timeout=max(0.0, float(timeout or 0)),
            max_attempts=max(1, int(max_retries) + 1),
            turn_deadline=turn.work_deadline if turn is not None else None,
        )

    @property
    def deadline(self) -> float | None:
        local = self.started_at + self.timeout if self.timeout > 0 else None
        if self.turn_deadline is None:
            return local
        return min(local, self.turn_deadline) if local is not None else self.turn_deadline

    def remaining(self) -> float | None:
        deadline = self.deadline
        if deadline is None:
            return None
        return max(0.0, deadline - time.monotonic())

    def can_attempt(self) -> bool:
        remaining = self.remaining()
        return (
            self.attempts < self.max_attempts
            and (remaining is None or remaining > 0)
        )

    def _raise_overall_timeout(self, *, deadline_expired: bool = False) -> None:
        from .turn_budget import TurnBudgetExceeded, check_work_budget, current_turn_budget
        check_work_budget()
        # asyncio timers may wake one clock-resolution tick before monotonic()
        # reaches the deadline (notably on Windows). Attribute a fired timer to
        # the deadline that limited it, even when the request timeout is disabled.
        turn = current_turn_budget()
        if (deadline_expired and turn is not None and self.turn_deadline is not None
                and self.deadline == self.turn_deadline):
            raise TurnBudgetExceeded(turn)
        raise LLMOverallTimeout(
            f"LLM request exceeded overall timeout {self.timeout:g}s"
        )

    def begin_attempt(self) -> int:
        if not self.can_attempt():
            self._raise_overall_timeout()
        self.attempts += 1
        return self.attempts

    async def run(self, factory, *, cap: float | None = None):
        """Run one factory within the deadline and an optional attempt cap."""

        remaining = self.remaining()
        if remaining is not None and remaining <= 0:
            self._raise_overall_timeout()

        attempt_cap = float(cap) if cap is not None and cap > 0 else None
        if remaining is None:
            limit = attempt_cap
            overall_is_limit = False
        else:
            limit = remaining if attempt_cap is None else min(remaining, attempt_cap)
            overall_is_limit = attempt_cap is None or remaining <= attempt_cap

        # Recheck immediately before constructing the provider awaitable. This
        # closes the begin_attempt/run gap instead of treating timeout=0 as an
        # unbounded asyncio.wait_for call.
        latest_remaining = self.remaining()
        if latest_remaining is not None and latest_remaining <= 0:
            self._raise_overall_timeout()
        if latest_remaining is not None:
            limit = (
                latest_remaining
                if attempt_cap is None
                else min(latest_remaining, attempt_cap)
            )
            overall_is_limit = (
                attempt_cap is None or latest_remaining <= attempt_cap
            )

        if limit is None:
            return await factory()

        timeout_context = asyncio.timeout(limit)
        try:
            async with timeout_context:
                return await factory()
        except LLMOverallTimeout:
            raise
        except TimeoutError as exc:
            if timeout_context.expired():
                if overall_is_limit:
                    self._raise_overall_timeout(deadline_expired=True)
                raise _RequestCapTimeout from exc
            raise

    async def backoff(self, delay: float) -> None:
        remaining = self.remaining()
        if remaining is not None and remaining <= 0:
            self._raise_overall_timeout()
        bounded_delay = max(0.0, float(delay))
        if remaining is not None and bounded_delay >= remaining:
            await asyncio.sleep(remaining)
            self._raise_overall_timeout(deadline_expired=True)
        await asyncio.sleep(bounded_delay)

    def fork_attempts(self, max_retries: int) -> "_RequestBudget":
        """Create a separate attempt counter on the same absolute deadline."""

        return _RequestBudget(
            timeout=self.timeout,
            max_attempts=max(1, int(max_retries) + 1),
            started_at=self.started_at,
            turn_deadline=self.turn_deadline,
        )


@dataclass
class _StreamReadTiming:
    """Per-attempt idle budget charges provider reads, not consumer pauses."""

    read_wait_seconds: float = 0.0
    idle_wait_seconds: float = 0.0
    local_pause_seconds: float = 0.0
    chunk_count: int = 0
    meaningful_chunk_count: int = 0
    last_chunk_kind: str = "none"
    _last_read_finished: float | None = None

    async def read(self, iterator, budget: _RequestBudget, idle_timeout: float):
        started = time.monotonic()
        if self._last_read_finished is not None:
            self.local_pause_seconds += max(0.0, started - self._last_read_finished)
        try:
            remaining = idle_timeout - self.idle_wait_seconds if idle_timeout > 0 else None
            if remaining is not None and remaining <= 0:
                # Keep the absolute request/turn deadline authoritative, even
                # when empty frames have exhausted the read-only idle budget.
                overall_remaining = budget.remaining()
                if overall_remaining is not None and overall_remaining <= 0:
                    budget._raise_overall_timeout()
                raise _RequestCapTimeout()
            return await budget.run(iterator.__anext__, cap=remaining)
        finally:
            finished = time.monotonic()
            elapsed = max(0.0, finished - started)
            self.read_wait_seconds += elapsed
            self.idle_wait_seconds += elapsed
            self._last_read_finished = finished

    def observe(self, kind: str) -> None:
        # Callers supply fixed labels, never provider text or tool arguments.
        self.chunk_count += 1
        self.last_chunk_kind = kind
        if kind in {"reasoning", "content", "tool", "finish"}:
            self.meaningful_chunk_count += 1
            self.idle_wait_seconds = 0.0


def _image_source_path(part: dict) -> str:
    metadata = part.get("metadata")
    source = metadata.get("source_path", "") if isinstance(metadata, dict) else ""
    raw_path = str(source or part.get("source_path") or "").strip()
    if not raw_path:
        return ""
    path = Path(raw_path).expanduser()
    if path.is_absolute():
        return str(path)
    candidate = (Path.cwd() / path).resolve()
    return str(candidate) if candidate.is_file() else ""


def _qwen_mm_image_routing(paths: list[str], tool_names: frozenset[str]) -> str:
    lines = [
        "[SYSTEM-SUPPLIED IMAGE ROUTING]",
        "The current model is text-only, but the user's image attachment was preserved locally.",
    ]
    lines.extend(
        f"Image #{index} local_path: {path}"
        for index, path in enumerate(paths, start=1)
    )
    qwen_tools = {
        name
        for name in tool_names
        if name.startswith("mcp__qwen-mm-plugins__")
    }
    preferred = {
        "general understanding": "mcp__qwen-mm-plugins__vision_chat",
        "OCR/text extraction": "mcp__qwen-mm-plugins__ocr",
        "object or region location": "mcp__qwen-mm-plugins__grounding",
    }
    available = [
        f"{purpose}: `{name}`"
        for purpose, name in preferred.items()
        if name in qwen_tools
    ]
    if available:
        lines.extend([
            "Before making claims about the pixels, call the narrowest available Qwen-MM tool "
            "with the exact local_path above:",
            *available,
            "Do not call the built-in `read_image`: it only re-attaches pixels to the same "
            "text-only model.",
        ])
    elif "activate_tool_group" in tool_names:
        lines.extend([
            "First call `activate_tool_group` for `mcp:qwen-mm-plugins`, then use "
            "`mcp__qwen-mm-plugins__vision_chat` for general understanding, "
            "`mcp__qwen-mm-plugins__ocr` for text, or "
            "`mcp__qwen-mm-plugins__grounding` for location.",
        ])
    else:
        lines.extend([
            "No external pixel-reading tool is exposed in this request. Do not pretend to see "
            "the image; report that Qwen-MM is not ready or suggest switching to a vision model.",
        ])
    lines.extend([
        "Do not say the image was omitted, and do not ask the user to provide its path again.",
        "[END SYSTEM-SUPPLIED IMAGE ROUTING]",
    ])
    return "\n".join(lines)


def _messages_for_capabilities(
    messages: list[dict],
    capabilities: frozenset[str],
    tool_names: frozenset[str] = frozenset(),
    vision_detail: str = "auto",
) -> list[dict]:
    """Downgrade multimodal content for providers configured as text-only.

    Some OpenAI-compatible APIs accept the chat schema but only implement the
    ``text`` content variant. Sending an ``image_url`` part to those endpoints
    otherwise fails before generation with a JSON deserialization error.
    """
    if "vision" in capabilities:
        default_detail = str(vision_detail or "auto").strip().lower()
        if default_detail not in {"auto", "low", "high", "original"}:
            default_detail = "auto"
        prepared: list[dict] = []
        changed = False
        for message in messages:
            content = message.get("content")
            if not isinstance(content, list):
                prepared.append(message)
                continue
            clean_parts = []
            message_changed = False
            for part in content:
                if not isinstance(part, dict):
                    clean_parts.append(part)
                    continue
                image = part.get("image_url")
                inject_detail = (
                    default_detail != "auto"
                    and part.get("type") == "image_url"
                    and isinstance(image, dict)
                    and not str(image.get("detail") or "").strip()
                )
                if "metadata" in part or "source_path" in part or inject_detail:
                    clean = dict(part)
                    clean.pop("metadata", None)
                    clean.pop("source_path", None)
                    if inject_detail and isinstance(image, dict):
                        clean_image = dict(image)
                        clean_image["detail"] = default_detail
                        clean["image_url"] = clean_image
                    clean_parts.append(clean)
                    message_changed = True
                else:
                    clean_parts.append(part)
            if message_changed:
                clone = dict(message)
                clone["content"] = clean_parts
                prepared.append(clone)
                changed = True
            else:
                prepared.append(message)
        return prepared if changed else messages
    prepared: list[dict] = []
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            prepared.append(message)
            continue
        text_parts: list[str] = []
        image_count = 0
        image_paths: list[str] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                text = str(part.get("text", "")).strip()
                if text:
                    text_parts.append(text)
                source_path = _image_source_path(part)
                if source_path and source_path not in image_paths:
                    image_paths.append(source_path)
                    image_count += 1
            elif part.get("type") == "image_url":
                image_count += 1
                source_path = _image_source_path(part)
                if source_path and source_path not in image_paths:
                    image_paths.append(source_path)
        if image_count:
            if image_paths:
                text_parts.insert(0, _qwen_mm_image_routing(image_paths, tool_names))
            else:
                noun = "image" if image_count == 1 else "images"
                text_parts.append(
                    f"[{image_count} {noun} unavailable to this text-only model: no local path "
                    "was retained. Switch to a vision-capable model to inspect pixels.]"
                )
        clone = dict(message)
        clone["content"] = "\n".join(text_parts)
        prepared.append(clone)
    return prepared


@dataclass
class LLMConfig:
    model: str = DEEPSEEK_FLASH
    api_key: str = ""
    base_url: str = "https://api.deepseek.com"
    temperature: float | None = None
    max_tokens: int = 4096
    top_p: float | None = None
    top_k: int | None = None
    min_p: float | None = None
    presence_penalty: float | None = None
    repetition_penalty: float | None = None
    # OpenAI-compatible servers disagree on this non-OpenAI extension:
    # oMLX uses repetition_penalty, while llama.cpp uses repeat_penalty.
    repetition_penalty_parameter: str | None = None
    timeout: float = 60.0
    connect_timeout: float = 30.0
    idle_timeout: float = 120.0
    overall_timeout: float | None = None
    max_retries: int = 2
    min_request_interval: float = 0.0
    max_concurrent_requests: int = 1
    provider: str = "openai-compatible"
    capabilities: frozenset[str] = field(default_factory=frozenset)
    vision_detail: str = "auto"
    vision_preprocess: VisionPreprocessPolicy | None = None
    # DeepSeek reasoning strength: low | medium | high | max. Only applied
    # to stable Flash and legacy V4 models; other OpenAI-compatible models
    # ignore it since the kwarg is not sent.
    reasoning_effort: str | None = "max"
    reasoning_levels: tuple[str, ...] = ()
    # DeepSeek accepts thinking={"type":"disabled"} to drop the (default-on,
    # token-hungry) reasoning phase. None = never sent, so existing callers are
    # unaffected; only honored for DeepSeek models.
    thinking_mode: str | None = None
    # Resolved provider context window, separate from proactive compression.
    # None keeps legacy/custom runtimes on their existing conservative budget.
    context_limit: int | None = None
    connection_required: bool = False

    def __post_init__(self) -> None:
        self.model = canonical_deepseek_model(self.model, self.base_url)


class _null_context:
    """No-op async context manager — used when timeout is disabled (0)."""
    async def __aenter__(self): return self
    async def __aexit__(self, *args): return False


async def _close_async_stream(stream) -> None:
    """Close an owned sync/async provider stream before its parent returns."""
    close = getattr(stream, "aclose", None) or getattr(stream, "close", None)
    if close is None:
        return
    result = close()
    if inspect.isawaitable(result):
        await result


class OpenAICompatibleProvider:
    def __init__(self, config: LLMConfig):
        self.config = config
        timeout_env = os.getenv("LLM_TIMEOUT")
        self.config.timeout = default_llm_timeout(config.base_url, timeout_env, self.config.timeout)
        self.config.connect_timeout = self._timeout_env(
            "LLM_CONNECT_TIMEOUT",
            self.config.connect_timeout,
        )
        self.config.idle_timeout = self._timeout_env(
            "LLM_IDLE_TIMEOUT",
            self.config.idle_timeout,
        )
        overall_env = os.getenv("LLM_OVERALL_TIMEOUT")
        if overall_env is not None:
            self.config.overall_timeout = self._timeout_value(
                overall_env,
                self.config.overall_timeout or 0,
            )
        elif timeout_env is not None:
            # Explicit legacy LLM_TIMEOUT remains an overall deadline.
            self.config.overall_timeout = self.config.timeout
        elif self.config.overall_timeout is None:
            hostname = (urllib.parse.urlparse(config.base_url).hostname or "").lower()
            self.config.overall_timeout = (
                0.0
                if hostname in {"localhost", "127.0.0.1", "::1"}
                else self.config.timeout
            )
        self._estimate_calibration = 1.0
        if AsyncOpenAI is None:
            raise RuntimeError("openai package is required to create LLMClient")
        self._proxy_url = active_proxy_for_url(config.base_url)
        self._client = self._build_client(self._proxy_url)
        self._route_clients = {self._proxy_url or "direct": self._client}
        self._route_lock = asyncio.Lock()

    def _build_client(self, proxy_url: str | None):
        if AsyncOpenAI is None or DefaultAsyncHttpxClient is None:
            raise RuntimeError("openai package is required to create LLMClient")
        http_client = (
            DefaultAsyncHttpxClient(trust_env=False, proxy=proxy_url)
            if proxy_url
            else DefaultAsyncHttpxClient(trust_env=False)
        )
        return AsyncOpenAI(
            # SDK construction requires a credential even in setup-only state.
            # _create_completion rejects every request until a real model is selected.
            api_key=("astra-setup" if self.config.connection_required else
                     self.config.api_key or os.getenv("LLM_API_KEY", "")),
            base_url=self.config.base_url,
            # Read/overall deadlines are progress-aware below. Keeping a second
            # SDK wall-clock timeout would reintroduce premature stream kills.
            timeout=None,
            max_retries=0,
            http_client=http_client,
        )

    async def _ensure_client_route(self) -> None:
        # A few embedders/tests provide a minimal provider via object.__new__.
        # Preserve that lightweight protocol when no managed route state exists.
        if not hasattr(self, "_proxy_url"):
            return
        proxy_url = active_proxy_for_url(self.config.base_url)
        if proxy_url == self._proxy_url:
            return
        async with self._route_lock:
            proxy_url = active_proxy_for_url(self.config.base_url)
            if proxy_url == self._proxy_url:
                return
            route_key = proxy_url or "direct"
            client = self._route_clients.get(route_key)
            if client is None:
                client = self._build_client(proxy_url)
                self._route_clients[route_key] = client
            # Keep both route clients alive: another concurrent completion may
            # still be consuming a stream on the previous route.
            self._client = client
            self._proxy_url = proxy_url

    async def _create_completion(self, kwargs: dict):
        if self.config.connection_required:
            raise ValueError("Connect a provider with /connect and select a model before sending a request.")
        await self._ensure_client_route()
        from .deepseek_files import DeepSeekImageFiles, supports_file_reuse
        prepared = kwargs
        used_files: set[str] = set()
        files = None
        if supports_file_reuse(self.config) and hasattr(self._client, "files"):
            files = getattr(self, "_image_files", None)
            if files is None:
                try:
                    files = DeepSeekImageFiles(self.config.base_url,
                                              self.config.api_key or os.getenv("LLM_API_KEY", ""))
                    self._image_files = files
                except (OSError, sqlite3.Error):
                    pass
            if files is not None:
                images, used_files = await files.prepare(kwargs["messages"], self._client)
                if used_files:
                    prepared = {**kwargs, "messages": images}
        try:
            return await self._client.chat.completions.create(**prepared)
        except APIError as exc:
            if not used_files or getattr(exc, "status_code", None) not in {400, 404, 422} or files is None:
                raise
            # This is a rejected request, before any stream or tool execution.
            # Retry its original inline representation once under the same
            # enclosing request deadline, and invalidate only our local refs.
            files.disabled_until = time.monotonic() + 120
            try:
                files.invalidate(used_files)
            except (OSError, sqlite3.Error):
                pass
            return await self._client.chat.completions.create(**kwargs)

    @staticmethod
    def _timeout_value(raw: str, fallback: float) -> float:
        try:
            return max(0.0, float(raw))
        except ValueError:
            return fallback

    @classmethod
    def _timeout_env(cls, name: str, fallback: float) -> float:
        raw = os.getenv(name)
        return fallback if raw is None else cls._timeout_value(raw, fallback)

    def _tokenize_url(self) -> str | None:
        override = os.getenv("LLM_TOKENIZE_URL", "").strip()
        if override:
            return override
        parsed = urllib.parse.urlparse(self.config.base_url)
        if parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            return None
        root = f"{parsed.scheme}://{parsed.netloc}"
        return root.rstrip("/") + "/tokenize"

    def _tokenize_timeout(self) -> float:
        try:
            return max(0.1, float(os.getenv("LLM_TOKENIZE_TIMEOUT", "1.0")))
        except ValueError:
            return 1.0

    def _messages_to_tokenize_text(self, messages: list[dict]) -> str:
        return messages_to_tokenize_text(
            _messages_for_capabilities(
                messages,
                self.config.capabilities,
                vision_detail=self.config.vision_detail,
            )
        )

    def _estimate_tokens_with_tokenize(self, messages: list[dict]) -> int | None:
        url = self._tokenize_url()
        if not url:
            return None
        payload = json.dumps({"content": self._messages_to_tokenize_text(messages)}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self._tokenize_timeout()) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
        except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError):
            return None
        if isinstance(data, dict):
            tokens = data.get("tokens")
            if isinstance(tokens, list):
                return max(1, len(tokens))
            for key in ("count", "token_count", "n_tokens"):
                value = data.get(key)
                if isinstance(value, int) and value > 0:
                    return value
        return None

    async def _with_retry(
        self,
        factory,
        *,
        timeout: float | None = None,
        max_retries: int | None = None,
        budget: _RequestBudget | None = None,
        per_attempt_timeout: float | None = None,
    ):
        retryable = (
            APIError,
            APITimeoutError,
            RateLimitError,
            TimeoutError,
            asyncio.TimeoutError,
        ) + TRANSPORT_REQUEST_ERRORS
        retry_limit = (
            self.config.max_retries
            if max_retries is None
            else max(0, int(max_retries))
        )
        if budget is None:
            logical_timeout = (
                self.config.overall_timeout if timeout is None else timeout
            )
            budget = _RequestBudget.from_policy(logical_timeout, retry_limit)

        while True:
            attempt = budget.begin_attempt()
            try:
                return await budget.run(factory, cap=per_attempt_timeout)
            except BadRequestError:
                # A deterministic 4xx cannot become valid through backoff.
                # Callers may still perform one scoped compatibility fallback.
                raise
            except LLMOverallTimeout:
                raise
            except retryable as e:
                if attempt >= budget.max_attempts:
                    summary = summarize_provider_error(e)
                    logger.error(
                        "llm request failed error_type=%s category=%s status=%s "
                        "request_id=%s component=request attempt=%s",
                        summary.error_type,
                        summary.category,
                        summary.status_code,
                        summary.request_id,
                        attempt,
                    )
                    if isinstance(e, _RequestCapTimeout):
                        cap = max(0.0, float(per_attempt_timeout or 0))
                        raise TimeoutError(
                            f"LLM request attempt timed out after {cap:g}s"
                        ) from e
                    raise
                summary = summarize_provider_error(e)
                logger.warning(
                    "llm request retry error_type=%s category=%s status=%s "
                    "request_id=%s component=request attempt=%s",
                    summary.error_type,
                    summary.category,
                    summary.status_code,
                    summary.request_id,
                    attempt,
                )
                await budget.backoff(_retry_backoff_seconds(attempt))

    def _completion_kwargs(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
        omit_tool_choice: bool = False,
        generation_overrides: dict | None = None,
    ) -> dict:
        generation = {
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            "top_p": self.config.top_p,
            "top_k": self.config.top_k,
            "min_p": self.config.min_p,
            "presence_penalty": self.config.presence_penalty,
            "repetition_penalty": self.config.repetition_penalty,
        }
        if generation_overrides:
            unknown = set(generation_overrides) - set(generation)
            if unknown:
                raise ValueError(
                    "Unsupported generation override(s): " + ", ".join(sorted(unknown))
                )
            generation.update(generation_overrides)

        kwargs = {
            "model": self.config.model,
            "messages": _messages_for_capabilities(
                [{k: v for k, v in m.items() if k != "_provider_state"} for m in messages],
                self.config.capabilities,
                frozenset(
                    str(tool.get("function", {}).get("name") or "")
                    for tool in (tools or [])
                    if isinstance(tool, dict)
                ),
                self.config.vision_detail,
            ),
            "max_tokens": max(1, int(generation["max_tokens"])),
        }
        if generation["temperature"] is not None:
            # None means "provider default": omit the parameter so the server
            # applies its own sampling default instead of a pinned value.
            kwargs["temperature"] = float(generation["temperature"])
        if generation["top_p"] is not None:
            kwargs["top_p"] = float(generation["top_p"])
        if generation["presence_penalty"] is not None:
            kwargs["presence_penalty"] = float(generation["presence_penalty"])

        extra_body = {}
        if generation["top_k"] is not None:
            extra_body["top_k"] = int(generation["top_k"])
        if generation["min_p"] is not None:
            extra_body["min_p"] = float(generation["min_p"])
        if (
            generation["repetition_penalty"] is not None
            and self.config.repetition_penalty_parameter
        ):
            extra_body[self.config.repetition_penalty_parameter] = float(
                generation["repetition_penalty"]
            )
        if self.config.thinking_mode and is_deepseek_model(self.config.model):
            extra_body["thinking"] = {"type": self.config.thinking_mode}
        if extra_body:
            kwargs["extra_body"] = extra_body

        if (
            self.config.reasoning_effort
            and is_deepseek_model(self.config.model)
        ):
            kwargs["reasoning_effort"] = self.config.reasoning_effort
        self._apply_reasoning_budget(kwargs)

        if tools:
            kwargs["tools"] = tools
            if not omit_tool_choice:
                kwargs["tool_choice"] = tool_choice or "auto"
        return kwargs

    def _apply_reasoning_budget(self, kwargs: dict) -> None:
        """Reserve room for reasoning AND the answer after per-call overrides.

        Thinking consumes the same completion allowance for several providers.
        Mode overrides must not replace the configured model allowance with a
        small answer-only cap. Explicitly disabled thinking keeps that small cap.
        """
        deepseek = is_deepseek_model(self.config.model)
        if not deepseek and not ({"reasoning", "thinking-required"} & self.config.capabilities):
            return
        extra_body = kwargs.get("extra_body") or {}
        thinking = extra_body.get("thinking") or {}
        template = extra_body.get("chat_template_kwargs") or {}
        disabled = thinking.get("type") == "disabled" or template.get("enable_thinking") is False
        if disabled and "thinking-required" not in self.config.capabilities:
            return
        minimum = self.config.max_tokens
        if deepseek:
            # Also cover custom/legacy profiles still using 4K or 8K. Matches
            # the maxTokens value in DeepSeek's official agent configuration.
            minimum = max(minimum, 384_000)
        context_limit = self.config.context_limit
        if context_limit is not None and context_limit > 0:
            # Proactive compression reserves at least half the context for
            # generation. Do not auto-expand beyond that on smaller gateways.
            minimum = min(minimum, max(1, context_limit // 2))
        kwargs["max_tokens"] = max(int(kwargs["max_tokens"]), minimum)

    def supports_forced_tool_choice(self) -> bool:
        """Whether the current thinking endpoint accepts tool_choice.

        DeepSeek V4's official OpenAI endpoint supports tools in thinking mode
        but rejects the tool_choice parameter itself. Other configured
        OpenAI-compatible endpoints keep the normal forced-function path.
        """
        host = urllib.parse.urlparse(self.config.base_url).hostname or ""
        return not (host.lower() == "api.deepseek.com" and "reasoning" in self.config.capabilities)

    async def chat_stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
        omit_tool_choice: bool = False,
        generation_overrides: dict | None = None,
    ):
        """流式调用 LLM，yield 事件。支持 stream_options 获取 usage。"""
        kwargs = self._completion_kwargs(
            messages,
            tools,
            tool_choice,
            omit_tool_choice,
            generation_overrides,
        )
        kwargs.update({"stream": True, "stream_options": {"include_usage": True}})

        budget = _RequestBudget.from_policy(
            self.config.overall_timeout,
            self.config.max_retries,
        )
        stream = await self._with_retry(
            lambda: self._create_completion(kwargs),
            budget=budget,
            per_attempt_timeout=self.config.connect_timeout,
        )
        full_content = ""
        full_reasoning = ""
        tool_calls_buffer: dict[int, dict] = {}
        usage = None
        last_chunk = None
        finish_reason = ""
        emitted_any = False
        recovered = False
        recovery_usage: dict = {}

        async def _consume_stream(timing: _StreamReadTiming):
            nonlocal emitted_any, full_content, full_reasoning, usage, last_chunk, finish_reason
            idle_timeout = self.config.idle_timeout

            async def next_chunk(iterator):
                try:
                    # Completion is already known. Allow a short usage trailer,
                    # not another full idle timeout waiting for socket closure.
                    effective_idle = min(idle_timeout, 1.0) if finish_reason and idle_timeout > 0 else 1.0 if finish_reason else idle_timeout
                    return await timing.read(iterator, budget, effective_idle)
                except LLMOverallTimeout:
                    if finish_reason:
                        raise StopAsyncIteration from None
                    raise
                except _RequestCapTimeout as exc:
                    if finish_reason:
                        raise StopAsyncIteration from None
                    raise LLMIdleTimeout(
                        f"LLM stream produced no meaningful progress for {idle_timeout:g}s"
                    ) from exc

            async def consume_chunks() -> AsyncGenerator[dict, None]:
                nonlocal emitted_any, full_content, full_reasoning, usage, last_chunk, finish_reason
                iterator = stream.__aiter__()
                while True:
                    try:
                        chunk = await next_chunk(iterator)
                    except StopAsyncIteration:
                        return

                    last_chunk = chunk

                    # — usage（最后一个无 choices 的 chunk）—
                    if hasattr(chunk, "usage") and chunk.usage:
                        usage = _usage_dict(chunk.usage)

                    if not chunk.choices:
                        timing.observe("usage" if getattr(chunk, "usage", None) else "empty")
                        continue

                    choice = chunk.choices[0]
                    # Usage may arrive after the terminal delta. Repeated
                    # terminal frames must not append arguments or text twice.
                    if finish_reason:
                        timing.observe("terminal_repeat")
                        continue
                    delta = choice.delta
                    # Role-only/empty heartbeat chunks must not keep a stalled
                    # generation alive forever when overall_timeout is disabled.
                    kind = "empty"
                    if choice.finish_reason:
                        kind = "finish"
                    elif delta:
                        if any(tc.id or (tc.function and (
                            tc.function.name or tc.function.arguments
                        )) for tc in (delta.tool_calls or [])):
                            kind = "tool"
                        elif delta.content:
                            kind = "content"
                        elif getattr(delta, "reasoning_content", None):
                            kind = "reasoning"
                    timing.observe(kind)
                    if choice.finish_reason:
                        finish_reason = str(choice.finish_reason)
                    if not delta:
                        continue

                    rc = getattr(delta, "reasoning_content", None)
                    if rc:
                        full_reasoning += rc
                        emitted_any = True
                        yield {"type": "reasoning", "content": rc}

                    if delta.content:
                        full_content += delta.content
                        emitted_any = True
                        yield {"type": "chunk", "content": delta.content}

                    if delta.tool_calls:
                        emitted_any = True
                        for tc in delta.tool_calls:
                            idx = tc.index
                            if isinstance(idx, bool) or not isinstance(idx, int) or idx < 0:
                                raise LLMResponseError("invalid_tool_index", "Tool delta has no valid index; no tool was executed.")
                            if idx not in tool_calls_buffer:
                                tool_calls_buffer[idx] = {"id": "", "name": "", "arguments": ""}
                            def accept_identity(key: str, incoming: object, index: int = idx) -> None:
                                if incoming is None or incoming == "":
                                    return
                                current = tool_calls_buffer[index][key]
                                if not isinstance(incoming, str) or (current and current != incoming):
                                    raise LLMResponseError("conflicting_tool_identity", "Tool identity changed during streaming; no tool was executed.")
                                tool_calls_buffer[index][key] = incoming

                            accept_identity("id", tc.id)
                            if tc.function:
                                accept_identity("name", tc.function.name)
                                if tc.function.arguments:
                                    if not isinstance(tc.function.arguments, str):
                                        raise LLMResponseError("invalid_tool_arguments", "Tool argument delta must be text; no tool was executed.")
                                    tool_calls_buffer[idx]["arguments"] += tc.function.arguments

            chunk_events = consume_chunks()
            try:
                async for event in chunk_events:
                    yield event
            finally:
                await chunk_events.aclose()
                await _close_async_stream(stream)

        retryable = (
            APIError,
            APITimeoutError,
            RateLimitError,
            TimeoutError,
            asyncio.TimeoutError,
        ) + TRANSPORT_REQUEST_ERRORS
        while True:
            timing = _StreamReadTiming()
            stream_events = _consume_stream(timing)
            try:
                try:
                    async for event in stream_events:
                        yield event
                finally:
                    await stream_events.aclose()
                if not finish_reason:
                    raise LLMResponseError("stream_closed", "Stream ended without a completion signal. Partial output was retained; no tool was executed.")
                if not full_content.strip() and not tool_calls_buffer:
                    code = "reasoning_only_response" if full_reasoning.strip() else "empty_response"
                    if finish_reason.lower() not in {"stop", "length", "max_tokens", "max_output_tokens"}:
                        raise LLMResponseError("incomplete_response", "Provider reported no usable answer or tool call.")
                    if recovered:
                        raise LLMResponseError(code, "The model returned no answer or tool call. Recovery was exhausted; retry the request or change the model.")
                    recovered = True
                    recovery_usage = dict(usage or {})
                    yield {"type": "generation_recovery", "code": code,
                           "message": "The model ended without an answer or tool call; attempting one bounded recovery."}
                    # No tool operation has been exposed or executed. The
                    # semantic retry has its own single attempt, sharing the
                    # original deadline; transport retries cannot multiply it.
                    budget = budget.fork_attempts(0)
                    kwargs = {**kwargs, "messages": [*kwargs["messages"], {
                        "role": "user",
                        "content": "Your previous response ended without an answer or a tool call. Complete the current request now with a concise answer or one complete tool call. Do not restart planning or repeat prior analysis.",
                    }]}
                    stream = await self._with_retry(
                        lambda request=kwargs: self._create_completion(request), budget=budget,
                        per_attempt_timeout=self.config.connect_timeout,
                    )
                    full_content = full_reasoning = finish_reason = ""
                    usage = last_chunk = None
                    emitted_any = False
                    continue
                break
            except retryable as exc:
                if emitted_any or not budget.can_attempt():
                    summary = summarize_provider_error(exc)
                    logger.error(
                        "llm stream failed error_type=%s category=%s status=%s "
                        "request_id=%s component=stream attempt=%s emitted_any=%s "
                        "read_wait_seconds=%.3f idle_wait_seconds=%.3f local_pause_seconds=%.3f "
                        "chunks=%s meaningful_chunks=%s last_chunk_kind=%s",
                        summary.error_type,
                        summary.category,
                        summary.status_code,
                        summary.request_id,
                        budget.attempts,
                        emitted_any,
                        timing.read_wait_seconds,
                        timing.idle_wait_seconds,
                        timing.local_pause_seconds,
                        timing.chunk_count,
                        timing.meaningful_chunk_count,
                        timing.last_chunk_kind,
                    )
                    raise
                logger.warning("llm stream retry attempt=%s", budget.attempts)
                await budget.backoff(_retry_backoff_seconds(budget.attempts))
                stream = await self._with_retry(
                    lambda request=kwargs: self._create_completion(request),
                    budget=budget,
                    per_attempt_timeout=self.config.connect_timeout,
                )
                full_content = ""
                full_reasoning = ""
                tool_calls_buffer.clear()
                usage = None
                last_chunk = None
                finish_reason = ""
                emitted_any = False

        finish = finish_reason

        # 如果没有 usage chunk（某些 API 不返回），用 last_chunk 的 usage 兜底
        if not usage and last_chunk and hasattr(last_chunk, "usage") and last_chunk.usage:
            usage = _usage_dict(last_chunk.usage)
        if recovery_usage:
            last_prompt_tokens = (usage or {}).get("prompt_tokens", 0)
            usage = {key: recovery_usage.get(key, 0) + (usage or {}).get(key, 0)
                     for key in recovery_usage.keys() | (usage or {}).keys()}
            usage["last_prompt_tokens"] = last_prompt_tokens

        if tool_calls_buffer:
            if finish.lower() not in {"stop", "end_turn", "tool_calls", "function_call", "length", "max_tokens", "max_output_tokens"}:
                raise LLMResponseError("incomplete_response", "Tool batch did not receive a successful completion signal; no tool was executed.")
            yield {"type": "tool_calls", "calls": [tool_calls_buffer[index] for index in sorted(tool_calls_buffer)],
                   "content": full_content, "reasoning_content": full_reasoning,
                   "finish_reason": finish,
                   "tool_call_state": (
                       "incomplete"
                       if finish.lower() in {"length", "max_tokens", "max_output_tokens"}
                       else "complete"
                   ),
                   "usage": usage}
        else:
            if finish.lower() in {"length", "max_tokens", "max_output_tokens"}:
                raise LLMResponseError("response_truncated", "The output limit was reached. Partial text was retained; request a shorter answer or continue explicitly.")
            if finish.lower() not in {"stop", "end_turn"}:
                raise LLMResponseError("incomplete_response", "The provider did not report a successful answer; partial output was retained.")
            yield {"type": "done", "content": full_content, "reasoning_content": full_reasoning,
                   "finish_reason": finish, "usage": usage}

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
        max_tokens: int | None = None,
    ) -> dict:
        kwargs = self._completion_kwargs(messages, tools, tool_choice)
        if max_tokens is not None:
            kwargs["max_tokens"] = max(64, int(max_tokens))
            self._apply_reasoning_budget(kwargs)
        budget = _RequestBudget.from_policy(
            self.config.overall_timeout,
            self.config.max_retries,
        )
        return await self._invoke_chat_completion(kwargs, budget)

    async def _invoke_chat_completion(
        self,
        kwargs: dict,
        budget: _RequestBudget,
    ) -> dict:
        """Pass the budget while preserving strict private test substitutes."""

        if _accepts_keyword(self._chat_completion, "budget"):
            return await self._chat_completion(kwargs, budget=budget)
        return await self._chat_completion(kwargs)

    async def chat_limited(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        *,
        max_tokens: int,
        temperature: float = 0.1,
        disable_thinking: bool = False,
        reasoning_effort: str | None = None,
        request_timeout: float | None = None,
        max_retries: int | None = None,
    ) -> dict:
        kwargs = self._completion_kwargs(messages, tools)
        if reasoning_effort is not None and is_deepseek_model(self.config.model):
            kwargs["reasoning_effort"] = reasoning_effort
        kwargs["max_tokens"] = max(64, int(max_tokens))
        kwargs["temperature"] = float(temperature)
        injected_disable_thinking = (
            disable_thinking
            and "thinking-required" not in self.config.capabilities
        )
        if injected_disable_thinking:
            extra_body = dict(kwargs.get("extra_body") or {})
            if is_deepseek_model(self.config.model):
                extra_body["thinking"] = {"type": "disabled"}
            else:
                template_kwargs = dict(extra_body.get("chat_template_kwargs") or {})
                template_kwargs["enable_thinking"] = False
                extra_body["chat_template_kwargs"] = template_kwargs
            kwargs["extra_body"] = extra_body
        self._apply_reasoning_budget(kwargs)
        retry_limit = (
            self.config.max_retries
            if max_retries is None
            else max(0, int(max_retries))
        )
        logical_timeout = (
            self.config.overall_timeout
            if request_timeout is None
            else request_timeout
        )
        budget = _RequestBudget.from_policy(logical_timeout, retry_limit)
        try:
            return await self._invoke_chat_completion(kwargs, budget)
        except BadRequestError as exc:
            message = str(exc).lower()
            thinking_rejected = (
                injected_disable_thinking
                and "enable_thinking" in message
                and (
                    "restricted to true" in message
                    or "must be true" in message
                    or "only supports true" in message
                )
            )
            if not thinking_rejected:
                raise
            # Some OpenAI-compatible reasoning models are thinking-only but do
            # not advertise that capability. Retry once without the optional
            # disable flag so the provider can use its required default.
            fallback = dict(kwargs)
            extra_body = dict(fallback.get("extra_body") or {})
            template_kwargs = dict(extra_body.get("chat_template_kwargs") or {})
            template_kwargs.pop("enable_thinking", None)
            if template_kwargs:
                extra_body["chat_template_kwargs"] = template_kwargs
            else:
                extra_body.pop("chat_template_kwargs", None)
            if extra_body:
                fallback["extra_body"] = extra_body
            else:
                fallback.pop("extra_body", None)
            self._apply_reasoning_budget(fallback)
            logger.warning(
                "provider rejected enable_thinking=false; retrying once with provider default "
                "model=%s",
                self.config.model,
            )
            compatibility_budget = budget.fork_attempts(retry_limit)
            return await self._invoke_chat_completion(
                fallback,
                compatibility_budget,
            )

    async def _chat_completion(
        self,
        kwargs: dict,
        *,
        request_timeout: float | None = None,
        max_retries: int | None = None,
        budget: _RequestBudget | None = None,
    ) -> dict:
        response = await self._with_retry(
            lambda: self._create_completion(kwargs),
            timeout=request_timeout,
            max_retries=max_retries,
            budget=budget,
        )
        choice = response.choices[0]
        result = {
            "content": choice.message.content or "",
            "tool_calls": [
                {"id": tc.id, "name": tc.function.name, "arguments": tc.function.arguments}
                for tc in (choice.message.tool_calls or [])
            ],
            "finish_reason": choice.finish_reason or "",
            "reasoning_content": getattr(choice.message, "reasoning_content", None) or "",
        }
        if hasattr(response, "usage") and response.usage:
            result["usage"] = _usage_dict(response.usage)
        return result

    def estimate_tokens(self, messages: list[dict]) -> int:
        prepared = _messages_for_capabilities(
            [{k: v for k, v in m.items() if k != "_provider_state"} for m in messages],
            self.config.capabilities,
            vision_detail=self.config.vision_detail,
        )
        tokenized = self._estimate_tokens_with_tokenize(prepared)
        if tokenized:
            return tokenized
        return max(1, round(estimate_messages_tokens(prepared) * self._estimate_calibration))

    def record_prompt_usage(self, estimated: int | None, actual: int | None):
        if not estimated or not actual or estimated <= 0 or actual <= 0:
            return
        # ``estimated`` already includes the current calibration. Treat the
        # usage ratio as a correction to that factor, not a replacement for it.
        ratio = max(0.25, min(4.0, self._estimate_calibration * actual / estimated))
        self._estimate_calibration = (self._estimate_calibration * 0.8) + (ratio * 0.2)


class LLMClient:
    def __init__(
        self,
        config: LLMConfig,
        provider=None,
        provider_factory=None,
        provider_registry: ProviderRegistry | None = None,
    ):
        self.config = config
        self.provider_registry = provider_registry or DEFAULT_PROVIDER_REGISTRY
        self._provider_factory = provider_factory
        self.provider = provider or self._create_provider(config)
        self._rate_lock = asyncio.Lock()
        self._request_slots = asyncio.Semaphore(max(1, config.max_concurrent_requests))
        self._last_request_started = 0.0

    def _create_provider(self, config: LLMConfig):
        if self._provider_factory is not None:
            return self._provider_factory(config)
        return self.provider_registry.create(config.provider, config)

    def switch_model(
        self,
        model: str,
        base_url: str | None = None,
        *,
        provider_name: str | None = None,
        api_key: str | None = None,
        capabilities: frozenset[str] | set[str] | None = None,
        **generation_settings,
    ):
        """Switch API model/endpoint without managing the external model server."""
        # A direct model switch must not inherit the previous model capacity.
        generation_settings.setdefault("context_limit", None)
        generation_settings.setdefault("connection_required", False)
        if provider_name and provider_name != self.config.provider:
            generation_settings.setdefault("reasoning_levels", ())
        next_config = replace(
            self.config,
            model=model,
            base_url=base_url or self.config.base_url,
            provider=provider_name or self.config.provider,
            api_key=self.config.api_key if api_key is None else api_key,
            capabilities=(
                self.config.capabilities
                if capabilities is None
                else frozenset(capabilities)
            ),
            **generation_settings,
        )
        next_provider = self._create_provider(next_config)
        self.config = next_config
        self.provider = next_provider

    async def _wait_for_rate_limit(self):
        if self.config.min_request_interval <= 0:
            return
        async with self._rate_lock:
            now = asyncio.get_running_loop().time()
            wait_for = self.config.min_request_interval - (now - self._last_request_started)
            if wait_for > 0:
                await asyncio.sleep(wait_for)
            self._last_request_started = asyncio.get_running_loop().time()

    async def chat_stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
        omit_tool_choice: bool = False,
        generation_overrides: dict | None = None,
    ):
        async with self._request_slots:
            await self._wait_for_rate_limit()
            with trace_span("agent.llm.stream", {
                "gen_ai.provider.name": self.config.provider,
                "gen_ai.request.model": self.config.model,
                "agent.llm.streaming": True,
                "agent.llm.message_count": len(messages),
                "agent.llm.tool_count": len(tools or []),
            }):
                if generation_overrides:
                    stream = self.provider.chat_stream(
                        messages,
                        tools,
                        tool_choice,
                        omit_tool_choice=omit_tool_choice,
                        generation_overrides=generation_overrides,
                    )
                elif tool_choice is None and not omit_tool_choice:
                    # Preserve the minimal provider protocol for custom and
                    # test providers that only implement (messages, tools).
                    stream = self.provider.chat_stream(messages, tools)
                else:
                    stream = self.provider.chat_stream(
                        messages,
                        tools,
                        tool_choice,
                        omit_tool_choice=omit_tool_choice,
                    )
                try:
                    async for event in stream:
                        yield event
                finally:
                    await _close_async_stream(stream)

    def supports_forced_tool_choice(self) -> bool:
        checker = getattr(self.provider, "supports_forced_tool_choice", None)
        return True if checker is None else bool(checker())

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
        max_tokens: int | None = None,
    ) -> dict:
        async with self._request_slots:
            await self._wait_for_rate_limit()
            with trace_span("agent.llm.chat", {
                "gen_ai.provider.name": self.config.provider,
                "gen_ai.request.model": self.config.model,
                "agent.llm.streaming": False,
                "agent.llm.message_count": len(messages),
                "agent.llm.tool_count": len(tools or []),
            }):
                if max_tokens is not None and _accepts_keyword(
                    self.provider.chat, "max_tokens"
                ):
                    if tool_choice is None:
                        return await self.provider.chat(
                            messages, tools, max_tokens=max_tokens,
                        )
                    return await self.provider.chat(
                        messages, tools, tool_choice, max_tokens=max_tokens,
                    )
                if tool_choice is None:
                    return await self.provider.chat(messages, tools)
                return await self.provider.chat(messages, tools, tool_choice)

    async def chat_limited(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        *,
        max_tokens: int,
        temperature: float = 0.1,
        disable_thinking: bool = False,
        reasoning_effort: str | None = None,
        request_timeout: float | None = None,
        max_retries: int | None = None,
    ) -> dict:
        async with self._request_slots:
            await self._wait_for_rate_limit()
            limited = getattr(self.provider, "chat_limited", None)
            if limited is None:
                return await self.provider.chat(messages, tools)
            options = {
                "max_tokens": max_tokens,
                "temperature": temperature,
                "disable_thinking": disable_thinking,
                "reasoning_effort": reasoning_effort,
            }
            if _accepts_keyword(limited, "request_timeout"):
                options["request_timeout"] = request_timeout
            if _accepts_keyword(limited, "max_retries"):
                options["max_retries"] = max_retries
            return await limited(
                messages,
                tools,
                **options,
            )

    def estimate_tokens(self, messages: list[dict]) -> int:
        estimator = getattr(self.provider, "estimate_tokens", None)
        if estimator:
            return estimator(messages)
        return estimate_messages_tokens(messages)

    def record_prompt_usage(self, estimated: int | None, actual: int | None):
        recorder = getattr(self.provider, "record_prompt_usage", None)
        if recorder:
            recorder(estimated, actual)
