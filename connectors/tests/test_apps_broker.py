"""apps broker against a fake open-connector: identity, per-user connections and runtime tokens, MCP."""

import asyncio
import json
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from starlette.testclient import TestClient

from connectors import apps_broker as b

KEY = ec.generate_private_key(ec.SECP256R1())


def token(sub: str, aud: str = "connections", iss: str = "agentic-bots", key=KEY) -> str:
    now = int(time.time())
    return jwt.encode({"iss": iss, "aud": aud, "sub": sub, "iat": now, "exp": now + 60}, key,
                      algorithm="ES256", headers={"kid": "k1"})


class FakeOC:
    """Just enough of the open-connector admin API and /mcp."""

    def __init__(self):
        self.connections = [{"id": "virt", "service": "hackernews", "connectionName": "default", "virtual": True}]
        self.tokens: dict[str, dict] = {}
        self.mcp_auth: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        body = json.loads(request.content) if request.content else {}
        if path == "/mcp":
            self.mcp_auth.append(request.headers["authorization"])
            reply = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"tools": [{"name": "search_actions"}]}}).encode()
            return httpx.Response(200, headers={"content-type": "application/json"}, stream=httpx.ByteStream(reply))
        assert request.headers["authorization"] == "Bearer admin"
        if path == "/api/connections" and method == "GET":
            return httpx.Response(200, json=self.connections)
        if path.startswith("/api/connections/") and method == "PUT":
            service = path.rsplit("/", 1)[1]
            if body["values"].get("apiKey") == "bad":
                return httpx.Response(400, json={"error": {"code": "credential_verification_failed", "message": "Bad credentials"}})
            self.connections.append({"id": f"c-{service}-{body['connectionName']}", "service": service,
                                     "connectionName": body["connectionName"], "authType": body["authType"],
                                     "profile": {"displayName": "me@example.com"}})
            return httpx.Response(200, json={})
        if path.startswith("/api/connections/") and method == "DELETE":
            service = path.rsplit("/", 1)[1]
            self.connections = [c for c in self.connections
                                if not (c["service"] == service and c["connectionName"] == body["connectionName"])]
            return httpx.Response(200, json={})
        if path == "/api/runtime-tokens" and method == "POST":
            tid = f"t{len(self.tokens) + 1}"
            self.tokens[tid] = body
            return httpx.Response(200, json={"token": f"secret-{tid}", "record": {"id": tid}})
        if path.startswith("/api/runtime-tokens/"):
            tid = path.rsplit("/", 1)[1]
            if tid not in self.tokens:
                return httpx.Response(404, json={"error": "not found"})
            if method == "PUT":
                self.tokens[tid] = {**self.tokens[tid], **body}
            else:
                del self.tokens[tid]
            return httpx.Response(200, json={})
        if path == "/api/oauth/authorizations":
            return httpx.Response(200, json={"service": body["service"], "authorizationUrl": "https://idp/auth?x", "state": "s"})
        if path == "/api/providers":
            return httpx.Response(200, json=[
                {"service": "github", "displayName": "GitHub", "categories": ["Developer Tools"],
                 "auth": [{"type": "oauth2"}, {"type": "api_key", "label": "Personal access token", "placeholder": "github_pat_..."}]},
                {"service": "hackernews", "displayName": "HN", "auth": [{"type": "no_auth"}]},
            ])
        if path == "/api/oauth/configs":
            return httpx.Response(200, json=[{"service": "github", "configured": True}])
        return httpx.Response(404, json={"error": f"unexpected {method} {path}"})


@pytest.fixture
def oc(tmp_path, monkeypatch):
    jwk = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(KEY.public_key()))
    jwks = tmp_path / "jwks.json"
    jwks.write_text(json.dumps({"keys": [{**jwk, "kid": "k1", "use": "sig", "alg": "ES256"}]}))
    fake = FakeOC()
    monkeypatch.setattr(b, "JWKS_FILE", jwks)
    monkeypatch.setattr(b, "STORE", tmp_path / "tokens.json")
    monkeypatch.setattr(b, "ADMIN_TOKEN", "admin")
    monkeypatch.setattr(b, "_client", httpx.AsyncClient(base_url="http://oc", transport=httpx.MockTransport(fake)))
    monkeypatch.setattr(b, "_jwks", {})
    monkeypatch.setattr(b, "_synced", {})
    monkeypatch.setattr(b, "_catalog", {})
    return fake


def api(user: str, aud: str = "connections") -> dict:
    return {"authorization": f"Bearer {token(user, aud)}"}


def test_only_valid_tokens_for_the_right_audience(oc):
    c = TestClient(b.app)
    assert c.get("/connections/api/connections").status_code == 401
    assert c.get("/connections/api/connections", headers=api("alice", "agentgateway")).status_code == 401
    other = ec.generate_private_key(ec.SECP256R1())
    forged = {"authorization": f"Bearer {token('alice', key=other)}"}
    assert c.get("/connections/api/connections", headers=forged).status_code == 401
    assert c.get("/connections/api/connections", headers=api("default")).status_code == 401  # reserved
    assert c.get("/connections/api/connections", headers=api("alice")).json() == {"user": "alice", "connections": []}


def test_connect_issues_a_token_for_only_your_connections(oc):
    c = TestClient(b.app)
    r = c.post("/connections/api/connections/github", headers=api("alice"), json={"authType": "api_key", "values": {"apiKey": "k"}})
    assert r.json() == {"connected": True}
    c.post("/connections/api/connections/github", headers=api("bob"), json={"authType": "api_key", "values": {"apiKey": "k"}})
    mine = c.get("/connections/api/connections", headers=api("alice")).json()["connections"]
    assert [(x["service"], x["account"]) for x in mine] == [("github", "me@example.com")]
    store = json.loads(b.STORE.read_text())
    assert oc.tokens[store["alice"]["id"]]["allowedConnections"] == ["c-github-alice"]  # never bob's, never empty
    assert "gmail.send_email" in oc.tokens[store["alice"]["id"]]["blockedActions"]

    bad = c.post("/connections/api/connections/slack", headers=api("alice"), json={"authType": "api_key", "values": {"apiKey": "bad"}})
    assert bad.status_code == 400 and bad.json()["error"] == "Bad credentials"
    assert c.post("/connections/api/connections/..%2Fx", headers=api("alice"), json={"authType": "api_key"}).status_code in (400, 404)

    # disconnecting the last one revokes the token: an empty grant list would mean "all connections"
    c.delete("/connections/api/connections/github", headers=api("alice"))
    assert "alice" not in json.loads(b.STORE.read_text()) and store["alice"]["id"] not in oc.tokens
    assert "bob" in json.loads(b.STORE.read_text())


def test_oauth_returns_the_authorization_url(oc):
    r = TestClient(b.app).post("/connections/api/connections/github", headers=api("alice"), json={"authType": "oauth2"})
    assert r.json() == {"authorizationUrl": "https://idp/auth?x"}


def test_providers_hide_no_auth_and_flag_oauth_setup(oc):
    d = TestClient(b.app).get("/connections/api/providers?q=git", headers=api("alice")).json()
    assert [p["service"] for p in d["providers"]] == ["github"]
    oauth, key = d["providers"][0]["auth"]
    assert oauth["ready"] is True and key["fields"][0]["key"] == "apiKey"


def test_mcp_forwards_with_the_users_token_or_serves_no_tools(oc):
    c = TestClient(b.app)
    call = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    assert c.post("/mcp", json=call).status_code == 401
    assert c.post("/mcp", json=call, headers=api("alice")).status_code == 401  # connections audience: not for MCP

    gw = api("alice", "agentgateway")
    init = {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}
    assert c.post("/mcp", json=init, headers=gw).json()["result"]["serverInfo"]["name"] == "apps"
    assert c.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=gw).status_code == 202
    assert c.post("/mcp", json=call, headers=gw).json()["result"] == {"tools": []}
    assert not oc.mcp_auth

    c.post("/connections/api/connections/github", headers=api("alice"), json={"authType": "api_key", "values": {"apiKey": "k"}})
    assert c.post("/mcp", json=call, headers=gw).json()["result"]["tools"] == [{"name": "search_actions"}]
    assert oc.mcp_auth == ["Bearer secret-t1"]  # the user's runtime token, never the admin token

    # a discovery probe is answered here as "method not found", never forwarded (open-connector says HTTP 400)
    probe = c.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "server/discover"}, headers=gw)
    assert probe.status_code == 200 and probe.json()["error"]["code"] == -32601
    assert oc.mcp_auth == ["Bearer secret-t1"]

    # the session's GET stream and DELETE are answered here too, not with 405, and only with a valid token
    assert c.get("/mcp").status_code == 401
    assert c.delete("/mcp", headers=gw).status_code == 200


def test_the_get_stream_stays_open_and_quiet():
    async def first():
        stream = b._idle_stream()
        try:
            return await anext(stream)
        finally:
            await stream.aclose()

    assert asyncio.run(first()) == b": keep-alive\n\n"  # an SSE comment: clients ignore it
