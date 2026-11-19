"""Direct Google AI Studio / Gemini v1beta REST transport for Astra's agent loop."""
from __future__ import annotations

import json
import logging
import os
import urllib.parse
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import httpx

from .gemini_wire import (
    BASE_URL,
    StreamAccumulator,
    build_request_body,
    normalize_discovery_models,
    normalize_model_id,
)
from .harness import default_llm_timeout
from .llm import (
    LLMConfig,
    LLMIdleTimeout,
    LLMOverallTimeout,
    LLMResponseError,
    _RequestBudget,
    _RequestCapTimeout,
    _StreamReadTiming,
    _messages_for_capabilities,
)
from .network import active_proxy_for_url
from .provider_errors import transport_error_diagnostics
from .token_estimator import estimate_messages_tokens
from .tool_preparation import with_tool_preparation

logger = logging.getLogger(__name__)

_MAX_DISCOVERY_PAGES = 5
_GENERATION_OVERRIDE_KEYS = frozenset({
    "temperature",
    "max_tokens",
    "top_p",
    "top_k",
    "min_p",
    "presence_penalty",
    "repetition_penalty",
})


async def _close_transport(resource, *, request_key: str, component: str, response=None) -> None:
    try:
        await resource.aclose()
    except Exception as exc:
        logger.warning(
            "gemini %s close failed request=%s diagnostics=%s",
            component,
            request_key,
            json.dumps(transport_error_diagnostics(exc, response=response), sort_keys=True),
        )


class GeminiRequestError(LLMResponseError):
    """Display only Astra-authored diagnostics, never raw upstream payloads or keys."""

    def __init__(self, message: str, *, code: str = "gemini_request_failed", status_code: int | None = None):
        super().__init__(code, message)
        self.message = message
        self.status_code = status_code

    @property
    def public_message(self) -> str:
        return self.message

    @property
    def public_code(self) -> str:
        return self.code


def _extract_error_fields(body: bytes) -> tuple[str, str]:
    """Read bounded status/message strings strictly for boolean classification."""
    if not body:
        return "", ""
    try:
        data = json.loads(body[:8192].decode("utf-8", errors="replace"))
    except ValueError:
        return "", ""
    if isinstance(data, list) and data and isinstance(data[0], dict):
        data = data[0]
    if not isinstance(data, dict):
        return "", ""
    err = data.get("error")
    if not isinstance(err, dict):
        return "", ""
    status = str(err.get("status") or "").strip().upper()
    message = str(err.get("message") or "").strip().lower()
    return status, message


def _is_invalid_api_key(status: int, body: bytes) -> bool:
    if status in {401, 403}:
        return True
    if status != 400:
        return False
    err_status, err_msg = _extract_error_fields(body)
    return (
        "api key" in err_msg
        and ("not valid" in err_msg or "invalid" in err_msg or "expired" in err_msg)
    ) or "api_key_invalid" in err_msg or err_status == "UNAUTHENTICATED"


def _is_thinking_level_rejection(status: int, body: bytes) -> bool:
    if status != 400:
        return False
    _, err_msg = _extract_error_fields(body)
    return (
        "thinking_level" in err_msg
        or "thinkinglevel" in err_msg
        or "thinking_level_xhigh" in err_msg
        or ("thinking" in err_msg and "xhigh" in err_msg)
    )


def _classify_http_error(status: int, body: bytes) -> GeminiRequestError:
    err_status, err_msg = _extract_error_fields(body)
    if _is_invalid_api_key(status, body):
        return GeminiRequestError(
            f"Gemini authentication failed (HTTP {status}). Check your GEMINI_API_KEY or reconnect Google AI Studio with /connect.",
            code="gemini_auth_failed",
            status_code=status,
        )
    if status == 400 and (
        ("input token count" in err_msg and "exceeds" in err_msg)
        or "maximum number of tokens allowed" in err_msg
        or "context length" in err_msg
        or "too many tokens" in err_msg
    ):
        # Match ReActAgent._is_context_overflow_error without reflecting raw text.
        return GeminiRequestError(
            "Gemini request rejected (HTTP 400): input token count exceeds the model's maximum context length.",
            code="context_length_exceeded",
            status_code=status,
        )
    if status == 404 or err_status == "NOT_FOUND":
        return GeminiRequestError(
            f"Gemini model or endpoint not found (HTTP {status}). Refresh models with /model or verify the model ID.",
            code="gemini_not_found",
            status_code=status,
        )
    if status == 429 or err_status == "RESOURCE_EXHAUSTED":
        return GeminiRequestError(
            f"Gemini rate limit or quota exceeded (HTTP {status}). Wait briefly or check your AI Studio quota.",
            code="gemini_rate_limited",
            status_code=status,
        )
    if status == 400:
        if "thought_signature" in err_msg or "thoughtsignature" in err_msg:
            return GeminiRequestError(
                "Gemini rejected the conversation state due to a missing or mismatched thought signature (HTTP 400). Start a new turn or switch models cleanly.",
                code="gemini_thought_signature_rejected",
                status_code=status,
            )
        return GeminiRequestError(
            "Gemini rejected the request (HTTP 400). Check the selected model, thinking level, or tool schema.",
            code="gemini_bad_request",
            status_code=status,
        )
    return GeminiRequestError(
        f"Gemini request failed (HTTP {status}). Retry shortly or check AI Studio service status.",
        code="gemini_request_failed",
        status_code=status,
    )


def _retry_after_seconds(response: httpx.Response, attempt: int) -> float:
    raw = response.headers.get("retry-after", "").strip()
    if raw:
        try:
            parsed = float(raw)
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


async def fetch_models(
    base_url: str = BASE_URL,
    api_key: str = "",
    *,
    timeout: float = 5.0,
    client: httpx.AsyncClient | None = None,
) -> list[dict[str, Any]]:
    """Fetch and normalize chat-capable Gemini models from native GET /v1beta/models."""
    resolved_url = (base_url or BASE_URL).rstrip("/")
    resolved_key = (api_key or "").strip()
    if not resolved_key:
        raise GeminiRequestError(
            "Missing Gemini API key. Connect Google AI Studio with /connect or set GEMINI_API_KEY.",
            code="gemini_auth_failed",
        )
    headers = {"x-goog-api-key": resolved_key}
    owned = client is None
    if client is None:
        proxy = active_proxy_for_url(resolved_url)
        client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
            proxy=proxy,
        )
    try:
        all_models: list[dict[str, Any]] = []
        page_token = ""
        for _ in range(_MAX_DISCOVERY_PAGES):
            params = {"pageSize": "1000"}
            if page_token:
                params["pageToken"] = page_token
            response = await client.get(resolved_url + "/models", headers=headers, params=params)
            if response.status_code != 200:
                if _is_invalid_api_key(response.status_code, response.content):
                    raise GeminiRequestError(
                        "Authentication rejected; check this route's API key",
                        code="auth_failed",
                        status_code=response.status_code,
                    )
                response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
                raise ValueError("Response is not a Gemini model list")
            all_models.extend(item for item in payload["models"] if isinstance(item, dict))
            next_token = payload.get("nextPageToken")
            if not isinstance(next_token, str) or not next_token.strip():
                break
            page_token = next_token.strip()
        return normalize_discovery_models({"models": all_models})
    finally:
        if owned:
            await client.aclose()


class GeminiProvider:
    """Native Google AI Studio / Gemini v1beta streaming provider."""

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
            self.config.overall_timeout = (
                0.0 if hostname in {"localhost", "127.0.0.1", "::1"} else self.config.timeout
            )
            if self.config.stream_overall_timeout is None:
                self.config.stream_overall_timeout = 0.0
        self.transport = transport
        self._estimate_calibration = 1.0
        self.served_model = ""

    def supports_forced_tool_choice(self) -> bool:
        return True

    def _endpoint_url(self, *, stream: bool = True) -> str:
        base = (self.config.base_url or BASE_URL).rstrip("/")
        model_id = normalize_model_id(self.config.model)
        if not model_id or "/" in model_id or any(ord(c) < 32 or ord(c) == 127 for c in model_id):
            raise GeminiRequestError("Invalid Gemini model ID.", code="gemini_bad_request")
        encoded = urllib.parse.quote(model_id, safe="._-:")
        if stream:
            return f"{base}/models/{encoded}:streamGenerateContent?alt=sse"
        return f"{base}/models/{encoded}:generateContent"

    def _api_key(self) -> str:
        if self.config.connection_required:
            raise ValueError("Connect a provider with /connect and select a model before sending a request.")
        key = (self.config.api_key or os.getenv("GEMINI_API_KEY", "")).strip()
        if not key:
            raise GeminiRequestError(
                "Missing Gemini API key. Connect Google AI Studio with /connect or set GEMINI_API_KEY.",
                code="gemini_auth_failed",
            )
        return key

    def _body(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        *,
        omit_tool_choice: bool = False,
        generation_overrides: dict[str, Any] | None = None,
        disable_thinking: bool = False,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        generation: dict[str, Any] = {
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            "top_p": self.config.top_p,
            "top_k": self.config.top_k,
        }
        if generation_overrides:
            unknown = set(generation_overrides) - _GENERATION_OVERRIDE_KEYS
            if unknown:
                raise ValueError("Unsupported generation override(s): " + ", ".join(sorted(unknown)))
            for key in ("temperature", "max_tokens", "top_p", "top_k"):
                if key in generation_overrides:
                    generation[key] = generation_overrides[key]

        prepared = _messages_for_capabilities(
            messages,
            self.config.capabilities,
            frozenset(
                str(tool.get("function", {}).get("name") or "")
                for tool in (tools or [])
                if isinstance(tool, dict)
            ),
            self.config.vision_detail,
        )
        return build_request_body(
            prepared,
            model=self.config.model,
            max_tokens=int(generation["max_tokens"]),
            configured_max_tokens=self.config.max_tokens,
            context_limit=self.config.context_limit,
            capabilities=self.config.capabilities,
            temperature=generation["temperature"],
            top_p=generation["top_p"],
            top_k=generation["top_k"],
            effort=reasoning_effort if reasoning_effort is not None else self.config.reasoning_effort,
            reasoning_levels=self.config.reasoning_levels,
            disable_thinking=disable_thinking,
            include_thoughts=not disable_thinking,
            tools=tools,
            tool_choice=tool_choice,
            omit_tool_choice=omit_tool_choice,
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
            messages,
            tools=tools,
            tool_choice=tool_choice,
            omit_tool_choice=omit_tool_choice,
            generation_overrides=generation_overrides,
            overall_timeout=(
                self.config.overall_timeout
                if self.config.stream_overall_timeout is None
                else self.config.stream_overall_timeout
            ),
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
        omit_tool_choice: bool = False,
        generation_overrides: dict[str, Any] | None = None,
        disable_thinking: bool = False,
        reasoning_effort: str | None = None,
        overall_timeout: float | None = None,
        max_retries: int | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        api_key = self._api_key()
        url = self._endpoint_url(stream=True)
        body = self._body(
            messages,
            tools,
            tool_choice,
            omit_tool_choice=omit_tool_choice,
            generation_overrides=generation_overrides,
            disable_thinking=disable_thinking,
            reasoning_effort=reasoning_effort,
        )
        retries = self.config.max_retries if max_retries is None else max(0, int(max_retries))
        logical_timeout = self.config.overall_timeout if overall_timeout is None else overall_timeout
        budget = _RequestBudget.from_policy(logical_timeout, retries)
        request_key = uuid.uuid4().hex[:12]
        timeout = httpx.Timeout(
            connect=_httpx_timeout_value(self.config.connect_timeout),
            read=None,
            write=_httpx_timeout_value(self.config.timeout),
            pool=_httpx_timeout_value(self.config.connect_timeout),
        )
        proxy = active_proxy_for_url(self.config.base_url) if self.transport is None else None
        client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
            transport=self.transport,
            proxy=proxy,
        )
        downgraded_xhigh = False
        try:
            while True:
                yield {"type": "_tool_preparation_reset"}
                budget.begin_attempt()
                response: httpx.Response | None = None
                visible = False
                timing = _StreamReadTiming()
                try:
                    request = client.build_request(
                        "POST",
                        url,
                        headers={
                            "x-goog-api-key": api_key,
                            "Content-Type": "application/json",
                            "Accept": "text/event-stream",
                        },
                        json=body,
                    )
                    response = await budget.run(
                        lambda request=request: client.send(request, stream=True),
                        cap=self.config.connect_timeout,
                    )
                    assert response is not None
                    if response.status_code != 200:
                        status = response.status_code
                        error_body = await budget.run(response.aread, cap=min(10.0, self.config.connect_timeout))
                        # If a Gemini variant/tier rejects thinkingLevel="xhigh", downgrade
                        # once to "high" within the same request deadline.
                        thinking_cfg = (body.get("generationConfig") or {}).get("thinkingConfig")
                        if (
                            not downgraded_xhigh
                            and isinstance(thinking_cfg, dict)
                            and thinking_cfg.get("thinkingLevel") == "xhigh"
                            and _is_thinking_level_rejection(status, error_body)
                        ):
                            downgraded_xhigh = True
                            body = {
                                **body,
                                "generationConfig": {
                                    **body["generationConfig"],
                                    "thinkingConfig": {**thinking_cfg, "thinkingLevel": "high"},
                                },
                            }
                            logger.warning(
                                "gemini rejected thinkingLevel=xhigh; retrying once with high model=%s request=%s",
                                self.config.model,
                                request_key,
                            )
                            await _close_transport(response, request_key=request_key, component="response", response=response)
                            response = None
                            budget = budget.fork_attempts(retries)
                            continue
                        if status in {429, 500, 502, 503, 504} and budget.can_attempt():
                            delay = _retry_after_seconds(response, budget.attempts)
                            logger.warning(
                                "gemini request retry request=%s attempt=%s/%s status=%s delay=%.2fs",
                                request_key,
                                budget.attempts,
                                budget.max_attempts,
                                status,
                                delay,
                            )
                            await _close_transport(response, request_key=request_key, component="response", response=response)
                            response = None
                            await budget.backoff(delay)
                            continue
                        raise _classify_http_error(status, error_body)

                    lines = response.aiter_lines().__aiter__()
                    accumulator = StreamAccumulator(model=self.config.model)
                    frame: list[str] = []

                    def _flush_frame(acc: StreamAccumulator = accumulator) -> list[dict[str, Any]]:
                        nonlocal frame
                        if not frame:
                            return []
                        encoded, frame = "\n".join(frame), []
                        if encoded == "[DONE]":
                            return []
                        try:
                            payload = json.loads(encoded)
                        except ValueError as exc:
                            raise LLMResponseError("incomplete_response", "Invalid Gemini SSE JSON frame.") from exc
                        if isinstance(payload, dict) and "error" in payload:
                            err_obj = payload["error"]
                            code = int(err_obj.get("code") or 500) if isinstance(err_obj, dict) else 500
                            raise _classify_http_error(code, encoded.encode("utf-8"))
                        events = acc.observe(payload)
                        if acc.served_model:
                            self.served_model = acc.served_model
                        return events

                    while True:
                        try:
                            effective_idle = (
                                min(self.config.idle_timeout, 1.0)
                                if accumulator.finish_reason and self.config.idle_timeout > 0
                                else 1.0 if accumulator.finish_reason else self.config.idle_timeout
                            )
                            line = await timing.read(lines, budget, effective_idle)
                        except StopAsyncIteration:
                            for event in _flush_frame():
                                kind = (
                                    "reasoning" if event["type"] == "reasoning"
                                    else "tool" if event["type"] == "_tool_preparation"
                                    else "content"
                                )
                                timing.observe(kind)
                                visible = True
                                yield event
                            break
                        except LLMOverallTimeout:
                            if accumulator.finish_reason:
                                break
                            raise
                        except _RequestCapTimeout:
                            if accumulator.finish_reason:
                                break
                            raise

                        if line.startswith("data:"):
                            frame.append(line[5:].lstrip())
                            continue
                        if line or not frame:
                            continue
                        events = _flush_frame()
                        if not events:
                            timing.observe("finish" if accumulator.finish_reason else "empty")
                        for event in events:
                            kind = (
                                "reasoning" if event["type"] == "reasoning"
                                else "tool" if event["type"] == "_tool_preparation"
                                else "content"
                            )
                            timing.observe(kind)
                            visible = True
                            yield event

                    result = accumulator.finalize()
                    if budget.attempts > 1:
                        logger.info("gemini stream recovered request=%s attempts=%s", request_key, budget.attempts)
                    yield result
                    return
                except (_RequestCapTimeout, httpx.RequestError, LLMResponseError) as exc:
                    transient = (
                        not isinstance(exc, LLMResponseError)
                        or exc.code == "stream_closed"
                        or (isinstance(exc, GeminiRequestError) and exc.status_code in {429, 500, 502, 503, 504})
                    )
                    retry = transient and budget.can_attempt()
                    diagnostics = json.dumps(transport_error_diagnostics(exc, response=response), sort_keys=True)
                    logger.log(
                        logging.WARNING if retry else logging.ERROR,
                        "gemini stream failed request=%s attempt=%s/%s retrying=%s visible=%s "
                        "read_wait_seconds=%.3f idle_wait_seconds=%.3f last_chunk_kind=%s diagnostics=%s",
                        request_key,
                        budget.attempts,
                        budget.max_attempts,
                        retry,
                        visible,
                        timing.read_wait_seconds,
                        timing.idle_wait_seconds,
                        timing.last_chunk_kind,
                        diagnostics,
                    )
                    if not retry:
                        if isinstance(exc, _RequestCapTimeout):
                            raise LLMIdleTimeout(
                                f"Gemini made no progress before the connection/idle timeout. Diagnostics: {diagnostics}"
                            ) from exc
                        if transient and not isinstance(exc, GeminiRequestError):
                            raise LLMResponseError(
                                "stream_closed",
                                f"Gemini connection failed; partial calls were discarded. Diagnostics: {diagnostics}",
                            ) from exc
                        raise
                finally:
                    if response is not None:
                        await _close_transport(response, request_key=request_key, component="response", response=response)
                yield {"type": "_tool_preparation_reset"}
                yield {
                    "type": "generation_recovery",
                    "code": "stream_closed",
                    "message": (
                        f"Model connection interrupted; retrying the current response "
                        f"({budget.attempts}/{budget.max_attempts - 1}). "
                        "Completed tools will not be replayed; unfinished output was discarded."
                    ),
                }
                await budget.backoff(float(min(8, 2 ** max(0, budget.attempts - 1))))
        finally:
            await _close_transport(client, request_key=request_key, component="client")

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        overrides = {"max_tokens": max(64, int(max_tokens))} if max_tokens is not None else None
        stream = self._stream_request(
            messages,
            tools=tools,
            tool_choice=tool_choice,
            generation_overrides=overrides,
            overall_timeout=self.config.overall_timeout,
        )
        try:
            async for event in stream:
                if event["type"] in {"done", "tool_calls"}:
                    return {**event, "tool_calls": event.get("calls", [])}
        finally:
            await stream.aclose()
        raise LLMResponseError("stream_closed", "Gemini returned no completed response.")

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
        overrides = {
            "max_tokens": max(64, int(max_tokens)),
            "temperature": float(temperature),
        }
        stream = self._stream_request(
            messages,
            tools=tools,
            generation_overrides=overrides,
            disable_thinking=disable_thinking,
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
        raise LLMResponseError("stream_closed", "Gemini returned no completed response.")

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
