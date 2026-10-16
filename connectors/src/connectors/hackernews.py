"""Hacker News connector (Algolia HN API, no key needed)."""

import time

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

ALGOLIA = "https://hn.algolia.com/api/v1"
READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)

server = MCPServer(
    "hackernews",
    instructions="Read Hacker News stories and comment threads.",
)


def _story(hit: dict) -> dict:
    oid = hit.get("objectID") or hit.get("id")
    return {
        "id": int(oid),
        "title": hit.get("title"),
        "url": hit.get("url") or f"https://news.ycombinator.com/item?id={oid}",
        "points": hit.get("points"),
        "comments": hit.get("num_comments"),
        "author": hit.get("author"),
        "created_at": hit.get("created_at"),
        "hn_url": f"https://news.ycombinator.com/item?id={oid}",
    }


@server.tool(annotations=READ_ONLY)
async def hn_top_stories(
    since_hours: int = 24,
    min_points: int = 10,
    limit: int = 150,
    query: str | None = None,
) -> list[dict]:
    """List Hacker News stories posted in the last `since_hours`, sorted by points (highest first).

    Returns a broad, compact list (id, title, url, points, comments). Filter for relevance
    yourself; `query` is an optional full-text filter and misses synonyms, so prefer leaving it empty.
    """
    since = int(time.time()) - since_hours * 3600
    params = {
        "tags": "story",
        "numericFilters": f"created_at_i>{since},points>={min_points}",
        "hitsPerPage": 1000,
    }
    if query:
        params["query"] = query
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{ALGOLIA}/search_by_date", params=params)
        r.raise_for_status()
    stories = [_story(h) for h in r.json()["hits"]]
    stories.sort(key=lambda s: s["points"] or 0, reverse=True)
    return stories[: max(1, min(limit, 500))]


def _comments(children: list[dict], depth: int, max_depth: int, budget: list[int]) -> list[dict]:
    out = []
    for c in children:
        if budget[0] <= 0:
            break
        if not c.get("text"):
            continue
        budget[0] -= 1
        item = {"author": c.get("author"), "text": c["text"][:1500]}
        if depth < max_depth and c.get("children"):
            item["replies"] = _comments(c["children"], depth + 1, max_depth, budget)
        out.append(item)
    return out


@server.tool(annotations=READ_ONLY)
async def hn_item(id: int, max_comments: int = 30) -> dict:
    """Get one Hacker News story with its top comments (nested two levels deep)."""
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{ALGOLIA}/items/{id}")
        r.raise_for_status()
    data = r.json()
    story = _story(data)
    story["text"] = data.get("text")
    story["comments"] = _comments(data.get("children", []), 0, 1, [max_comments])
    return story
