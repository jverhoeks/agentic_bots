import os
import tempfile
from pathlib import Path

_tmp = Path(tempfile.mkdtemp(prefix="agentic-bots-test-"))
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp / 'test.db'}"
os.environ["BOTS_DIR"] = str(_tmp / "bots")
if not os.environ.get("E2E"):  # e2e tests use the real models from .env
    os.environ["ALLOWED_MODELS"] = "test-model"
    os.environ["DEFAULT_BOT_MODEL"] = "test-model"
    os.environ["BUILDER_MODEL"] = "test-model"

from app.db import init_db  # noqa: E402

init_db()
