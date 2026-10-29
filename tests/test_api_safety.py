"""API boundary tests that never bootstrap a model or create sessions."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.requests import Request

from agent.cli import api_server


@pytest.fixture(autouse=True)
def isolated_api(monkeypatch):
    monkeypatch.delenv("ASTRA_API_KEY", raising=False)
    monkeypatch.delenv("ASTRA_API_HOST", raising=False)
    monkeypatch.delenv("ASTRA_API_PORT", raising=False)
    monkeypatch.setattr(api_server, "_agent", SimpleNamespace(llm=SimpleNamespace(model="test")))
    runner = AsyncMock(return_value="answer")
    monkeypatch.setattr(api_server, "_run_agent", runner)
    return runner


def request(body=None, *, authorization=None, server="127.0.0.1", raw=None, origin=None):
    headers = [] if authorization is None else [(b"authorization", authorization.encode())]
    if origin is not None:
        headers += [(b"origin", origin.encode()), (b"content-type", b"text/plain")]

    async def receive():
        return {
            "type": "http.request",
            "body": json.dumps(body).encode() if raw is None else raw,
        }

    return Request({
        "type": "http", "method": "POST", "path": "/v1/chat/completions",
        "headers": headers, "server": (server, 8900),
    }, receive)


@pytest.mark.parametrize("handler", [api_server.health, api_server.list_models, api_server.chat_completions])
@pytest.mark.parametrize("authorization", [None, "Bearer wrong", "Basic secret", "Bearer"])
def test_configured_key_denies_every_endpoint_before_agent_use(
    monkeypatch, isolated_api, handler, authorization,
):
    monkeypatch.setenv("ASTRA_API_KEY", "private-key")
    monkeypatch.setattr(api_server, "_agent", None)

    response = asyncio.run(handler(request(authorization=authorization, raw=b"invalid JSON")))

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert b"private-key" not in response.body
    isolated_api.assert_not_called()


def test_valid_bearer_key_allows_existing_chat_behavior(monkeypatch, isolated_api):
    monkeypatch.setenv("ASTRA_API_KEY", "private-key")
    response = asyncio.run(api_server.chat_completions(request(
        {"messages": [{"role": "user", "content": "hello"}]},
        authorization="bearer private-key",
    )))
    assert response.status_code == 200
    isolated_api.assert_awaited_once_with("hello", None, usage={})


def test_external_asgi_binding_without_key_cannot_bypass_main(isolated_api):
    response = asyncio.run(api_server.chat_completions(request(
        {"messages": [{"role": "user", "content": "hello"}]}, server="192.0.2.10",
    )))
    assert response.status_code == 503
    isolated_api.assert_not_called()


def test_browser_origin_requires_configured_bearer_auth(monkeypatch, isolated_api):
    body = {"messages": [{"role": "user", "content": "hello"}]}
    denied = asyncio.run(api_server.chat_completions(request(body, origin="https://example.com")))
    assert denied.status_code == 403
    isolated_api.assert_not_called()

    monkeypatch.setenv("ASTRA_API_KEY", "private-key")
    allowed = asyncio.run(api_server.chat_completions(request(
        body, authorization="Bearer private-key", origin="https://example.com",
    )))
    assert allowed.status_code == 200


def test_lifespan_rejects_unsafe_config_before_bootstrap(monkeypatch):
    from agent.cli import backend

    monkeypatch.setattr(backend, "load_project_env", lambda _root: None)
    monkeypatch.setenv("ASTRA_API_HOST", "0.0.0.0")
    bootstrap = AsyncMock()
    monkeypatch.setattr(api_server, "_bootstrap_agent", bootstrap)

    async def startup():
        async with api_server.lifespan(api_server.app):
            pass

    with pytest.raises(RuntimeError, match="ASTRA_API_KEY"):
        asyncio.run(startup())
    bootstrap.assert_not_called()


@pytest.mark.parametrize("body", [
    None, [], "hello", 42,
    {"messages": None}, {"messages": {}}, {"messages": ["hello"]},
    {"messages": [{"role": 2, "content": "hello"}]},
    {"messages": [{"role": "user", "content": None}]},
    {"messages": [{"role": "user", "content": 42}]},
    {"messages": [{"role": "user", "content": ["hello"]}]},
    {"messages": [{"role": "user", "content": [{"type": "text", "text": 42}]}]},
    {"messages": [{"role": "user", "content": "hello"}], "stream": "false"},
    {"messages": [{"role": "user", "content": "hello"}], "model": []},
])
def test_malformed_request_returns_400_before_agent_use(isolated_api, body):
    response = asyncio.run(api_server.chat_completions(request(body)))
    assert response.status_code == 400
    assert json.loads(response.body)["error"]["type"] == "invalid_request_error"
    isolated_api.assert_not_called()


def test_valid_text_parts_and_assistant_tool_history_remain_accepted(isolated_api):
    response = asyncio.run(api_server.chat_completions(request({"messages": [
        {"role": "assistant", "content": None, "tool_calls": []},
        {"role": "user", "content": [
            {"type": "text", "text": "first"},
            {"type": "text", "text": "second"},
        ]},
    ]})))
    assert response.status_code == 200
    call = isolated_api.await_args
    assert call.args[0].text == "first\nsecond" and call.args[0].history == []
    assert [block.data["text"] for block in call.args[0].blocks] == ["first", "second"]


def test_main_defaults_to_loopback(monkeypatch):
    import uvicorn
    from agent.cli import backend

    calls = []
    monkeypatch.setattr(backend, "load_project_env", lambda _root: None)
    monkeypatch.setattr(uvicorn, "run", lambda *_args, **kwargs: calls.append(kwargs))
    api_server.main()
    assert calls[0]["host"] == "127.0.0.1"


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.0.2.10", "example.com"])
def test_non_loopback_startup_requires_key(monkeypatch, host):
    import uvicorn
    from agent.cli import backend

    calls = []
    monkeypatch.setattr(backend, "load_project_env", lambda _root: None)
    monkeypatch.setenv("ASTRA_API_HOST", host)
    monkeypatch.setattr(uvicorn, "run", lambda *_args, **kwargs: calls.append(kwargs))
    with pytest.raises(RuntimeError, match="ASTRA_API_KEY"):
        api_server.main()
    assert calls == []


def test_public_binding_with_key_is_allowed_without_printing_it(monkeypatch, capsys):
    import uvicorn
    from agent.cli import backend

    calls = []
    monkeypatch.setattr(backend, "load_project_env", lambda _root: None)
    monkeypatch.setenv("ASTRA_API_HOST", "0.0.0.0")
    monkeypatch.setenv("ASTRA_API_KEY", "private-key")
    monkeypatch.setattr(uvicorn, "run", lambda *_args, **kwargs: calls.append(kwargs))
    api_server.main()
    assert calls[0]["host"] == "0.0.0.0"
    assert "private-key" not in capsys.readouterr().out
