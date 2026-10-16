"""Tool access via MCP. Servers are declared in a standard `mcp.json` ({"mcpServers": {...}}).

In practice there is one entry: agentgateway, which multiplexes all connectors and prefixes
tool names with the connector name (e.g. `hackernews_hn_top_stories`).
"""

import inspect
import json
import os
import re
import time
from typing import Any

from mcp import Client
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.toolsets import AbstractToolset

from app.config import get_settings

_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
_catalog_cache: tuple[float, list[dict]] | None = None


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


def servers() -> dict[str, dict]:
    """HTTP MCP servers from mcp.json (env vars expanded)."""
    data = _expand(json.loads(get_settings().mcp_config.read_text()))
    return {name: cfg for name, cfg in data["mcpServers"].items() if cfg.get("url")}


def toolsets(*, bot: str | None, allowed: list[str] | None) -> list[AbstractToolset]:
    """MCP toolsets for an agent, limited to `allowed` tool names (None = all, [] = none)."""
    if allowed == []:
        return []
    result: list[AbstractToolset] = []
    for name, cfg in servers().items():
        headers = dict(cfg.get("headers") or {})
        if bot:
            headers["X-Bot-Id"] = bot  # identity for gateway logs now, authz (JWT) later
        ts: AbstractToolset = MCPToolset(cfg["url"], id=name, headers=headers, max_retries=3)
        if allowed is not None:
            allow = set(allowed)
            ts = ts.filtered(lambda _ctx, tool_def, allow=allow: tool_def.name in allow)
        result.append(ts)
    return result


async def catalog(refresh: bool = False) -> list[dict]:
    """All tools reachable through the gateway: name, connector, description, read-only hint."""
    global _catalog_cache
    if not refresh and _catalog_cache and time.monotonic() - _catalog_cache[0] < 60:
        return _catalog_cache[1]
    tools: list[dict] = []
    for name, cfg in servers().items():
        async with Client(cfg["url"]) as client:
            listed = await client.list_tools()
        for t in listed.tools:
            connector, _, short = t.name.partition("_")
            tools.append(
                {
                    "name": t.name,
                    "connector": connector if short else name,
                    "description": inspect.cleandoc(t.description or ""),
                    "read_only": bool(t.annotations and t.annotations.read_only_hint),
                    "input_schema": t.input_schema,
                }
            )
    _catalog_cache = (time.monotonic(), tools)
    return tools


def expand_tool_refs(refs: list[str], names: list[str]) -> list[str]:
    """Turn refs like "web" (a whole connector) or "web_fetch_url" into concrete tool names."""
    out: list[str] = []
    for ref in refs:
        if ref in names:
            out.append(ref)
        else:
            out.extend(n for n in names if n.startswith(f"{ref}_"))
    return list(dict.fromkeys(out))
