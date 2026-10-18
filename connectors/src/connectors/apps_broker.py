"""apps-broker: per-user SaaS connections (open-connector) without its secrets leaving the connector side.

It is the only holder of the open-connector admin token and of each user's runtime token
(in its own volume). Two faces, both behind a JWT signed by the backend (secrets/jwks.json, public key):

- `/mcp`: the `apps` target of agentgateway. The gateway forwards the caller's token (audience
  `agentgateway`); the broker takes the user from its `sub` and forwards the request to open-connector
  with that user's runtime token, which grants only the user's own connections. A user without
  connections gets an empty tool list instead of an error.
- `/connections/api/*`: the Connections page of the app (audience `connections`, from the backend's
  /api/connections/token). The browser reaches it through the UI proxy, not through the backend, so
  API keys a user types never pass the app. Every connection is named after its user (connectionName
  = `sub`), and a user can only see, add and remove their own.

OAuth providers need an OAuth client, set up once by an admin in the open-connector console.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path

import httpx
import jwt
import uvicorn
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

log = logging.getLogger("apps-broker")

OC_URL = os.environ.get("OPEN_CONNECTOR_URL", "http://open-connector:3000")
ADMIN_TOKEN = os.environ.get("OOMOL_CONNECT_ADMIN_TOKEN", "")
JWKS_FILE = Path(os.environ.get("JWKS_FILE", "/secrets/jwks.json"))
ISSUER = os.environ.get("JWT_ISSUER", "agentic-bots")
STORE = Path(os.environ.get("DATA_DIR", "/data")) / "tokens.json"
# Action policy of every user's runtime token (open-connector rules: exact ids, "<service>.*" or "*").
# Blocked by default: Gmail actions that send, forward or delete, the usual targets of a prompt injection.
ALLOWED_ACTIONS = [a for a in os.environ.get("APPS_ALLOWED_ACTIONS", "*").split(",") if a]
BLOCKED_ACTIONS = [a for a in os.environ.get("APPS_BLOCKED_ACTIONS", ",".join(
    f"gmail.{a}" for a in ("send_email", "send_draft", "reply_email", "reply_to_thread", "create_filter",
                           "update_vacation_settings", "move_to_trash", "move_thread_to_trash",
                           "delete_draft", "delete_filter", "delete_label"))).split(",") if a]
SYNC_TTL = 60  # seconds: how stale a user's token grants may be on the MCP path (new OAuth connections)
USER_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
SERVICE_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,63}$")
RESERVED = {"default", "shared"}  # "default": open-connector's own connections; "shared": owner-less bots
FORWARD_HEADERS = ("content-type", "accept", "mcp-protocol-version", "mcp-session-id", "traceparent", "tracestate")
NOT_CONNECTED = "No apps connected for this user. Connect them on the Connections page of the app."


class Denied(Exception):
    pass


# --- identity ---------------------------------------------------------------------------------------

_jwks: dict = {}


def _keys() -> jwt.PyJWKSet:
    mtime = JWKS_FILE.stat().st_mtime
    if _jwks.get("mtime") != mtime:
        _jwks.update(mtime=mtime, keys=jwt.PyJWKSet.from_json(JWKS_FILE.read_text()))
    return _jwks["keys"]


def user_of(request: Request, audience: str) -> str:
    """The verified `sub` of the request's bearer token; never a header the caller sets itself."""
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise Denied("missing bearer token")
    token = auth[7:].strip()
    try:
        kid = jwt.get_unverified_header(token).get("kid")
        key = next((k for k in _keys().keys if k.key_id == kid), None)
        if key is None:
            raise Denied("unknown signing key")
        claims = jwt.decode(token, key.key, algorithms=["ES256"], audience=audience, issuer=ISSUER,
                            options={"require": ["exp", "sub"]})
    except jwt.InvalidTokenError as e:
        raise Denied(f"invalid token: {e}") from None
    user = claims["sub"]
    if not USER_RE.match(user):
        raise Denied("invalid user")
    return user


# --- open-connector admin API -----------------------------------------------------------------------

_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(base_url=OC_URL, timeout=30)
    return _client


async def oc(method: str, path: str, body: dict | None = None):
    r = await client().request(method, path, json=body, headers={"authorization": f"Bearer {ADMIN_TOKEN}"})
    if r.status_code >= 400:
        try:
            data = r.json()
            msg = data.get("message") or data.get("error") or r.text
            if isinstance(msg, dict):
                msg = msg.get("message") or msg.get("code") or r.text
        except (ValueError, AttributeError):
            msg = r.text
        raise OCError(r.status_code, str(msg)[:300])
    return r.json() if r.content else None


class OCError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def _items(data, key: str) -> list[dict]:
    return data if isinstance(data, list) else (data or {}).get(key) or []


async def my_connections(user: str) -> list[dict]:
    return [c for c in _items(await oc("GET", "/api/connections"), "connections")
            if c.get("connectionName") == user and c.get("id") and not c.get("virtual")]


# --- per-user runtime tokens ------------------------------------------------------------------------

_locks: dict[str, asyncio.Lock] = {}
_synced: dict[str, float] = {}


def _load() -> dict:
    return json.loads(STORE.read_text()) if STORE.exists() else {}


def _save(store: dict) -> None:
    STORE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE.with_suffix(".tmp")
    tmp.write_text(json.dumps(store, indent=1))
    tmp.chmod(0o600)
    tmp.replace(STORE)


async def sync(user: str, force: bool = True) -> str | None:
    """Make the user's runtime token grant exactly their connections. Returns the token, or None without any."""
    async with _locks.setdefault(user, asyncio.Lock()):
        rec = _load().get(user)
        if not force and time.monotonic() - _synced.get(user, 0) < SYNC_TTL:
            return rec["token"] if rec else None
        ids = sorted(c["id"] for c in await my_connections(user))
        store = _load()
        rec = store.get(user)
        if not ids:  # an empty allowedConnections list means "all connections": never keep such a token
            if rec:
                await _revoke(rec["id"])
                del store[user]
                _save(store)
            _synced[user] = time.monotonic()
            return None
        policy = {"allowedActions": ALLOWED_ACTIONS, "blockedActions": BLOCKED_ACTIONS,
                  "allowedProxies": [], "allowedConnections": ids}
        if rec and (rec.get("connections") != ids or rec.get("policy") != [ALLOWED_ACTIONS, BLOCKED_ACTIONS]):
            try:
                await oc("PUT", f"/api/runtime-tokens/{rec['id']}", policy)
            except OCError as e:
                if e.status != 404:
                    raise
                rec = None  # revoked in the console: issue a new one
        if not rec:
            created = await oc("POST", "/api/runtime-tokens", {"name": f"user:{user}", **policy}) or {}
            token = created.get("token")
            record = created.get("record") or {}
            if not token or not record.get("id"):
                raise OCError(502, "open-connector returned no token")
            rec = {"id": record["id"], "token": token}
        rec.update(connections=ids, policy=[ALLOWED_ACTIONS, BLOCKED_ACTIONS])
        store[user] = rec
        _save(store)
        _synced[user] = time.monotonic()
        return rec["token"]


async def _revoke(token_id: str) -> None:
    try:
        await oc("DELETE", f"/api/runtime-tokens/{token_id}")
    except OCError as e:
        if e.status != 404:
            raise


# --- MCP: the gateway's `apps` target --------------------------------------------------------------

def _local_reply(msg: dict) -> dict | None:
    """MCP answers for a user without connections: a valid server with no tools."""
    if "id" not in msg:
        return None  # notification
    method = msg.get("method")
    if method == "initialize":
        version = (msg.get("params") or {}).get("protocolVersion") or "2025-06-18"
        result = {"protocolVersion": version, "capabilities": {"tools": {}},
                  "serverInfo": {"name": "apps", "version": "1.0"}, "instructions": NOT_CONNECTED}
    elif method == "tools/list":
        result = {"tools": []}
    elif method == "ping":
        result = {}
    elif method == "tools/call":
        result = {"content": [{"type": "text", "text": NOT_CONNECTED}], "isError": True}
    else:
        return {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": NOT_CONNECTED}}
    return {"jsonrpc": "2.0", "id": msg["id"], "result": result}


async def mcp(request: Request) -> Response:
    if request.method != "POST":  # open-connector is stateless: no SSE stream, no sessions
        return Response(status_code=405, headers={"allow": "POST"})
    try:
        user = user_of(request, "agentgateway")
    except Denied as e:
        return JSONResponse({"error": str(e)}, status_code=401)
    body = await request.body()
    try:
        token = await sync(user, force=False)
    except (OCError, httpx.HTTPError) as e:
        log.warning("sync for %s failed: %s", user, e)
        return JSONResponse({"error": "apps are unavailable"}, status_code=502)
    if not token:
        try:
            msg = json.loads(body)
        except ValueError:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        replies = [r for r in map(_local_reply, msg if isinstance(msg, list) else [msg]) if r]
        if not replies:
            return Response(status_code=202)
        return JSONResponse(replies if isinstance(msg, list) else replies[0])
    headers = {k: v for k in FORWARD_HEADERS if (v := request.headers.get(k))}
    upstream = client().build_request("POST", "/mcp", content=body,
                                      headers={**headers, "authorization": f"Bearer {token}"}, timeout=120)
    r = await client().send(upstream, stream=True)
    keep = {k: v for k, v in r.headers.items() if k.lower() in ("content-type", "mcp-session-id", "mcp-protocol-version")}
    return StreamingResponse(r.aiter_raw(), status_code=r.status_code, headers=keep, background=BackgroundTask(r.aclose))


# --- Connections API (the app's Connections page) --------------------------------------------------

_catalog: dict = {}
CATALOG_TTL = 600


async def catalog() -> list[dict]:
    """Providers that need a user connection, compacted for the UI, with whether OAuth is set up."""
    if time.monotonic() - _catalog.get("at", -CATALOG_TTL) < CATALOG_TTL:
        return _catalog["items"]
    providers = _items(await oc("GET", "/api/providers"), "providers")
    oauth = {c["service"]: bool(c.get("configured")) for c in _items(await oc("GET", "/api/oauth/configs"), "configs")}
    items = []
    for p in providers:
        auth = []
        for a in p.get("auth") or []:
            kind = a.get("type")
            if kind == "no_auth":
                continue
            entry = {"type": kind, "label": a.get("label") or {"oauth2": "Sign in", "api_key": "API key"}.get(kind, kind),
                     "description": a.get("description") or ""}
            if kind == "api_key":
                entry["fields"] = [{"key": "apiKey", "label": a.get("label") or "API key", "inputType": "password",
                                    "required": True, "secret": True, "placeholder": a.get("placeholder") or ""}]
            elif kind == "custom_credential":
                entry["fields"] = [{k: f.get(k) for k in ("key", "label", "inputType", "required", "secret", "placeholder", "description")}
                                   for f in a.get("fields") or []]
            elif kind == "oauth2":
                entry["ready"] = oauth.get(p["service"], False)
            auth.append(entry)
        if auth:
            items.append({"service": p["service"], "name": p.get("displayName") or p["service"],
                          "categories": p.get("categories") or [], "homepage": p.get("homepageUrl") or "", "auth": auth})
    _catalog.update(at=time.monotonic(), items=items)
    return items


def _err(e: Exception) -> JSONResponse:
    if isinstance(e, Denied):
        return JSONResponse({"error": str(e)}, status_code=401)
    if isinstance(e, OCError):
        return JSONResponse({"error": e.message}, status_code=400 if e.status < 500 else 502)
    if isinstance(e, httpx.HTTPError):
        return JSONResponse({"error": "open-connector is unreachable"}, status_code=502)
    raise e


def _api_user(request: Request) -> str:
    user = user_of(request, "connections")
    if user in RESERVED:
        raise Denied("this account cannot have connections")
    return user


async def providers(request: Request) -> Response:
    try:
        _api_user(request)
        items = await catalog()
    except Exception as e:  # noqa: BLE001
        return _err(e)
    q = request.query_params.get("q", "").strip().lower()
    if q:
        items = [p for p in items if q in p["service"] or q in p["name"].lower() or any(q in c.lower() for c in p["categories"])]
        items.sort(key=lambda p: (not (p["service"].startswith(q) or p["name"].lower().startswith(q)), p["name"].lower()))
    limit = min(int(request.query_params.get("limit", "40") or 40), 100)
    return JSONResponse({"total": len(items), "providers": items[:limit]})


def _summary(c: dict) -> dict:
    profile = c.get("profile") or {}
    return {"id": c["id"], "service": c["service"], "authType": c.get("authType"),
            "account": profile.get("displayName") or profile.get("email") or profile.get("accountId") or ""}


async def connections(request: Request) -> Response:
    try:
        user = _api_user(request)
        mine = await my_connections(user)
        await sync(user)
    except Exception as e:  # noqa: BLE001
        return _err(e)
    return JSONResponse({"user": user, "connections": [_summary(c) for c in mine]})


async def connect(request: Request) -> Response:
    service = request.path_params["service"]
    try:
        user = _api_user(request)
        if not SERVICE_RE.match(service):
            return JSONResponse({"error": "invalid service"}, status_code=400)
        body = await request.json()
        kind = body.get("authType")
        if kind == "oauth2":
            started = await oc("POST", "/api/oauth/authorizations", {"service": service, "connectionName": user}) or {}
            if not started.get("authorizationUrl"):
                raise OCError(502, "open-connector returned no authorization URL")
            return JSONResponse({"authorizationUrl": started["authorizationUrl"]})
        if kind not in ("api_key", "custom_credential"):
            return JSONResponse({"error": "authType must be oauth2, api_key or custom_credential"}, status_code=400)
        values = body.get("values") or {}
        if not isinstance(values, dict) or not all(isinstance(v, str) for v in values.values()):
            return JSONResponse({"error": "values must be strings"}, status_code=400)
        await oc("PUT", f"/api/connections/{service}", {"authType": kind, "connectionName": user, "values": values})
        await sync(user)
    except Exception as e:  # noqa: BLE001
        return _err(e)
    log.info("connected %s for %s (%s)", service, user, kind)
    return JSONResponse({"connected": True})


async def disconnect(request: Request) -> Response:
    service = request.path_params["service"]
    try:
        user = _api_user(request)
        if not SERVICE_RE.match(service):
            return JSONResponse({"error": "invalid service"}, status_code=400)
        await oc("DELETE", f"/api/connections/{service}", {"connectionName": user})
        await sync(user)
    except Exception as e:  # noqa: BLE001
        return _err(e)
    log.info("disconnected %s for %s", service, user)
    return JSONResponse({"disconnected": True})


async def health(_request: Request) -> Response:
    return JSONResponse({"status": "ok", "admin_token": bool(ADMIN_TOKEN)})


app = Starlette(routes=[
    Route("/health", health),
    Route("/mcp", mcp, methods=["GET", "POST", "DELETE"]),
    Route("/connections/api/providers", providers),
    Route("/connections/api/connections", connections),
    Route("/connections/api/connections/{service}", connect, methods=["POST"]),
    Route("/connections/api/connections/{service}", disconnect, methods=["DELETE"]),
])


def main():
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "8120")))
