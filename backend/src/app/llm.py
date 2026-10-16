"""Model access through the external LiteLLM proxy (OpenAI-compatible API)."""

import asyncio
import time

import httpx
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from app.config import get_settings

_models_cache: tuple[float, list[str]] | None = None


def _base_url() -> str:
    url = get_settings().litellm_base_url.rstrip("/")
    return url if url.endswith("/v1") else f"{url}/v1"


def make_model(name: str) -> OpenAIChatModel:
    s = get_settings()
    return OpenAIChatModel(name, provider=OpenAIProvider(base_url=_base_url(), api_key=s.litellm_api_key or "none"))


async def list_models() -> list[str]:
    """Models bots may use: ALLOWED_MODELS if set, otherwise what LiteLLM exposes for this key."""
    global _models_cache
    s = get_settings()
    if s.allowed_model_list:
        return s.allowed_model_list
    if _models_cache and time.monotonic() - _models_cache[0] < 300:
        return _models_cache[1]
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(f"{_base_url()}/models", headers={"Authorization": f"Bearer {s.litellm_api_key}"})
        r.raise_for_status()
    models = sorted(m["id"] for m in r.json().get("data", []))
    _models_cache = (time.monotonic(), models)
    return models


async def _smoke() -> None:
    from pydantic_ai import Agent

    s = get_settings()
    print(f"LiteLLM: {_base_url()}")
    try:
        print("models:", ", ".join(await list_models()) or "(none)")
    except Exception as e:  # listing is optional, chat is what matters
        print(f"could not list models: {e}")
    agent = Agent(make_model(s.builder_model))
    result = await agent.run("Reply with exactly: pong")
    print(f"{s.builder_model}: {result.output}")


if __name__ == "__main__":
    asyncio.run(_smoke())
