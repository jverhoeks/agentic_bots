from pydantic_ai import ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart

from app import gateway, history


def turn(i):
    return [
        ModelRequest(parts=[UserPromptPart(content=f"q{i}")]),
        ModelResponse(parts=[ToolCallPart(tool_name="t", args={}, tool_call_id=f"c{i}")]),
        ModelRequest(parts=[ToolReturnPart(tool_name="t", content="x" * 5000, tool_call_id=f"c{i}")]),
        ModelResponse(parts=[TextPart(content=f"a{i}")]),
    ]


def test_compact_keeps_recent_turns_full():
    msgs = [m for i in range(5) for m in turn(i)]
    out = history.compact(msgs, full_turns=2)
    assert len(out) == len(msgs)
    returns = [p for m in out for p in getattr(m, "parts", []) if isinstance(p, ToolReturnPart)]
    assert [len(p.content) > 4000 for p in returns] == [False, False, False, True, True]
    assert history.load(history.dump(msgs))  # serialises


def test_expand_tool_refs():
    names = ["hackernews_hn_top_stories", "hackernews_hn_item", "web_web_search", "web_fetch_url"]
    assert gateway.expand_tool_refs(["web", "hackernews_hn_item"], names) == [
        "web_web_search", "web_fetch_url", "hackernews_hn_item"]
    assert gateway.expand_tool_refs(["nope"], names) == []


def test_allowed_models_ignores_inline_comment():
    from app.config import Settings

    assert Settings(allowed_models="# optional comma list; empty = all").allowed_model_list == []
    assert Settings(allowed_models="a, b  # comment").allowed_model_list == ["a", "b"]


def test_gateway_headers_carry_user_and_apps_rule_only_when_granted():
    from app import gateway, spec, tokens
    from app.agents import bot_runner

    h = gateway._headers({"headers": {"X-A": "1"}}, "alice", "news-bot")
    claims = tokens.verify(h["Authorization"].removeprefix("Bearer "))
    assert h["X-A"] == "1" and claims["sub"] == "alice" and claims["bot"] == "news-bot"
    # shared bots have no owner: a token for the shared subject, which has no personal connectors
    assert tokens.verify(gateway._headers({}, None)["Authorization"][7:])["sub"] == tokens.SHARED
    bot = spec.BotSpec(name="apps-bot", title="Apps", description="d", instructions="# Goal\nRead my inbox daily.",
                       model="m", tools=["apps"])
    assert "apps_list_connections" in bot_runner._apps_rule(bot)
    assert bot_runner._apps_rule(bot.model_copy(update={"tools": ["web"]})) == ""
