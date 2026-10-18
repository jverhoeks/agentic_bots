import os
from functools import lru_cache
from typing import Literal
from pathlib import Path

from zoneinfo import ZoneInfo

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[3]
os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")


class Settings(BaseSettings):
    # secrets/trace-ingest.env: the gateway span token (`make jwt-init`), for a backend run on the host
    model_config = SettingsConfigDict(env_file=(ROOT / "secrets" / "trace-ingest.env", ROOT / ".env", ".env"), extra="ignore")

    # Models. LLM_PROVIDER serves model names without a prefix; "<provider>:<model>" picks another one per bot
    # (e.g. "anthropic:claude-sonnet-5-5", "openrouter:openai/gpt-5"). Each provider needs its key (or URL).
    llm_provider: Literal["litellm", "openai", "anthropic", "openrouter"] = "litellm"
    litellm_base_url: str = "http://localhost:4000"  # LiteLLM proxy (OpenAI-compatible)
    litellm_api_key: str = ""
    openai_api_key: str = ""
    openai_base_url: str = ""  # empty = api.openai.com; any OpenAI-compatible server (vLLM, Ollama, Azure, ...)
    anthropic_api_key: str = ""
    openrouter_api_key: str = ""
    builder_model: str = "claude-sonnet"
    default_bot_model: str = "claude-sonnet"
    allowed_models: str = ""  # comma separated; empty = ask LiteLLM /v1/models

    # Tools: MCP servers in standard mcp.json format (points at agentgateway)
    mcp_config: Path = ROOT / "config" / "mcp.json"
    a2a_agents_config: Path = ROOT / "config" / "a2a-agents.json"  # remote A2A agents bots may call

    # Login (basic, until OIDC): cookie signing secret (empty = generated into data/), HTTPS-only cookie,
    # and whether anyone may register after the first account.
    session_secret: str = ""
    cookie_secure: bool = False
    allow_signup: bool = False
    data_dir: Path = ROOT / "data"
    # Gateway identity: the backend signs a short-lived JWT (sub = bot owner) for every MCP/A2A call;
    # agentgateway checks it with the public JWKS. Both files come from `make jwt-init`.
    jwt_private_key: Path = ROOT / "secrets" / "jwt-private.pem"
    jwt_issuer: str = "agentic-bots"
    # Single sign-on (any OpenID Connect provider); on when issuer and client id are set.
    oidc_issuer: str = ""  # e.g. https://login.microsoftonline.com/<tenant>/v2.0, https://accounts.google.com
    oidc_client_id: str = ""
    oidc_client_secret: str = ""  # empty for a public client (PKCE only)
    oidc_redirect_url: str = ""  # empty = <this backend>/api/auth/oidc/callback; behind a proxy set the public URL
    oidc_scopes: str = "openid profile email"
    oidc_name: str = "SSO"  # button label
    oidc_auto_signup: bool = True  # create accounts on first SSO login (false: only the very first account)
    oidc_post_login_url: str = "/"

    # Storage
    bots_dir: Path = ROOT / "bots"
    database_url: str = f"sqlite:///{ROOT / 'data' / 'bots.db'}"

    # Bot runtime
    schedule_timezone: str = "UTC"  # bots' cron schedules are in this zone, e.g. Europe/Amsterdam
    history_full_turns: int = 6  # older turns get tool outputs stripped

    # Guardrails, per agent turn (0 = no limit). Hitting one ends the turn with an error and an audit event.
    max_prompt_chars: int = 20_000
    turn_token_limit: int = 1_500_000
    turn_tool_call_limit: int = 60

    # Observability. Every chat turn, run, tool call, MCP and A2A request is a trace, stored in the app
    # database (Trace/TraceSpan, per user and session) and also sent to this OTLP/HTTP endpoint if set (a collector).
    otel_exporter_otlp_endpoint: str = ""
    trace_store: bool = True  # keep spans in the app database
    trace_content: bool = True  # prompts, answers, tool arguments and results in spans (redacted, capped)
    trace_max_attr_chars: int = 20_000  # per span attribute
    trace_retention_days: int = 30  # 0 = keep forever
    # agentgateway sends this with its spans (secrets/trace-ingest.env, `make jwt-init`); empty = refuse them
    trace_ingest_token: str = ""
    trace_ui_url: str = ""  # optional external trace UI behind that collector (<url>/trace/<id>); empty = no links
    # Activity events (tool, LLM, MCP, A2A calls) as CloudEvents, POSTed in batches to this URL if set
    # (application/cloudevents-batch+json), e.g. the lineage/quality repo or a NATS/Kafka bridge. Best effort.
    event_sink_url: str = ""
    event_sink_token: str = ""  # sent as `authorization: Bearer <token>`
    event_content: bool = False  # include tool arguments and results (redacted, capped) in the events
    gateway_ui_url: str = "http://localhost:15000/ui"

    @field_validator("schedule_timezone")
    @classmethod
    def _zone(cls, v: str) -> str:
        ZoneInfo(v)  # fail at startup on a typo, not silently in the scheduler
        return v

    @property
    def allowed_model_list(self) -> list[str]:
        # model ids never contain '#': drop inline comments (docker env_file keeps "KEY=  # text" as the value)
        return [m.strip() for m in self.allowed_models.split("#")[0].split(",") if m.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
