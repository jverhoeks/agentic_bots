"""Smoke test an MCP endpoint: list tools and optionally call one.

uv run --project connectors python scripts/mcp_smoke.py http://localhost:3000/mcp [tool] ['{"json": "args"}']
"""

import asyncio
import json
import sys

from mcp import Client


async def main(url: str, tool: str | None, args: str | None):
    async with Client(url) as client:
        tools = await client.list_tools()
        for t in tools.tools:
            print(f"- {t.name}: {(t.description or '').splitlines()[0]}")
        if tool:
            res = await client.call_tool(tool, json.loads(args or "{}"))
            out = res.structured_content or [c.text for c in res.content if hasattr(c, "text")]
            print(json.dumps(out, indent=2)[:3000])


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], *(sys.argv[2:4] + [None, None])[:2]))
