"""Model access: LiteLLM (default), OpenAI (or any OpenAI-compatible server), Anthropic and OpenRouter.

A model name is "<provider>:<model>" or a bare "<model>" for LLM_PROVIDER, so bots can mix providers:
"claude-sonnet" (via LiteLLM), "anthropic:claude-sonnet-5-5", "openai:gpt-5", "openrouter:anthropic/claude-sonnet-5.5".
"""

import asyncio
import time

import httpx
from pydantic_ai.models import Model

from app.config import get_settings

PROVIDERS = ("litellm", "openai", "anthropic", "openrouter")
_models_cache: tuple[float, list[str]] | None = None


def split(name: str) -> tuple[str, str]:
    """("anthropic", "claude-...") from "anthropic:claude-..."; a bare name belongs to LLM_PROVIDER."""
    provider, sep, model = name.partition(":")
    return (provider, model) if sep and provider in PROVIDERS else (get_settings().llm_provider, name)


def _v1(url: str) -> str:
    url = url.rstrip("/")
    return url if url.endswith("/v1") else f"{url}/v1"


def configured() -> list[str]:
    """Providers with credentials (LiteLLM counts when it is the default: it may run without a key)."""
    s = get_settings()
    have = {"litellm": s.llm_provider == "litellm" or bool(s.litellm_api_key), "openai": bool(s.openai_api_key or s.openai_base_url),
            "anthropic": bool(s.anthropic_api_key), "openrouter": bool(s.openrouter_api_key)}
    return [p for p in PROVIDERS if have[p]]


def make_model(name: str) -> Model:
    s = get_settings()
    provider, model = split(name)
    if provider == "anthropic":
        from pydantic_ai.models.anthropic import AnthropicModel
        from pydantic_ai.providers.anthropic import AnthropicProvider

        return AnthropicModel(model, provider=AnthropicProvider(api_key=s.anthropic_api_key or None))
    from pydantic_ai.models.openai import OpenAIChatModel

    if provider == "openrouter":
        from pydantic_ai.providers.openrouter import OpenRouterProvider

        return OpenAIChatModel(model, provider=OpenRouterProvider(api_key=s.openrouter_api_key or None, app_title="agentic-bots"))
    from pydantic_ai.providers.openai import OpenAIProvider

    if provider == "openai":
        return OpenAIChatModel(model, provider=OpenAIProvider(base_url=s.openai_base_url or None, api_key=s.openai_api_key or "none"))
    return OpenAIChatModel(model, provider=OpenAIProvider(base_url=_v1(s.litellm_base_url), api_key=s.litellm_api_key or "none"))


async def _list(client: httpx.AsyncClient, provider: str) -> list[str]:
    s = get_settings()
    if provider == "anthropic":
        r = await client.get("https://api.anthropic.com/v1/models", params={"limit": 1000},
                             headers={"x-api-key": s.anthropic_api_key, "anthropic-version": "2023-06-01"})
    elif provider == "openrouter":
        r = await client.get("https://openrouter.ai/api/v1/models", headers={"Authorization": f"Bearer {s.openrouter_api_key}"})
    elif provider == "openai":
        r = await client.get(f"{_v1(s.openai_base_url or 'https://api.openai.com')}/models",
                             headers={"Authorization": f"Bearer {s.openai_api_key}"})
    else:
        r = await client.get(f"{_v1(s.litellm_base_url)}/models", headers={"Authorization": f"Bearer {s.litellm_api_key}"})
    r.raise_for_status()
    prefix = "" if provider == s.llm_provider else f"{provider}:"
    return [prefix + m["id"] for m in r.json().get("data", [])]


async def list_models() -> list[str]:
    """Models bots may use: ALLOWED_MODELS if set, otherwise what every configured provider lists for its key.

    OpenRouter lists hundreds of models: set ALLOWED_MODELS to keep the builder's choice short.
    """
    global _models_cache
    s = get_settings()
    if s.allowed_model_list:
        return s.allowed_model_list
    if _models_cache and time.monotonic() - _models_cache[0] < 300:
        return _models_cache[1]
    providers = configured()
    async with httpx.AsyncClient(timeout=15) as client:
        found = await asyncio.gather(*(_list(client, p) for p in providers), return_exceptions=True)
    if found and all(isinstance(f, BaseException) for f in found):
        raise found[0]
    models = sorted(m for f in found if not isinstance(f, BaseException) for m in f)
    _models_cache = (time.monotonic(), models)
    return models


async def _smoke() -> None:
    from pydantic_ai import Agent

    s = get_settings()
    print(f"default provider: {s.llm_provider}; configured: {', '.join(configured()) or '(none)'}")
    try:
        models = await list_models()
        print(f"models: {len(models)}", ", ".join(models[:20]) + (" ..." if len(models) > 20 else ""))
    except Exception as e:  # listing is optional, chat is what matters
        print(f"could not list models: {e}")
    agent = Agent(make_model(s.builder_model))
    result = await agent.run("Reply with exactly: pong")
    print(f"{s.builder_model}: {result.output}")


if __name__ == "__main__":
    asyncio.run(_smoke())
