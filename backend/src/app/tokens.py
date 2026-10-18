"""Gateway identity: short-lived ES256 JWTs the backend signs and agentgateway verifies.

Every MCP request (bot runs, the builder's catalog) and every A2A call through the gateway carries
`Authorization: Bearer <jwt>` with `sub` = the user whose connections are used (the bot owner).
agentgateway checks the signature with secrets/jwks.json (public key only) and turns `sub` into the
X-User-Id header, so a caller cannot pick another user. The backend's /a2a endpoint checks the same
token again, so it is also safe when reached without the gateway.

    python -m app.tokens init            # create secrets/jwt-private.pem + secrets/jwks.json (once),
                                         # and secrets/trace-ingest.env (gateway -> backend OTLP token)
    python -m app.tokens token <user>    # a token for an external MCP/A2A client (e.g. make smoke)
"""

import json
import secrets
import time

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.config import get_settings

AUDIENCE = "agentgateway"
KID = "bots-1"
SHARED = "shared"  # subject for bots without an owner: no personal connections
TTL = 2 * 3600  # a run builds its toolsets once, so this bounds the longest run


def _jwks_path():
    return get_settings().jwt_private_key.with_name("jwks.json")


def init() -> bool:
    """Create the key pair (and the trace ingest token) if missing. Returns True when new keys were written."""
    key_path = get_settings().jwt_private_key
    _init_trace_token(key_path.with_name("trace-ingest.env"))
    if key_path.exists():
        return False
    key = ec.generate_private_key(ec.SECP256R1())
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    key_path.chmod(0o600)
    public = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(key.public_key()))
    _jwks_path().write_text(json.dumps({"keys": [{**public, "kid": KID, "use": "sig", "alg": "ES256"}]}, indent=2))
    return True


def _init_trace_token(path) -> None:
    """Shared secret agentgateway sends with its spans, so only it can write gateway spans into the trace store."""
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"TRACE_INGEST_TOKEN={secrets.token_urlsafe(32)}\n")
    path.chmod(0o600)


_key_cache: dict = {}


def _private_key():
    path = get_settings().jwt_private_key
    if path not in _key_cache:
        if not path.exists():
            raise RuntimeError(f"{path} is missing: run `make jwt-init`")
        _key_cache[path] = serialization.load_pem_private_key(path.read_bytes(), password=None)
    return _key_cache[path]


def issue(user: str | None, bot: str | None = None, ttl: int = TTL, tools: list[str] | None = None) -> str:
    """`tools`: the gateway tools this token may list and call (a bot's allowed-tools); None = all."""
    now = int(time.time())
    claims = {"iss": get_settings().jwt_issuer, "aud": AUDIENCE, "sub": user or SHARED, "iat": now, "exp": now + ttl}
    if bot:
        claims["bot"] = bot
    if tools is not None:
        claims["tools"] = sorted(tools)
    return jwt.encode(claims, _private_key(), algorithm="ES256", headers={"kid": KID})


def verify(token: str) -> dict:
    """Claims of a token we issued; raises jwt.InvalidTokenError otherwise."""
    public = _private_key().public_key()
    return jwt.decode(token, public, algorithms=["ES256"], audience=AUDIENCE, issuer=get_settings().jwt_issuer,
                      options={"require": ["exp", "sub"]})


if __name__ == "__main__":
    import sys

    args = sys.argv[1:]
    if args == ["init"]:
        print("wrote secrets/jwt-private.pem and secrets/jwks.json" if init() else "JWT keys exist, keeping them")
    elif args[:1] == ["token"] and len(args) == 2:
        print(issue(args[1], ttl=12 * 3600))
    else:
        sys.exit(__doc__)
