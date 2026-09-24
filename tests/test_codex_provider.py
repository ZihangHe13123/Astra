import asyncio
import base64
import json
import os
import time
from copy import deepcopy
from dataclasses import replace

import httpx
import pytest

from agent.cli import connections, model_catalog, provider_connections
from agent.runtime import codex_auth
from agent.runtime.codex_provider import CodexProvider
from agent.runtime.codex_wire import STATE_KEY, completed_response, input_items, signature
from agent.runtime.llm import LLMConfig, LLMIdleTimeout, LLMResponseError, OpenAICompatibleProvider


def token(account="test-account", exp=None):
    claims = {"exp": exp or time.time() + 3600,
              "https://api.openai.com/auth": {"chatgpt_account_id": account}}
    return "header." + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=") + ".signature"


@pytest.fixture(autouse=True)
def auth_home(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRA_HOME", str(tmp_path / "state"))


def signed_in(**overrides):
    data = codex_auth._credentials({"access_token": token(), "refresh_token": "refresh-private"})
    codex_auth._write({**data, **overrides})
    return data


def config(**kw):
    return LLMConfig(model="test-codex", base_url=codex_auth.BASE_URL, provider="openai-codex",
                     capabilities=frozenset({"reasoning", "tools", "streaming", "vision"}), **kw)


def test_body_defaults_function_strict_false_without_changing_optional_schemas():
    tools = [
        {"type": "function", "function": {
            "name": "read_file", "description": "Read a page",
            "parameters": {"type": "object", "properties": {
                "path": {"type": "string"},
                "offset": {"type": "integer", "default": 0},
                "byte_offset": {"type": "integer", "minimum": 0},
            }, "required": ["path"]},
        }},
        {"type": "function", "function": {
            "name": "delegate_task", "description": "Delegate with optional effort",
            "parameters": {"type": "object", "properties": {
                "goal": {"type": "string"},
                "reasoning_effort": {"type": "string", "enum": ["low", "high"]},
            }, "required": ["goal"], "additionalProperties": False},
        }},
    ]
    original = deepcopy(tools)

    body = CodexProvider(config())._body([], tools, None, "account")

    assert tools == original
    assert body["tools"] == [
        {"type": "function", "strict": False, **tool["function"]} for tool in original
    ]


@pytest.mark.parametrize("strict", [False, True])
def test_body_preserves_explicit_function_strict_intent(strict):
    tools = [{"type": "function", "function": {
        "name": "probe", "strict": strict,
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    }}]
    original = deepcopy(tools)

    body = CodexProvider(config())._body([], tools, None, "account")

    assert body["tools"][0]["strict"] is strict
    assert tools == original


def message(text="Done", phase="final_answer"):
    return {"type": "message", "id": "msg_server", "status": "completed", "role": "assistant", "phase": phase,
            "content": [{"type": "output_text", "text": text}]}


def response(output=None):
    return {"status": "completed", "output": output if output is not None else [message()],
            "usage": {"input_tokens": 25, "output_tokens": 9, "input_tokens_details": {"cached_tokens": 20}}}


def sse(*events):
    return "".join("data: " + json.dumps(e) + "\n\n" for e in events).encode()


def terminal(output=None):
    return {"type": "response.completed", "response": response(output)}


def completed_items(output):
    return [*({"type": "response.output_item.done", "output_index": index, "item": item}
              for index, item in enumerate(output)), terminal([])]


@pytest.mark.parametrize("omit_output", [False, True])
def test_completed_items_survive_empty_terminal_output(omit_output):
    signed_in()
    thought = {"type": "reasoning", "encrypted_content": "opaque", "summary": [
        {"type": "summary_text", "text": "Check the greeting"}]}
    events = completed_items([thought, message("早上好")])
    if omit_output:
        events[-1]["response"].pop("output")
    provider = CodexProvider(config(), transport=httpx.MockTransport(lambda r: httpx.Response(200, content=sse(
        {"type": "response.output_text.delta", "output_index": 1, "delta": "早上"}, *events))))
    async def work():
        return [e async for e in provider.chat_stream([])]
    emitted = asyncio.run(work())
    assert "".join(e["content"] for e in emitted if e["type"] == "chunk") == "早上好"
    assert emitted[-1]["type"] == "done"
    assert emitted[-1]["content"] == "早上好"
    assert emitted[-1]["usage"]["prompt_tokens"] == 25
    assert emitted[-1]["reasoning_content"] == "推理摘要\nCheck the greeting"
    assert emitted[-1][STATE_KEY]["items"][0]["encrypted_content"] == "opaque"


@pytest.mark.parametrize("failure", ["missing_item", "unfinished_item", "changed_item", "no_completion", "delta_only"])
def test_completed_items_do_not_accept_partial_or_conflicting_streams(failure):
    signed_in()
    call = {"type": "function_call", "call_id": "call_1", "name": "read_file", "arguments": "{}"}
    events = completed_items([call])
    if failure == "missing_item":
        events[0]["output_index"] = 1
    elif failure == "unfinished_item":
        events.insert(0, {"type": "response.output_item.added", "output_index": 1,
                          "item": {**call, "call_id": "call_2", "status": "in_progress"}})
    elif failure == "changed_item":
        events[-1] = terminal([{**call, "arguments": '{"path":"unexpected"}'}])
    elif failure == "no_completion":
        events.pop()
    elif failure == "delta_only":
        events = [{"type": "response.output_text.delta", "output_index": 0, "delta": "partial"}, terminal([])]
    provider = CodexProvider(config(max_retries=0), transport=httpx.MockTransport(
        lambda r: httpx.Response(200, content=sse(*events))))
    emitted = []
    async def work():
        with pytest.raises(LLMResponseError):
            async for event in provider.chat_stream([]):
                emitted.append(event)
    asyncio.run(work())
    assert not any(e["type"] in {"done", "tool_calls"} for e in emitted)


def test_max_uses_highest_advertised_effort_supported_by_endpoint():
    provider = CodexProvider(config(reasoning_effort="max", reasoning_levels=("low", "high", "xhigh", "max", "ultra")))
    assert provider._body([], None, None, "account")["reasoning"]["effort"] == "max"


def test_xhigh_runs_as_is_or_at_the_highest_level_below_it():
    def effort(levels):
        provider = CodexProvider(config(reasoning_effort="xhigh", reasoning_levels=levels))
        return provider._body([], None, None, "account")["reasoning"]["effort"]
    assert effort(("low", "medium", "high", "xhigh")) == "xhigh"
    assert effort(("low", "medium", "high")) == "high"
    assert effort(()) == "xhigh"


def test_device_login_uses_independent_private_store(monkeypatch):
    requests, progress = [], []
    original_sleep = asyncio.sleep
    async def fast_sleep(_):
        await original_sleep(0)
    monkeypatch.setattr(codex_auth.asyncio, "sleep", fast_sleep)
    def handler(request):
        requests.append(request)
        if request.url.path.endswith("usercode"):
            return httpx.Response(200, json={"device_auth_id": "device", "user_code": "TEST-CODE", "interval": "3"})
        if request.url.path.endswith("deviceauth/token"):
            count = sum(r.url.path.endswith("deviceauth/token") for r in requests)
            return httpx.Response(403) if count == 1 else httpx.Response(200, json={"authorization_code": "code", "code_verifier": "verifier"})
        assert request.url.path == "/oauth/token"
        assert b"code_verifier=verifier" in request.content
        return httpx.Response(200, json={"access_token": token(), "refresh_token": "private-refresh"})
    async def work():
        async def show(event):
            progress.append(event)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await codex_auth.device_login(show, client=client)
    asyncio.run(work())
    assert progress[0]["verification_uri"] == codex_auth.DEVICE_URL
    assert progress[0]["user_code"] == "TEST-CODE"
    assert codex_auth.auth_status()["connected"]
    assert "private-refresh" not in str(codex_auth.auth_status()) + codex_auth.credential_hint()
    assert {r.url.host for r in requests} == {"auth.openai.com"}
    if os.name != "nt":
        assert codex_auth.auth_path().stat().st_mode & 0o777 == 0o600


def test_cancel_login_does_not_save_or_poll():
    seen = []
    async def show(_):
        raise asyncio.CancelledError
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"device_auth_id": "device", "user_code": "code"})
    async def work():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(asyncio.CancelledError):
                await codex_auth.device_login(show, client=client)
    asyncio.run(work())
    assert len(seen) == 1 and not codex_auth.auth_path().exists()


def test_refresh_is_serialized_and_rejection_is_quarantined():
    signed_in(expires_at=0)
    requests = []
    async def handler(request):
        requests.append(request)
        await asyncio.sleep(.02)
        return httpx.Response(200, json={"access_token": token(), "refresh_token": "rotated"})
    async def work():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            values = await asyncio.gather(codex_auth.credentials(client=client), codex_auth.credentials(client=client))
            assert values[0] == values[1]
        assert len(requests) == 1
        signed_in(expires_at=0)
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(400, text="private-secret"))) as client:
            with pytest.raises(codex_auth.CodexAuthError, match="revoked"):
                await codex_auth.credentials(client=client)
            with pytest.raises(codex_auth.CodexAuthError, match="not signed in"):
                await codex_auth.credentials(client=client)
    asyncio.run(work())
    assert not codex_auth.auth_status()["connected"]
    assert codex_auth._read() == {"version": 1, "reauth_required": True}


def test_account_catalog_normalizes_capabilities_and_scopes_cache(monkeypatch):
    signed_in()
    real_fetch = codex_auth.fetch_models
    async def fetch():
        def handler(request):
            assert request.headers["ChatGPT-Account-ID"] == "test-account"
            assert request.url.params["client_version"] == "0.155.0"
            return httpx.Response(200, json={"models": [
                {"slug": "test-codex", "context_window": 128000, "input_modalities": ["text", "image"],
                 "supported_reasoning_levels": [{"effort": "high"}, {"effort": "xhigh"}], "supported_in_api": False},
                {"slug": "hidden", "visibility": "hide"}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await real_fetch(client=client)
    monkeypatch.setattr(codex_auth, "fetch_models", fetch)
    provider_id, catalog = asyncio.run(connections.connect_provider("codex"))
    assert provider_id == "codex" and len(catalog.entries) == 1
    profile = catalog.entries[0].profile
    assert profile.provider == "openai-codex"
    assert profile.context_limit == 128000
    assert {"vision", "tools", "reasoning"} <= profile.capabilities
    assert profile.reasoning_levels == ("high", "xhigh")
    assert profile.generation_settings()["reasoning_levels"] == ("high", "xhigh")
    assert "refresh-private" not in str(provider_connections.read_connections())
    restored = model_catalog.configured_model_catalog().resolve_persisted("codex::test-codex")
    assert restored and restored.profile.reasoning_levels == ("high", "xhigh")


def test_stream_summary_text_usage_and_replay():
    signed_in()
    reasoning = {"type": "reasoning", "id": "rs_server", "encrypted_content": "opaque-not-for-display",
                 "summary": [{"type": "summary_text", "text": "Plan"}]}
    events = [
        {"type": "response.reasoning_text.delta", "delta": "raw-reasoning-must-not-display"},
        {"type": "response.reasoning_summary_text.delta", "output_index": 0, "summary_index": 0, "delta": "Plan"},
        {"type": "response.output_text.delta", "delta": "Do"}, terminal([reasoning, message()])]
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, content=sse(*events), headers={"content-type": "text/event-stream"})
    provider = CodexProvider(config(reasoning_effort="max", reasoning_levels=("high", "xhigh")), transport=httpx.MockTransport(handler))
    async def work():
        return [e async for e in provider.chat_stream([{"role": "system", "content": "system"}, {"role": "user", "content": "test"}])]
    emitted = asyncio.run(work())
    assert "".join(e["content"] for e in emitted if e["type"] == "chunk") == "Done"
    assert "".join(e["content"] for e in emitted if e["type"] == "reasoning") == "推理摘要\nPlan"
    assert "opaque" not in str([e for e in emitted if e["type"] != "done"])
    assert "raw-reasoning" not in str(emitted)
    assert emitted[-1]["usage"]["prompt_cache_hit_tokens"] == 20
    body = requests[0]
    assert body["reasoning"] == {"effort": "xhigh", "summary": "auto"}
    assert body["instructions"] == "system" and body["store"] is False
    assert "max_output_tokens" not in body and "temperature" not in body
    stored = {"role": "assistant", "content": "Done", STATE_KEY: emitted[-1][STATE_KEY]}
    _, items = input_items([stored], account="test-account", model="test-codex")
    assert items[0]["encrypted_content"] == "opaque-not-for-display"
    assert "id" not in items[0] and items[1]["phase"] == "final_answer"


def test_tool_round_trip_and_phase_do_not_duplicate_calls():
    call = {"type": "function_call", "call_id": "call_1", "name": "read_file", "arguments": '{"path":"a.txt"}'}
    raw = completed_response(response([message("Checking", "commentary"), call]), account="a", model="m")
    stored = {"role": "assistant", **raw}
    history = [stored, {"role": "tool", "tool_call_id": "call_1", "content": "apple"}]
    _, items = input_items(history, account="a", model="m")
    assert [i["type"] for i in items] == ["message", "function_call", "function_call_output"]
    assert items[0]["phase"] == "commentary"
    assert items[1]["call_id"] == items[2]["call_id"] == "call_1"
    # Equivalent JSON formatting from durable tool argument sanitation is fine.
    stored["tool_calls"][0]["arguments"] = '{ "path": "a.txt" }'
    assert signature(stored) == raw[STATE_KEY]["signature"]


@pytest.mark.parametrize("change", ["model", "account", "content", "tool_args"])
def test_stale_opaque_state_never_replays(change):
    r = {"type": "reasoning", "encrypted_content": "opaque", "summary": []}
    call = {"type": "function_call", "call_id": "c", "name": "tool", "arguments": '{"key":"private"}'}
    raw = completed_response(response([r, message("old"), call]), account="a", model="m")
    stored = {"role": "assistant", **raw}
    if change == "content":
        stored["content"] = "edited"
    if change == "tool_args":
        stored["tool_calls"][0]["arguments"] = '{"key":"[redacted]"}'
    _, items = input_items([stored], account="other" if change == "account" else "a", model="other" if change == "model" else "m")
    assert all(i["type"] != "reasoning" for i in items)
    assert "private" not in json.dumps(raw[STATE_KEY])


@pytest.mark.parametrize("ending", [b"", sse({"type": "response.incomplete", "response": {"status": "incomplete"}}),
    sse({"type": "response.failed"}), sse(terminal([{ "type": "function_call", "call_id": "a", "name": "tool", "arguments": '{"broken":'}]))])
def test_incomplete_stream_never_dispatches_tools_or_retries(ending):
    signed_in()
    seen, emitted = [], []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, content=sse({"type": "response.function_call_arguments.delta", "output_index": 0, "delta": '{"x":'}) + ending)
    async def work():
        provider = CodexProvider(config(), transport=httpx.MockTransport(handler))
        with pytest.raises(LLMResponseError):
            async for e in provider.chat_stream([{"role": "user", "content": "test"}]):
                emitted.append(e)
    asyncio.run(work())
    assert len(seen) == 1 and not any(e["type"] == "tool_calls" for e in emitted)


def test_vision_and_cross_provider_state_isolation():
    _, items = input_items([{"role": "user", "content": [
        {"type": "text", "text": "See"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,a", "detail": "high"}}]}], account="a", model="m")
    assert items[0]["content"][1] == {"type": "input_image", "image_url": "data:image/png;base64,a", "detail": "high"}
    other = OpenAICompatibleProvider(LLMConfig(api_key="test"))
    body = other._completion_kwargs([{"role": "assistant", "content": "hello", STATE_KEY: {"encrypted_content": "private"}}])
    assert STATE_KEY not in body["messages"][0] and "private" not in str(body)


def test_foreign_endpoint_cannot_receive_oauth():
    with pytest.raises(ValueError, match="official"):
        CodexProvider(replace(config(), base_url="https://example.com"))


def test_heartbeat_only_stream_times_out_and_closes():
    signed_in()
    closed = []
    class Heartbeats(httpx.AsyncByteStream):
        async def __aiter__(self):
            while True:
                await asyncio.sleep(.004)
                yield b": ping\n\n"
        async def aclose(self):
            closed.append(True)
    provider = CodexProvider(config(idle_timeout=.03), transport=httpx.MockTransport(
        lambda r: httpx.Response(200, stream=Heartbeats())))
    async def work():
        with pytest.raises(LLMIdleTimeout):
            await provider.chat([{"role": "user", "content": "test"}])
    asyncio.run(work())
    assert closed


@pytest.mark.parametrize("empty_terminal_output", [False, True])
def test_real_agent_loop_and_saved_history_preserve_continuation(tmp_path, empty_terminal_output):
    from agent.core.msg import ContentBlock, Msg
    from agent.runtime.context import AgentContext
    from agent.runtime.llm import LLMClient
    from agent.runtime.react import ReActAgent
    from agent.runtime.tools.registry import ToolDef, ToolRegistry

    signed_in()
    requests, ran = [], []
    thought = {"type": "reasoning", "encrypted_content": "opaque", "summary": [{"type": "summary_text", "text": "Check"}]}
    call = {"type": "function_call", "call_id": "call_1", "name": "fixture_read", "arguments": "{}"}
    def stream(output):
        return sse(*(completed_items(output) if empty_terminal_output else [terminal(output)]))
    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            assert body["tools"][0]["type"] == "function"
            return httpx.Response(200, content=stream([thought, message("Checking", "commentary"), call]))
        assert [i["type"] for i in body["input"]][-4:] == ["reasoning", "message", "function_call", "function_call_output"]
        assert body["input"][-1]["call_id"] == "call_1"
        assert "apple" in body["input"][-1]["output"]
        return httpx.Response(200, content=stream([thought, message("Done")]))
    def read():
        ran.append(True)
        return "apple"
    async def work():
        registry = ToolRegistry()
        registry.register(ToolDef("fixture_read", "read", {"type": "object", "properties": {}}, read, risk="read"))
        llm = LLMClient(config(), provider=CodexProvider(config(), transport=httpx.MockTransport(handler)))
        agent = ReActAgent("test", llm, registry, max_iterations=3, progressive_tools=False, timing_log_enabled=False)
        agent.context.set_session(str(tmp_path / "session.json"))
        events = [e async for e in agent.reply_stream(Msg(content=[ContentBlock.text("read fixture")]))]
        assert not [e for e in events if e["type"] == "error"]
        assert len(ran) == 1 and len(requests) == 2
        assert agent.context.messages[-1][STATE_KEY]["provider"] == "openai-codex"
        agent.context.save()
        restored = AgentContext()
        restored.set_session(str(tmp_path / "session.json"))
        assert restored.load()
        _, items = input_items(restored.get_prompt(), account="test-account", model="test-codex")
        assert sum(i["type"] == "reasoning" for i in items) == 2
        assert items[-1]["content"][0]["text"] == "Done"
    asyncio.run(work())


def test_reasoning_summary_final_only_and_no_fabricated_thinking():
    signed_in()
    async def work():
        for summary in ([], [{"type": "summary_text", "text": "Final summary"}]):
            thought = {"type": "reasoning", "encrypted_content": "opaque", "summary": summary}
            provider = CodexProvider(config(), transport=httpx.MockTransport(
                lambda r, thought=thought: httpx.Response(200, content=sse(terminal([thought, message()])))))
            events = [e async for e in provider.chat_stream([])]
            thinking = [e["content"] for e in events if e["type"] == "reasoning"]
            assert thinking == (["推理摘要\nFinal summary"] if summary else [])
    asyncio.run(work())


def test_ciphertext_is_not_counted_as_text_tokens():
    from agent.runtime.token_estimator import estimate_messages_tokens
    base = {"role": "assistant", "content": "ok"}
    stored = {**base, STATE_KEY: {"encrypted_content": "x" * 100000, "reasoning_tokens": 127}}
    assert estimate_messages_tokens([stored]) == estimate_messages_tokens([base]) + 127


@pytest.mark.parametrize("field,new", [("name", "other"), ("call_id", "other"), ("arguments", '{"x":2}')])
def test_contradictory_tool_stream_is_rejected(field, new):
    signed_in()
    call = {"type": "function_call", "call_id": "a", "name": "tool", "arguments": '{"x":1}'}
    payload = sse(
        {"type": "response.output_item.added", "output_index": 0, "item": {**call, "arguments": ""}},
        {"type": "response.function_call_arguments.delta", "output_index": 0, "delta": '{"x":1}'},
        terminal([{**call, field: new}]))
    provider = CodexProvider(config(), transport=httpx.MockTransport(lambda r: httpx.Response(200, content=payload)))
    with pytest.raises(LLMResponseError):
        asyncio.run(provider.chat([]))


def test_sign_in_error_is_actionable_and_has_no_tokens():
    from agent.runtime.codex_provider import CodexRequestError
    provider = CodexProvider(config(), transport=httpx.MockTransport(lambda r: pytest.fail("must not send")))
    with pytest.raises(CodexRequestError) as failure:
        asyncio.run(provider.chat([]))
    assert "astra auth login" in failure.value.public_message


def test_401_refresh_rebuilds_account_bound_continuation(monkeypatch):
    state = completed_response(response([{"type": "reasoning", "encrypted_content": "old-account", "summary": []},
                                         message()]), account="a", model="test-codex")
    history = [{"role": "assistant", **state}]
    seen = []
    async def credentials(**kwargs):
        account = "b" if kwargs.get("rejected_token") else "a"
        return {"account_id": account, "access_token": account}
    monkeypatch.setattr(codex_auth, "credentials", credentials)
    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(401) if len(seen) == 1 else httpx.Response(200, content=sse(terminal()))
    provider = CodexProvider(config(), transport=httpx.MockTransport(handler))
    asyncio.run(provider.chat(history))
    assert seen[0]["input"][0]["encrypted_content"] == "old-account"
    assert all(i["type"] != "reasoning" for i in seen[1]["input"])


def test_logout_removes_only_astra_auth_and_leaves_actionable_status():
    signed_in()
    neighbor = codex_auth.auth_path().parent / "other.json"
    neighbor.write_text("untouched")
    asyncio.run(codex_auth.logout())
    assert not codex_auth.auth_path().exists()
    assert neighbor.read_text() == "untouched"
    assert not codex_auth.auth_status()["connected"]


def test_subscription_limit_never_falls_back_to_api():
    from agent.runtime.codex_provider import CodexRequestError
    signed_in()
    seen = []
    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(429, text="SENSITIVE-UPSTREAM-BODY")
    provider = CodexProvider(config(max_retries=0), transport=httpx.MockTransport(handler))
    with pytest.raises(CodexRequestError) as failure:
        asyncio.run(provider.chat([]))
    assert "usage limit" in failure.value.public_message
    assert "SENSITIVE" not in str(failure.value)
    assert seen == [codex_auth.BASE_URL + "/responses"]
