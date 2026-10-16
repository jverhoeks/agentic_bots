"""Web connector: search (pluggable provider) and readable page fetch."""

import httpx
import trafilatura
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from connectors import search_providers

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
USER_AGENT = "Mozilla/5.0 (compatible; agentic-bots/0.1)"

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


@server.tool(annotations=READ_ONLY)
async def fetch_url(url: str, max_chars: int = 8000) -> dict:
    """Fetch a web page and return its main readable text (boilerplate removed), truncated to max_chars."""
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
            r = await client.get(url)
            r.raise_for_status()
    except httpx.HTTPStatusError as e:  # paywalls / bot blocks are normal: report, don't crash the agent turn
        return {"url": url, "error": f"HTTP {e.response.status_code}, page not readable; use other sources"}
    except httpx.HTTPError as e:
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
