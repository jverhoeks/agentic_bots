"""Smoke test an MCP endpoint: list tools and optionally call one.

uv run --project connectors python scripts/mcp_smoke.py http://localhost:3000/mcp [tool] ['{"json": "args"}']
The gateway needs a token: MCP_TOKEN=$(cd backend && uv run python -m app.tokens token <user>) (`make smoke` does it).
"""

import asyncio
import json
import os
import sys

import httpx
from mcp import Client
from mcp.client.streamable_http import streamable_http_client


async def main(url: str, tool: str | None, args: str | None):
    token = os.environ.get("MCP_TOKEN")
    http = httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"} if token else {}, timeout=60)
    async with Client(streamable_http_client(url, http_client=http)) as client:
        tools = await client.list_tools()
        for t in tools.tools:
            print(f"- {t.name}: {(t.description or '').splitlines()[0]}")
        if tool:
            res = await client.call_tool(tool, json.loads(args or "{}"))
            out = res.structured_content or [c.text for c in res.content if hasattr(c, "text")]
            print(json.dumps(out, indent=2)[:3000])


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], *(sys.argv[2:4] + [None, None])[:2]))
