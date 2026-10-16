"""All connectors in one ASGI app, each mounted at /<name>/mcp (stateless streamable HTTP)."""

import contextlib
import os

import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from connectors import hackernews, web

SERVERS = {"hackernews": hackernews.server, "web": web.server}

# host="0.0.0.0" disables the localhost-only Host check: connectors are reached by
# agentgateway over the internal docker network, never directly by clients.
APPS = {
    name: srv.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, host="0.0.0.0")
    for name, srv in SERVERS.items()
}


@contextlib.asynccontextmanager
async def lifespan(_app):
    async with contextlib.AsyncExitStack() as stack:
        for srv in SERVERS.values():
            await stack.enter_async_context(srv.session_manager.run())
        yield


async def health(_request):
    return JSONResponse({"status": "ok", "servers": list(SERVERS)})


app = Starlette(
    routes=[Route("/health", health), *[Mount(f"/{name}", app=a) for name, a in APPS.items()]],
    lifespan=lifespan,
)


def main():
    uvicorn.run(app, host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "8100")))
