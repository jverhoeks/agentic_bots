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
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.toolsets import AbstractToolset

from app import tokens
from app.config import get_settings

_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
_catalog_cache: dict[str | None, tuple[float, list[dict]]] = {}


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


def _headers(cfg: dict, user: str | None, bot: str | None = None, tools: list[str] | None = None) -> dict[str, str]:
    """Signed identity for the gateway: it maps `sub` to the user's connector credentials (`apps`)
    and only lets the token list and call `tools` (the bot's allowed-tools), when given."""
    headers = dict(cfg.get("headers") or {})
    headers["Authorization"] = f"Bearer {tokens.issue(user, bot, tools=tools)}"
    if bot:
        headers["X-Bot-Id"] = bot  # for gateway logs; the token's `bot` claim is the trusted copy
    return headers


def toolsets(*, bot: str | None, allowed: list[str] | None, user: str | None = None) -> list[AbstractToolset]:
    """MCP toolsets for an agent, limited to `allowed` tool names (None = all, [] = none).

    `user` is the bot's owner, not the person watching: runs also happen in the background
    (A2A, schedules), and they act with the owner's connections.
    """
    if allowed == []:
        return []
    result: list[AbstractToolset] = []
    for name, cfg in servers().items():
        headers = _headers(cfg, user, bot, allowed)
        ts: AbstractToolset = MCPToolset(cfg["url"], id=name, headers=headers, max_retries=3)
        if allowed is not None:
            allow = set(allowed)
            ts = ts.filtered(lambda _ctx, tool_def, allow=allow: tool_def.name in allow)
        result.append(ts)
    return result


async def catalog(refresh: bool = False, user: str | None = None) -> list[dict]:
    """Tools reachable through the gateway for `user`: name, connector, description, read-only hint."""
    cached = _catalog_cache.get(user)
    if not refresh and cached and time.monotonic() - cached[0] < 60:
        return cached[1]
    tools: list[dict] = []
    for name, cfg in servers().items():
        transport = streamable_http_client(cfg["url"], http_client=create_mcp_http_client(headers=_headers(cfg, user)))
        async with Client(transport) as client:
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
    from app.agents import a2a  # built-in connector, served in-process rather than via the gateway

    tools.extend(a2a.catalog())
    _catalog_cache[user] = (time.monotonic(), tools)
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
