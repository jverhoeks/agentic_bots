"""Single sign-on against a fake OpenID Connect provider (discovery, PKCE, token exchange, JWKS)."""

import base64
import hashlib
import json
import time
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from app import oidc
from app.config import get_settings
from app.db import db
from app.main import app
from app.models import User

ISSUER = "https://idp.test"
CLIENT = "agentic-bots"


class FakeIdP:
    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.claims: dict = {}
        self.codes: dict[str, dict] = {}  # code -> authorize params

    def jwks(self) -> dict:
        jwk = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(self.key.public_key()))
        return {"keys": [{**jwk, "kid": "idp-1", "alg": "ES256", "use": "sig"}]}

    def authorize(self, url: str) -> str:
        params = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        code = f"code-{len(self.codes)}"
        self.codes[code] = params
        return code

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(200, json={"issuer": ISSUER, "authorization_endpoint": f"{ISSUER}/authorize",
                                             "token_endpoint": f"{ISSUER}/token", "jwks_uri": f"{ISSUER}/jwks"})
        if request.url.path == "/jwks":
            return httpx.Response(200, json=self.jwks())
        if request.url.path == "/token":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            auth = self.codes.pop(form["code"], None)
            challenge = base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"].encode()).digest()).rstrip(b"=")
            if not auth or challenge.decode() != auth["code_challenge"] or form["redirect_uri"] != auth["redirect_uri"]:
                return httpx.Response(400, json={"error": "invalid_grant"})
            now = int(time.time())
            token = jwt.encode({"iss": ISSUER, "aud": CLIENT, "iat": now, "exp": now + 300, "nonce": auth["nonce"],
                                **self.claims}, self.key, algorithm="ES256", headers={"kid": "idp-1"})
            return httpx.Response(200, json={"id_token": token, "access_token": "x", "token_type": "Bearer"})
        return httpx.Response(404)


@pytest.fixture
def idp(monkeypatch):
    fake = FakeIdP()
    s = get_settings()
    monkeypatch.setattr(s, "oidc_issuer", ISSUER)
    monkeypatch.setattr(s, "oidc_client_id", CLIENT)
    monkeypatch.setattr(oidc, "_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)))
    oidc._discovery.clear()
    with db() as session:
        for u in session.query(User).all():
            session.delete(u)
        session.commit()
    return fake


async def _sso(idp: FakeIdP, claims: dict) -> httpx.AsyncClient:
    idp.claims = claims
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bots.test")
    r = await c.get("/api/auth/oidc/login")
    assert r.status_code == 302 and r.headers["location"].startswith(f"{ISSUER}/authorize?")
    code = idp.authorize(r.headers["location"])
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    r = await c.get("/api/auth/oidc/callback", params={"code": code, "state": state})
    assert r.status_code == 302, r.text
    return c


async def test_sso_creates_and_reuses_the_account(idp):
    c = await _sso(idp, {"sub": "u-1", "preferred_username": "Jane.Doe@corp.example", "name": "Jane Doe"})
    status = (await c.get("/api/auth/status")).json()
    assert status["user"]["id"] == "jane-doe" and status["user"]["role"] == "admin"  # first account
    assert status["oidc"] == "SSO"
    assert (await c.get("/api/bots")).status_code == 200

    again = await _sso(idp, {"sub": "u-1", "preferred_username": "renamed"})
    assert (await again.get("/api/auth/status")).json()["user"]["id"] == "jane-doe"  # linked by sub, not by name

    other = await _sso(idp, {"sub": "u-2", "preferred_username": "jane.doe"})  # same name, other person
    user = (await other.get("/api/auth/status")).json()["user"]
    assert user["id"] == "jane-doe-2" and user["role"] is None
    with db() as s:  # SSO accounts have no password
        assert s.get(User, "jane-doe").password_hash == oidc.NO_PASSWORD
    bad = await other.post("/api/auth/login", json={"username": "jane-doe", "password": oidc.NO_PASSWORD * 3})
    assert bad.status_code == 401


async def test_sso_rejects_bad_state_and_foreign_tokens(idp, monkeypatch):
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bots.test")
    r = await c.get("/api/auth/oidc/login")
    code = idp.authorize(r.headers["location"])
    r = await c.get("/api/auth/oidc/callback", params={"code": code, "state": "forged"})
    assert r.status_code == 302 and "sso_error=" in r.headers["location"]

    r = await c.get("/api/auth/oidc/login")
    code = idp.authorize(r.headers["location"])
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    idp.claims = {"sub": "u-3"}
    monkeypatch.setattr(idp, "key", ec.generate_private_key(ec.SECP256R1()))  # signs with a key not in the JWKS…
    monkeypatch.setattr(idp, "jwks", lambda: FakeIdP().jwks())  # …that the provider does not publish
    r = await c.get("/api/auth/oidc/callback", params={"code": code, "state": state})
    assert r.status_code == 302 and "invalid+id_token" in r.headers["location"]
    assert "bots_session" not in c.cookies


async def test_sso_is_off_without_config():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bots.test") as c:
        assert (await c.get("/api/auth/oidc/login")).status_code == 404
