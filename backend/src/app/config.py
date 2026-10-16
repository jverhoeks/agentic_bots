import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[3]
os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=(ROOT / ".env", ".env"), extra="ignore")

    # Models: external LiteLLM (OpenAI-compatible)
    litellm_base_url: str = "http://localhost:4000"
    litellm_api_key: str = ""
    builder_model: str = "claude-sonnet"
    default_bot_model: str = "claude-sonnet"
    allowed_models: str = ""  # comma separated; empty = ask LiteLLM /v1/models

    # Tools: MCP servers in standard mcp.json format (points at agentgateway)
    mcp_config: Path = ROOT / "config" / "mcp.json"

    # Storage
    bots_dir: Path = ROOT / "bots"
    database_url: str = f"sqlite:///{ROOT / 'data' / 'bots.db'}"

    # Bot runtime
    history_full_turns: int = 6  # older turns get tool outputs stripped

    @property
    def allowed_model_list(self) -> list[str]:
        return [m.strip() for m in self.allowed_models.split(",") if m.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
