"""Safe page fetching: http(s) only, no private networks, bounded size, manual redirects."""

from __future__ import annotations

import asyncio
import codecs
import ipaddress
import os
import socket
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

import httpx
from mcp.server.mcpserver.exceptions import ToolError

MAX_BYTES = int(os.environ.get("WEB_INTEL_MAX_BYTES", 3_000_000))
TIMEOUT = float(os.environ.get("WEB_INTEL_TIMEOUT", 15))
TOTAL_TIMEOUT = float(os.environ.get("WEB_INTEL_TOTAL_TIMEOUT", 30))  # whole fetch, incl. redirects
MAX_REDIRECTS = 5
USER_AGENT = os.environ.get("WEB_INTEL_USER_AGENT", "WebIntelMCP/0.1")


class FetchError(ToolError):
    """Bad input or unreachable page; the client sees the message as an isError result."""


@dataclass
class Page:
    url: str
    final_url: str
    status: int
    headers: dict[str, str]
    text: str
    elapsed_ms: int
    redirects: list[str] = field(default_factory=list)
    truncated: bool = False


def _allow_private() -> bool:
    return os.environ.get("WEB_INTEL_ALLOW_PRIVATE") == "1"


def validate_url(url: str) -> str:
    url = (url or "").strip()
    if "://" not in url:
        url = "https://" + url
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise FetchError(f"Only http(s) URLs are supported: {url!r}")
    return url


def _check_peer(resp: httpx.Response) -> None:
    """Re-check the address actually connected to (defeats DNS rebinding between check and connect)."""
    if _allow_private():
        return
    stream = resp.extensions.get("network_stream")
    addr = stream.get_extra_info("server_addr") if stream is not None else None
    if addr and not ipaddress.ip_address(addr[0]).is_global:
        raise FetchError("The host resolved to a non-public address on connect; refused.")


async def _check_host(host: str) -> None:
    if _allow_private():
        return
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, None)
    except socket.gaierror as exc:
        raise FetchError(f"Cannot resolve host {host!r}.") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise FetchError(f"Host {host!r} resolves to a non-public address; refused.")


def _encoding(name: str | None) -> str:
    try:
        return codecs.lookup(name or "utf-8").name
    except LookupError:
        return "utf-8"


async def fetch(url: str) -> Page:
    """Fetch with a hard deadline, so a server that drips bytes cannot hold a worker forever."""
    url = validate_url(url)
    try:
        return await asyncio.wait_for(_fetch(url), TOTAL_TIMEOUT)
    except asyncio.TimeoutError as exc:
        raise FetchError(f"{url} took longer than {TOTAL_TIMEOUT:g}s; gave up.") from exc


async def _fetch(url: str) -> Page:
    current, redirects = url, []
    start = time.perf_counter()
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False,
                                 headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*;q=0.8"}) as client:
        for _ in range(MAX_REDIRECTS + 1):
            await _check_host(urlsplit(current).hostname or "")
            try:
                async with client.stream("GET", current) as resp:
                    _check_peer(resp)
                    if resp.is_redirect and "location" in resp.headers:
                        redirects.append(current)
                        current = validate_url(urljoin(current, resp.headers["location"]))
                        continue
                    buf, truncated = bytearray(), False
                    async for chunk in resp.aiter_bytes():
                        buf.extend(chunk)
                        if len(buf) > MAX_BYTES:
                            truncated = True
                            del buf[MAX_BYTES:]
                            break
                    enc = _encoding(resp.charset_encoding)
                    return Page(url=url, final_url=current, status=resp.status_code,
                                headers={k.lower(): v for k, v in resp.headers.items()},
                                text=buf.decode(enc, errors="replace"),
                                elapsed_ms=int((time.perf_counter() - start) * 1000),
                                redirects=redirects, truncated=truncated)
            except httpx.HTTPError as exc:
                raise FetchError(f"Could not fetch {current}: {exc.__class__.__name__}: {exc}") from exc
    raise FetchError(f"Too many redirects (>{MAX_REDIRECTS}) starting at {url}.")
