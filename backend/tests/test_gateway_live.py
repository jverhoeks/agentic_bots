import pytest
from pydantic_ai import Agent
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai import ModelResponse, TextPart, ToolCallPart

from app import gateway

pytestmark = pytest.mark.network


async def test_catalog_lists_connectors():
    tools = {t["name"]: t for t in await gateway.catalog(refresh=True)}
    assert {"hackernews_hn_top_stories", "web_web_search", "web_fetch_url"} <= set(tools)
    assert tools["hackernews_hn_top_stories"]["read_only"]


async def test_bot_sees_only_allowed_tools_and_calls_through_gateway():
    seen: list[str] = []

    def model(messages, info: AgentInfo):
        if len(messages) == 1:
            seen.extend(t.name for t in info.function_tools)
            return ModelResponse(parts=[ToolCallPart("hackernews_hn_top_stories", {"limit": 2})])
        return ModelResponse(parts=[TextPart(str(messages[-1].parts[0].content)[:200])])

    agent = Agent(FunctionModel(model), toolsets=gateway.toolsets(bot="t", allowed=["hackernews_hn_top_stories"]))
    result = await agent.run("go")
    assert seen == ["hackernews_hn_top_stories"]
    assert result.output.startswith("[{'id':")  # the gateway returned HN stories
