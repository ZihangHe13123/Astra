import asyncio
import time
from types import SimpleNamespace

import pytest

import agent.runtime.llm as llm_module
from agent.runtime.llm import (
    LLMClient,
    LLMConfig,
    LLMIdleTimeout,
    LLMOverallTimeout,
    OpenAICompatibleProvider,
)


def run(awaitable):
    return asyncio.run(awaitable)


class FakeTransportError(Exception):
    pass


class SequenceStream:
    def __init__(self, items):
        self.items = iter(items)

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = next(self.items, StopAsyncIteration)
        if item is StopAsyncIteration:
            raise StopAsyncIteration
        if isinstance(item, BaseException):
            raise item
        return item


class IdleBeforeOutputStream:
    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(1)
        raise StopAsyncIteration


class SlowCreation:
    def __init__(self, delay=1):
        self.delay = delay


def content_chunk(text, finish_reason=None):
    return SimpleNamespace(
        choices=[SimpleNamespace(
            finish_reason=finish_reason,
            delta=SimpleNamespace(
                reasoning_content=None,
                content=text,
                tool_calls=None,
            ),
        )],
        usage=None,
    )


def reasoning_chunk(text):
    return SimpleNamespace(
        choices=[SimpleNamespace(
            finish_reason=None,
            delta=SimpleNamespace(
                reasoning_content=text,
                content=None,
                tool_calls=None,
            ),
        )],
        usage=None,
    )


def usage_chunk():
    return SimpleNamespace(
        choices=[],
        usage=SimpleNamespace(
            prompt_tokens=1,
            completion_tokens=0,
            total_tokens=1,
        ),
    )


async def collect(stream):
    return [event async for event in stream]


@pytest.fixture
def provider_read_clock(monkeypatch):
    # Drive the provider's read accounting without relying on sub-100ms OS
    # scheduling. asyncio's clock and the real stall/cancellation tests stay real.
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(llm_module, "time", SimpleNamespace(monotonic=lambda: clock.now))
    return clock


@pytest.mark.parametrize("chunk", [content_chunk(""), usage_chunk()])
def test_empty_heartbeats_do_not_reset_progress_deadline(chunk, provider_read_clock):
    class Heartbeats:
        closed = False
        reads = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            self.reads += 1
            # A broken reset must fail this test, not spin forever without
            # yielding to the real asyncio timer used as the outer guard.
            assert self.reads <= 10, "empty frames kept the idle budget alive"
            provider_read_clock.now += 0.01
            return chunk

        async def aclose(self):
            self.closed = True

    stream = Heartbeats()
    provider, created = provider_with_streams([stream])
    provider.config.idle_timeout = 0.02
    provider.config.max_retries = 0
    with pytest.raises(LLMIdleTimeout):
        run(asyncio.wait_for(collect(provider.chat_stream([])), timeout=1))
    assert provider_read_clock.now == pytest.approx(provider.config.idle_timeout)
    assert len(created) == 1
    assert stream.closed


@pytest.mark.parametrize("kind", ["text", "reasoning", "tool"])
def test_meaningful_progress_allows_stream_longer_than_idle_limit(kind, provider_read_clock):
    chunk = {"text": content_chunk("text"), "reasoning": reasoning_chunk("think"),
             "tool": tool_call_chunk()}[kind]
    class Progress(SequenceStream):
        closed = False

        async def __anext__(self):
            provider_read_clock.now += 0.01
            return await super().__anext__()

        async def aclose(self):
            self.closed = True

    # This fixture exercises idle progress; finish with an actual answer and
    # explicit provider terminal signal rather than relying on silent EOF.
    stream = Progress([chunk] * 12 + [content_chunk("answer", "stop")])
    provider, created = provider_with_streams([stream])
    provider.config.idle_timeout = 0.08
    provider.config.max_retries = 0
    events = run(collect(provider.chat_stream([])))
    assert provider_read_clock.now > provider.config.idle_timeout
    assert events[-1]["type"] == ("tool_calls" if kind == "tool" else "done")
    assert events[-1]["content"] == ("text" * 12 + "answer" if kind == "text" else "answer")
    assert events[-1]["reasoning_content"] == ("think" * 12 if kind == "reasoning" else "")
    if kind == "tool":
        assert events[-1]["calls"][0]["arguments"] == "{" * 12
    assert len(created) == 1
    assert stream.closed


def tool_call_chunk():
    return SimpleNamespace(
        choices=[SimpleNamespace(
            finish_reason=None,
            delta=SimpleNamespace(
                reasoning_content=None,
                content=None,
                tool_calls=[SimpleNamespace(
                    index=0,
                    id="call-1",
                    function=SimpleNamespace(
                        name="execute_shell",
                        arguments="{",
                    ),
                )],
            ),
        )],
        usage=None,
    )


def provider_with_streams(streams):
    provider = object.__new__(OpenAICompatibleProvider)
    provider.config = LLMConfig(
        connect_timeout=1,
        idle_timeout=1,
        overall_timeout=0,
        max_retries=1,
    )
    provider._estimate_calibration = 1.0
    provider._ensure_client_route = lambda: asyncio.sleep(0)
    created = []

    async def create(**_kwargs):
        created.append(True)
        item = streams.pop(0)
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, SlowCreation):
            await asyncio.sleep(item.delay)
            return SequenceStream([])
        return item

    provider._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    return provider, created


def test_portable_transport_tuple_includes_installed_request_error_types():
    for module_name in ("httpx", "httpx2"):
        try:
            module = __import__(module_name)
        except ModuleNotFoundError:
            continue
        assert module.RequestError in llm_module.TRANSPORT_REQUEST_ERRORS


def test_connection_transport_failure_is_classified_and_bounded(monkeypatch):
    monkeypatch.setattr(
        llm_module,
        "TRANSPORT_REQUEST_ERRORS",
        (FakeTransportError,),
        raising=False,
    )
    provider, created = provider_with_streams([
        FakeTransportError("connect reset"),
        FakeTransportError("connect reset"),
    ])

    with pytest.raises(FakeTransportError, match="connect reset"):
        run(collect(provider.chat_stream([
            {"role": "user", "content": "go"}
        ])))

    assert len(created) == 2


def test_stream_retries_transport_read_before_any_emission(monkeypatch):
    monkeypatch.setattr(
        llm_module,
        "TRANSPORT_REQUEST_ERRORS",
        (FakeTransportError,),
        raising=False,
    )
    provider, created = provider_with_streams([
        SequenceStream([FakeTransportError("reset")]),
        SequenceStream([content_chunk("recovered"), content_chunk("", "stop")]),
    ])

    async def scenario():
        return [event async for event in provider.chat_stream([
            {"role": "user", "content": "continue"}
        ])]

    events = run(scenario())
    assert len(created) == 2
    assert [event.get("content") for event in events if event["type"] == "chunk"] == [
        "recovered"
    ]
    assert events[-1]["type"] == "done"


@pytest.mark.parametrize(
    "first_chunk",
    [reasoning_chunk("thinking"), content_chunk("partial"), tool_call_chunk()],
)
def test_stream_never_retries_after_any_observable_delta(
    monkeypatch,
    first_chunk,
):
    monkeypatch.setattr(
        llm_module,
        "TRANSPORT_REQUEST_ERRORS",
        (FakeTransportError,),
        raising=False,
    )
    provider, created = provider_with_streams([
        SequenceStream([first_chunk, FakeTransportError("reset")]),
        SequenceStream([content_chunk("must not run")]),
    ])

    async def scenario():
        async for _event in provider.chat_stream([
            {"role": "user", "content": "continue"}
        ]):
            pass

    with pytest.raises(FakeTransportError, match="reset"):
        run(scenario())
    assert len(created) == 1


def test_usage_only_chunk_does_not_block_safe_retry(monkeypatch):
    monkeypatch.setattr(
        llm_module,
        "TRANSPORT_REQUEST_ERRORS",
        (FakeTransportError,),
        raising=False,
    )
    provider, created = provider_with_streams([
        SequenceStream([usage_chunk(), FakeTransportError("reset")]),
        SequenceStream([content_chunk("ok"), content_chunk("", "stop")]),
    ])

    events = run(collect(provider.chat_stream([
        {"role": "user", "content": "go"}
    ])))

    assert len(created) == 2
    assert [event.get("content") for event in events if event["type"] == "chunk"] == [
        "ok"
    ]


def test_stream_retries_pre_output_idle_timeout_without_emitting_events():
    provider, created = provider_with_streams([
        IdleBeforeOutputStream(),
        IdleBeforeOutputStream(),
    ])
    provider.config.idle_timeout = 0.01
    emitted = []

    async def scenario():
        async for event in provider.chat_stream([
            {"role": "user", "content": "go"}
        ]):
            emitted.append(event)

    with pytest.raises(LLMIdleTimeout, match="produced no meaningful progress"):
        run(scenario())

    assert len(created) == 2
    assert emitted == []


def test_chat_limited_shares_deadline_with_thinking_compatibility_fallback(
    monkeypatch,
):
    class FakeBadRequestError(Exception):
        pass

    monkeypatch.setattr(llm_module, "BadRequestError", FakeBadRequestError)
    provider = object.__new__(OpenAICompatibleProvider)
    provider.config = LLMConfig(
        model="bounded-review",
        overall_timeout=180.0,
        max_retries=3,
    )
    captured = []

    async def fake_completion(kwargs, **policy):
        captured.append((kwargs, policy))
        if len(captured) == 1:
            raise FakeBadRequestError(
                "The value of the enable_thinking parameter is restricted to True."
            )
        return {"content": "{}"}

    provider._chat_completion = fake_completion

    result = run(provider.chat_limited(
        [{"role": "user", "content": "review"}],
        max_tokens=256,
        disable_thinking=True,
        request_timeout=180.0,
        max_retries=0,
    ))

    assert result == {"content": "{}"}
    assert len(captured) == 2
    first_budget = captured[0][1]["budget"]
    fallback_budget = captured[1][1]["budget"]
    assert first_budget is not fallback_budget
    assert first_budget.deadline is not None
    assert fallback_budget.deadline == first_budget.deadline
    assert first_budget.max_attempts == fallback_budget.max_attempts == 1


def test_retry_backoff_cannot_reset_overall_deadline(monkeypatch):
    monkeypatch.setattr(llm_module, "TRANSPORT_REQUEST_ERRORS", (FakeTransportError,))
    provider = object.__new__(OpenAICompatibleProvider)
    provider.config = LLMConfig(overall_timeout=0.05, max_retries=1)
    attempts = 0

    async def fail():
        nonlocal attempts
        attempts += 1
        raise FakeTransportError("provider echoed SENSITIVE_SENTINEL")

    started = time.monotonic()
    with pytest.raises(LLMOverallTimeout):
        run(provider._with_retry(fail))
    elapsed = time.monotonic() - started
    assert attempts == 1
    assert elapsed < 0.25


def test_retry_can_succeed_inside_one_shared_deadline(monkeypatch):
    monkeypatch.setattr(llm_module, "TRANSPORT_REQUEST_ERRORS", (FakeTransportError,))
    monkeypatch.setattr(
        llm_module, "_retry_backoff_seconds", lambda _attempt: 0.001, raising=False
    )
    provider = object.__new__(OpenAICompatibleProvider)
    provider.config = LLMConfig(overall_timeout=0.25, max_retries=1)
    attempts = 0

    async def flaky():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise FakeTransportError("transient")
        return "ok"

    # Returning at all is the proof: past the shared deadline the retry raises instead. Timing the
    # whole run would also count starting the event loop, which no deadline covers.
    assert run(provider._with_retry(flaky)) == "ok"
    assert attempts == 2


def test_provider_timeout_remains_retryable_before_overall_deadline(monkeypatch):
    monkeypatch.setattr(
        llm_module, "_retry_backoff_seconds", lambda _attempt: 0.001
    )
    provider = object.__new__(OpenAICompatibleProvider)
    provider.config = LLMConfig(overall_timeout=0.25, max_retries=1)
    attempts = 0

    async def flaky():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TimeoutError("provider attempt timed out")
        return "ok"

    assert run(provider._with_retry(flaky)) == "ok"
    assert attempts == 2


def test_timeout_disabled_policy_still_enforces_exact_attempt_cap(monkeypatch):
    monkeypatch.setattr(llm_module, "TRANSPORT_REQUEST_ERRORS", (FakeTransportError,))
    monkeypatch.setattr(
        llm_module, "_retry_backoff_seconds", lambda _attempt: 0.0
    )
    provider = object.__new__(OpenAICompatibleProvider)
    provider.config = LLMConfig(overall_timeout=0, max_retries=2)
    attempts = 0

    async def fail():
        nonlocal attempts
        attempts += 1
        raise FakeTransportError("still down")

    with pytest.raises(FakeTransportError, match="still down"):
        run(provider._with_retry(fail))
    assert attempts == 3


def test_expired_budget_does_not_start_factory_between_attempt_and_run(
    monkeypatch,
):
    budget = llm_module._RequestBudget(
        timeout=1.0,
        max_attempts=1,
        started_at=10.0,
    )
    remaining = iter((0.5, 0.0))
    monkeypatch.setattr(budget, "remaining", lambda: next(remaining))
    started = False

    async def factory():
        nonlocal started
        started = True
        return "must not run"

    assert budget.begin_attempt() == 1
    with pytest.raises(LLMOverallTimeout):
        run(budget.run(factory))
    assert started is False


def test_cancellation_interrupts_retry_backoff(monkeypatch):
    monkeypatch.setattr(llm_module, "TRANSPORT_REQUEST_ERRORS", (FakeTransportError,))
    monkeypatch.setattr(
        llm_module, "_retry_backoff_seconds", lambda _attempt: 30.0, raising=False
    )
    provider = object.__new__(OpenAICompatibleProvider)
    provider.config = LLMConfig(overall_timeout=60, max_retries=1)
    attempted = asyncio.Event()
    backoff_started = asyncio.Event()
    real_backoff = llm_module._RequestBudget.backoff

    async def observed_backoff(self, delay):
        backoff_started.set()
        await real_backoff(self, delay)

    monkeypatch.setattr(llm_module._RequestBudget, "backoff", observed_backoff)

    async def fail():
        attempted.set()
        raise FakeTransportError("transient")

    async def scenario():
        task = asyncio.create_task(provider._with_retry(fail))
        await attempted.wait()
        await backoff_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(scenario())


def test_stream_creation_and_read_share_one_attempt_budget(monkeypatch):
    monkeypatch.setattr(llm_module, "TRANSPORT_REQUEST_ERRORS", (FakeTransportError,))
    monkeypatch.setattr(
        llm_module, "_retry_backoff_seconds", lambda _attempt: 0.001
    )
    provider, created = provider_with_streams([
        SequenceStream([FakeTransportError("read reset")]),
        FakeTransportError("reconnect reset"),
        SequenceStream([content_chunk("must not run")]),
    ])

    with pytest.raises(FakeTransportError, match="reconnect reset"):
        run(collect(provider.chat_stream([{"role": "user", "content": "go"}])))
    assert len(created) == 2


def test_stream_retry_backoff_cannot_reset_overall_deadline(monkeypatch):
    monkeypatch.setattr(llm_module, "TRANSPORT_REQUEST_ERRORS", (FakeTransportError,))
    provider, created = provider_with_streams([
        SequenceStream([FakeTransportError("read reset")]),
        SequenceStream([content_chunk("must not run")]),
    ])
    provider.config.overall_timeout = 0.05
    started = time.monotonic()
    with pytest.raises(LLMOverallTimeout):
        run(collect(provider.chat_stream([{"role": "user", "content": "go"}])))
    assert time.monotonic() - started < 0.25
    assert len(created) == 1


def test_overall_deadline_wins_over_longer_idle_timeout():
    provider, created = provider_with_streams([IdleBeforeOutputStream()])
    provider.config.overall_timeout = 0.02
    provider.config.idle_timeout = 1.0
    with pytest.raises(LLMOverallTimeout):
        run(collect(provider.chat_stream([{"role": "user", "content": "go"}])))
    assert len(created) == 1


def test_provider_read_timeout_is_not_misclassified_as_idle_timeout():
    provider, created = provider_with_streams([
        SequenceStream([TimeoutError("provider read timeout")]),
    ])
    provider.config.max_retries = 0

    with pytest.raises(TimeoutError, match="provider read timeout") as caught:
        run(collect(provider.chat_stream([{"role": "user", "content": "go"}])))

    assert type(caught.value) is TimeoutError
    assert len(created) == 1


def test_initial_stream_connect_cap_exhaustion_uses_public_timeout(monkeypatch):
    monkeypatch.setattr(
        llm_module, "_retry_backoff_seconds", lambda _attempt: 0.001
    )
    provider, created = provider_with_streams([
        SlowCreation(),
        SlowCreation(),
    ])
    provider.config.connect_timeout = 0.01

    with pytest.raises(
        TimeoutError,
        match=r"^LLM request attempt timed out after 0\.01s$",
    ) as caught:
        run(collect(provider.chat_stream([{"role": "user", "content": "go"}])))

    assert type(caught.value) is TimeoutError
    assert not isinstance(caught.value, llm_module._RequestCapTimeout)
    assert len(created) == 2


def test_recreated_stream_connect_cap_exhaustion_uses_public_timeout(monkeypatch):
    monkeypatch.setattr(llm_module, "TRANSPORT_REQUEST_ERRORS", (FakeTransportError,))
    monkeypatch.setattr(
        llm_module, "_retry_backoff_seconds", lambda _attempt: 0.001
    )
    provider, created = provider_with_streams([
        SequenceStream([FakeTransportError("read reset")]),
        SlowCreation(),
        SlowCreation(),
    ])
    provider.config.connect_timeout = 0.01
    provider.config.max_retries = 2

    with pytest.raises(
        TimeoutError,
        match=r"^LLM request attempt timed out after 0\.01s$",
    ) as caught:
        run(collect(provider.chat_stream([{"role": "user", "content": "go"}])))

    assert type(caught.value) is TimeoutError
    assert not isinstance(caught.value, llm_module._RequestCapTimeout)
    assert len(created) == 3


def test_stream_cancellation_propagates_during_retry_backoff(monkeypatch):
    monkeypatch.setattr(llm_module, "TRANSPORT_REQUEST_ERRORS", (FakeTransportError,))
    monkeypatch.setattr(
        llm_module, "_retry_backoff_seconds", lambda _attempt: 30.0
    )
    provider, created = provider_with_streams([
        SequenceStream([FakeTransportError("read reset")]),
        SequenceStream([content_chunk("must not run")]),
    ])
    provider.config.overall_timeout = 60
    backoff_started = asyncio.Event()
    real_backoff = llm_module._RequestBudget.backoff

    async def observed_backoff(self, delay):
        backoff_started.set()
        await real_backoff(self, delay)

    monkeypatch.setattr(llm_module._RequestBudget, "backoff", observed_backoff)

    async def scenario():
        task = asyncio.create_task(collect(provider.chat_stream([
            {"role": "user", "content": "go"},
        ])))
        await backoff_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(scenario())
    assert len(created) == 1


def test_llm_client_keeps_request_policy_compatible_with_strict_provider():
    class StrictProvider:
        async def chat_limited(
            self,
            messages,
            tools,
            *,
            max_tokens,
            temperature,
            disable_thinking,
            reasoning_effort,
        ):
            return {"content": messages[0]["content"], "max_tokens": max_tokens}

    client = LLMClient(LLMConfig(), provider=StrictProvider())
    result = run(client.chat_limited(
        [{"role": "user", "content": "review"}],
        max_tokens=256,
        request_timeout=180.0,
        max_retries=1,
    ))

    assert result == {"content": "review", "max_tokens": 256}


def test_completion_kwargs_omits_temperature_for_provider_default():
    # _completion_kwargs only reads self.config; skip the real constructor to
    # avoid building an OpenAI client (which requires credentials).
    provider = object.__new__(OpenAICompatibleProvider)
    provider.config = LLMConfig()

    kwargs = provider._completion_kwargs(messages=[{"role": "user", "content": "hi"}])
    assert "temperature" not in kwargs

    # {"temperature": None} means "provider default": the parameter is omitted
    # so the server applies its own default (e.g. DeepSeek).
    kwargs = provider._completion_kwargs(
        messages=[{"role": "user", "content": "hi"}],
        generation_overrides={"temperature": None},
    )
    assert "temperature" not in kwargs

    kwargs = provider._completion_kwargs(
        messages=[{"role": "user", "content": "hi"}],
        generation_overrides={"temperature": 0.9},
    )
    assert kwargs["temperature"] == 0.9
