"""Web connector: search (pluggable provider) and readable page fetch."""

import asyncio
import ipaddress
import socket

import httpx
import trafilatura
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from connectors import search_providers

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
USER_AGENT = "Mozilla/5.0 (compatible; agentic-bots/0.1)"
MAX_REDIRECTS = 5

server = MCPServer("web", instructions="Search the web and read web pages.")


@server.tool(annotations=READ_ONLY)
async def web_search(query: str, max_results: int = 10) -> dict:
    """Search the web. Returns title, url and snippet per result.

    No hits or a provider hiccup (rate limit) is reported in `note`, not raised: rephrase or move on.
    """
    try:
        results = await search_providers.search(query, max(1, min(max_results, 20)))
    except Exception as e:  # ddgs raises on zero hits; providers rate-limit
        msg = str(e)
        note = "no results, try a shorter or different query" if "No results" in msg else f"search failed: {msg[:200]}"
        return {"query": query, "results": [], "note": note}
    return {"query": query, "results": results, "note": None if results else "no results, try a different query"}


class Blocked(Exception):
    """The URL points at a non-public address (this network, the host, cloud metadata, ...)."""


def _public(ip: str) -> bool:
    addr = ipaddress.ip_address(ip.split("%")[0])
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return addr.is_global


async def _check(url: httpx.URL) -> None:
    """Public internet only: a prompt-injected page must not make a bot read the gateway admin API,
    other containers or services on the host (SSRF). Every resolved address must be public."""
    if url.scheme not in ("http", "https") or not url.host:
        raise Blocked(f"only http(s) URLs: {url}")
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(url.host, url.port or (443 if url.scheme == "https" else 80),
                                                             type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise httpx.ConnectError(f"cannot resolve {url.host}: {e}") from e
    if not infos or not all(_public(info[4][0]) for info in infos):
        raise Blocked(f"{url.host} is not a public address")


async def _get(client: httpx.AsyncClient, url: str) -> httpx.Response:
    """GET with redirects followed by hand, so every hop is checked, and the connected peer checked too
    (a DNS answer that changes between the check and the connect)."""
    target = httpx.URL(url)
    for _ in range(MAX_REDIRECTS + 1):
        await _check(target)
        r = await client.send(client.build_request("GET", target), stream=True)
        stream = r.extensions.get("network_stream")
        peer = stream.get_extra_info("server_addr") if stream else None
        if peer and not _public(peer[0]):
            await r.aclose()
            raise Blocked(f"{target.host} is not a public address")
        if r.is_redirect and "location" in r.headers:
            await r.aclose()
            target = target.join(r.headers["location"])
            continue
        await r.aread()
        return r
    raise httpx.TooManyRedirects(f"more than {MAX_REDIRECTS} redirects")


@server.tool(annotations=READ_ONLY)
async def fetch_url(url: str, max_chars: int = 8000) -> dict:
    """Fetch a public web page and return its main readable text (boilerplate removed), truncated to max_chars."""
    try:
        async with httpx.AsyncClient(timeout=30, headers={"User-Agent": USER_AGENT}) as client:
            r = await _get(client, url)
            r.raise_for_status()
    except Blocked as e:
        return {"url": url, "error": f"refused: {e}. Only public web pages can be fetched."}
    except httpx.HTTPStatusError as e:  # paywalls / bot blocks are normal: report, don't crash the agent turn
        return {"url": url, "error": f"HTTP {e.response.status_code}, page not readable; use other sources"}
    except (httpx.HTTPError, httpx.InvalidURL, ValueError) as e:
        return {"url": url, "error": f"fetch failed: {type(e).__name__}: {str(e)[:200]}"}
    ctype = r.headers.get("content-type", "")
    if "html" in ctype:
        text = trafilatura.extract(r.text, include_links=False, include_comments=False) or ""
        meta = trafilatura.extract_metadata(r.text)
        title = meta.title if meta else None
    else:
        text, title = r.text, None
    return {
        "url": str(r.url),
        "title": title,
        "text": text[:max_chars],
        "truncated": len(text) > max_chars,
    }
