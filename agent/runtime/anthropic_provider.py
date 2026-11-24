"""Direct Anthropic Messages API transport (a Claude API key) for Astra's agent loop.

Each logical request has a deadline and attempt budget. A temporary TLS failure before the response
headers can reconnect once within that budget. The wire translation is in ``anthropic_wire``.
"""
from __future__ import annotations

import json
import logging
import os
import re
import ssl
import urllib.parse
from collections.abc import AsyncGenerator
from typing import Any

import httpx

from .anthropic_wire import (
    API_KEY_ENV,
    API_VERSION,
    BASE_URL,
    StreamAccumulator,
    adaptive_thinking,
    build_request_body,
    normalize_discovery_models,
    normalize_model_id,
    request_id,
)
from .harness import default_llm_timeout
from .llm import (
    LLMConfig,
    LLMIdleTimeout,
    LLMOverallTimeout,
    LLMResponseError,
    _messages_for_capabilities,
    _RequestBudget,
    _RequestCapTimeout,
    _StreamReadTiming,
)
from .network import active_proxy_for_url
from .provider_errors import tls_failure_reason, transport_error_diagnostics
from .token_estimator import estimate_messages_tokens
from .tool_preparation import with_tool_preparation

logger = logging.getLogger(__name__)

_MAX_DISCOVERY_PAGES = 5
_RETRY_STATUSES = frozenset({408, 409, 429, 500, 502, 503, 504, 529})
_TRANSIENT_STREAM_ERRORS = frozenset({"overloaded_error", "api_error", "rate_limit_error", "timeout_error"})
_TRANSIENT_TLS_ERRORS = frozenset({"bad_record_mac", "unexpected_tls_eof"})
_GENERATION_OVERRIDE_KEYS = frozenset({
    "temperature", "max_tokens", "top_p", "top_k", "min_p", "presence_penalty", "repetition_penalty",
})
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
# Optional request features switched off for the rest of the process after the API rejects them,
# so a change on Anthropic's side costs one retried request instead of every request failing.
_DISABLED_FEATURES: set[str] = set()


class AnthropicRequestError(LLMResponseError):
    """Astra-authored diagnostics, with at most a bounded upstream reason for a rejected request."""

    def __init__(self, message: str, *, code: str = "anthropic_request_failed", status_code: int | None = None,
                 transient: bool = False):
        super().__init__(code, message)
        self.message = message
        self.status_code = status_code
        self.transient = transient

    @property
    def public_message(self) -> str:
        return self.message

    @property
    def public_code(self) -> str:
        return self.code


def _error_fields(body: bytes) -> tuple[str, str]:
    """The error type and message from an API error body, bounded and without control characters."""
    if not body:
        return "", ""
    try:
        data = json.loads(body[:16384].decode("utf-8", errors="replace"))
    except ValueError:
        return "", ""
    error = data.get("error") if isinstance(data, dict) else None
    if not isinstance(error, dict):
        return "", ""
    kind = str(error.get("type") or "").strip()
    message = _CONTROL.sub(" ", str(error.get("message") or "")).strip()
    return kind, message[:400]


def _classify_http_error(status: int, body: bytes) -> AnthropicRequestError:
    kind, message = _error_fields(body)
    lowered = message.lower()
    if status == 401 or kind == "authentication_error":
        return AnthropicRequestError(
            f"Claude API authentication failed (HTTP {status}). Check {API_KEY_ENV} or reconnect Claude API with /connect.",
            code="anthropic_auth_failed", status_code=status)
    if "credit balance" in lowered or kind == "billing_error":
        return AnthropicRequestError(
            "The Claude API account has no credit left for this request. Add credit in the Claude Console "
            "or switch models with /model.", code="anthropic_billing", status_code=status)
    if status == 403 or kind == "permission_error":
        return AnthropicRequestError(
            f"The Claude API key may not use this model or feature (HTTP {status}).",
            code="anthropic_permission_denied", status_code=status)
    if status == 404 or kind == "not_found_error":
        return AnthropicRequestError(
            f"Claude model or endpoint not found (HTTP {status}). Refresh models with /model or check the model ID.",
            code="anthropic_not_found", status_code=status)
    if status == 413 or kind == "request_too_large":
        return AnthropicRequestError(
            "Claude rejected the request as too large (HTTP 413). Remove large images or attachments.",
            code="anthropic_request_too_large", status_code=status)
    if "prompt is too long" in lowered or "context window" in lowered:
        return AnthropicRequestError(
            "Claude request rejected: prompt is too long for the model's context window.",
            code="context_length_exceeded", status_code=status)
    if status == 429 or kind == "rate_limit_error":
        return AnthropicRequestError(
            f"Claude API rate limit reached (HTTP {status}). Wait briefly and retry.",
            code="anthropic_rate_limited", status_code=status, transient=True)
    if status == 529 or kind == "overloaded_error":
        return AnthropicRequestError(
            f"Claude is overloaded (HTTP {status}). Retry shortly.",
            code="anthropic_overloaded", status_code=status, transient=True)
    if status in {400, 422}:
        reason = f": {message}" if message else ""
        return AnthropicRequestError(f"Claude rejected the request (HTTP {status}){reason}",
                                     code="anthropic_bad_request", status_code=status)
    return AnthropicRequestError(
        f"Claude request failed (HTTP {status}). Retry shortly or check status.claude.com.",
        code="anthropic_request_failed", status_code=status, transient=status in _RETRY_STATUSES)


def _rejected_features(status: int, body: bytes, sent: set[str]) -> set[str]:
    """Optional features a 400 names, among those this request sent."""
    if status != 400:
        return set()
    _, message = _error_fields(body)
    lowered = message.lower()
    named = set()
    if "fallback" in lowered:
        named.add("fallbacks")
    if "block_binding" in lowered or "prefix_mismatch" in lowered:
        named.add("thinking_binding")
    if "anthropic-beta" in lowered or "beta" in lowered.split():
        named |= {"fallbacks", "thinking_binding"}
    return named & sent


def _retry_after_seconds(response: httpx.Response, attempt: int) -> float:
    raw = response.headers.get("retry-after", "").strip()
    try:
        parsed = float(raw) if raw else 0.0
    except ValueError:
        parsed = 0.0
    if parsed > 0:
        return min(30.0, parsed)
    return float(min(8, 2 ** max(0, attempt - 1)))


def _timeout_value(raw: str, fallback: float) -> float:
    try:
        return max(0.0, float(raw))
    except ValueError:
        return fallback


def _httpx_timeout_value(value: float) -> float | None:
    return value if value > 0 else None


def _timeout_env(name: str, fallback: float) -> float:
    raw = os.getenv(name)
    return fallback if raw is None else _timeout_value(raw, fallback)


def _headers(api_key: str, betas: list[str] | None = None) -> dict[str, str]:
    headers = {"x-api-key": api_key, "anthropic-version": API_VERSION, "Content-Type": "application/json"}
    if betas:
        headers["anthropic-beta"] = ",".join(betas)
    return headers


async def fetch_models(
    base_url: str = BASE_URL,
    api_key: str = "",
    *,
    timeout: float = 5.0,
    client: httpx.AsyncClient | None = None,
) -> list[dict[str, Any]]:
    """Fetch and normalize the models this key can use from GET /v1/models."""
    resolved_url = (base_url or BASE_URL).rstrip("/")
    resolved_key = (api_key or "").strip()
    if not resolved_key:
        raise AnthropicRequestError(
            f"Missing Claude API key. Connect Claude API with /connect or set {API_KEY_ENV}.",
            code="anthropic_auth_failed")
    owned = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False,
                                   proxy=active_proxy_for_url(resolved_url))
    try:
        items: list[Any] = []
        params: dict[str, str] = {"limit": "1000"}
        for _ in range(_MAX_DISCOVERY_PAGES):
            response = await client.get(resolved_url + "/models", headers=_headers(resolved_key), params=params)
            if response.status_code != 200:
                if response.status_code in {401, 403}:
                    raise AnthropicRequestError("Authentication rejected; check this route's API key",
                                                code="auth_failed", status_code=response.status_code)
                response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                raise ValueError("Response is not a Claude model list")
            items.extend(payload["data"])
            last = payload.get("last_id")
            if payload.get("has_more") is not True or not isinstance(last, str) or not last:
                break
            params = {"limit": "1000", "after_id": last}
        return normalize_discovery_models({"data": items})
    finally:
        if owned:
            await client.aclose()


async def _close(resource, *, request_key: str, component: str, response=None) -> None:
    try:
        await resource.aclose()
    except Exception as exc:
        logger.warning("anthropic %s close failed request=%s diagnostics=%s", component, request_key,
                       json.dumps(transport_error_diagnostics(exc, response=response), sort_keys=True))


class AnthropicProvider:
    """Native Anthropic Messages API streaming provider."""

    def __init__(self, config: LLMConfig, *, transport=None):
        self.config = config
        timeout_env = os.getenv("LLM_TIMEOUT")
        self.config.timeout = default_llm_timeout(config.base_url, timeout_env, self.config.timeout)
        self.config.connect_timeout = _timeout_env("LLM_CONNECT_TIMEOUT", self.config.connect_timeout)
        self.config.idle_timeout = _timeout_env("LLM_IDLE_TIMEOUT", self.config.idle_timeout)
        overall_env = os.getenv("LLM_OVERALL_TIMEOUT")
        if overall_env is not None:
            self.config.overall_timeout = _timeout_value(overall_env, self.config.overall_timeout or 0)
            self.config.stream_overall_timeout = self.config.overall_timeout
        elif timeout_env is not None:
            self.config.overall_timeout = self.config.timeout
            self.config.stream_overall_timeout = self.config.overall_timeout
        elif self.config.overall_timeout is None:
            hostname = (urllib.parse.urlparse(config.base_url).hostname or "").lower()
            self.config.overall_timeout = 0.0 if hostname in {"localhost", "127.0.0.1", "::1"} else self.config.timeout
            if self.config.stream_overall_timeout is None:
                self.config.stream_overall_timeout = 0.0
        self.transport = transport
        self._estimate_calibration = 1.0
        self.served_model = ""

    def supports_forced_tool_choice(self) -> bool:
        # Current Claude models reject forced tool use, and every model rejects it with thinking on.
        return False

    def _api_key(self) -> str:
        if self.config.connection_required:
            raise ValueError("Connect a provider with /connect and select a model before sending a request.")
        key = (self.config.api_key or os.getenv(API_KEY_ENV, "")).strip()
        if not key:
            raise AnthropicRequestError(
                f"Missing Claude API key. Connect Claude API with /connect or set {API_KEY_ENV}.",
                code="anthropic_auth_failed")
        return key

    def _endpoint_url(self) -> str:
        model = normalize_model_id(self.config.model)
        if not model or any(ord(c) < 32 or ord(c) == 127 for c in model):
            raise AnthropicRequestError("Invalid Claude model ID.", code="anthropic_bad_request")
        return (self.config.base_url or BASE_URL).rstrip("/") + "/messages"

    def _max_tokens(self, requested: int) -> int:
        """With thinking on, thinking and answer share max_tokens: keep the configured room (billing
        follows the tokens produced, not the cap), as the Gemini provider does."""
        value = max(1, int(requested))
        if not adaptive_thinking(self.config.model):
            return value
        minimum = max(1, int(self.config.max_tokens))
        if self.config.context_limit:
            minimum = min(minimum, max(1, int(self.config.context_limit) // 2))
        return max(value, minimum)

    def _body(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        *,
        generation_overrides: dict[str, Any] | None = None,
        disable_thinking: bool = False,
        reasoning_effort: str | None = None,
    ) -> tuple[dict[str, Any], list[str], dict[str, str]]:
        max_tokens = self.config.max_tokens
        if generation_overrides:
            unknown = set(generation_overrides) - _GENERATION_OVERRIDE_KEYS
            if unknown:
                raise ValueError("Unsupported generation override(s): " + ", ".join(sorted(unknown)))
            # Sampling settings are rejected by current Claude models; only the output limit applies.
            max_tokens = int(generation_overrides.get("max_tokens", max_tokens))
        prepared = _messages_for_capabilities(
            messages,
            self.config.capabilities,
            frozenset(str(tool.get("function", {}).get("name") or "") for tool in (tools or []) if isinstance(tool, dict)),
            self.config.vision_detail,
        )
        return build_request_body(
            prepared,
            model=self.config.model,
            max_tokens=self._max_tokens(max_tokens),
            effort=reasoning_effort if reasoning_effort is not None else self.config.reasoning_effort,
            reasoning_levels=self.config.reasoning_levels,
            low_effort=disable_thinking,
            tools=tools,
            tool_choice=tool_choice,
            thinking_binding="thinking_binding" not in _DISABLED_FEATURES,
            fallbacks="fallbacks" not in _DISABLED_FEATURES,
        )

    @with_tool_preparation
    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        *,
        omit_tool_choice: bool = False,
        generation_overrides: dict[str, Any] | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        stream = self._stream_request(
            messages, tools=tools, tool_choice=None if omit_tool_choice else tool_choice,
            generation_overrides=generation_overrides,
            overall_timeout=(self.config.overall_timeout if self.config.stream_overall_timeout is None
                             else self.config.stream_overall_timeout),
        )
        try:
            async for event in stream:
                yield event
        finally:
            await stream.aclose()

    async def _stream_request(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        generation_overrides: dict[str, Any] | None = None,
        disable_thinking: bool = False,
        reasoning_effort: str | None = None,
        overall_timeout: float | None = None,
        max_retries: int | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        api_key = self._api_key()
        url = self._endpoint_url()

        def build():
            return self._body(messages, tools, tool_choice, generation_overrides=generation_overrides,
                              disable_thinking=disable_thinking, reasoning_effort=reasoning_effort)

        body, betas, names = build()
        retries = self.config.max_retries if max_retries is None else max(0, int(max_retries))
        logical_timeout = self.config.overall_timeout if overall_timeout is None else overall_timeout
        budget = _RequestBudget.from_policy(logical_timeout, retries)
        request_key = request_id()
        timeout = httpx.Timeout(
            connect=_httpx_timeout_value(self.config.connect_timeout),
            read=None,
            write=_httpx_timeout_value(self.config.timeout),
            pool=_httpx_timeout_value(self.config.connect_timeout),
        )
        proxy = active_proxy_for_url(self.config.base_url) if self.transport is None else None
        client = httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False,
                                   transport=self.transport, proxy=proxy)
        tls_retried = False
        try:
            while True:
                yield {"type": "_tool_preparation_reset"}
                budget.begin_attempt()
                response: httpx.Response | None = None
                visible = False
                timing = _StreamReadTiming()
                try:
                    request = client.build_request(
                        "POST", url, headers={**_headers(api_key, betas), "Accept": "text/event-stream"}, json=body)
                    response = await budget.run(lambda request=request: client.send(request, stream=True),
                                                cap=self.config.connect_timeout)
                    assert response is not None
                    if response.status_code != 200:
                        status = response.status_code
                        error_body = await budget.run(response.aread, cap=min(10.0, self.config.connect_timeout))
                        sent = {name for name, flag in (("fallbacks", "fallbacks" in body),
                                                        ("thinking_binding", "block_binding" in body.get("thinking", {})))
                                if flag}
                        rejected = _rejected_features(status, error_body, sent)
                        if rejected:
                            _DISABLED_FEATURES.update(rejected)
                            logger.warning("anthropic rejected %s; retrying without it for this process request=%s",
                                           ",".join(sorted(rejected)), request_key)
                            await _close(response, request_key=request_key, component="response", response=response)
                            response = None
                            body, betas, names = build()
                            budget = budget.fork_attempts(retries)
                            continue
                        if status in _RETRY_STATUSES and budget.can_attempt():
                            delay = _retry_after_seconds(response, budget.attempts)
                            logger.warning("anthropic request retry request=%s attempt=%s/%s status=%s delay=%.2fs",
                                           request_key, budget.attempts, budget.max_attempts, status, delay)
                            await _close(response, request_key=request_key, component="response", response=response)
                            response = None
                            await budget.backoff(delay)
                            continue
                        error = _classify_http_error(status, error_body)
                        logger.warning("anthropic request failed request=%s status=%s code=%s", request_key, status,
                                       error.code)
                        raise error

                    lines = response.aiter_lines().__aiter__()
                    accumulator = StreamAccumulator(model=self.config.model, names=names)
                    data: list[str] = []

                    def flush(acc: StreamAccumulator = accumulator) -> list[dict[str, Any]]:
                        nonlocal data
                        if not data:
                            return []
                        encoded, data = "\n".join(data), []
                        try:
                            payload = json.loads(encoded)
                        except ValueError as exc:
                            raise LLMResponseError("stream_closed", "Invalid Claude stream frame.") from exc
                        if isinstance(payload, dict) and payload.get("type") == "error":
                            raw_error = payload.get("error")
                            error: dict[str, Any] = raw_error if isinstance(raw_error, dict) else {}
                            kind = str(error.get("type") or "")
                            raise AnthropicRequestError(
                                f"Claude stream reported an error ({kind or 'unknown'}).",
                                code="anthropic_overloaded" if kind == "overloaded_error" else "anthropic_stream_error",
                                transient=kind in _TRANSIENT_STREAM_ERRORS)
                        events = acc.observe(payload)
                        if acc.served_model:
                            self.served_model = acc.served_model
                        return events

                    while True:
                        try:
                            idle = (min(self.config.idle_timeout, 1.0) if accumulator.stopped and self.config.idle_timeout > 0
                                    else 1.0 if accumulator.stopped else self.config.idle_timeout)
                            line = await timing.read(lines, budget, idle)
                        except StopAsyncIteration:
                            for event in flush():
                                timing.observe(_kind(event))
                                visible = True
                                yield event
                            break
                        except (LLMOverallTimeout, _RequestCapTimeout):
                            if accumulator.stopped:
                                break
                            raise
                        except (ssl.SSLError, httpx.RequestError) as exc:
                            if accumulator.stopped and tls_failure_reason(exc) in _TRANSIENT_TLS_ERRORS:
                                break
                            raise
                        if line.startswith("data:"):
                            data.append(line[5:].lstrip())
                            continue
                        if line or not data:
                            continue
                        events = flush()
                        if not events:
                            timing.observe("finish" if accumulator.stopped else "empty")
                        for event in events:
                            timing.observe(_kind(event))
                            visible = True
                            yield event

                    if accumulator.dropped_thinking:
                        logger.info("anthropic dropped %s earlier thinking block(s) request=%s",
                                    accumulator.dropped_thinking, request_key)
                    result = accumulator.finalize()
                    if budget.attempts > 1:
                        logger.info("anthropic stream recovered request=%s attempts=%s", request_key, budget.attempts)
                    yield result
                    return
                except (_RequestCapTimeout, httpx.RequestError, ssl.SSLError, LLMResponseError) as exc:
                    tls_reason = tls_failure_reason(exc) if isinstance(exc, (httpx.RequestError, ssl.SSLError)) else None
                    if tls_reason is not None:
                        transient = response is None and not tls_retried and tls_reason in _TRANSIENT_TLS_ERRORS
                    else:
                        transient = (
                            not isinstance(exc, LLMResponseError)
                            or exc.code == "stream_closed"
                            or (isinstance(exc, AnthropicRequestError) and exc.transient)
                        )
                    retry = transient and budget.can_attempt()
                    diagnostics = json.dumps(transport_error_diagnostics(exc, response=response), sort_keys=True)
                    logger.log(
                        logging.WARNING if retry else logging.ERROR,
                        "anthropic stream failed request=%s attempt=%s/%s retrying=%s visible=%s "
                        "read_wait_seconds=%.3f idle_wait_seconds=%.3f last_chunk_kind=%s tls_reason=%s diagnostics=%s",
                        request_key, budget.attempts, budget.max_attempts, retry, visible,
                        timing.read_wait_seconds, timing.idle_wait_seconds, timing.last_chunk_kind, tls_reason, diagnostics)
                    if not retry:
                        if tls_reason == "certificate_verify_failed":
                            raise AnthropicRequestError("Claude TLS certificate verification failed.",
                                                        code="anthropic_tls_certificate_failed") from None
                        if tls_reason is not None:
                            raise AnthropicRequestError("Claude TLS connection failed.",
                                                        code="anthropic_tls_failed") from None
                        if isinstance(exc, _RequestCapTimeout):
                            raise LLMIdleTimeout(
                                f"Claude made no progress before the connection/idle timeout. Diagnostics: {diagnostics}"
                            ) from exc
                        if transient and not isinstance(exc, LLMResponseError):
                            raise LLMResponseError(
                                "stream_closed",
                                f"Claude connection failed; partial calls were discarded. Diagnostics: {diagnostics}",
                            ) from exc
                        raise
                    if tls_reason is not None:
                        tls_retried = True
                finally:
                    if response is not None:
                        await _close(response, request_key=request_key, component="response", response=response)
                yield {"type": "_tool_preparation_reset"}
                yield {
                    "type": "generation_recovery",
                    "code": "stream_closed",
                    "message": (f"Model connection interrupted; retrying the current response "
                                f"({budget.attempts}/{budget.max_attempts - 1}). "
                                "Completed tools will not be replayed; unfinished output was discarded."),
                }
                await budget.backoff(float(min(8, 2 ** max(0, budget.attempts - 1))))
        finally:
            await _close(client, request_key=request_key, component="client")

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        overrides = {"max_tokens": max(64, int(max_tokens))} if max_tokens is not None else None
        stream = self._stream_request(messages, tools=tools, tool_choice=tool_choice,
                                      generation_overrides=overrides, overall_timeout=self.config.overall_timeout)
        try:
            async for event in stream:
                if event["type"] in {"done", "tool_calls"}:
                    return {**event, "tool_calls": event.get("calls", [])}
        finally:
            await stream.aclose()
        raise LLMResponseError("stream_closed", "Claude returned no completed response.")

    async def chat_limited(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        max_tokens: int,
        temperature: float = 0.1,
        disable_thinking: bool = False,
        reasoning_effort: str | None = None,
        request_timeout: float | None = None,
        max_retries: int | None = None,
    ) -> dict[str, Any]:
        overrides = {"max_tokens": max(64, int(max_tokens)), "temperature": float(temperature)}
        stream = self._stream_request(
            messages, tools=tools, generation_overrides=overrides, disable_thinking=disable_thinking,
            reasoning_effort=reasoning_effort,
            overall_timeout=self.config.overall_timeout if request_timeout is None else request_timeout,
            max_retries=max_retries,
        )
        try:
            async for event in stream:
                if event["type"] in {"done", "tool_calls"}:
                    return {**event, "tool_calls": event.get("calls", [])}
        finally:
            await stream.aclose()
        raise LLMResponseError("stream_closed", "Claude returned no completed response.")

    def estimate_tokens(self, messages: list[dict[str, Any]]) -> int:
        prepared = _messages_for_capabilities(
            [{k: v for k, v in m.items() if k != "_provider_state"} for m in messages],
            self.config.capabilities,
            vision_detail=self.config.vision_detail,
        )
        return max(1, round(estimate_messages_tokens(prepared) * self._estimate_calibration))

    def record_prompt_usage(self, estimated: int | None, actual: int | None) -> None:
        if not estimated or not actual or estimated <= 0 or actual <= 0:
            return
        ratio = max(0.25, min(4.0, self._estimate_calibration * actual / estimated))
        self._estimate_calibration = (self._estimate_calibration * 0.8) + (ratio * 0.2)


def _kind(event: dict[str, Any]) -> str:
    kind = event.get("type")
    return "reasoning" if kind == "reasoning" else "tool" if kind == "_tool_preparation" else "content"
