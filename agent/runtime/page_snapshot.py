"""A picture of a page, asked of the client that shows pages.

A page is an HTML file the model wrote to be looked at. Only the desktop renders one, in an
isolated frame of its own, so the runtime cannot draw it: it asks the client for a picture and
waits for the answer. The request is an event like any other; the answer is a command.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

PAGE_EXTENSIONS = frozenset({".html", ".htm"})
# The desktop renders a page whole or not at all, and shows a larger file as source.
MAX_PAGE_BYTES = 1024 * 1024
PAGE_SNAPSHOT_TIMEOUT_SECONDS = 20.0


class PageSnapshotError(Exception):
    """The client could not, or did not, return a picture."""


@dataclass(frozen=True)
class PageSnapshot:
    """Where the client saved the picture, and what the picture leaves out."""

    image_path: Path
    # How much of the page the picture shows, and how tall the page is; zero when the client did not say.
    height: int = 0
    page_height: int = 0
    # What a script on the page raised while it was drawn, in the page's own words.
    script_error: str = ""


def _pixels(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 < value <= 1_000_000 else 0


def is_page_path(path: str) -> bool:
    return Path(str(path).strip().strip("\"'")).suffix.lower() in PAGE_EXTENSIONS


class PageSnapshotBroker:
    """Pairs each request for a picture with the client's answer to it."""

    def __init__(self, send: Callable[[dict], None], *, timeout: float = PAGE_SNAPSHOT_TIMEOUT_SECONDS) -> None:
        self._send = send
        self._timeout = timeout
        self._pending: dict[str, asyncio.Future[PageSnapshot]] = {}

    async def request(self, path: Path) -> PageSnapshot:
        """Ask for a picture of the page at ``path``; return what the client saved."""
        request_id = uuid.uuid4().hex
        future: asyncio.Future[PageSnapshot] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            self._send({"type": "page_snapshot_request", "request_id": request_id, "path": str(path)})
            return await asyncio.wait_for(future, self._timeout)
        except asyncio.TimeoutError:
            raise PageSnapshotError("The desktop did not return a picture of the page in time.") from None
        finally:
            self._pending.pop(request_id, None)

    def resolve(self, command: dict) -> bool:
        """Settle the request a client's answer names. False when nothing is waiting for it."""
        future = self._pending.get(str(command.get("request_id") or ""))
        if future is None or future.done():
            return False
        error = command.get("error")
        image_path = command.get("image_path")
        if isinstance(error, str) and error.strip():
            future.set_exception(PageSnapshotError(error.strip()[:500]))
        elif isinstance(image_path, str) and image_path.strip():
            script_error = command.get("script_error")
            future.set_result(PageSnapshot(
                image_path=Path(image_path.strip()),
                height=_pixels(command.get("height")),
                page_height=_pixels(command.get("page_height")),
                script_error=script_error.strip()[:300] if isinstance(script_error, str) else "",
            ))
        else:
            future.set_exception(PageSnapshotError("The desktop returned no picture of the page."))
        return True
