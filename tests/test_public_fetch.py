import asyncio
import gzip
import socket
import ssl

import httpx
import pytest

from agent.runtime.public_fetch import PageTLSContext, fetch_page_text, private_url_origins, resolve_page_target
from agent.runtime.tools import web
from agent.runtime.tools.registry import ToolRegistry


def test_unapproved_private_fetch_never_dispatches_even_without_a_handler(monkeypatch):
    async def run():
        registry = ToolRegistry()
        web.register_web_tools(registry, object())
        result = await registry.execute("fetch_url", {"url": "http://127.0.0.1:1/private"})
        assert result["code"] == "approval_required" and result["not_executed"]
    asyncio.run(run())


def test_redirect_is_rejected_before_private_target_and_origin_grant_is_scoped():
    async def run():
        seen = []
        def answer(request):
            seen.append(request)
            return httpx.Response(302, headers={"location": "http://localhost:9999/private"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
            token = private_url_origins.set(frozenset({"http://127.0.0.1:9999"}))
            try:
                with pytest.raises(ValueError, match="Private page"):
                    await fetch_page_text(client, "http://127.0.0.1:9999/start", timeout=2)
            finally:
                private_url_origins.reset(token)
        assert len(seen) == 1 and seen[0].url.path == "/start"
        with pytest.raises(ValueError, match="Private page"):
            await resolve_page_target("http://127.0.0.1:9999/start")
    asyncio.run(run())


def test_validated_dns_is_pinned_and_mixed_answers_are_rejected(monkeypatch):
    async def run():
        dns, requests = [], []
        def resolve(host, port, **kwargs):
            dns.append(host)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]
        monkeypatch.setattr(socket, "getaddrinfo", resolve)
        def answer(request):
            requests.append(request)
            return httpx.Response(200, stream=httpx.ByteStream(b"page"))
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
            text = await fetch_page_text(client, "https://public.example.test/page", timeout=2)
        assert text == "page" and dns == ["public.example.test"]
        assert requests[0].url.host == "93.184.216.34"
        assert requests[0].headers["host"] == "public.example.test"
        assert requests[0].extensions["sni_hostname"] == "public.example.test"
        monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: resolve(*args, **kwargs) + [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))])
        with pytest.raises(ValueError, match="Private page"):
            await resolve_page_target("https://public.example.test/page")
    asyncio.run(run())


@pytest.mark.parametrize("compressed", [False, True])
def test_download_and_decompression_are_bounded(compressed):
    async def run():
        data = b"x" * 100_000
        headers = {"content-encoding": "gzip"} if compressed else {}
        if compressed:
            data = gzip.compress(data)
        def answer(request):
            return httpx.Response(200, headers=headers, stream=httpx.ByteStream(data))
        token = private_url_origins.set(frozenset({"http://127.0.0.1"}))
        try:
            async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
                with pytest.raises(ValueError, match="exceeds"):
                    await fetch_page_text(client, "http://127.0.0.1/", timeout=2, max_bytes=1024)
        finally:
            private_url_origins.reset(token)
    asyncio.run(run())


def test_tls_to_a_pinned_address_still_verifies_original_hostname():
    context = PageTLSContext("public.example.test")
    tls = context.wrap_bio(ssl.MemoryBIO(), ssl.MemoryBIO(), server_hostname="93.184.216.34")
    assert tls.server_hostname == "public.example.test"
    assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
