"""Native Google AI Studio / Gemini v1beta provider tests."""
from __future__ import annotations

import asyncio
import json
from copy import deepcopy

import httpx
import pytest

from agent.cli import connections, mode_preferences, model_catalog, models, provider_connections
from agent.core.msg import ContentBlock, Msg
from agent.runtime import gemini_provider
from agent.runtime.bar_mode import BarModeController
from agent.runtime.gemini_provider import GeminiProvider, GeminiRequestError
from agent.runtime.gemini_wire import (
    BASE_URL,
    PROVIDER_NAME,
    SKIP_THOUGHT_SIGNATURE,
    STATE_KEY,
    build_contents,
    build_request_body,
    normalize_discovery_models,
)
from agent.runtime.llm import LLMClient, LLMConfig, LLMResponseError
from agent.runtime.providers import DEFAULT_PROVIDER_REGISTRY
from agent.runtime.react import ReActAgent
from agent.runtime.tools.delegate import _worker_llm
from agent.runtime.tools.registry import ToolDef, ToolRegistry


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRA_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("AGENT_USER_MODELS_FILE", str(tmp_path / "user-models.yaml"))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_BASE_URL", raising=False)


def config(**kw) -> LLMConfig:
    defaults = {
        "model": "gemini-4-neon",
        "base_url": BASE_URL,
        "api_key": "fixture-gemini-key",
        "provider": PROVIDER_NAME,
        "context_limit": 1_048_576,
        "max_tokens": 65_536,
        "capabilities": frozenset({"streaming", "tools", "reasoning", "thinking-required", "vision"}),
        "reasoning_effort": "high",
        "reasoning_levels": ("low", "medium", "high", "xhigh"),
        "max_retries": 0,
    }
    defaults.update(kw)
    return LLMConfig(**defaults)


def sse(*frames: dict) -> bytes:
    return "".join("data: " + json.dumps(frame) + "\n\n" for frame in frames).encode("utf-8")


def test_registry_and_bundled_gemini_4_presets(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_MODELS_FILE", str(models.DEFAULT_MODELS_PATH))
    monkeypatch.setenv("AGENT_USER_MODELS_FILE", str(tmp_path / "absent.yaml"))
    profiles = models.model_profiles()
    expected_outputs = {
        "gemini-4-neon": 65_536,
        "gemini-4-hydrogen": 65_536,
        "gemini-4-carbon": 32_768,
        "gemini-4-argon": 32_768,
    }
    for name, expected_out in expected_outputs.items():
        profile = profiles[name]
        assert profile.provider == PROVIDER_NAME
        assert profile.base_url == BASE_URL
        assert profile.api_key_env == "GEMINI_API_KEY"
        assert profile.catalog_provider == "gemini"
        assert profile.context_limit == 1_048_576
        assert profile.max_tokens == expected_out
        assert {"tools", "reasoning", "thinking-required", "vision", "streaming"} <= profile.capabilities
        assert profile.reasoning_levels == ("low", "medium", "high", "xhigh")
        assert profile.temperature is None
        assert profile.top_p is None
        assert profile.top_k is None
        assert profile.generation_settings()["reasoning_levels"] == ("low", "medium", "high", "xhigh")

    client = LLMClient(config())
    assert isinstance(client.provider, GeminiProvider)
    assert PROVIDER_NAME in DEFAULT_PROVIDER_REGISTRY.names


def test_zero_llm_timeout_maps_to_unlimited_httpx_write_timeout(monkeypatch):
    monkeypatch.setenv("LLM_TIMEOUT", "0")
    provider = GeminiProvider(config())
    assert provider.config.timeout == 0.0
    assert gemini_provider._httpx_timeout_value(provider.config.timeout) is None
    assert gemini_provider._httpx_timeout_value(30.0) == 30.0


def test_gemini_4_omits_sampling_params_and_maps_thinking_levels():
    bar_overrides = BarModeController.generation_overrides()
    provider = GeminiProvider(config(temperature=0.7, top_p=0.9, top_k=40, reasoning_effort="max"))
    body = provider._body(
        [{"role": "system", "content": "Be concise."}, {"role": "user", "content": "Hello"}],
        generation_overrides=bar_overrides,
    )
    gen = body["generationConfig"]
    assert "temperature" not in gen
    assert "topP" not in gen
    assert "topK" not in gen
    assert gen["maxOutputTokens"] == 65_536
    assert gen["thinkingConfig"] == {"includeThoughts": True, "thinkingLevel": "xhigh"}
    assert body["systemInstruction"] == {"parts": [{"text": "Be concise."}]}

    # Non-Gemini-4 models retain explicit sampling parameters and use thinkingBudget on 2.5.
    legacy = GeminiProvider(config(
        model="gemini-2.5-flash",
        temperature=0.4,
        top_p=0.9,
        top_k=32,
        capabilities=frozenset({"streaming", "tools", "reasoning", "vision"}),
        reasoning_levels=("low", "medium", "high"),
        reasoning_effort="low",
    ))
    legacy_body = legacy._body([{"role": "user", "content": "Hi"}])
    assert legacy_body["generationConfig"]["temperature"] == 0.4
    assert legacy_body["generationConfig"]["topP"] == 0.9
    assert legacy_body["generationConfig"]["topK"] == 32
    assert legacy_body["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 1024, "includeThoughts": True}

    disabled_25 = legacy._body(
        [{"role": "user", "content": "Summarize"}],
        generation_overrides={"max_tokens": 256},
        disable_thinking=True,
    )
    assert disabled_25["generationConfig"]["maxOutputTokens"] == 256
    assert disabled_25["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 0, "includeThoughts": False}

    # Gemini 4 chat_limited with disable_thinking uses low thinking, no chat_template_kwargs, and safe headroom.
    disabled_g4 = provider._body(
        [{"role": "user", "content": "Summarize"}],
        generation_overrides={"max_tokens": 256, "temperature": 0.1},
        disable_thinking=True,
    )
    assert "chat_template_kwargs" not in json.dumps(disabled_g4)
    assert "temperature" not in disabled_g4["generationConfig"]
    assert disabled_g4["generationConfig"]["maxOutputTokens"] == 65_536
    assert disabled_g4["generationConfig"]["thinkingConfig"] == {"includeThoughts": False, "thinkingLevel": "low"}


def test_tools_use_parameters_json_schema_and_tool_config():
    tools = [
        {
            "type": "function",
            "function": {
                "name": "call_tool",
                "description": "Dispatch loaded tool",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "arguments": {"type": "object"},
                    },
                    "required": ["name"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a file",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    },
                    "additionalProperties": False,
                },
            },
        },
    ]
    original = deepcopy(tools)
    body = build_request_body(
        [{"role": "user", "content": "Read"}],
        model="gemini-4-neon",
        max_tokens=65_536,
        tools=tools,
        tool_choice={"type": "function", "function": {"name": "read_file"}},
    )
    assert tools == original
    decls = body["tools"][0]["functionDeclarations"]
    assert decls[0]["name"] == "call_tool"
    assert decls[0]["parametersJsonSchema"] == tools[0]["function"]["parameters"]
    assert "parameters" not in decls[0]
    assert decls[1]["parametersJsonSchema"] == tools[1]["function"]["parameters"]
    assert body["toolConfig"] == {
        "functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": ["read_file"]}
    }


def test_stream_thought_summary_parallel_calls_and_signature_roundtrip():
    seen_requests: list[tuple[httpx.Request, dict]] = []
    turn = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen_requests.append((request, payload))
        turn["count"] += 1
        if turn["count"] == 1:
            return httpx.Response(200, content=sse(
                {
                    "modelVersion": "gemini-4-neon-20261101",
                    "candidates": [{"content": {"role": "model", "parts": [
                        {"text": "Need to read both files.", "thought": True},
                    ]}}],
                },
                {
                    "candidates": [{
                        "content": {"role": "model", "parts": [
                            {
                                "functionCall": {"id": "fc_1", "name": "read_file", "args": {"path": "a.py"}},
                                "thoughtSignature": "ENC_SIG_PARALLEL_1",
                            },
                            {
                                "functionCall": {"id": "fc_2", "name": "stat_file", "args": {"path": "b.py"}},
                            },
                        ]},
                        "finishReason": "STOP",
                    }],
                    "usageMetadata": {
                        "promptTokenCount": 120,
                        "candidatesTokenCount": 30,
                        "thoughtsTokenCount": 90,
                        "cachedContentTokenCount": 80,
                        "totalTokenCount": 240,
                    },
                },
            ))
        return httpx.Response(200, content=sse(
            {"candidates": [{"content": {"role": "model", "parts": [
                {"text": "Both checked."},
                {"text": "", "thoughtSignature": "ENC_SIG_FINAL"},
            ]}, "finishReason": "STOP"}]},
            {"usageMetadata": {"promptTokenCount": 200, "candidatesTokenCount": 10, "totalTokenCount": 210}},
        ))

    provider = GeminiProvider(config(), transport=httpx.MockTransport(handler))

    async def work():
        first_events = [e async for e in provider.chat_stream([
            {"role": "system", "content": "System rules"},
            {"role": "user", "content": [
                {"type": "text", "text": "Inspect screenshot and files"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}},
            ]},
        ])]
        terminal = first_events[-1]
        assistant_msg = {
            "role": "assistant",
            "content": terminal["content"],
            "tool_calls": [
                {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["arguments"]}}
                for c in terminal["calls"]
            ],
            STATE_KEY: terminal[STATE_KEY],
        }
        second_events = [e async for e in provider.chat_stream([
            {"role": "system", "content": "System rules"},
            {"role": "user", "content": "Inspect screenshot and files"},
            assistant_msg,
            {"role": "tool", "tool_call_id": "fc_1", "content": "contents of a.py"},
            {"role": "tool", "tool_call_id": "fc_2", "content": "size=42"},
            # Astra attaches tool screenshots as a follow-up user turn; it must merge cleanly.
            {"role": "user", "content": [
                {"type": "text", "text": "Attached screenshot"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            ]},
        ])]
        return first_events, second_events

    first_events, second_events = asyncio.run(work())
    req1, body1 = seen_requests[0]
    assert req1.headers["x-goog-api-key"] == "fixture-gemini-key"
    assert "fixture-gemini-key" not in str(req1.url)
    assert str(req1.url) == f"{BASE_URL}/models/gemini-4-neon:streamGenerateContent?alt=sse"
    assert body1["contents"][0]["parts"][1] == {"inlineData": {"mimeType": "image/png", "data": "iVBORw0KGgo="}}
    assert provider.served_model == "gemini-4-neon-20261101"

    assert "".join(e["content"] for e in first_events if e["type"] == "reasoning") == "Need to read both files."
    term1 = first_events[-1]
    assert term1["type"] == "tool_calls"
    assert [c["id"] for c in term1["calls"]] == ["fc_1", "fc_2"]
    assert term1["usage"] == {
        "prompt_tokens": 120,
        "completion_tokens": 120,
        "total_tokens": 240,
        "prompt_cache_hit_tokens": 80,
        "prompt_cache_miss_tokens": 40,
    }
    assert "Need to read both files." not in json.dumps(term1[STATE_KEY])

    _, body2 = seen_requests[1]
    # Strict alternating turns: user -> model (with exact thoughtSignature) -> user (batched functionResponses + image).
    assert [turn["role"] for turn in body2["contents"]] == ["user", "model", "user"]
    model_parts = body2["contents"][1]["parts"]
    assert model_parts[0] == {
        "functionCall": {"id": "fc_1", "name": "read_file", "args": {"path": "a.py"}},
        "thoughtSignature": "ENC_SIG_PARALLEL_1",
    }
    assert model_parts[1] == {
        "functionCall": {"id": "fc_2", "name": "stat_file", "args": {"path": "b.py"}},
    }
    user_turn_parts = body2["contents"][2]["parts"]
    assert user_turn_parts[0] == {
        "functionResponse": {"id": "fc_1", "name": "read_file", "response": {"output": "contents of a.py"}}
    }
    assert user_turn_parts[1] == {
        "functionResponse": {"id": "fc_2", "name": "stat_file", "response": {"output": "size=42"}}
    }
    assert user_turn_parts[2] == {"text": "Attached screenshot"}
    assert user_turn_parts[3] == {"inlineData": {"mimeType": "image/png", "data": "AAAA"}}

    term2 = second_events[-1]
    assert term2["type"] == "done"
    assert term2["content"] == "Both checked."
    assert any(item.get("thoughtSignature") == "ENC_SIG_FINAL" for item in term2[STATE_KEY]["items"])


def test_cross_provider_history_uses_skip_thought_signature_sentinel():
    _, contents = build_contents(
        [
            {"role": "user", "content": "Check repo"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "call_openai_1", "type": "function", "function": {"name": "stat_file", "arguments": '{"path": "a"}'}},
                    {"id": "call_openai_2", "type": "function", "function": {"name": "stat_file", "arguments": '{"path": "b"}'}},
                ],
            },
            {"role": "tool", "tool_call_id": "call_openai_1", "content": "ok-a"},
            {"role": "tool", "tool_call_id": "call_openai_2", "content": "ok-b"},
        ],
        model="gemini-4-hydrogen",
    )
    assert [t["role"] for t in contents] == ["user", "model", "user"]
    assert contents[1]["parts"][0]["thoughtSignature"] == SKIP_THOUGHT_SIGNATURE
    assert "thoughtSignature" not in contents[1]["parts"][1]
    assert contents[2]["parts"][0]["functionResponse"]["id"] == "call_openai_1"
    assert contents[2]["parts"][1]["functionResponse"]["id"] == "call_openai_2"


def test_xhigh_rejection_retries_once_with_high():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen.append(payload["generationConfig"]["thinkingConfig"]["thinkingLevel"])
        if len(seen) == 1:
            return httpx.Response(400, json={
                "error": {"code": 400, "status": "INVALID_ARGUMENT", "message": "THINKING_LEVEL_XHIGH is not supported"}
            })
        return httpx.Response(200, content=sse(
            {"candidates": [{"content": {"role": "model", "parts": [{"text": "Recovered"}]}, "finishReason": "STOP"}]}
        ))

    provider = GeminiProvider(config(reasoning_effort="xhigh", max_retries=0), transport=httpx.MockTransport(handler))
    result = asyncio.run(provider.chat([{"role": "user", "content": "Hi"}]))
    assert seen == ["xhigh", "high"]
    assert result["content"] == "Recovered"


def test_http_errors_classify_overflow_and_redact_upstream_text():
    def overflow_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={
            "error": {
                "code": 400,
                "status": "INVALID_ARGUMENT",
                "message": "The input token count (1200000) exceeds the maximum number of tokens allowed (1048576). SECRET_PROMPT_SNIPPET",
            }
        })

    provider = GeminiProvider(config(), transport=httpx.MockTransport(overflow_handler))
    with pytest.raises(GeminiRequestError) as exc_info:
        asyncio.run(provider.chat([{"role": "user", "content": "Huge"}]))
    assert ReActAgent._is_context_overflow_error(exc_info.value)
    assert "SECRET_PROMPT_SNIPPET" not in str(exc_info.value)
    assert "fixture-gemini-key" not in str(exc_info.value)

    def bad_key_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={
            "error": {"code": 400, "status": "INVALID_ARGUMENT", "message": "API key not valid. Please pass a valid API key."}
        })

    provider_bad = GeminiProvider(config(), transport=httpx.MockTransport(bad_key_handler))
    with pytest.raises(GeminiRequestError) as auth_exc:
        asyncio.run(provider_bad.chat([{"role": "user", "content": "Hi"}]))
    assert auth_exc.value.code == "gemini_auth_failed"


def test_unconfigured_gemini_provider_never_dispatches_requests():
    called = []
    provider = GeminiProvider(
        config(api_key="", connection_required=True),
        transport=httpx.MockTransport(lambda r: (called.append(r), httpx.Response(200))[1]),
    )
    with pytest.raises(ValueError, match="/connect"):
        asyncio.run(provider.chat([{"role": "user", "content": "private"}]))
    assert not called


@pytest.mark.parametrize("finish_reason,expected_code", [
    ("MAX_TOKENS", "response_truncated"),
    ("SAFETY", "incomplete_response"),
    ("MALFORMED_FUNCTION_CALL", "incomplete_response"),
])
def test_non_stop_finish_reasons_raise_structured_errors(finish_reason, expected_code):
    provider = GeminiProvider(
        config(),
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, content=sse(
            {"candidates": [{"content": {"role": "model", "parts": [{"text": "partial"}]}, "finishReason": finish_reason}]}
        ))),
    )
    with pytest.raises(LLMResponseError) as exc_info:
        asyncio.run(provider.chat([{"role": "user", "content": "Hi"}]))
    assert exc_info.value.code == expected_code


def test_discovery_connect_and_env_only_setup(monkeypatch):
    seen = []

    async def fake_fetch(base_url=BASE_URL, api_key="", *, timeout=5.0, client=None):
        del timeout, client
        seen.append((base_url, api_key))
        return normalize_discovery_models({
            "models": [
                {
                    "name": "models/gemini-4-neon",
                    "inputTokenLimit": 1_048_576,
                    "outputTokenLimit": 1_048_576,
                    "supportedGenerationMethods": ["generateContent", "countTokens"],
                    "thinking": True,
                },
                {
                    "name": "models/gemini-4-future-preview",
                    "inputTokenLimit": 1_048_576,
                    "outputTokenLimit": 65_536,
                    "supportedGenerationMethods": ["generateContent"],
                },
                {
                    "name": "models/gemini-2.0-flash",
                    "inputTokenLimit": 1_048_576,
                    "outputTokenLimit": 8_192,
                    "supportedGenerationMethods": ["generateContent"],
                },
                {
                    "name": "models/text-embedding-004",
                    "inputTokenLimit": 2048,
                    "supportedGenerationMethods": ["embedContent"],
                },
                {
                    "name": "models/imagen-4.0-generate-001",
                    "inputTokenLimit": 4096,
                    "supportedGenerationMethods": ["generateContent"],
                },
                {
                    "name": "models/gemini-3-pro-image",
                    "inputTokenLimit": 131072,
                    "supportedGenerationMethods": ["generateContent"],
                },
                {
                    "name": "models/gemini-3.5-transcribe",
                    "inputTokenLimit": 98304,
                    "supportedGenerationMethods": ["generateContent"],
                },
            ]
        })

    monkeypatch.setattr(gemini_provider, "fetch_models", fake_fetch)

    provider_id, catalog = asyncio.run(connections.connect_provider("gemini", api_key="AIza-private-test"))
    assert provider_id == "gemini"
    assert seen == [(BASE_URL, "AIza-private-test")]
    by_id = {entry.model_id: entry for entry in catalog.entries}
    assert set(by_id) == {"gemini-4-neon", "gemini-4-future-preview", "gemini-2.0-flash"}
    future = by_id["gemini-4-future-preview"].profile
    assert future.provider == PROVIDER_NAME
    assert future.context_limit == 1_048_576
    assert future.max_tokens == 65_536
    assert {"tools", "reasoning", "thinking-required", "vision", "streaming"} <= future.capabilities
    assert future.reasoning_levels == ("low", "medium", "high", "xhigh")

    flash20 = by_id["gemini-2.0-flash"].profile
    assert "reasoning" not in flash20.capabilities
    assert "thinking-required" not in flash20.capabilities
    assert {"tools", "vision", "streaming"} <= flash20.capabilities

    assert "AIza-private-test" not in json.dumps([e.to_event() for e in catalog.entries])
    restored = model_catalog.configured_model_catalog().resolve_persisted("gemini::gemini-4-future-preview")
    assert restored is not None
    assert restored.profile.provider == PROVIDER_NAME
    assert restored.profile.api_key() == "AIza-private-test"

    # Also verify env-only GEMINI_API_KEY without a /connect file creates a google-gemini endpoint.
    for path in provider_connections.connections_path().glob("*.json"):
        path.unlink()
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-env-only")
    monkeypatch.setenv("AGENT_MODELS_FILE", str(models.DEFAULT_MODELS_PATH))
    env_endpoint = next(e for e in model_catalog.provider_endpoints() if e.id == "gemini")
    assert env_endpoint.profile.provider == PROVIDER_NAME
    assert env_endpoint.profile.api_key() == "AIza-env-only"


def test_fetch_models_uses_header_auth_and_maps_invalid_key():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.headers.get("x-goog-api-key") == "bad":
            return httpx.Response(400, json={
                "error": {"code": 400, "status": "INVALID_ARGUMENT", "message": "API key not valid."}
            })
        return httpx.Response(200, json={"models": [
            {"name": "models/gemini-4-hydrogen", "inputTokenLimit": 1_048_576, "outputTokenLimit": 65_536,
             "supportedGenerationMethods": ["generateContent"]}
        ]})

    async def work():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            items = await gemini_provider.fetch_models(BASE_URL, "good-key", client=client)
            assert [i["id"] for i in items] == ["gemini-4-hydrogen"]
            with pytest.raises(GeminiRequestError) as err:
                await gemini_provider.fetch_models(BASE_URL, "bad", client=client)
            assert err.value.code == "auth_failed"

    asyncio.run(work())
    assert requests[0].headers["x-goog-api-key"] == "good-key"
    assert "good-key" not in str(requests[0].url)


def test_react_agent_roundtrips_gemini_thought_signature_and_persists_session(tmp_path):
    captured = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.append(body)
        if len(captured) == 1:
            return httpx.Response(200, content=sse({
                "candidates": [{
                    "content": {"role": "model", "parts": [
                        {"text": "Checking echo tool", "thought": True},
                        {
                            "functionCall": {"id": "call_g4_1", "name": "echo_note", "args": {"note": "hello"}},
                            "thoughtSignature": "REACT_SIG_001",
                        },
                    ]},
                    "finishReason": "STOP",
                }],
                "usageMetadata": {"promptTokenCount": 50, "candidatesTokenCount": 15, "thoughtsTokenCount": 25, "totalTokenCount": 90},
            }))
        return httpx.Response(200, content=sse({
            "candidates": [{
                "content": {"role": "model", "parts": [{"text": "Echo returned: hello"}]},
                "finishReason": "STOP",
            }],
            "usageMetadata": {"promptTokenCount": 80, "candidatesTokenCount": 12, "totalTokenCount": 92},
        }))

    provider = GeminiProvider(config(), transport=httpx.MockTransport(handler))
    client = LLMClient(provider.config, provider=provider)
    registry = ToolRegistry()
    registry.register(ToolDef(
        name="echo_note",
        description="Echo a note",
        parameters={"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"], "additionalProperties": False},
        fn=lambda note: f"echo:{note}",
        risk="read",
        idempotent=True,
    ))
    agent = ReActAgent("gemini-test", client, registry, system_prompt="Test system", progressive_tools=False)
    session_file = tmp_path / "gemini-session.json"
    agent.context.set_session(str(session_file))

    async def run_turn():
        events = []
        async for ev in agent.reply_stream(Msg(sender="user", role="user", content=[ContentBlock.text("Run echo_note")])):
            events.append(ev)
        return events

    events = asyncio.run(run_turn())
    assert len(captured) == 2
    model_parts = captured[1]["contents"][1]["parts"]
    assert model_parts[0]["thoughtSignature"] == "REACT_SIG_001"
    assert captured[1]["contents"][2]["parts"][0]["functionResponse"]["id"] == "call_g4_1"
    streamed_text = "".join(ev.get("content", "") for ev in events if ev.get("type") == "chunk")
    assert "Echo returned: hello" in streamed_text

    # Restored session from disk retains _provider_state safely.
    from agent.runtime.context import AgentContext
    restored = AgentContext()
    restored.set_session(str(session_file))
    restored.load()
    assistant_msgs = [m for m in restored.messages if m.get("role") == "assistant" and m.get("tool_calls")]
    assert assistant_msgs and assistant_msgs[0][STATE_KEY]["provider"] == PROVIDER_NAME
    assert assistant_msgs[0][STATE_KEY]["items"][0]["thoughtSignature"] == "REACT_SIG_001"


def test_mode_status_and_delegate_deepseek_guard():
    status = mode_preferences.reasoning_effort_status(config(reasoning_effort="xhigh"))
    assert "Google AI Studio" in status
    assert "thinkingLevel" in status

    client = LLMClient(config())
    with pytest.raises(ValueError, match="incompatible with the DeepSeek model override"):
        _worker_llm(client, {}, model="deepseek-flash")
