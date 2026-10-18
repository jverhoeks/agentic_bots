"""Single sign-on with any OpenID Connect provider (Entra ID, Google, Keycloak, Authentik, Dex, ...).

Authorization code flow with PKCE. On if OIDC_ISSUER and OIDC_CLIENT_ID are set; the password login keeps working.
- /api/auth/oidc/login redirects to the provider; state, nonce and the PKCE verifier travel in a short-lived signed cookie.
- /api/auth/oidc/callback exchanges the code, verifies the id_token (signature from the provider's JWKS,
  issuer, audience, expiry, nonce) and logs in the user linked to that `sub`.
- Unknown `sub`: a new account is created, named after preferred_username or the e-mail's local part
  (a suffix is added when the name is taken: an SSO login never takes over an existing account).
"""

import base64
import hashlib
import json
import re
import secrets
import time

import httpx
import jwt
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlmodel import select

from app import audit
from app.auth import COOKIE, TTL, _sign, _user_count, make_cookie
from app.config import get_settings
from app.db import db
from app.models import User
from app.tokens import SHARED

FLOW_COOKIE = "bots_oidc"
FLOW_TTL = 600
NO_PASSWORD = "!oidc"  # never matches a scrypt hash: SSO accounts cannot log in with a password

router = APIRouter(prefix="/api/auth/oidc")
_discovery: dict[str, dict] = {}


def enabled() -> bool:
    s = get_settings()
    return bool(s.oidc_issuer and s.oidc_client_id)


def _client() -> httpx.AsyncClient:  # tests swap in a MockTransport
    return httpx.AsyncClient(timeout=15)


async def _config() -> dict:
    issuer = get_settings().oidc_issuer.rstrip("/")
    if issuer not in _discovery:
        async with _client() as c:
            r = await c.get(f"{issuer}/.well-known/openid-configuration")
            r.raise_for_status()
            _discovery[issuer] = r.json()
    return _discovery[issuer]


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _pack(flow: dict) -> str:
    payload = _b64(json.dumps(flow).encode())
    return f"{payload}.{_sign(payload)}"


def _unpack(value: str | None) -> dict | None:
    if not value or "." not in value:
        return None
    payload, sig = value.rsplit(".", 1)
    if not secrets.compare_digest(sig, _sign(payload)):
        return None
    flow = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    return flow if flow.get("exp", 0) > time.time() else None


def _redirect_uri(request: Request) -> str:
    return get_settings().oidc_redirect_url or str(request.url_for("oidc_callback"))


@router.get("/login")
async def login(request: Request):
    if not enabled():
        raise HTTPException(404, "single sign-on is not configured")
    cfg = await _config()
    s = get_settings()
    flow = {"state": secrets.token_urlsafe(16), "nonce": secrets.token_urlsafe(16),
            "verifier": secrets.token_urlsafe(48), "exp": int(time.time()) + FLOW_TTL}
    params = {"response_type": "code", "client_id": s.oidc_client_id, "redirect_uri": _redirect_uri(request),
              "scope": s.oidc_scopes, "state": flow["state"], "nonce": flow["nonce"],
              "code_challenge": _b64(hashlib.sha256(flow["verifier"].encode()).digest()),
              "code_challenge_method": "S256"}
    response = RedirectResponse(str(httpx.URL(cfg["authorization_endpoint"], params=params)), status_code=302)
    response.set_cookie(FLOW_COOKIE, _pack(flow), max_age=FLOW_TTL, httponly=True, samesite="lax",
                        secure=s.cookie_secure, path="/api/auth/oidc")
    return response


async def _verify_id_token(id_token: str, nonce: str) -> dict:
    cfg = await _config()
    s = get_settings()
    async with _client() as c:
        r = await c.get(cfg["jwks_uri"])
        r.raise_for_status()
        keys = r.json()["keys"]
    kid = jwt.get_unverified_header(id_token).get("kid")
    jwk = next((k for k in keys if kid is None or k.get("kid") == kid), None)
    if jwk is None:
        raise HTTPException(401, "id_token signed with an unknown key")
    key = jwt.PyJWK.from_dict(jwk)
    claims = jwt.decode(id_token, key.key, algorithms=[key.algorithm_name], audience=s.oidc_client_id,
                        issuer=cfg.get("issuer", s.oidc_issuer), options={"require": ["exp", "iat", "sub"]})
    if claims.get("nonce") != nonce:
        raise HTTPException(401, "id_token nonce mismatch")
    return claims


def _username(claims: dict) -> str:
    raw = claims.get("preferred_username") or (claims.get("email") or "").split("@")[0] or claims.get("name") or "user"
    base = re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", raw.split("@")[0].lower())).strip("-")[:32] or "user"
    if len(base) < 2 or base == SHARED:
        base = f"user-{base}".strip("-")
    name, n = base, 1
    with db() as s:
        while s.get(User, name):
            n += 1
            name = f"{base}-{n}"
    return name


def user_for(claims: dict) -> User:
    """The account linked to this OIDC subject, created on first login."""
    subject = f"{get_settings().oidc_issuer.rstrip('/')}|{claims['sub']}"
    with db() as s:
        user = s.exec(select(User).where(User.oidc_sub == subject)).first()
    if user:
        return user
    if not get_settings().oidc_auto_signup and _user_count():
        raise HTTPException(403, "no account is linked to this login; ask an admin")
    first = _user_count() == 0
    user = User(id=_username(claims), name=claims.get("name") or claims.get("email") or claims["sub"],
                password_hash=NO_PASSWORD, oidc_sub=subject, role="admin" if first else None)
    with db() as s:
        s.add(user)
        s.commit()
    audit.record("auth.register", user=user.id, detail={"via": "oidc", "email": claims.get("email")})
    return user


@router.get("/callback", name="oidc_callback")
async def callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    """Browser redirect from the provider: failures go back to the login page as ?sso_error=."""
    try:
        return await _callback(request, code, state, error)
    except HTTPException as e:
        url = httpx.URL(get_settings().oidc_post_login_url).copy_merge_params({"sso_error": e.detail})
        response = RedirectResponse(str(url), status_code=302)
        response.delete_cookie(FLOW_COOKIE, path="/api/auth/oidc")
        return response


async def _callback(request: Request, code: str | None, state: str | None, error: str | None):
    flow = _unpack(request.cookies.get(FLOW_COOKIE))
    if error:
        raise HTTPException(401, f"login refused by the identity provider: {error}")
    if not flow or not state or not secrets.compare_digest(state, flow["state"]) or not code:
        audit.record("auth.login_failed", detail={"via": "oidc", "reason": "state"})
        raise HTTPException(400, "login expired or invalid; start again")
    cfg = await _config()
    s = get_settings()
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": _redirect_uri(request),
            "client_id": s.oidc_client_id, "code_verifier": flow["verifier"]}
    if s.oidc_client_secret:
        form["client_secret"] = s.oidc_client_secret
    async with _client() as c:
        r = await c.post(cfg["token_endpoint"], data=form, headers={"accept": "application/json"})
    if r.status_code != 200 or "id_token" not in r.json():
        audit.record("auth.login_failed", detail={"via": "oidc", "reason": f"token endpoint {r.status_code}"})
        raise HTTPException(401, "the identity provider did not return an id_token")
    try:
        claims = await _verify_id_token(r.json()["id_token"], flow["nonce"])
    except jwt.PyJWTError as e:
        audit.record("auth.login_failed", detail={"via": "oidc", "reason": str(e)})
        raise HTTPException(401, f"invalid id_token: {e}")
    user = user_for(claims)
    audit.record("auth.login", user=user.id, detail={"via": "oidc"})
    response = RedirectResponse(s.oidc_post_login_url, status_code=302)
    response.set_cookie(COOKIE, make_cookie(user.id), max_age=TTL, httponly=True, samesite="lax", secure=s.cookie_secure)
    response.delete_cookie(FLOW_COOKIE, path="/api/auth/oidc")
    return response
