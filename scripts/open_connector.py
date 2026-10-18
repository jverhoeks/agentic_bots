"""Secrets for open-connector (oomol-lab/open-connector) and the apps broker.

    python scripts/open_connector.py init   # generate secrets/open-connector.env and secrets/apps-broker.env (once)

- secrets/open-connector.env: open-connector only (encryption key, admin token, runtime auth).
- secrets/apps-broker.env: the admin token only, for the apps broker (connectors/src/connectors/apps_broker.py),
  which issues each user's runtime token and keeps it in its own volume.

Users connect their apps on the Connections page of the app; nothing here is per user any more.
Standard library only, so it runs with any python3.
"""

from __future__ import annotations

import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SECRETS = ROOT / "secrets"
CONNECTOR_ENV = SECRETS / "open-connector.env"
BROKER_ENV = SECRETS / "apps-broker.env"


def _env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    pairs = (line.split("=", 1) for line in path.read_text().splitlines() if "=" in line and not line.startswith("#"))
    return {k: v for k, v in pairs}


def _write_env(path: Path, values: dict[str, str], header: str) -> None:
    SECRETS.mkdir(exist_ok=True)
    path.write_text(header + "".join(f"{k}={v}\n" for k, v in values.items()))
    path.chmod(0o600)


def init() -> None:
    if CONNECTOR_ENV.exists():
        print(f"{CONNECTOR_ENV.relative_to(ROOT)} exists, keeping it")
    else:
        _write_env(CONNECTOR_ENV, {
            "OOMOL_CONNECT_ENCRYPTION_KEY": secrets.token_urlsafe(32),
            "OOMOL_CONNECT_ADMIN_TOKEN": secrets.token_urlsafe(32),
            # turns on runtime auth for /mcp; used by no client (the apps broker sends per-user tokens)
            "OOMOL_CONNECT_RUNTIME_TOKEN": secrets.token_urlsafe(32),
        }, "# open-connector only. Losing the encryption key makes stored credentials unreadable.\n")
        print(f"wrote {CONNECTOR_ENV.relative_to(ROOT)}")
    if BROKER_ENV.exists():
        return
    admin = _env(CONNECTOR_ENV)["OOMOL_CONNECT_ADMIN_TOKEN"]
    _write_env(BROKER_ENV, {"OOMOL_CONNECT_ADMIN_TOKEN": admin},
               "# apps broker only: the open-connector admin token (copied from open-connector.env)\n")
    print(f"wrote {BROKER_ENV.relative_to(ROOT)}")


if __name__ == "__main__":
    if sys.argv[1:] == ["init"]:
        init()
    elif sys.argv[1:2] == ["user"]:
        sys.exit("per-user tokens are issued by the apps broker now: connect apps on the Connections page of the app")
    else:
        sys.exit(__doc__)
