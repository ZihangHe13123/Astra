import asyncio
from types import SimpleNamespace

from agent.runtime import llm as llm_module
from agent.runtime import network
from agent.runtime.llm import LLMConfig, OpenAICompatibleProvider
from agent.runtime.tools import web as web_module
from agent.runtime.tools.registry import ToolRegistry


def _clear_proxy_env(monkeypatch):
    for name in (
        "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy",
        "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy", "ASTRA_PROXY_MODE",
    ):
        monkeypatch.delenv(name, raising=False)


def test_active_proxy_uses_only_a_reachable_endpoint(monkeypatch):
    _clear_proxy_env(monkeypatch)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setenv("ASTRA_PROXY_MODE", "auto")
    attempts = []

    class Connection:
        def close(self):
            attempts.append("closed")

    def connect(address, timeout):
        attempts.append((address, timeout))
        return Connection()

    monkeypatch.setattr(network.socket, "create_connection", connect)

    assert network.active_proxy_for_url("https://example.com") == "http://127.0.0.1:7890"
    assert attempts[0][0] == ("127.0.0.1", 7890)
    assert attempts[-1] == "closed"

    def unavailable(*_args, **_kwargs):
        raise ConnectionRefusedError

    monkeypatch.setattr(network.socket, "create_connection", unavailable)
    assert network.active_proxy_for_url("https://example.com") is None


def test_active_proxy_defaults_to_direct_even_when_parent_has_proxy(monkeypatch):
    _clear_proxy_env(monkeypatch)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setattr(
        network.socket,
        "create_connection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not probe")),
    )

    assert network.active_proxy_for_url("https://example.com") is None


def test_active_proxy_bypasses_local_and_no_proxy_hosts(monkeypatch):
    _clear_proxy_env(monkeypatch)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setenv("NO_PROXY", "api.deepseek.com,.example.cn")
    monkeypatch.setattr(
        network.socket,
        "create_connection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not probe")),
    )

    assert network.active_proxy_for_url("http://192.168.0.1:8000/v1") is None
    assert network.active_proxy_for_url("https://api.deepseek.com/v1") is None
    assert network.active_proxy_for_url("https://docs.example.cn/page") is None


def test_llm_provider_switches_between_live_proxy_and_direct(monkeypatch):
    route = ["http://127.0.0.1:7890"]
    http_clients = []
    sdk_clients = []

    def fake_http_client(**kwargs):
        http_clients.append(kwargs)
        return kwargs

    def fake_sdk(**kwargs):
        client = SimpleNamespace(kwargs=kwargs)
        sdk_clients.append(client)
        return client

    monkeypatch.setattr(llm_module, "active_proxy_for_url", lambda _url: route[0])
    monkeypatch.setattr(llm_module, "DefaultAsyncHttpxClient", fake_http_client)
    monkeypatch.setattr(llm_module, "AsyncOpenAI", fake_sdk)

    provider = OpenAICompatibleProvider(
        LLMConfig(base_url="https://api.deepseek.com", api_key="test-key")
    )
    assert http_clients[0] == {"trust_env": False, "proxy": "http://127.0.0.1:7890"}

    route[0] = None
    asyncio.run(provider._ensure_client_route())

    assert provider._proxy_url is None
    assert http_clients[1] == {"trust_env": False}
    assert provider._client is sdk_clients[1]


def test_web_tools_recheck_proxy_route_per_request(monkeypatch):
    routes = iter(["http://127.0.0.1:7890", None])
    client_kwargs = []

    class Response:
        text = "<html><body>Dynamic proxy routing returns enough readable content.</body></html>"

        def __init__(self, url):
            self.url = url

        def raise_for_status(self):
            return None

    class Client:
        def __init__(self, **kwargs):
            client_kwargs.append(kwargs)

        async def get(self, url, **_kwargs):
            return Response(url)

    async def safe_url(_url):
        return True

    monkeypatch.setenv("WEB_URL_CACHE_TTL", "0")
    monkeypatch.setattr(web_module, "active_proxy_for_url", lambda _url: next(routes))
    from http_page_fixture import install_page_client
    install_page_client(monkeypatch, web_module, Client)
    monkeypatch.setattr(web_module, "_is_safe_public_url", safe_url)

    async def scenario():
        registry = ToolRegistry()
        register = web_module.register_web_tools
        register(registry, None)
        await registry.execute("fetch_url", {"url": "https://example.test/one"})
        await registry.execute("fetch_url", {"url": "https://example.test/two"})

    asyncio.run(scenario())

    assert client_kwargs[0]["proxy"] == "http://127.0.0.1:7890"
    assert "proxy" not in client_kwargs[1]
    assert all(kwargs["trust_env"] is False for kwargs in client_kwargs)
