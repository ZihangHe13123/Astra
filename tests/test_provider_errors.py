import asyncio
import importlib
import importlib.util
import logging
from types import SimpleNamespace
from typing import ClassVar

import pytest

import agent.cli.backend as backend_module
import agent.runtime.llm as llm_module
from agent.runtime.llm import LLMConfig, OpenAICompatibleProvider


class SensitiveProviderError(Exception):
    status_code = 503
    request_id = "req-safe_123"
    body: ClassVar[dict[str, object]] = {
        "error": {
            "message": (
                "prompt=SENSITIVE_SENTINEL "
                "Authorization: Bearer provider-secret \x1b[31m"
            ),
        },
    }


def run(awaitable):
    return asyncio.run(awaitable)


def _provider_errors_module():
    spec = importlib.util.find_spec("agent.runtime.provider_errors")
    assert spec is not None, "provider error safety module is missing"
    return importlib.import_module("agent.runtime.provider_errors")


def _assert_sensitive_text_absent(rendered: str) -> None:
    assert "SENSITIVE_SENTINEL" not in rendered
    assert "provider-secret" not in rendered
    assert "Bearer" not in rendered
    assert "\x1b" not in rendered


def test_provider_error_helper_is_default_deny_and_bounded():
    provider_errors = _provider_errors_module()
    exc = SensitiveProviderError(
        "Authorization: Bearer provider-secret\n\x1b[31mSENSITIVE_SENTINEL"
    )

    rendered = provider_errors.format_provider_error(
        exc,
        component="backend-stream",
        attempt=2,
        timeout=180.0,
    )

    _assert_sensitive_text_absent(rendered)
    assert "SensitiveProviderError" in rendered
    assert "503" in rendered
    assert "req-safe_123" in rendered
    assert "component=backend-stream" in rendered
    assert "attempt=2" in rendered
    assert "timeout=180s" in rendered
    assert len(rendered) <= 320


@pytest.mark.parametrize(
    "request_id",
    ["bad id with spaces", "x" * 200, "\x1b[31m"],
)
def test_unsafe_request_ids_are_omitted(request_id):
    exc = SensitiveProviderError("secret")
    exc.request_id = request_id

    rendered = backend_module._stream_error_message(exc)

    assert request_id not in rendered
    _assert_sensitive_text_absent(rendered)


def test_safe_status_can_come_from_plain_response_scalar():
    provider_errors = _provider_errors_module()

    class ResponseStatusError(Exception):
        response = SimpleNamespace(status_code="418")
        request_id = "trace-correlation_42"

    rendered = provider_errors.format_provider_error(
        ResponseStatusError("SENSITIVE_SENTINEL"),
        component="learning-review",
    )

    assert "status=418" in rendered
    assert "request_id=trace-correlation_42" in rendered
    _assert_sensitive_text_absent(rendered)


def test_malformed_provider_attributes_are_omitted_without_coercion():
    provider_errors = _provider_errors_module()

    class Trap:
        def __int__(self):
            raise AssertionError("unsafe status coercion")

        def __float__(self):
            raise AssertionError("unsafe timeout coercion")

        def __str__(self):
            raise AssertionError("unsafe scalar stringification")

    class MalformedProviderError(Exception):
        status_code = Trap()
        request_id = Trap()

    rendered = provider_errors.format_provider_error(
        MalformedProviderError("SENSITIVE_SENTINEL"),
        component=Trap(),
        attempt=Trap(),
        timeout=Trap(),
    )

    assert rendered == (
        "Provider request failed "
        "[type=MalformedProviderError, component=provider]"
    )


def test_scalar_subclasses_and_hostile_exception_metadata_are_not_executed():
    provider_errors = _provider_errors_module()

    class HostileInt(int):
        def __le__(self, _other):
            raise AssertionError("unsafe integer comparison")

        def __ge__(self, _other):
            raise AssertionError("unsafe integer comparison")

    class HostileFloat(float):
        def __float__(self):
            raise AssertionError("unsafe float coercion")

    class HostileStr(str):
        def __str__(self):
            raise AssertionError("unsafe string coercion")

    class ExplodingMeta(type):
        def __getattribute__(cls, name):
            if name == "__name__":
                raise RuntimeError("SENSITIVE_SENTINEL")
            return super().__getattribute__(name)

    class HostileProviderError(Exception, metaclass=ExplodingMeta):
        status_code = HostileInt(503)
        request_id = HostileStr("trace-safe_123")

    rendered = provider_errors.format_provider_error(
        HostileProviderError("SENSITIVE_SENTINEL"),
        component=HostileStr("backend-stream"),
        attempt=HostileInt(2),
        timeout=HostileFloat(180.0),
    )

    assert rendered == "Provider request failed [type=ProviderError, component=provider]"


def test_numeric_diagnostics_omit_extreme_and_nonfinite_values_without_raising():
    provider_errors = _provider_errors_module()
    exc = SensitiveProviderError("SENSITIVE_SENTINEL")
    expected = (
        "Provider request failed "
        "[type=SensitiveProviderError, component=provider, status=503, "
        "request_id=req-safe_123]"
    )
    invalid_values = (
        10**5000,
        -(10**5000),
        0,
        -1,
        1e308,
        -1e308,
        float("nan"),
        float("inf"),
        float("-inf"),
    )

    for value in invalid_values:
        assert provider_errors.format_provider_error(
            exc,
            component="provider",
            attempt=value,
            timeout=value,
        ) == expected


def test_numeric_diagnostic_boundaries_remain_available():
    provider_errors = _provider_errors_module()

    rendered = provider_errors.format_provider_error(
        SensitiveProviderError("SENSITIVE_SENTINEL"),
        component="provider",
        attempt=999_999,
        timeout=1_000_000_000.0,
    )

    assert "attempt=999999" in rendered
    assert "timeout=1e+09s" in rendered
    assert len(rendered) <= 320


def test_exploding_provider_attributes_and_string_are_default_deny():
    provider_errors = _provider_errors_module()

    class ExplodingProviderError(Exception):
        @property
        def status_code(self):
            raise RuntimeError("SENSITIVE_SENTINEL")

        @property
        def request_id(self):
            raise RuntimeError("Bearer provider-secret")

        @property
        def body(self):
            raise RuntimeError("\x1b[31m")

        def __str__(self):
            raise RuntimeError("SENSITIVE_SENTINEL")

    exc = ExplodingProviderError()
    rendered = provider_errors.format_provider_error(exc, component="backend-stream")

    _assert_sensitive_text_absent(rendered)
    assert provider_errors.is_system_message_order_error(exc) is False


def test_unsafe_exception_type_name_uses_fixed_fallback():
    provider_errors = _provider_errors_module()
    unsafe_type = type("TemporaryProviderError", (Exception,), {})
    unsafe_type.__name__ = "Bearer\x1b[31m" + ("x" * 200)

    rendered = provider_errors.format_provider_error(
        unsafe_type("SENSITIVE_SENTINEL"),
        component="backend-stream",
    )

    _assert_sensitive_text_absent(rendered)
    assert "type=ProviderError" in rendered


def test_exact_system_message_match_never_reflects_exception_text():
    class TemplateError(Exception):
        status_code = 400
        body: ClassVar[dict[str, object]] = {
            "error": {"message": "  System message must be at\n the beginning.  "}
        }

    rendered = backend_module._stream_error_message(
        TemplateError("raw SENSITIVE_SENTINEL Bearer provider-secret \x1b[31m")
    )

    assert rendered == (
        "Provider request rejected (400): "
        "System message must be at the beginning."
    )
    _assert_sensitive_text_absent(rendered)


@pytest.mark.parametrize(
    "message",
    [
        "prefix System message must be at the beginning.",
        "System message must be at the beginning. suffix",
        "System message must be at the beginning",
    ],
)
def test_near_system_message_match_remains_generic(message):
    class TemplateError(Exception):
        status_code = 400
        body: ClassVar[dict[str, object]] = {"error": {"message": message}}

    rendered = backend_module._stream_error_message(
        TemplateError("SENSITIVE_SENTINEL Bearer provider-secret \x1b[31m")
    )

    assert rendered != (
        "Provider request rejected (400): "
        "System message must be at the beginning."
    )
    assert "prefix" not in rendered
    assert "suffix" not in rendered
    _assert_sensitive_text_absent(rendered)


def test_backend_protocol_messages_keep_only_safe_diagnostics():
    exc = SensitiveProviderError(
        "provider echoed SENSITIVE_SENTINEL Bearer provider-secret \x1b[31m"
    )

    rendered = backend_module._stream_error_message(exc)

    _assert_sensitive_text_absent(rendered)
    assert "SensitiveProviderError" in rendered
    assert "status=503" in rendered
    assert "request_id=req-safe_123" in rendered
    assert "component=backend-stream" in rendered
    assert len(rendered) <= 320


@pytest.mark.parametrize("code", ["context_budget_unavailable", "context_budget_exceeded", "appshot_vision_unavailable"])
def test_local_appshot_admission_errors_show_the_reason(code):
    from agent.cli.appshots import AppshotValidationError

    rendered = backend_module._stream_error_message(AppshotValidationError(code))
    assert rendered.startswith("Request blocked")
    assert code in rendered
    assert "Provider request failed" not in rendered


def test_unrecognized_appshot_error_does_not_expose_arbitrary_text():
    from agent.cli.appshots import AppshotValidationError

    rendered = backend_module._stream_error_message(AppshotValidationError("SENSITIVE_SENTINEL Bearer provider-secret \x1b[31m"))
    _assert_sensitive_text_absent(rendered)


def test_backend_stream_terminal_log_omits_exception_value(caplog):
    log_stream_error = getattr(backend_module, "_log_stream_provider_error", None)
    assert callable(log_stream_error), "backend provider log helper is missing"
    exc = SensitiveProviderError(
        "SENSITIVE_SENTINEL Bearer provider-secret \x1b[31m"
    )

    with caplog.at_level(logging.ERROR, logger="agent.cli.backend"):
        log_stream_error(exc)

    _assert_sensitive_text_absent(caplog.text)
    assert "error_type=SensitiveProviderError" in caplog.text
    assert "category=provider_failure" in caplog.text
    assert "status=503" in caplog.text
    assert "request_id=req-safe_123" in caplog.text
    assert "component=backend-stream" in caplog.text


def test_llm_terminal_retry_log_omits_exception_value(monkeypatch, caplog):
    monkeypatch.setattr(
        llm_module,
        "TRANSPORT_REQUEST_ERRORS",
        (SensitiveProviderError,),
    )
    provider = object.__new__(OpenAICompatibleProvider)
    provider.config = LLMConfig(overall_timeout=0, max_retries=0)

    async def fail():
        raise SensitiveProviderError(
            "SENSITIVE_SENTINEL Bearer provider-secret \x1b[31m"
        )

    with caplog.at_level(
        logging.ERROR,
        logger="agent.runtime.llm",
    ), pytest.raises(SensitiveProviderError):
        run(provider._with_retry(fail))

    _assert_sensitive_text_absent(caplog.text)
    assert "error_type=SensitiveProviderError" in caplog.text
    assert "category=provider_failure" in caplog.text
    assert "status=503" in caplog.text
    assert "request_id=req-safe_123" in caplog.text
    assert "component=request" in caplog.text
    assert "attempt=1" in caplog.text


def test_llm_stream_terminal_log_omits_exception_value(monkeypatch, caplog):
    monkeypatch.setattr(
        llm_module,
        "TRANSPORT_REQUEST_ERRORS",
        (SensitiveProviderError,),
    )

    class FailingStream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise SensitiveProviderError(
                "SENSITIVE_SENTINEL Bearer provider-secret \x1b[31m"
            )

    async def create(**_kwargs):
        return FailingStream()

    provider = object.__new__(OpenAICompatibleProvider)
    provider.config = LLMConfig(
        overall_timeout=0,
        max_retries=0,
        connect_timeout=0,
        idle_timeout=0,
    )
    provider._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )

    async def consume():
        return [event async for event in provider.chat_stream([{"role": "user", "content": "go"}])]

    with caplog.at_level(
        logging.ERROR,
        logger="agent.runtime.llm",
    ), pytest.raises(SensitiveProviderError):
        run(consume())

    _assert_sensitive_text_absent(caplog.text)
    assert "error_type=SensitiveProviderError" in caplog.text
    assert "category=provider_failure" in caplog.text
    assert "status=503" in caplog.text
    assert "request_id=req-safe_123" in caplog.text
    assert "component=stream" in caplog.text
    assert "attempt=1" in caplog.text
