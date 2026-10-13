"""Default-deny formatting for provider-controlled failures."""

from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_SAFE_STATUS_TEXT = re.compile(r"^[0-9]{3}$")
_SYSTEM_ORDER_MESSAGE = "System message must be at the beginning."
_MAX_ATTEMPT = 999_999
_MAX_TIMEOUT_SECONDS = 1_000_000_000


@dataclass(frozen=True)
class SafeProviderError:
    """Bounded provider diagnostics that are safe to emit."""

    error_type: str
    category: str
    status_code: int | None = None
    request_id: str | None = None


def _safe_getattr(value: object, name: str) -> object | None:
    try:
        return getattr(value, name, None)
    except Exception:  # noqa: BLE001 - provider-owned descriptors are untrusted
        return None


def _status_scalar(value: object) -> int | None:
    if type(value) is int:
        status = value
    elif type(value) is str and _SAFE_STATUS_TEXT.fullmatch(value):
        status = int(value)
    else:
        return None
    return status if 100 <= status <= 599 else None


def _safe_status(exc: BaseException) -> int | None:
    status = _status_scalar(_safe_getattr(exc, "status_code"))
    if status is not None:
        return status
    response = _safe_getattr(exc, "response")
    if response is None:
        return None
    return _status_scalar(_safe_getattr(response, "status_code"))


def _safe_request_id(exc: BaseException) -> str | None:
    value = _safe_getattr(exc, "request_id")
    if type(value) is not str:
        return None
    return value if _SAFE_IDENTIFIER.fullmatch(value) else None


def _safe_error_type(exc: BaseException) -> str:
    value = _safe_getattr(type(exc), "__name__")
    if type(value) is str and _SAFE_IDENTIFIER.fullmatch(value):
        return value
    return "ProviderError"


def summarize_provider_error(exc: BaseException) -> SafeProviderError:
    """Classify an exception without reflecting any provider text."""

    error_type = _safe_error_type(exc)
    status = _safe_status(exc)
    lowered = error_type.lower()
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)) or "timeout" in lowered:
        category = "timeout"
    elif status == 429 or "ratelimit" in lowered:
        category = "rate_limit"
    elif status is not None and 400 <= status < 500:
        category = "rejected"
    elif any(marker in lowered for marker in ("connection", "transport", "readerror")):
        category = "transport"
    else:
        category = "provider_failure"
    return SafeProviderError(
        error_type=error_type,
        category=category,
        status_code=status,
        request_id=_safe_request_id(exc),
    )


def transport_error_diagnostics(exc: BaseException, *, response=None) -> dict:
    """Keep the transport cause chain and OS codes without logging payloads.

    Exception strings can contain credentials, URLs or provider text. Only
    recognize fixed transport reasons; never copy arbitrary exception text.
    errno works on POSIX and winerror preserves the native Windows socket code.
    """
    chain, seen = [], set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen and len(chain) < 6:
        seen.add(id(current))
        item: dict[str, object] = {"type": _safe_error_type(current)}
        code = _safe_getattr(current, "code")
        if type(code) is str and _SAFE_IDENTIFIER.fullmatch(code):
            item["code"] = code
        for field in ("errno", "winerror"):
            value = _safe_getattr(current, field)
            if type(value) is int and -(2**31) <= value < 2**31:
                item[field] = value
        try:
            text = str(current).lower()[:2048]
        except Exception:
            text = ""
        for marker, reason in (
            ("stream ended before response.completed", "eof_before_completion"),
            ("did not provide a completed response", "done_without_completion"),
            ("incomplete chunked read", "incomplete_chunked_read"),
            ("peer closed connection", "peer_closed_connection"),
            ("server disconnected", "server_disconnected"),
            ("connection reset", "connection_reset"),
            ("connection refused", "connection_refused"),
            ("certificate verify failed", "certificate_verify_failed"),
            ("timed out", "timed_out"),
        ):
            if marker in text:
                item["reason"] = reason
                break
        chain.append(item)
        cause = _safe_getattr(current, "__cause__")
        if cause is None and not _safe_getattr(current, "__suppress_context__"):
            cause = _safe_getattr(current, "__context__")
        current = cause if isinstance(cause, BaseException) else None
    summary = summarize_provider_error(exc)
    details = {"causes": chain, "status": summary.status_code, "request_id": summary.request_id}
    if response is not None:
        details["status"] = _status_scalar(_safe_getattr(response, "status_code"))
        headers = _safe_getattr(response, "headers")
        for name in ("x-request-id", "x-openai-request-id"):
            try:
                value = headers.get(name) if isinstance(headers, Mapping) else None
            except Exception:
                value = None
            if type(value) is str and _SAFE_IDENTIFIER.fullmatch(value):
                details["request_id"] = value
                break
    return details


def _safe_attempt(value: object) -> int | None:
    if type(value) is not int or not 1 <= value <= _MAX_ATTEMPT:
        return None
    return value


def _safe_timeout(value: object) -> float | None:
    if type(value) is int:
        if not 0 < value <= _MAX_TIMEOUT_SECONDS:
            return None
        return float(value)
    if type(value) is not float:
        return None
    timeout = float(cast(int | float, value))
    return (
        timeout
        if 0 < timeout <= _MAX_TIMEOUT_SECONDS and math.isfinite(timeout)
        else None
    )


def format_provider_error(
    exc: BaseException,
    *,
    component: str,
    attempt: int | None = None,
    timeout: float | None = None,
) -> str:
    """Return a bounded message containing only allowlisted diagnostics."""

    summary = summarize_provider_error(exc)
    phrases = {
        "timeout": "Provider request timed out",
        "rate_limit": "Provider rate limit reached",
        "rejected": "Provider request rejected",
        "transport": "Provider transport failed",
        "provider_failure": "Provider request failed",
    }
    safe_component = (
        component
        if type(component) is str and _SAFE_IDENTIFIER.fullmatch(component)
        else "provider"
    )
    fields = [f"type={summary.error_type}", f"component={safe_component}"]
    if summary.status_code is not None:
        fields.append(f"status={summary.status_code}")
    safe_attempt = _safe_attempt(attempt)
    if safe_attempt is not None:
        fields.append(f"attempt={safe_attempt}")
    if summary.request_id is not None:
        fields.append(f"request_id={summary.request_id}")
    safe_timeout = _safe_timeout(timeout)
    if safe_timeout is not None:
        fields.append(f"timeout={safe_timeout:g}s")
    return f"{phrases[summary.category]} [{', '.join(fields)}]"[:320]


def _normalized_provider_message(exc: BaseException) -> str:
    """Inspect provider text only for one exact, boolean compatibility check."""

    body = _safe_getattr(exc, "body")
    raw: object | None = None
    if type(body) is dict:
        nested = body.get("error")
        if type(nested) is dict:
            raw = nested.get("message")
        if raw is None:
            raw = body.get("message")
    if type(raw) is not str:
        try:
            raw = str(exc)
        except Exception:  # noqa: BLE001 - provider exception text may be malformed
            return ""
    if len(raw) > 256:
        return ""
    return " ".join(raw.split())


def is_system_message_order_error(exc: BaseException) -> bool:
    """Return whether the provider supplied the one exact known message."""

    return _normalized_provider_message(exc) == _SYSTEM_ORDER_MESSAGE
