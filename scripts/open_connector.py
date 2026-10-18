"""Secrets and per-user access for open-connector (oomol-lab/open-connector).

    python scripts/open_connector.py init            # generate secrets/*.env (once)
    python scripts/open_connector.py user <user-id>  # (re)issue that user's runtime token

Per-user model, until login exists:
- each connection in open-connector is named after its user (connectionName = user id, e.g. "local"),
- each user has a persistent runtime token whose `allowedConnections` lists only those connections,
- agentgateway picks the token from OOMOL_USER_TOKENS (secrets/gateway.env) by the X-User-Id header.

Tokens never reach the app: they live in secrets/gateway.env (agentgateway only) and as hashes in
open-connector. Standard library only, so it runs with any python3.
"""

from __future__ import annotations

import json
import re
import secrets
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SECRETS = ROOT / "secrets"
CONNECTOR_ENV = SECRETS / "open-connector.env"
GATEWAY_ENV = SECRETS / "gateway.env"
URL = "http://localhost:3010"
USER_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
# Read-only Gmail actions until the approvals queue exists (exact IDs); widen per user with --actions=.
DEFAULT_ACTIONS = ",".join(f"gmail.{a}" for a in (
    "search_threads", "list_threads", "fetch_emails", "get_message", "fetch_message_by_message_id",
    "fetch_message_by_thread_id", "get_profile", "list_labels", "get_label", "list_history", "download_attachment",
))


def _env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    pairs = (line.split("=", 1) for line in path.read_text().splitlines() if "=" in line and not line.startswith("#"))
    return {k: v for k, v in pairs}


def _write_env(path: Path, values: dict[str, str], header: str) -> None:
    SECRETS.mkdir(exist_ok=True)
    path.write_text(header + "".join(f"{k}={v}\n" for k, v in values.items()))
    path.chmod(0o600)


def _tokens() -> dict[str, str]:
    return json.loads(_env(GATEWAY_ENV).get("OOMOL_USER_TOKENS") or "{}")


def _save_tokens(tokens: dict[str, str]) -> None:
    _write_env(GATEWAY_ENV, {"OOMOL_USER_TOKENS": json.dumps(tokens, separators=(",", ":"))},
               "# agentgateway only: per-user open-connector runtime tokens (scripts/open_connector.py user)\n")


def init() -> None:
    if CONNECTOR_ENV.exists():
        print(f"{CONNECTOR_ENV.relative_to(ROOT)} exists, keeping it")
    else:
        _write_env(CONNECTOR_ENV, {
            "OOMOL_CONNECT_ENCRYPTION_KEY": secrets.token_urlsafe(32),
            "OOMOL_CONNECT_ADMIN_TOKEN": secrets.token_urlsafe(32),
            # turns on runtime auth for /mcp; used by no client (the gateway sends per-user tokens)
            "OOMOL_CONNECT_RUNTIME_TOKEN": secrets.token_urlsafe(32),
        }, "# open-connector only. Losing the encryption key makes stored credentials unreadable.\n")
        print(f"wrote {CONNECTOR_ENV.relative_to(ROOT)}")
    if not GATEWAY_ENV.exists():
        _save_tokens({})
        print(f"wrote {GATEWAY_ENV.relative_to(ROOT)} (no users yet)")


def _api(method: str, path: str, body: dict | None = None):
    admin = _env(CONNECTOR_ENV).get("OOMOL_CONNECT_ADMIN_TOKEN", "")
    req = urllib.request.Request(f"{URL}{path}", method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"authorization": f"Bearer {admin}", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        sys.exit(f"{method} {path}: {e.code} {e.read().decode()[:500]}")


def _items(data, key: str) -> list[dict]:
    return data if isinstance(data, list) else (data or {}).get(key) or []


def user(user_id: str, actions: str = DEFAULT_ACTIONS) -> None:
    if not USER_RE.match(user_id):
        sys.exit(f"invalid user id {user_id!r} (lowercase letters, digits and dashes)")
    conns = [c for c in _items(_api("GET", "/api/connections"), "connections")
             if c.get("connectionName") == user_id and c.get("id")]
    if not conns:
        # an empty allowedConnections list would mean "every connection": never issue that
        sys.exit(f"no connections named {user_id!r} yet. In the console ({URL}) connect a provider "
                 f"with connection name {user_id!r}, then rerun.")
    body = {"name": f"user:{user_id}", "allowedActions": [a for a in actions.split(",") if a],
            "blockedActions": [], "allowedProxies": [], "allowedConnections": [c["id"] for c in conns]}
    old = [t["id"] for t in _items(_api("GET", "/api/runtime-tokens"), "tokens") if t.get("name") == body["name"]]
    created = _api("POST", "/api/runtime-tokens", body)
    token = created.get("token") or (created.get("runtimeToken") or {}).get("token")
    if not token:
        sys.exit(f"unexpected response, no token in it: {list(created)}")
    _save_tokens({**_tokens(), user_id: token})
    for token_id in old:  # rotate: the secret is only shown on create, so replace rather than reuse
        _api("DELETE", f"/api/runtime-tokens/{token_id}")
    names = ", ".join(f"{c.get('service')}" for c in conns)
    print(f"{user_id}: token for {len(conns)} connection(s) ({names}), actions {body['allowedActions'] or 'all'}")
    print("apply it: docker compose up -d agentgateway")


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["init"]:
        init()
    elif args[:1] == ["user"] and len(args) in (2, 3):
        user(args[1], *(a.removeprefix("--actions=") for a in args[2:]))
    else:
        sys.exit(__doc__)
