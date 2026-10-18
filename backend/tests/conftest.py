import os
import tempfile
from pathlib import Path

_tmp = Path(tempfile.mkdtemp(prefix="agentic-bots-test-"))
# TEST_DATABASE_URL=postgresql+psycopg://... runs the suite on PostgreSQL (use a throwaway database)
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{_tmp / 'test.db'}"
os.environ["BOTS_DIR"] = str(_tmp / "bots")
os.environ["DATA_DIR"] = str(_tmp / "data")  # session-cookie secret
os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = ""  # no traces from tests, whatever .env says
os.environ["TRACE_INGEST_TOKEN"] = "test-trace-token"  # the gateway's OTLP export token
os.environ["JWT_PRIVATE_KEY"] = str(_tmp / "secrets" / "jwt-private.pem")  # gateway tokens
if not os.environ.get("E2E"):  # e2e tests use the real models from .env
    os.environ["ALLOWED_MODELS"] = "test-model"
    os.environ["DEFAULT_BOT_MODEL"] = "test-model"
    os.environ["BUILDER_MODEL"] = "test-model"

from app import tokens  # noqa: E402
from app.db import init_db  # noqa: E402

init_db()
tokens.init()
