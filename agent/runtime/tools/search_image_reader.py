"""Bounded downloads of selected image-search candidates for native vision."""

import asyncio
import hashlib
import tempfile
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable
from urllib.parse import urljoin

from PIL import Image, UnidentifiedImageError

if TYPE_CHECKING:
    import httpx


MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_PIXELS = 16_000_000


class SearchImageError(ValueError):
    """A user-facing download failure that does not expose transport secrets.

    ``retryable`` is true when the same download can succeed later (a timeout,
    a dropped connection or a server error).
    """

    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


def _save_raster(data: bytes, directory: Path) -> Path:
    try:
        with Image.open(BytesIO(data)) as picture:
            if picture.format not in {"PNG", "JPEG", "WEBP", "GIF", "AVIF"}:
                raise SearchImageError("Unsupported image format.")
            if picture.width * picture.height > MAX_IMAGE_PIXELS:
                raise SearchImageError("Image exceeds the 16 megapixel inspection limit.")
            picture.load()
            extension = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp", "GIF": "gif", "AVIF": "png"}[picture.format]
            if picture.format == "AVIF":
                # The existing attachment transport accepts PNG but not AVIF.
                # Keep original dimensions; the normal vision policy owns tiling.
                converted = BytesIO()
                picture.save(converted, format="PNG")
                data = converted.getvalue()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise SearchImageError("The response is not a decodable supported image.") from exc
    if len(data) > MAX_IMAGE_BYTES:
        raise SearchImageError("Image exceeds the 8 MiB inspection limit.")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"search-{hashlib.sha256(data).hexdigest()}.{extension}"
    # A complete file is published atomically, including when two reads overlap.
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=directory, delete=False) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(data)
        temporary_path.replace(path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return path.resolve()


async def load_search_image(
    url: str,
    directory: Path,
    is_public_url: Callable[[str], Awaitable[bool]],
    client_for_url: Callable[[str], "httpx.AsyncClient"],
    is_transient: Callable[[BaseException], bool] | None = None,
) -> Path:
    """Follow at most three checked redirects and never fetch an unbounded body."""
    try:
        async with asyncio.timeout(12):
            for _ in range(4):
                if not await is_public_url(url):
                    raise SearchImageError("Image URL or redirect is not public.")
                client = client_for_url(url)
                async with client.stream("GET", url, timeout=6, follow_redirects=False) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise SearchImageError("Image redirect has no destination.")
                        url = urljoin(url, location)
                        continue
                    if response.status_code != 200:
                        raise SearchImageError(
                            f"Image download returned HTTP {response.status_code}.",
                            retryable=response.status_code >= 500 or response.status_code in {408, 425, 429},
                        )
                    length = response.headers.get("content-length", "")
                    if length.isdigit() and int(length) > MAX_IMAGE_BYTES:
                        raise SearchImageError("Image exceeds the 8 MiB inspection limit.")
                    body = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                        body.extend(chunk)
                        if len(body) > MAX_IMAGE_BYTES:
                            raise SearchImageError("Image exceeds the 8 MiB inspection limit.")
                    return await asyncio.to_thread(_save_raster, bytes(body), directory)
            raise SearchImageError("Image redirect limit exceeded.")
    except SearchImageError:
        raise
    except TimeoutError as exc:
        raise SearchImageError("Image download timed out; try another candidate.", retryable=True) from exc
    except Exception as exc:
        raise SearchImageError(
            "Could not load this image; try another candidate.",
            retryable=bool(is_transient and is_transient(exc)),
        ) from exc
