import asyncio
from io import BytesIO

import httpx
import pytest
from PIL import Image

from agent.runtime.tools import search_image_reader as reader


def png_bytes():
    output = BytesIO()
    Image.new("RGB", (12, 8), (7, 133, 249)).save(output, format="PNG")
    return output.getvalue()


async def public(url):
    return "127.0.0.1" not in url


def test_download_preserves_actual_pixels_and_format_through_checked_redirect(tmp_path):
    requested = []
    checked = []

    def respond(request):
        requested.append(str(request.url))
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "/actual.jpg"})
        return httpx.Response(200, content=png_bytes())

    async def check(url):
        checked.append(url)
        return await public(url)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            path = await reader.load_search_image("https://images.example/start", tmp_path, check, lambda _: client)
        assert path.suffix == ".png"  # Raster bytes, not the URL extension, determine MIME.
        assert path.read_bytes() == png_bytes()
        with Image.open(path) as picture:
            assert picture.size == (12, 8) and picture.getpixel((0, 0)) == (7, 133, 249)
        assert checked == requested == ["https://images.example/start", "https://images.example/actual.jpg"]

    asyncio.run(scenario())


@pytest.mark.parametrize("start", ["https://images.example/start", "http://127.0.0.1/private.png"])
def test_private_initial_url_and_redirect_are_never_fetched(tmp_path, start):
    requested = []

    def respond(request):
        requested.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private.png"})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with pytest.raises(reader.SearchImageError, match="not public"):
                await reader.load_search_image(start, tmp_path, public, lambda _: client)
        assert all("127.0.0.1" not in url for url in requested)
        assert not list(tmp_path.iterdir())

    asyncio.run(scenario())


@pytest.mark.parametrize("case", ["html", "http_error", "redirect_loop", "length", "chunked", "pixels", "timeout"])
def test_invalid_oversized_and_failed_downloads_leave_no_artifacts(tmp_path, monkeypatch, case):
    sent = []
    if case in {"length", "chunked"}:
        monkeypatch.setattr(reader, "MAX_IMAGE_BYTES", 64)
    if case == "pixels":
        monkeypatch.setattr(reader, "MAX_IMAGE_PIXELS", 50)

    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(10):
                sent.append(1)
                yield b"x" * 64 * 1024

    def respond(request):
        if case == "html":
            return httpx.Response(200, headers={"content-type": "image/png"}, content=b"<html>not an image</html>")
        if case == "http_error":
            return httpx.Response(403)
        if case == "redirect_loop":
            return httpx.Response(302, headers={"location": "/again"})
        if case == "length":
            return httpx.Response(200, headers={"content-length": "1000"}, content=b"x")
        if case == "chunked":
            return httpx.Response(200, stream=Chunks())
        if case == "timeout":
            raise TimeoutError("internal token must not be exposed")
        return httpx.Response(200, content=png_bytes())

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with pytest.raises(reader.SearchImageError) as failure:
                await reader.load_search_image("https://images.example/image", tmp_path, public, lambda _: client)
            assert "internal token" not in str(failure.value)
        assert not list(tmp_path.iterdir())
        if case == "chunked":
            assert len(sent) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("answer,retryable", [
    (403, False), (404, False), (429, True), (503, True), ("timeout", True), ("refused", False),
])
def test_download_failure_records_whether_a_retry_can_help(tmp_path, answer, retryable):
    def respond(request):
        if answer == "timeout":
            raise TimeoutError("slow origin")
        if answer == "refused":
            raise httpx.ConnectError("All connection attempts failed")
        return httpx.Response(answer)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with pytest.raises(reader.SearchImageError) as failure:
                await reader.load_search_image("https://images.example/image", tmp_path, public, lambda _: client)
            assert failure.value.retryable is retryable
            if answer == "refused":
                # Only the caller's transport knows which of its errors are transient.
                with pytest.raises(reader.SearchImageError) as classified:
                    await reader.load_search_image(
                        "https://images.example/image", tmp_path, public, lambda _: client,
                        lambda exc: isinstance(exc, httpx.ConnectError),
                    )
                assert classified.value.retryable is True
                assert str(classified.value) == str(failure.value)

    asyncio.run(scenario())
