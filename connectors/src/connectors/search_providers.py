"""Pluggable web search backends, selected with WEB_SEARCH_PROVIDER."""

import asyncio
import os

import httpx


async def duckduckgo(query: str, max_results: int) -> list[dict]:
    from ddgs import DDGS

    def run():
        return DDGS().text(query, max_results=max_results)

    hits = await asyncio.to_thread(run)
    return [{"title": h.get("title"), "url": h.get("href"), "snippet": h.get("body")} for h in hits]


async def tavily(query: str, max_results: int) -> list[dict]:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            "https://api.tavily.com/search",
            headers={"Authorization": f"Bearer {os.environ['TAVILY_API_KEY']}"},
            json={"query": query, "max_results": max_results},
        )
        r.raise_for_status()
    return [
        {"title": h.get("title"), "url": h.get("url"), "snippet": h.get("content")}
        for h in r.json().get("results", [])
    ]


async def brave(query: str, max_results: int) -> list[dict]:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            "https://api.search.brave.com/res/v1/web/search",
            headers={"X-Subscription-Token": os.environ["BRAVE_API_KEY"], "Accept": "application/json"},
            params={"q": query, "count": max_results},
        )
        r.raise_for_status()
    return [
        {"title": h.get("title"), "url": h.get("url"), "snippet": h.get("description")}
        for h in r.json().get("web", {}).get("results", [])
    ]


async def searxng(query: str, max_results: int) -> list[dict]:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            f"{os.environ['SEARXNG_URL'].rstrip('/')}/search",
            params={"q": query, "format": "json"},
        )
        r.raise_for_status()
    return [
        {"title": h.get("title"), "url": h.get("url"), "snippet": h.get("content")}
        for h in r.json().get("results", [])[:max_results]
    ]


PROVIDERS = {"duckduckgo": duckduckgo, "tavily": tavily, "brave": brave, "searxng": searxng}


async def search(query: str, max_results: int) -> list[dict]:
    name = os.environ.get("WEB_SEARCH_PROVIDER", "duckduckgo")
    try:
        provider = PROVIDERS[name]
    except KeyError:
        raise ValueError(f"Unknown WEB_SEARCH_PROVIDER {name!r}, choose from {sorted(PROVIDERS)}")
    return await provider(query, max_results)
