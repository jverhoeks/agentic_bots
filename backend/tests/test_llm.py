"""Model names pick their provider: "<provider>:<model>", or a bare name for LLM_PROVIDER."""

from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.openai import OpenAIChatModel

from app import llm
from app.config import get_settings


def test_names_pick_the_provider(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "llm_provider", "litellm")
    monkeypatch.setattr(s, "litellm_base_url", "http://localhost:4000")
    monkeypatch.setattr(s, "litellm_api_key", "")
    monkeypatch.setattr(s, "openai_api_key", "")
    monkeypatch.setattr(s, "openai_base_url", "")
    monkeypatch.setattr(s, "anthropic_api_key", "test")
    monkeypatch.setattr(s, "openrouter_api_key", "test")
    assert llm.split("claude-sonnet") == ("litellm", "claude-sonnet")
    assert llm.split("aws/claude-5-5-opus") == ("litellm", "aws/claude-5-5-opus")
    assert llm.split("openrouter:anthropic/claude-sonnet-5.5") == ("openrouter", "anthropic/claude-sonnet-5.5")
    assert llm.split("ollama:llama3") == ("litellm", "ollama:llama3")  # unknown prefix: part of the name

    m = llm.make_model("anthropic:claude-sonnet-5-5")
    assert isinstance(m, AnthropicModel) and m.model_name == "claude-sonnet-5-5"
    m = llm.make_model("openrouter:openai/gpt-5")
    assert isinstance(m, OpenAIChatModel) and m.model_name == "openai/gpt-5" and "openrouter.ai" in m.base_url
    assert "localhost:4000/v1" in llm.make_model("claude-sonnet").base_url

    monkeypatch.setattr(s, "llm_provider", "anthropic")
    assert isinstance(llm.make_model("claude-sonnet-5-5"), AnthropicModel)
    assert llm.configured() == ["anthropic", "openrouter"]
