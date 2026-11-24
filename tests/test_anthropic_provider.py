"""Native Anthropic Messages API (Claude API key) provider tests."""
from __future__ import annotations

import asyncio
import json
import ssl
from copy import deepcopy

import httpx
import pytest

from agent.cli import connections, mode_preferences, model_catalog, models, provider_connections
from agent.core.msg import ContentBlock, Msg
from agent.runtime import anthropic_provider
from agent.runtime.anthropic_provider import AnthropicProvider, AnthropicRequestError
from agent.runtime.anthropic_wire import (
    BASE_URL,
    FALLBACK_BETA,
    PROVIDER_NAME,
    STATE_KEY,
    THINKING_BINDING_BETA,
    build_request_body,
    normalize_discovery_models,
)
from agent.runtime.llm import LLMClient, LLMConfig, LLMResponseError
from agent.runtime.prompt_budget import prompt_cache_seconds, prompt_token_budget
from agent.runtime.providers import DEFAULT_PROVIDER_REGISTRY
from agent.runtime.react import ReActAgent
from agent.runtime.tools.delegate import _worker_llm
from agent.runtime.tools.registry import ToolDef, ToolRegistry


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRA_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("AGENT_USER_MODELS_FILE", str(tmp_path / "user-models.yaml"))
    monkeypatch.delenv("CLAUDE_API_KEY", raising=False)
    monkeypatch.setattr(anthropic_provider, "_DISABLED_FEATURES", set())


TOOLS = [{"type": "function", "function": {
    "name": "echo_note", "description": "Echo a note",
    "parameters": {"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"]}}}]


def config(**kw) -> LLMConfig:
    defaults = {
        "model": "claude-opus-5-5",
        "base_url": BASE_URL,
        "api_key": "fixture-claude-key",
        "provider": PROVIDER_NAME,
        "context_limit": 1_000_000,
        "max_tokens": 64_000,
        "capabilities": frozenset({"streaming", "tools", "reasoning", "vision"}),
        "reasoning_effort": "high",
        "reasoning_levels": ("low", "medium", "high", "xhigh", "max"),
        "max_retries": 0,
    }
    defaults.update(kw)
    return LLMConfig(**defaults)


def sse(*events: dict) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode("utf-8")


def start(model="claude-opus-5-5", **usage):
    return {"type": "message_start", "message": {"model": model, "usage": {
        "input_tokens": 10, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 1, **usage}}}


def thinking(index, text, sig):
    return [{"type": "content_block_start", "index": index, "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
            {"type": "content_block_delta", "index": index, "delta": {"type": "thinking_delta", "thinking": text}},
            {"type": "content_block_delta", "index": index, "delta": {"type": "signature_delta", "signature": sig}},
            {"type": "content_block_stop", "index": index}]


def text(index, value):
    return [{"type": "content_block_start", "index": index, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": index, "delta": {"type": "text_delta", "text": value}},
            {"type": "content_block_stop", "index": index}]


def tool_use(index, call_id, name, arguments: str):
    return [{"type": "content_block_start", "index": index, "content_block": {"type": "tool_use", "id": call_id, "name": name, "input": {}}},
            {"type": "content_block_delta", "index": index, "delta": {"type": "input_json_delta", "partial_json": arguments}},
            {"type": "content_block_stop", "index": index}]


def finish(reason, output_tokens=20, **extra):
    return [{"type": "message_delta", "delta": {"stop_reason": reason, **extra}, "usage": {"output_tokens": output_tokens}},
            {"type": "message_stop"}]


def collect(provider, messages, tools=None):
    async def work():
        return [event async for event in provider.chat_stream(messages, tools)]
    return asyncio.run(work())


def strip_cache_control(value):
    if isinstance(value, dict):
        return {k: strip_cache_control(v) for k, v in value.items() if k != "cache_control"}
    if isinstance(value, list):
        return [strip_cache_control(v) for v in value]
    return value


def test_registry_and_bundled_claude_presets(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_MODELS_FILE", str(models.DEFAULT_MODELS_PATH))
    monkeypatch.setenv("AGENT_USER_MODELS_FILE", str(tmp_path / "absent.yaml"))
    profiles = models.model_profiles()
    for name in ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-2"):
        profile = profiles[name]
        assert profile.provider == PROVIDER_NAME
        assert profile.base_url == BASE_URL
        assert profile.api_key_env == "CLAUDE_API_KEY"
        assert profile.catalog_provider == "claude-api"
        assert profile.context_limit == 1_000_000
        assert {"tools", "reasoning", "vision", "streaming"} <= profile.capabilities
        assert profile.temperature is None and profile.top_p is None
        assert profile.generation_settings()["reasoning_levels"] == ("low", "medium", "high", "xhigh", "max")
    assert isinstance(LLMClient(config()).provider, AnthropicProvider)
    assert PROVIDER_NAME in DEFAULT_PROVIDER_REGISTRY.names
    assert not AnthropicProvider(config()).supports_forced_tool_choice()


def test_body_sends_effort_thinking_and_betas_but_no_sampling():
    provider = AnthropicProvider(config(temperature=0.7, top_p=0.9, top_k=40, reasoning_effort="max"))
    body, betas, _ = provider._body(
        [{"role": "system", "content": "Be concise."}, {"role": "user", "content": "Hello"}],
        generation_overrides={"temperature": 0.2, "top_p": 0.5, "max_tokens": 512},
    )
    assert body["system"][0]["text"] == "Be concise."
    assert not {"temperature", "top_p", "top_k"} & set(body)
    assert body["thinking"] == {"type": "adaptive", "display": "summarized",
                                "block_binding": {"prefix_mismatch_behavior": "drop_block"}}
    assert body["output_config"] == {"effort": "max"}
    # Thinking shares max_tokens with the answer: a small override keeps the configured room.
    assert body["max_tokens"] == 64_000
    assert body["fallbacks"] == "default"
    assert set(betas) == {THINKING_BINDING_BETA, FALLBACK_BETA}

    low, _, _ = provider._body([{"role": "user", "content": "hi"}], disable_thinking=True)
    assert low["output_config"] == {"effort": "low"}


@pytest.mark.parametrize(("model", "effort", "expected_effort", "fallback"), [
    ("claude-sonnet-4-6", "xhigh", "high", False),
    ("claude-haiku-5-5", "max", "max", False),
    ("claude-sonnet-5-5", "medium", "medium", True),
    ("claude-fable-5-2", "xhigh", "xhigh", True),
    ("claude-haiku-4-5-20251001", "high", None, False),
])
def test_model_rules_for_effort_thinking_and_fallbacks(model, effort, expected_effort, fallback):
    body, betas, _ = build_request_body([{"role": "user", "content": "hi"}], model=model, max_tokens=1000, effort=effort)
    assert body.get("output_config", {}).get("effort") == expected_effort
    assert ("thinking" in body) == (expected_effort is not None)
    assert ("fallbacks" in body) == fallback
    assert (FALLBACK_BETA in betas) == fallback


def test_tools_aliases_schema_fixes_and_tool_choice_none():
    tools = [
        *TOOLS,
        {"type": "function", "function": {"name": "mcp.github/search issues", "description": "d",
                                          "parameters": {"properties": {"q": {"type": "string"}}}}},
        {"type": "function", "function": {"name": "odd", "parameters": {"type": "array"}}},
    ]
    body, _, names = build_request_body([{"role": "user", "content": "hi"}], model="claude-opus-5-5",
                                        max_tokens=1000, tools=tools, tool_choice="none")
    wire = [tool["name"] for tool in body["tools"]]
    assert wire[0] == "echo_note"
    assert all(len(name) <= 64 and name.replace("_", "").replace("-", "").isalnum() for name in wire)
    assert names[wire[1]] == "mcp.github/search issues"
    assert body["tools"][1]["input_schema"] == {"type": "object", "properties": {"q": {"type": "string"}}}
    assert body["tools"][2]["input_schema"] == {"type": "object", "properties": {}}
    assert "not a JSON object schema" in body["tools"][2]["description"]
    assert all(tool["eager_input_streaming"] is True for tool in body["tools"])
    assert body["tool_choice"] == {"type": "none"}
    forced, _, _ = build_request_body([{"role": "user", "content": "hi"}], model="claude-opus-5-5", max_tokens=1000,
                                      tools=TOOLS, tool_choice={"type": "function", "function": {"name": "echo_note"}})
    assert "tool_choice" not in forced


def test_stream_assembles_thinking_text_and_calls_into_replayable_state():
    events = [start(cache_read_input_tokens=900, cache_creation_input_tokens=50),
              *thinking(0, "Plan: echo it.", "SIG_A"), *text(1, "Calling."),
              *tool_use(2, "toolu_1", "echo_note", '{"note": "hi"}'),
              *tool_use(3, "toolu_2", "echo_note", '{"note": "yo"}'), *finish("tool_use")]
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, content=sse(*events))

    provider = AnthropicProvider(config(), transport=httpx.MockTransport(handler))
    out = collect(provider, [{"role": "user", "content": "go"}], TOOLS)
    final = out[-1]
    assert final["type"] == "tool_calls" and final["tool_call_state"] == "complete"
    assert [c["id"] for c in final["calls"]] == ["toolu_1", "toolu_2"]
    assert json.loads(final["calls"][1]["arguments"]) == {"note": "yo"}
    assert final["content"] == "Calling." and final["reasoning_content"] == "Plan: echo it."
    assert final["usage"] == {"prompt_tokens": 960, "completion_tokens": 20, "total_tokens": 980,
                              "prompt_cache_hit_tokens": 900, "prompt_cache_miss_tokens": 60}
    assert [i["type"] for i in final[STATE_KEY]["items"]] == ["thinking", "text", "tool_use", "tool_use"]
    assert any(e["type"] == "tool_preparing" for e in out)
    request = seen[0]
    assert request.headers["x-api-key"] == "fixture-claude-key"
    assert request.headers["anthropic-version"] == "2023-06-01"
    assert THINKING_BINDING_BETA in request.headers["anthropic-beta"]
    assert "fixture-claude-key" not in str(request.url)


def test_react_agent_replays_thinking_verbatim_and_requests_only_append(tmp_path):
    captured = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        if len(captured) == 1:
            return httpx.Response(200, content=sse(
                start(), *thinking(0, "I should echo.", "SIG_REACT"), *text(1, "Let me check."),
                *tool_use(2, "toolu_react", "echo_note", '{"note": "hello"}'), *finish("tool_use")))
        return httpx.Response(200, content=sse(start(), *thinking(0, "Done.", "SIG_2"),
                                               *text(1, "Echo returned: hello"), *finish("end_turn")))

    provider = AnthropicProvider(config(), transport=httpx.MockTransport(handler))
    client = LLMClient(provider.config, provider=provider)
    registry = ToolRegistry()
    registry.register(ToolDef(
        name="echo_note", description="Echo a note",
        parameters={"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"],
                    "additionalProperties": False},
        fn=lambda note: f"echo:{note}", risk="read", idempotent=True,
    ))
    agent = ReActAgent("claude-test", client, registry, system_prompt="Test system", progressive_tools=False)
    session_file = tmp_path / "claude-session.json"
    agent.context.set_session(str(session_file))

    async def run_turn():
        return [ev async for ev in agent.reply_stream(
            Msg(sender="user", role="user", content=[ContentBlock.text("Run echo_note")]))]

    events = asyncio.run(run_turn())
    assert len(captured) == 2
    first, second = captured
    assistant = second["messages"][1]
    assert assistant["role"] == "assistant"
    assert assistant["content"][0] == {"type": "thinking", "thinking": "I should echo.", "signature": "SIG_REACT"}
    assert assistant["content"][1] == {"type": "text", "text": "Let me check."}
    assert assistant["content"][2]["type"] == "tool_use" and assistant["content"][2]["id"] == "toolu_react"
    result = second["messages"][2]["content"][0]
    assert result["type"] == "tool_result" and result["tool_use_id"] == "toolu_react"
    # The second request only appends to the first (cache_control may move, nothing else may).
    assert strip_cache_control(second["system"]) == strip_cache_control(first["system"])
    assert strip_cache_control(second["tools"]) == strip_cache_control(first["tools"])
    assert strip_cache_control(second["messages"][:len(first["messages"])]) == strip_cache_control(first["messages"])
    markers = json.dumps(second).count('"cache_control"')
    assert markers == 3
    assert second["messages"][0]["content"][-1]["cache_control"]["ttl"] == "1h"
    assert second["messages"][-1]["content"][-1]["cache_control"]["ttl"] == "1h"
    assert "Echo returned: hello" in "".join(ev.get("content", "") for ev in events if ev.get("type") == "chunk")

    from agent.runtime.context import AgentContext
    restored = AgentContext()
    restored.set_session(str(session_file))
    restored.load()
    with_calls = [m for m in restored.messages if m.get("role") == "assistant" and m.get("tool_calls")]
    assert with_calls and with_calls[0][STATE_KEY]["provider"] == PROVIDER_NAME
    assert with_calls[0][STATE_KEY]["items"][0]["signature"] == "SIG_REACT"


def test_edited_or_foreign_history_is_sent_without_thinking():
    state = {"provider": PROVIDER_NAME, "signature": "stale", "items": [
        {"type": "thinking", "thinking": "old", "signature": "S"}, {"type": "text", "start": 0, "end": 5}]}
    messages = [{"role": "user", "content": "q"},
                {"role": "assistant", "content": "Hello", STATE_KEY: state},
                {"role": "assistant", "content": "", "tool_calls": [
                    {"id": "call:1/x", "type": "function", "function": {"name": "echo_note", "arguments": '{"note":"a"}'}},
                    {"id": "c2", "type": "function", "function": {"name": "gone_tool", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "call:1/x", "content": "ok"},
                {"role": "tool", "tool_call_id": "c2", "content": "late"}]
    body, _, _ = build_request_body(messages, model="claude-opus-5-5", max_tokens=1000, tools=TOOLS)
    assistant = body["messages"][1]["content"]
    assert assistant[0] == {"type": "text", "text": "Hello"}
    assert assistant[1]["type"] == "tool_use" and assistant[1]["id"] == "call_1_x"
    assert assistant[2]["type"] == "text" and "gone_tool" in assistant[2]["text"]
    user = body["messages"][2]["content"]
    assert user[0]["type"] == "tool_result" and user[0]["tool_use_id"] == "call_1_x"
    assert any(b.get("text") == "[tool result for call c2]" for b in user)
    # A conversation that ends on the assistant asks for the continuation instead of a prefill.
    ended, _, _ = build_request_body(messages[:2], model="claude-opus-5-5", max_tokens=1000)
    assert ended["messages"][-1] == {"role": "user", "content": [
        {"type": "text", "text": "Continue.", "cache_control": {"type": "ephemeral", "ttl": "1h"}}]}


def test_mid_output_fallback_drops_declined_calls_and_thinking():
    events = [start(), *thinking(0, "partial", "SIG_X"), *text(1, "Working "),
              *tool_use(2, "toolu_declined", "echo_note", '{"note": "x"}'),
              {"type": "content_block_start", "index": 3, "content_block": {
                  "type": "fallback", "from": {"model": "claude-opus-5-5"}, "to": {"model": "claude-opus-4-8"}}},
              {"type": "content_block_stop", "index": 3},
              *text(4, "on it."), *finish("end_turn")]
    provider = AnthropicProvider(config(), transport=httpx.MockTransport(lambda r: httpx.Response(200, content=sse(*events))))
    final = collect(provider, [{"role": "user", "content": "go"}], TOOLS)[-1]
    assert final["type"] == "done" and final["content"] == "Working on it."
    items = final[STATE_KEY]["items"]
    assert [i["type"] for i in items] == ["text", "block", "text"]
    assert items[1]["block"]["type"] == "fallback"


@pytest.mark.parametrize(("status", "error", "code"), [
    (401, {"type": "authentication_error", "message": "invalid x-api-key"}, "anthropic_auth_failed"),
    (400, {"type": "invalid_request_error", "message": "Your credit balance is too low to access the Anthropic API."},
     "anthropic_billing"),
    (400, {"type": "invalid_request_error", "message": "prompt is too long: 1200000 tokens > 1000000 maximum"},
     "context_length_exceeded"),
    (404, {"type": "not_found_error", "message": "model: claude-nope"}, "anthropic_not_found"),
])
def test_http_errors_are_classified(status, error, code):
    provider = AnthropicProvider(config(), transport=httpx.MockTransport(
        lambda r: httpx.Response(status, json={"type": "error", "error": error})))
    with pytest.raises(AnthropicRequestError) as caught:
        collect(provider, [{"role": "user", "content": "hi"}])
    assert caught.value.code == code
    assert "fixture-claude-key" not in caught.value.public_message


def test_overloaded_and_rejected_optional_feature_are_retried(monkeypatch):
    async def no_sleep(self, delay):
        return None

    monkeypatch.setattr(anthropic_provider._RequestBudget, "backoff", no_sleep)
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            return httpx.Response(529, json={"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}})
        if len(bodies) == 2:
            return httpx.Response(400, json={"type": "error", "error": {
                "type": "invalid_request_error", "message": "fallbacks: Extra inputs are not permitted"}})
        return httpx.Response(200, content=sse(start(), *text(0, "ok"), *finish("end_turn")))

    provider = AnthropicProvider(config(max_retries=1), transport=httpx.MockTransport(handler))
    final = collect(provider, [{"role": "user", "content": "hi"}])[-1]
    assert final["content"] == "ok"
    assert [("fallbacks" in b) for b in bodies] == [True, True, False]
    assert anthropic_provider._DISABLED_FEATURES == {"fallbacks"}
    assert "block_binding" in bodies[2]["thinking"]


def _tls_alert(reason, *, error_type=ssl.SSLError, wrapped=False):
    error = error_type(ssl.SSL_ERROR_SSL, "private TLS fixture detail")
    error.reason = reason
    if wrapped:
        outer = httpx.ConnectError("private transport fixture detail")
        outer.__cause__ = error
        return outer
    return error


@pytest.fixture
def retry_delays(monkeypatch):
    delays = []

    async def record_delay(self, delay):
        delays.append(delay)

    monkeypatch.setattr(anthropic_provider._RequestBudget, "backoff", record_delay)
    return delays


@pytest.mark.parametrize(("error_type", "reason", "wrapped"), [
    (ssl.SSLError, "SSLV3_ALERT_BAD_RECORD_MAC", False),
    (ssl.SSLError, "DECRYPTION_FAILED_OR_BAD_RECORD_MAC", True),
    (ssl.SSLEOFError, "UNEXPECTED_EOF_WHILE_READING", False),
])
def test_tls_interruption_before_headers_retries_identical_request(error_type, reason, wrapped, retry_delays):
    bodies = []

    def handler(request):
        bodies.append(request.content)
        if len(bodies) == 1:
            raise _tls_alert(reason, error_type=error_type, wrapped=wrapped)
        return httpx.Response(200, content=sse(start(), *text(0, "recovered"), *finish("end_turn")))

    provider = AnthropicProvider(config(max_retries=1), transport=httpx.MockTransport(handler))
    events = collect(provider, [{"role": "user", "content": "test the retry"}])
    assert len(bodies) == 2 and bodies[0] == bodies[1]
    assert retry_delays == [1.0]
    assert [event["type"] for event in events].count("generation_recovery") == 1
    assert events[-1]["type"] == "done" and events[-1]["content"] == "recovered"


@pytest.mark.parametrize(("max_retries", "expected_attempts"), [(0, 1), (3, 2)])
def test_repeated_tls_alert_has_at_most_one_reconnect(max_retries, expected_attempts, retry_delays, caplog):
    requests = []

    def handler(request):
        requests.append(request)
        raise _tls_alert("SSLV3_ALERT_BAD_RECORD_MAC")

    provider = AnthropicProvider(config(max_retries=max_retries), transport=httpx.MockTransport(handler))
    with pytest.raises(AnthropicRequestError) as caught:
        collect(provider, [{"role": "user", "content": "test the retry limit"}])
    assert caught.value.code == "anthropic_tls_failed"
    assert len(requests) == expected_attempts
    assert len(retry_delays) == expected_attempts - 1
    assert "tls_reason=bad_record_mac" in caplog.text
    assert "private TLS fixture detail" not in caplog.text
    assert "private TLS fixture detail" not in caught.value.public_message


@pytest.mark.parametrize(("error_type", "reason", "wrapped", "code"), [
    (ssl.SSLCertVerificationError, "CERTIFICATE_VERIFY_FAILED", False, "anthropic_tls_certificate_failed"),
    (ssl.SSLCertVerificationError, "CERTIFICATE_VERIFY_FAILED", True, "anthropic_tls_certificate_failed"),
    (ssl.SSLError, "WRONG_VERSION_NUMBER", True, "anthropic_tls_failed"),
])
def test_permanent_tls_errors_fail_without_retry(error_type, reason, wrapped, code, retry_delays):
    requests = []

    def handler(request):
        requests.append(request)
        raise _tls_alert(reason, error_type=error_type, wrapped=wrapped)

    provider = AnthropicProvider(config(max_retries=2), transport=httpx.MockTransport(handler))
    with pytest.raises(AnthropicRequestError) as caught:
        collect(provider, [{"role": "user", "content": "test TLS validation"}])
    assert caught.value.code == code
    assert len(requests) == 1 and retry_delays == []


class _InterruptedTLSStream(httpx.AsyncByteStream):
    def __init__(self, content: bytes):
        self.content = content
        self.closed = False

    async def __aiter__(self):
        yield self.content
        raise _tls_alert("SSLV3_ALERT_BAD_RECORD_MAC")

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize("completed", [False, True])
def test_tls_alert_after_stream_output_does_not_resend_request(completed, retry_delays):
    stream = _InterruptedTLSStream(sse(
        start(), *text(0, "answer"), *(finish("end_turn") if completed else []),
    ))
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, stream=stream)

    provider = AnthropicProvider(config(max_retries=2), transport=httpx.MockTransport(handler))
    events = []

    async def run():
        async for event in provider.chat_stream([{"role": "user", "content": "test incomplete streams"}]):
            events.append(event)

    if completed:
        asyncio.run(run())
        assert events[-1]["type"] == "done" and events[-1]["content"] == "answer"
    else:
        with pytest.raises(AnthropicRequestError) as caught:
            asyncio.run(run())
        assert caught.value.code == "anthropic_tls_failed"
        assert any(event["type"] == "chunk" for event in events)
    assert all(event["type"] != "generation_recovery" for event in events)
    assert len(requests) == 1 and retry_delays == [] and stream.closed


@pytest.mark.parametrize(("tail", "code"), [
    (finish("refusal", stop_details={"type": "refusal", "category": "cyber"}), "model_refusal"),
    (finish("max_tokens"), "response_truncated"),
    (finish("model_context_window_exceeded"), "context_length_exceeded"),
    ([{"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 3}}], "stream_closed"),
])
def test_unfinished_or_declined_answers_raise(tail, code):
    provider = AnthropicProvider(config(), transport=httpx.MockTransport(
        lambda r: httpx.Response(200, content=sse(start(), *text(0, "partial"), *tail))))
    with pytest.raises(LLMResponseError) as caught:
        collect(provider, [{"role": "user", "content": "hi"}])
    assert caught.value.code == code


def test_truncated_tool_call_is_incomplete_and_never_parsed():
    events = [start(), {"type": "content_block_start", "index": 0, "content_block": {
        "type": "tool_use", "id": "toolu_cut", "name": "echo_note", "input": {}}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"note": "unfini'}},
        {"type": "content_block_stop", "index": 0}, *finish("max_tokens")]
    provider = AnthropicProvider(config(), transport=httpx.MockTransport(lambda r: httpx.Response(200, content=sse(*events))))
    final = collect(provider, [{"role": "user", "content": "hi"}], TOOLS)[-1]
    assert final["type"] == "tool_calls" and final["tool_call_state"] == "incomplete"
    assert final["finish_reason"] == "length"


@pytest.mark.parametrize(("base_url", "mode", "expected_proxy"), [
    (BASE_URL, "off", "http://127.0.0.1:1055"),
    ("https://api.anthropic.com.example.invalid/v1", "always", "http://127.0.0.1:9000"),
    ("http://api.anthropic.com/v1", "off", None),
])
def test_claude_proxy_is_scoped_for_messages_and_model_discovery(monkeypatch, base_url, mode, expected_proxy):
    monkeypatch.setenv("CLAUDE_API_PROXY_URL", "http://127.0.0.1:1055")
    monkeypatch.setenv("ASTRA_PROXY_MODE", mode)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9000")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    actual_client = httpx.AsyncClient
    routes = []

    def response(request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "claude-opus-5-5"}]})
        return httpx.Response(200, content=sse(start(), *text(0, "ok"), *finish("end_turn")))

    def capture_client(**options):
        routes.append((options["proxy"], options["trust_env"]))
        return actual_client(transport=httpx.MockTransport(response))

    monkeypatch.setattr(httpx, "AsyncClient", capture_client)
    models = asyncio.run(anthropic_provider.fetch_models(base_url, "fixture-key"))
    result = collect(AnthropicProvider(config(base_url=base_url)), [{"role": "user", "content": "test route"}])[-1]
    assert [item["id"] for item in models] == ["claude-opus-5-5"]
    assert result["content"] == "ok"
    assert routes == [(expected_proxy, False), (expected_proxy, False)]


def test_unavailable_claude_proxy_fails_both_requests_at_connect(monkeypatch):
    monkeypatch.setenv("ASTRA_PROXY_MODE", "off")
    actual_client = httpx.AsyncClient
    connects = []

    async def reject_tunnel(reader, writer):
        try:
            request = await reader.readuntil(b"\r\n\r\n")
            connects.append(request.splitlines()[0])
            writer.write(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def work():
        server = await asyncio.start_server(reject_tunnel, "127.0.0.1", 0)
        async with server:
            url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
            monkeypatch.setenv("CLAUDE_API_PROXY_URL", url)

            def loopback_only_client(**options):
                assert options["proxy"] == url
                return actual_client(**options)

            monkeypatch.setattr(httpx, "AsyncClient", loopback_only_client)
            with pytest.raises(httpx.ProxyError):
                await anthropic_provider.fetch_models(BASE_URL, "fixture-key")
            provider = AnthropicProvider(config())
            with pytest.raises(LLMResponseError) as caught:
                async for _ in provider.chat_stream([{"role": "user", "content": "test route"}]):
                    pass
            assert caught.value.code == "stream_closed"

    asyncio.run(work())
    assert connects == [b"CONNECT api.anthropic.com:443 HTTP/1.1"] * 2


def test_fetch_models_paginates_with_header_auth():
    requests = []

    def handler(request):
        requests.append(request)
        if request.headers.get("x-api-key") == "bad":
            return httpx.Response(401, json={"type": "error", "error": {"type": "authentication_error"}})
        if "after_id" not in request.url.params:
            return httpx.Response(200, json={"data": [{"id": "claude-opus-5-5", "max_input_tokens": 1_000_000,
                                                       "max_tokens": 128_000}], "has_more": True,
                                             "last_id": "claude-opus-5-5"})
        return httpx.Response(200, json={"data": [{"id": "claude-haiku-4-5-20251001", "max_input_tokens": 200_000}],
                                         "has_more": False, "last_id": "claude-haiku-4-5-20251001"})

    async def work():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            items = await anthropic_provider.fetch_models(BASE_URL, "good-key", client=client)
            with pytest.raises(AnthropicRequestError) as err:
                await anthropic_provider.fetch_models(BASE_URL, "bad", client=client)
            return items, err.value

    items, error = asyncio.run(work())
    assert [i["id"] for i in items] == ["claude-opus-5-5", "claude-haiku-4-5-20251001"]
    assert items[0]["max_completion_tokens"] == 64_000 and items[0]["reasoning_levels"][-1] == "max"
    assert "reasoning" not in items[1]["supported_parameters"]
    assert error.code == "auth_failed"
    assert requests[1].url.params["after_id"] == "claude-opus-5-5"
    assert "good-key" not in str(requests[0].url)


def test_connect_discovery_and_env_only_setup(monkeypatch):
    seen = []

    async def fake_fetch(base_url=BASE_URL, api_key="", *, timeout=5.0, client=None):
        del timeout, client
        seen.append((base_url, api_key))
        return normalize_discovery_models({"data": [
            {"id": "claude-opus-5-5", "max_input_tokens": 1_000_000, "max_tokens": 128_000},
            {"id": "claude-haiku-4-5-20251001", "max_input_tokens": 200_000, "max_tokens": 64_000},
        ]})

    monkeypatch.setattr(anthropic_provider, "fetch_models", fake_fetch)
    provider_id, catalog = asyncio.run(connections.connect_provider("claude-api", api_key="sk-ant-private-test"))
    assert provider_id == "claude-api"
    assert seen == [(BASE_URL, "sk-ant-private-test")]
    by_id = {entry.model_id: entry for entry in catalog.entries}
    opus = by_id["claude-opus-5-5"].profile
    assert opus.provider == PROVIDER_NAME and opus.context_limit == 1_000_000 and opus.max_tokens == 64_000
    assert {"tools", "reasoning", "vision"} <= opus.capabilities
    haiku = by_id["claude-haiku-4-5-20251001"].profile
    assert "reasoning" not in haiku.capabilities and haiku.reasoning_levels == ()
    assert "sk-ant-private-test" not in json.dumps([e.to_event() for e in catalog.entries])
    restored = model_catalog.configured_model_catalog().resolve_persisted("claude-api::claude-opus-5-5")
    assert restored is not None and restored.profile.api_key() == "sk-ant-private-test"

    for path in provider_connections.connections_path().glob("*.json"):
        path.unlink()
    monkeypatch.setenv("CLAUDE_API_KEY", "sk-ant-env-only")
    monkeypatch.setenv("AGENT_MODELS_FILE", str(models.DEFAULT_MODELS_PATH))
    endpoint = next(e for e in model_catalog.provider_endpoints() if e.id == "claude-api")
    assert endpoint.profile.provider == PROVIDER_NAME
    assert endpoint.profile.api_key() == "sk-ant-env-only"


def test_compaction_line_cache_lifetime_mode_status_and_delegate_guard():
    assert prompt_cache_seconds(PROVIDER_NAME) == 3600
    # The hour-long cache is shared with Claude Code; its 150k line is not: that line answers the
    # CLI's replay of every earlier turn, which the Messages API does not do.
    assert prompt_token_budget(1_000_000, 64_000, model="claude-opus-5-5", provider=PROVIDER_NAME) == 500_000
    status = mode_preferences.reasoning_effort_status(config(reasoning_effort="xhigh"))
    assert "output_config.effort" in status
    assert mode_preferences.applies_reasoning_effort(PROVIDER_NAME, "claude-opus-5-5")
    assert not mode_preferences.applies_reasoning_effort(PROVIDER_NAME, "claude-haiku-4-5-20251001")
    with pytest.raises(ValueError, match="incompatible with the DeepSeek model override"):
        _worker_llm(LLMClient(config()), {}, model="deepseek-flash")


def test_unconfigured_provider_never_dispatches():
    calls = []
    provider = AnthropicProvider(config(api_key="", connection_required=True),
                                 transport=httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(500)))
    with pytest.raises(ValueError, match="Connect a provider"):
        collect(provider, [{"role": "user", "content": "hi"}])
    missing = AnthropicProvider(config(api_key=""), transport=httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(500)))
    with pytest.raises(AnthropicRequestError) as caught:
        collect(missing, [{"role": "user", "content": "hi"}])
    assert caught.value.code == "anthropic_auth_failed"
    assert calls == []


def test_images_and_tool_result_errors():
    messages = [{"role": "user", "content": [
        {"type": "text", "text": "look"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        {"type": "image_url", "image_url": {"url": "https://example.com/a.jpg"}},
        {"type": "image_url", "image_url": {"url": "data:image/tiff;base64,AAAA"}}]},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "toolu_e", "type": "function", "function": {"name": "echo_note", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "toolu_e", "content": "[Tool result: echo_note | status: error]\nboom"}]
    body, _, _ = build_request_body(deepcopy(messages), model="claude-opus-5-5", max_tokens=1000, tools=TOOLS)
    first = body["messages"][0]["content"]
    assert first[1]["source"] == {"type": "base64", "media_type": "image/png", "data": "AAAA"}
    assert first[2]["source"] == {"type": "url", "url": "https://example.com/a.jpg"}
    assert first[3]["type"] == "text" and "image omitted" in first[3]["text"]
    assert body["messages"][2]["content"][0]["is_error"] is True
