"""Basic login: users with a scrypt password hash and an HMAC-signed session cookie.

Placeholder for OIDC (Entra ID / Google): the rest of the app only uses the user id from
`current_user`, so swapping the login method later does not touch bots or runs.
The first account can register itself; after that, registration is closed unless ALLOW_SIGNUP=true.
"""

import base64
import hashlib
import hmac
import re
import secrets
import time
from contextvars import ContextVar

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlmodel import func, select

from app.config import get_settings
from app.db import db
from app.models import User

COOKIE = "bots_session"
ADMIN = "admin"
TTL = 14 * 24 * 3600
USER_RE = r"^[a-z0-9]+(-[a-z0-9]+)*$"  # also the connection name in open-connector
# user of the request being handled (builder tools read it; bot runs use the bot owner instead)
current_user: ContextVar[str | None] = ContextVar("current_user", default=None)

router = APIRouter(prefix="/api/auth")


def _secret() -> bytes:
    s = get_settings()
    if s.session_secret:
        return s.session_secret.encode()
    path = s.data_dir / "session-secret"  # generated once, so a restart does not log everyone out
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secrets.token_urlsafe(32))
        path.chmod(0o600)
    return path.read_text().strip().encode()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def check_password(password: str, stored: str) -> bool:
    try:
        _, salt, digest = stored.split("$")
        got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=2**14, r=8, p=1)
    except ValueError:
        return False
    return hmac.compare_digest(got, base64.b64decode(digest))


def _sign(payload: str) -> str:
    return hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()


def make_cookie(user_id: str) -> str:
    payload = f"{user_id}.{int(time.time()) + TTL}"
    return f"{payload}.{_sign(payload)}"


def read_cookie(value: str | None) -> str | None:
    if not value or value.count(".") != 2:
        return None
    user_id, expires, sig = value.split(".")
    if not hmac.compare_digest(sig, _sign(f"{user_id}.{expires}")) or not expires.isdigit() or int(expires) < time.time():
        return None
    with db() as s:
        return user_id if s.get(User, user_id) else None


async def require_user(request: Request) -> str:  # async: the contextvar must be set in the request task
    """FastAPI dependency for /api routes: the logged-in user id, or 401."""
    user = read_cookie(request.cookies.get(COOKIE))
    if not user:
        raise HTTPException(401, "not logged in")
    current_user.set(user)
    from app import audit

    audit.scope(user=user)
    return user


async def require_admin(request: Request) -> str:
    """FastAPI dependency for /api/admin: the logged-in user, if they are an admin; else 401/403."""
    user = await require_user(request)
    with db() as s:
        u = s.get(User, user)
    if not u or u.role != ADMIN:
        raise HTTPException(403, "admins only")
    return user


class Credentials(BaseModel):
    username: str = Field(pattern=USER_RE, min_length=2, max_length=40)
    password: str = Field(min_length=8, max_length=200)
    name: str | None = Field(None, max_length=80)


def _login(response: Response, user: User, kind: str = "auth.login") -> dict:
    from app import audit

    response.set_cookie(COOKIE, make_cookie(user.id), max_age=TTL, httponly=True, samesite="lax",
                        secure=get_settings().cookie_secure)
    audit.record(kind, user=user.id)
    return user_dict(user)


def user_dict(user: User) -> dict:
    return {"id": user.id, "name": user.name, "role": user.role}


def _user_count() -> int:
    with db() as s:
        return s.exec(select(func.count()).select_from(User)).one()


def create_user(username: str, password: str, name: str | None = None, role: str | None = None) -> User:
    """A new account; the very first one is an admin."""
    from app.tokens import SHARED

    if not re.match(USER_RE, username) or username == SHARED:  # SHARED: gateway subject of owner-less bots
        raise ValueError(f"invalid username {username!r}")
    user = User(id=username, name=name or username, password_hash=hash_password(password),
                role=role or (ADMIN if _user_count() == 0 else None))
    with db() as s:
        if s.get(User, username):
            raise ValueError(f"user {username!r} exists")
        s.add(user)
        s.commit()
    return user


def set_role(username: str, role: str | None) -> User:
    with db() as s:
        user = s.get(User, username)
        if not user:
            raise ValueError(f"no user {username!r}")
        user.role = role
        s.add(user)
        s.commit()
    return user


def ensure_admin() -> None:
    """Databases from before roles existed have no admin: promote the oldest account (once)."""
    with db() as s:
        if s.exec(select(User).where(User.role == ADMIN)).first():
            return
        first = s.exec(select(User).order_by(User.created_at)).first()
        if first:
            first.role = ADMIN
            s.add(first)
            s.commit()


@router.get("/status")
def status(request: Request):
    """Public: whether someone is logged in and whether registration is open (for the login page)."""
    user = read_cookie(request.cookies.get(COOKIE))
    with db() as s:
        u = s.get(User, user) if user else None
    cfg = get_settings()
    return {"user": user_dict(u) if u else None,
            "can_register": _user_count() == 0 or cfg.allow_signup,
            "oidc": cfg.oidc_name if cfg.oidc_issuer and cfg.oidc_client_id else None}  # SSO button label


@router.post("/register")
def register(body: Credentials, response: Response):
    if _user_count() and not get_settings().allow_signup:
        raise HTTPException(403, "registration is closed; ask an admin for an account")
    try:
        user = create_user(body.username, body.password, body.name)
    except ValueError as e:
        raise HTTPException(409, str(e))
    return _login(response, user, "auth.register")


@router.post("/login")
def login(body: Credentials, response: Response):
    with db() as s:
        user = s.get(User, body.username)
    if not user or not check_password(body.password, user.password_hash):
        from app import audit

        audit.record("auth.login_failed", detail={"username": body.username})
        raise HTTPException(401, "wrong username or password")
    return _login(response, user)


@router.post("/token")
def token(request: Request):
    """A gateway token (12h) for the logged-in user, for external MCP/A2A clients (Claude, goose, ...)."""
    from app import tokens

    user = read_cookie(request.cookies.get(COOKIE))
    if not user:
        raise HTTPException(401, "not logged in")
    from app import audit

    audit.record("auth.token_issued", user=user, detail={"ttl": 12 * 3600})
    return {"token": tokens.issue(user, ttl=12 * 3600), "expires_in": 12 * 3600,
            "mcp": "http://localhost:3000/mcp", "a2a": "http://localhost:3000/a2a/<bot>/"}


@router.post("/logout")
def logout(request: Request, response: Response):
    if user := read_cookie(request.cookies.get(COOKIE)):
        from app import audit

        audit.record("auth.logout", user=user)
    response.delete_cookie(COOKIE)
    return {"ok": True}


if __name__ == "__main__":
    # python -m app.auth <username> [name]       add a user (asks for the password)
    # python -m app.auth --admin <username>      make an existing user admin (--no-admin to revoke)
    import getpass
    import sys

    from app.db import init_db

    args = sys.argv[1:]
    if not args:
        sys.exit("usage: python -m app.auth <username> [display name] | --admin <username> | --no-admin <username>")
    init_db()
    if args[0] in ("--admin", "--no-admin") and len(args) == 2:
        u = set_role(args[1], ADMIN if args[0] == "--admin" else None)
        print(f"{u.id}: role {u.role or 'user'}")
    else:
        pw = getpass.getpass(f"password for {args[0]}: ")
        u = create_user(args[0], pw, " ".join(args[1:]) or None)
        print(f"created {u.id}" + (" (admin)" if u.role == ADMIN else ""))
