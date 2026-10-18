"""DeepSeek Search/Fetch as an optional provider of the web tools.

Every request is answered in memory (an httpx mock transport or a local HTTP
server); nothing reaches DeepSeek.
"""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest

import agent.runtime.tools.web as web_module
from agent.runtime.tools.conclave import _parse_search_results
from agent.runtime.tools.registry import ToolRegistry
from agent.runtime.tools.web import register_web_tools

TEST_KEY = "sk-test-deepseek-key-0123456789"
SEARCH_URL = "https://api.deepseek.com/v1/tools/search"
FETCH_URL = "https://api.deepseek.com/v1/tools/fetch"
LONG_SUMMARY = "Long summary sentence for the model. " * 60


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _web_environment(monkeypatch):
    for name in (
        "EXA_API_KEY", "EXA_ENV_FILE", "TAVILY_API_KEY", "PARALLEL_API_KEY", "FIRECRAWL_API_KEY",
        "FIRECRAWL_URL", "SEARXNG_URL", "WEB_SEARCH_PROVIDER", "WEB_EXTRACT_PROVIDER",
        "DEEPSEEK_API_KEY", "DEEPSEEK_TOOLS_URL", "DEEPSEEK_SEARCH_TIMEOUT",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(web_module, "_DEEPSEEK_RETRY_DELAY", 0)

    async def safe_url(url):
        return True

    # Extraction checks the target's DNS; the test hosts do not exist.
    monkeypatch.setattr(web_module, "_is_safe_public_url", safe_url)


class _Seen:
    def __init__(self, request: httpx.Request):
        self.method = request.method
        self.url = str(request.url)
        self.headers = request.headers
        self.body: dict = json.loads(request.content) if request.content else {}


def _mock_transport(monkeypatch, handler):
    """Answer every web tool request with ``handler(request)`` and record it."""
    seen: list[_Seen] = []
    real_client = httpx.AsyncClient

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(_Seen(request))
        return handler(request)

    def client(*args, **kwargs):
        kwargs.pop("proxy", None)
        return real_client(*args, transport=httpx.MockTransport(answer), **kwargs)

    monkeypatch.setattr(web_module.httpx, "AsyncClient", client)
    return seen


def _search_answer(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"results": [
        {
            "title": "Release notes",
            "url": "https://news.example.test/release",
            "snippet": "Short display snippet.",
            "summary": "A model-oriented summary\nthat spans lines.",
            "content": "",
            "site_name": "Example News",
            "site_icon": None,
            "date": "2026-10-15T08:00:00Z",
            "rerank_score": 2.1,
            "authority": 3,
        },
        {
            "title": "Snippet only",
            "url": "https://blog.example.test/post",
            "snippet": "Only a snippet here.",
            "summary": "",
            "site_name": None,
            "date": None,
        },
        {
            "title": "Long one",
            "url": "https://long.example.test/page",
            "snippet": "unused",
            "summary": LONG_SUMMARY,
        },
        {"title": "", "url": "", "snippet": "no url, skipped"},
    ]})


def _deepseek_only(handler):
    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.host != "api.deepseek.com":
            raise AssertionError(f"unexpected request: {request.url}")
        return handler(request)

    return answer


def test_deepseek_search_sends_only_query_and_count_and_shows_summaries(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", TEST_KEY)
    seen = _mock_transport(monkeypatch, _deepseek_only(_search_answer))

    async def scenario():
        registry = ToolRegistry()
        register_web_tools(registry, None)
        arguments = {"query": 'agent runtime site:docs.example.test "tool calls"', "provider": "deepseek"}
        result = await registry.execute("search_web", arguments)

        assert result["error"] == ""
        assert len(seen) == 1
        request = seen[0]
        assert (request.method, request.url) == ("POST", SEARCH_URL)
        assert request.headers["authorization"] == f"Bearer {TEST_KEY}"
        assert request.headers["content-type"] == "application/json"
        # Nothing but the query and the count leaves Astra.
        assert request.body == {
            "query": 'agent runtime site:docs.example.test "tool calls"',
            "count": 5,
            "need_content": False,
        }

        output = result["output"]
        assert "Provider: DeepSeek" in output
        assert "Not applied" not in output
        assert TEST_KEY not in output
        assert "    Published: 2026-10-15T08:00:00Z" in output
        assert "    Site: Example News" in output
        assert "    Snippet: A model-oriented summary that spans lines." in output
        assert "Short display snippet." not in output
        assert "    Snippet: Only a snippet here." in output
        assert "no url, skipped" not in output

        # The stable result contract other tools parse, such as Conclave's.
        findings = _parse_search_results(output)
        assert [item["url"] for item in findings] == [
            "https://news.example.test/release",
            "https://blog.example.test/post",
            "https://long.example.test/page",
        ]
        long_snippet = findings[2]["snippet"]
        assert long_snippet.endswith("...")
        assert len(long_snippet) == web_module._DEEPSEEK_TEXT_CHARS + 3
        assert LONG_SUMMARY.strip().startswith(long_snippet[:-3].strip())

        # The same search is answered from the cache; a larger count asks again.
        again = await registry.execute("search_web", arguments)
        assert again["output"] == output
        assert len(seen) == 1
        await registry.execute("search_web", {**arguments, "max_results": 12})
        assert len(seen) == 2
        assert seen[1].body["count"] == 12

    run(scenario())


def test_deepseek_search_names_the_parameters_it_did_not_apply(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", TEST_KEY)
    seen = _mock_transport(monkeypatch, _deepseek_only(_search_answer))

    async def scenario():
        registry = ToolRegistry()
        register_web_tools(registry, None)
        result = await registry.execute("search_web", {
            "query": "model release",
            "provider": "deepseek",
            "page": 2,
            "language": "ja",
            "category": "news",
        })

        note = next(line for line in result["output"].splitlines() if line.startswith("Not applied"))
        assert note.startswith("Not applied on the DeepSeek route:")
        for named in ("page=2", "language=ja", "category=news"):
            assert named in note
        assert "site:" in note
        assert set(seen[0].body) == {"query", "count", "need_content"}

    run(scenario())


def test_automatic_routes_never_reach_deepseek(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", TEST_KEY)
    monkeypatch.setenv("EXA_API_KEY", "test-exa-key")
    page = (
        "<html><body><main><h1>Readable page</h1><p>"
        + "Plain words that a static reader accepts as the body of a real page. " * 4
        + "</p></main></body></html>"
    )

    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.exa.ai" and request.url.path == "/search":
            return httpx.Response(200, json={"results": [
                {"title": "Exa hit", "url": "https://exa.example.test/one", "highlights": ["Exa."]},
            ]})
        if request.url.host == "api.exa.ai":
            return httpx.Response(500, json={"error": "exa contents down"})
        if request.url.path == "/search":
            return httpx.Response(200, json={"query": "q", "results": [
                {"title": "SearXNG hit", "url": "https://searx.example.test/hit", "content": "snippet"},
            ]})
        if request.url.host == "localhost":
            raise httpx.ConnectError("All connection attempts failed")
        return httpx.Response(200, text=page)

    seen = _mock_transport(monkeypatch, answer)

    async def scenario():
        registry = ToolRegistry()
        register_web_tools(registry, None)
        news = await registry.execute("search_web", {"query": "latest model release news"})
        plain = await registry.execute("search_web", {"query": "how to bake bread"})
        extracted = await registry.execute("web_extract", {"urls": ["https://example.test/article"]})

        assert "Exa hit" in news["output"]
        assert "SearXNG hit" in plain["output"]
        chain = json.loads(extracted["output"])["provider_chain"]
        assert "deepseek" not in chain
        assert seen
        assert all(request.url.split("/")[2] != "api.deepseek.com" for request in seen)

    run(scenario())


def test_deepseek_can_be_the_configured_default(monkeypatch, tmp_path):
    from agent.cli.search_preferences import (
        load_selected_search_provider,
        resolve_startup_search_provider,
        save_selected_search_provider,
    )

    monkeypatch.setenv("AGENT_SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setenv("WEB_SEARCH_PROVIDER", "deepseek")
    assert resolve_startup_search_provider() == "deepseek"
    monkeypatch.setenv("WEB_SEARCH_PROVIDER", "auto")
    save_selected_search_provider("deepseek")
    assert load_selected_search_provider() == "deepseek"
    assert resolve_startup_search_provider() == "deepseek"

    monkeypatch.setenv("DEEPSEEK_API_KEY", TEST_KEY)
    seen = _mock_transport(monkeypatch, _deepseek_only(_search_answer))

    async def scenario():
        registry = ToolRegistry()
        state = register_web_tools(registry, None, default_provider=resolve_startup_search_provider())
        assert state.provider == "deepseek"
        result = await registry.execute("search_web", {"query": "agent runtimes"})
        assert "Provider: DeepSeek" in result["output"]
        assert [request.url for request in seen] == [SEARCH_URL]

        # Switching back (as /search auto does) leaves DeepSeek out again.
        state.set("auto")
        assert state.provider == "auto"

    run(scenario())


def _status(status, body=None, headers=None):
    def handler(request):
        return httpx.Response(status, json=body if body is not None else {}, headers=headers)

    return handler


def _sequence(*handlers):
    remaining = list(handlers)

    def handler(request):
        return remaining.pop(0)(request) if len(remaining) > 1 else remaining[0](request)

    return handler


def _times_out(request):
    raise httpx.ReadTimeout("")


@pytest.mark.parametrize("handler,requests,code,retryable,in_error,in_hint", [
    pytest.param(
        _status(401, {"error": {"message": f"Authentication Fails, your api key {TEST_KEY} is invalid"}}),
        1, "search_failed", False, ["HTTP 401", "API key was rejected"], ["DEEPSEEK_API_KEY"],
        id="key-rejected",
    ),
    pytest.param(
        _status(402, {"error": {"message": "Insufficient Balance"}}),
        1, "search_failed", False, ["HTTP 402", "top-up", "Insufficient Balance"], ["top-up"],
        id="no-balance",
    ),
    pytest.param(
        _status(400, {"error": {"message": f"query is invalid near {TEST_KEY}", "type": "invalid_request"}}),
        1, "search_failed", False, ["HTTP 400", "[redacted]"], ["128 characters"],
        id="bad-request-echoing-the-key",
    ),
    pytest.param(
        _status(429, {"error": {"message": "Rate limit reached"}}),
        2, "search_failed", True, ["HTTP 429", "5 requests per second"], ["Retry later"],
        id="rate-limited-twice",
    ),
    pytest.param(
        _status(503), 2, "search_failed", True, ["HTTP 503", "server error"], ["Retry later"],
        id="server-error-twice",
    ),
    pytest.param(
        _status(307, headers={"location": "https://elsewhere.example.test/collect"}),
        1, "search_failed", False, ["HTTP 307", "redirect not followed"], [],
        id="redirect-refused",
    ),
    pytest.param(
        _times_out, 1, "search_failed", True, ["ReadTimeout"], ["Retry later"],
        id="timeout-not-repeated",
    ),
])
def test_deepseek_search_failures_are_named_and_retried_only_when_asked(
    monkeypatch, handler, requests, code, retryable, in_error, in_hint,
):
    monkeypatch.setenv("DEEPSEEK_API_KEY", TEST_KEY)
    seen = _mock_transport(monkeypatch, _deepseek_only(handler))

    async def scenario():
        registry = ToolRegistry()
        register_web_tools(registry, None)
        result = await registry.execute("search_web", {"query": "agent runtimes", "provider": "deepseek"})

        assert result["output"] == ""
        assert result["code"] == code
        assert result["retryable"] is retryable
        assert len(seen) == requests
        assert all(request.url == SEARCH_URL for request in seen)
        for text in in_error:
            assert text in result["error"]
        for text in in_hint:
            assert text in result["recovery_hint"]
        assert "provider=searxng" in result["recovery_hint"]
        assert TEST_KEY not in json.dumps(result)

    run(scenario())


def test_deepseek_search_succeeds_after_one_rate_limited_answer(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", TEST_KEY)
    seen = _mock_transport(monkeypatch, _deepseek_only(_sequence(_status(429), _search_answer)))

    async def scenario():
        registry = ToolRegistry()
        register_web_tools(registry, None)
        result = await registry.execute("search_web", {"query": "agent runtimes", "provider": "deepseek"})

        assert result["error"] == ""
        assert "Release notes" in result["output"]
        assert len(seen) == 2

    run(scenario())


def test_deepseek_search_without_key_or_with_a_long_query_sends_nothing(monkeypatch):
    seen = _mock_transport(monkeypatch, _deepseek_only(_search_answer))

    async def scenario():
        registry = ToolRegistry()
        register_web_tools(registry, None)
        missing = await registry.execute("search_web", {"query": "agent runtimes", "provider": "deepseek"})
        assert missing["code"] == "deepseek_not_configured"
        assert missing["retryable"] is False
        assert "DEEPSEEK_API_KEY" in missing["error"]
        assert "provider=searxng" in missing["recovery_hint"]

        monkeypatch.setenv("DEEPSEEK_API_KEY", TEST_KEY)
        monkeypatch.setenv("EXA_API_KEY", "test-exa-key")
        too_long = await registry.execute("search_web", {"query": "字" * 129, "provider": "deepseek"})
        assert too_long["code"] == "invalid_arguments"
        assert "129" in too_long["error"] and "128" in too_long["error"]
        assert "provider=exa or provider=searxng" in too_long["recovery_hint"]

        at_limit = await registry.execute("search_web", {"query": "字" * 128, "provider": "deepseek"})
        assert at_limit["error"] == ""
        assert [request.body["query"] for request in seen] == ["字" * 128]

    run(scenario())


def _fetch_and_static(fetch_handler, page_text="Static page words. " * 20):
    page = f"<html><body><main><h1>Static</h1><p>{page_text}</p></main></body></html>"

    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.deepseek.com":
            return fetch_handler(request)
        if request.url.host == "localhost":
            raise httpx.ConnectError("All connection attempts failed")
        return httpx.Response(200, text=page)

    return answer


def test_web_extract_uses_deepseek_fetch_only_when_named(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", TEST_KEY)

    def fetched(request):
        return httpx.Response(200, json={
            "title": "Fetched title",
            "content": f"# Fetched\n\nMarkdown body for {request.content.decode()}",
        })

    seen = _mock_transport(monkeypatch, _fetch_and_static(fetched))

    async def scenario():
        registry = ToolRegistry()
        register_web_tools(registry, None)
        result = await registry.execute("web_extract", {
            "urls": ["https://example.test/a", "https://example.test/b"],
            "provider": "deepseek",
        })

        payload = json.loads(result["output"])
        assert payload["provider_chain"][0] == "deepseek"
        assert payload["provider_chain"][-1] == "http"
        assert [item["backend"] for item in payload["results"]] == ["deepseek", "deepseek"]
        assert [item["url"] for item in payload["results"]] == ["https://example.test/a", "https://example.test/b"]
        assert payload["results"][0]["title"] == "Fetched title"
        assert "# Fetched" in payload["results"][0]["content"]
        fetches = [request for request in seen if request.url == FETCH_URL]
        assert sorted(request.body["url"] for request in fetches) == ["https://example.test/a", "https://example.test/b"]
        assert all(set(request.body) == {"url"} for request in fetches)
        assert all(request.headers["authorization"] == f"Bearer {TEST_KEY}" for request in fetches)
        assert len(seen) == 2

    run(scenario())


def test_web_extract_configured_deepseek_falls_back_per_url(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", TEST_KEY)
    monkeypatch.setenv("WEB_EXTRACT_PROVIDER", "deepseek")

    def fetched(request):
        if json.loads(request.content)["url"].endswith("/blocked"):
            return httpx.Response(403, json={"error": {"message": "refused by policy"}})
        return httpx.Response(200, json={"title": "", "content": "DeepSeek markdown body"})

    seen = _mock_transport(monkeypatch, _fetch_and_static(fetched))

    async def scenario():
        registry = ToolRegistry()
        register_web_tools(registry, None)
        result = await registry.execute("web_extract", {
            "urls": ["https://example.test/open", "https://example.test/blocked"],
        })

        payload = json.loads(result["output"])
        assert payload["provider_chain"][0] == "deepseek"
        opened, blocked = payload["results"]
        assert opened["backend"] == "deepseek"
        assert blocked["backend"] == "http"
        assert "Static page words." in blocked["content"]
        assert "deepseek" in blocked["fallback_from"]
        # A refusal is not repeated; the page goes on down the usual order.
        assert [request.url for request in seen].count(FETCH_URL) == 2

    run(scenario())


def test_web_extract_reports_a_deepseek_failure_when_nothing_reads_the_page(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", TEST_KEY)

    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.deepseek.com":
            return httpx.Response(402, json={"error": {"message": "Insufficient Balance"}})
        if request.url.host == "localhost":
            raise httpx.ConnectError("All connection attempts failed")
        return httpx.Response(404, text="gone")

    _mock_transport(monkeypatch, answer)

    async def scenario():
        registry = ToolRegistry()
        register_web_tools(registry, None)
        result = await registry.execute("web_extract", {
            "urls": ["https://example.test/gone"],
            "provider": "deepseek",
        })

        assert result["code"] == "extract_failed"
        assert "deepseek: HTTP 402 (insufficient balance; the DeepSeek account needs a top-up)" in result["error"]
        assert TEST_KEY not in json.dumps(result)

    run(scenario())


def test_web_extract_names_deepseek_but_skips_it_without_a_key(monkeypatch):
    seen = _mock_transport(monkeypatch, _fetch_and_static(_status(500)))

    async def scenario():
        registry = ToolRegistry()
        register_web_tools(registry, None)
        result = await registry.execute("web_extract", {"urls": ["https://example.test/a"], "provider": "deepseek"})

        payload = json.loads(result["output"])
        assert "deepseek" not in payload["provider_chain"]
        assert payload["results"][0]["backend"] == "http"
        assert all(request.url != FETCH_URL for request in seen)

    run(scenario())


class _RedirectingTools(BaseHTTPRequestHandler):
    requests: list[tuple[str, str, str]] = []

    def do_POST(self):  # noqa: N802 - http.server API
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        type(self).requests.append(("POST", self.path, self.headers.get("Authorization", "")))
        self.send_response(307)
        self.send_header("Location", "/collect")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):  # noqa: N802 - http.server API
        type(self).requests.append(("GET", self.path, self.headers.get("Authorization", "")))
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):  # noqa: A002 - http.server API
        pass


def test_deepseek_redirect_is_refused_without_httpx(monkeypatch):
    _RedirectingTools.requests = []
    server = HTTPServer(("127.0.0.1", 0), _RedirectingTools)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv("DEEPSEEK_API_KEY", TEST_KEY)
        monkeypatch.setenv("DEEPSEEK_TOOLS_URL", f"http://127.0.0.1:{server.server_port}/v1/tools/")
        monkeypatch.setattr(web_module, "httpx", None)

        async def scenario():
            registry = ToolRegistry()
            register_web_tools(registry, None)
            return await registry.execute("search_web", {"query": "agent runtimes", "provider": "deepseek"})

        result = run(scenario())
    finally:
        server.shutdown()
        server.server_close()

    assert result["code"] == "search_failed"
    assert "HTTP 307" in result["error"]
    # The key went only to the configured endpoint; the redirect target was never asked.
    assert _RedirectingTools.requests == [("POST", "/v1/tools/search", f"Bearer {TEST_KEY}")]
    assert TEST_KEY not in json.dumps(result)
