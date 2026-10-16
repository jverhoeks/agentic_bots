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
