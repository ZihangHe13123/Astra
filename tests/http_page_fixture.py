"""Adapt extraction-only fake HTTP clients to the streamed, DNS-pinned page transport.

These tests supply response bodies/errors; perimeter behavior has independent
real-transport tests in test_public_fetch.py. No fixture host is resolved online.
"""
from contextlib import asynccontextmanager
from urllib.parse import urlsplit


def install_page_client(monkeypatch, web_module, client_class):
    async def target(url):
        parsed = urlsplit(url)
        return "93.184.216.34", parsed.netloc

    @asynccontextmanager
    async def stream(self, method, url, *, timeout, headers, **kwargs):
        host = urlsplit("//" + headers["Host"]).hostname
        original = str(url.copy_with(host=host))
        response = await self.get(original, timeout=timeout)
        final = str(getattr(response, "url", "") or original)
        redirected = final.rstrip("/") != original.rstrip("/")

        class Response:
            status_code = 302 if redirected else getattr(response, "status_code", 200)
            encoding = "utf-8"
            headers = {"location": final} if redirected else {}

            def raise_for_status(self):
                check = getattr(response, "raise_for_status", None)
                if check:
                    check()

            async def aiter_raw(self, **kwargs):
                yield str(response.text).encode()

        yield Response()

    monkeypatch.setattr("agent.runtime.public_fetch.resolve_page_target", target)
    monkeypatch.setattr(client_class, "stream", stream, raising=False)
    monkeypatch.setattr(web_module.httpx, "AsyncClient", client_class)
