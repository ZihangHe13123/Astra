"""Bounded requests for untrusted page URLs; configured service endpoints use their own clients."""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
import ipaddress
import socket
import ssl
import zlib
from typing import Any
from urllib.parse import urljoin, urlsplit

from .tools.approval import normalized_origin

private_url_origins: ContextVar[frozenset[str]] = ContextVar("private_url_origins", default=frozenset())
MAX_PAGE_BYTES = 8 * 1024 * 1024
MAX_REDIRECTS = 5


class PageTLSContext(ssl.SSLContext):
    """Verify the original hostname when a proxy tunnels to a pinned IP.

    httpcore's direct connection honors sni_hostname, but its proxy tunnel uses
    the CONNECT host. Each page origin therefore owns a separate client/pool.
    """
    def __new__(cls, hostname: str):
        return super().__new__(cls, ssl.PROTOCOL_TLS_CLIENT)

    def __init__(self, hostname: str):
        import certifi
        self.hostname = hostname
        self.load_verify_locations(cafile=certifi.where())

    def wrap_bio(self, incoming, outgoing, server_side=False, server_hostname=None, session=None):
        if server_hostname:
            try:
                ipaddress.ip_address(server_hostname)
            except ValueError:
                pass
            else:
                server_hostname = self.hostname
        return super().wrap_bio(incoming, outgoing, server_side=server_side,
                                server_hostname=server_hostname, session=session)


async def resolve_page_target(url: str) -> tuple[str, str]:
    """Return a validated address and original HTTP Host. Connect to this address, without resolving again."""
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Invalid page URL (HTTP(S), without credentials, required)")
    host = parsed.hostname.rstrip(".").lower()
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    permitted = normalized_origin(url) in private_url_origins.get()
    if not permitted and (host == "localhost" or host.endswith((".localhost", ".local", ".internal"))):
        raise ValueError("Private page target requires explicit approval")
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, port, type=socket.SOCK_STREAM)
        addresses = list(dict.fromkeys(ipaddress.ip_address(info[4][0]) for info in infos))
    if not addresses:
        raise ValueError("Page hostname did not resolve")
    if not permitted and any(not address.is_global or address.is_multicast for address in addresses):
        raise ValueError("Private page target requires explicit approval")
    # Reject mixed public/private DNS answers as well. A single approved address is pinned.
    address = next((str(value) for value in addresses if value.version == 4), str(addresses[0]))
    authority = f"[{host}]" if ":" in host else host
    if parsed.port is not None:
        authority += f":{parsed.port}"
    return address, authority


async def fetch_page_text(client: Any, url: str, *, timeout: float, same_origin: bool = False,
                          max_bytes: int = MAX_PAGE_BYTES) -> str:
    """Check every hop, pin DNS, preserve TLS/Host, and bound decoded response bytes."""
    import httpx

    original = normalized_origin(url)
    async with asyncio.timeout(timeout):
        for hop in range(MAX_REDIRECTS + 1):
            address, authority = await resolve_page_target(url)
            target = httpx.URL(url).copy_with(host=address)
            # httpcore uses sni_hostname for TLS and certificate validation; the TCP/CONNECT target
            # remains the pinned IP, including when using a configured HTTP proxy.
            active_client: Any = client(url) if callable(client) else client
            async with active_client.stream("GET", target, timeout=timeout, follow_redirects=False,
                                     headers={"Host": authority, "Accept-Encoding": "identity"},
                                     extensions={"sni_hostname": urlsplit(url).hostname}) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError("Redirect response has no Location")
                    destination = urljoin(url, location)
                    if same_origin and normalized_origin(destination) != original:
                        raise ValueError(f"Cross-origin redirect blocked before request: {url} -> {destination}")
                    if hop == MAX_REDIRECTS:
                        raise ValueError("Too many page redirects")
                    url = destination
                    continue
                response.raise_for_status()
                encoding = response.headers.get("content-encoding", "identity").lower().strip()
                if encoding not in {"", "identity", "gzip", "deflate"}:
                    raise ValueError("Unsupported page content encoding")
                decoder = zlib.decompressobj(31 if encoding == "gzip" else 15) if encoding in {"gzip", "deflate"} else None
                chunks, size, wire_size = [], 0, 0
                async for raw in response.aiter_raw(chunk_size=64 * 1024):
                    wire_size += len(raw)
                    if wire_size > max_bytes:
                        raise ValueError(f"Page response exceeds {max_bytes} bytes")
                    chunk = decoder.decompress(raw, max_bytes - size + 1) if decoder else raw
                    size += len(chunk)
                    if size > max_bytes:
                        raise ValueError(f"Page response exceeds {max_bytes} bytes")
                    chunks.append(chunk)
                if decoder is not None and not decoder.eof:
                    raise ValueError("Incomplete compressed page response")
                return b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
    raise ValueError("No page response")
