"""A bot can change itself, from its chat or a run (never from A2A), and the user always gets a heads-up."""

import json

import pytest

from app import audit, auth, events, spec
from app.agents import bot_runner, builder_tools

BOT = "self-edit-bot"
CATALOG = [{"name": n, "connector": n.split("_")[0], "description": "", "read_only": True}
           for n in ("web_web_search", "web_fetch_url", "hackernews_hn_top_stories")]


@pytest.fixture
def bot(monkeypatch):
    async def catalog(refresh=False, user=None):
        return CATALOG

    async def models():
        return ["test-model"]

    monkeypatch.setattr(builder_tools.gateway, "catalog", catalog)
    monkeypatch.setattr(builder_tools, "_list_models", models)
    b = spec.save_bot(spec.BotSpec(name=BOT, title="Self edit", description="d", instructions="# Goal\nA daily HN digest of the top stories.",
                                   model="test-model", tools=["hackernews_hn_top_stories"], owner="alice"))
    token = auth.current_user.set("alice")
    got: list[dict] = []
    events.on(events.OWN, got.append)
    yield b, got
    events.off(got.append)
    auth.current_user.reset(token)
    spec.delete_bot(BOT)


def _tool_names(toolsets) -> set[str]:
    return {n for ts in toolsets for n in getattr(ts.wrapped, "tools", {})}


def test_editing_is_opt_in(bot):
    b, _ = bot
    assert "update_my_definition" not in _tool_names(bot_runner._toolsets(b))  # A2A
    assert {"get_my_definition", "update_my_definition"} <= _tool_names(bot_runner._toolsets(b, editable=True))
    assert "update_my_definition" in str(bot_runner.build_agent(b, editable=True)._instructions)
    assert "update_my_definition" not in str(bot_runner.build_agent(b)._instructions)


async def test_update_from_the_bot_chat(bot):
    b, got = bot
    tools = builder_tools.self_edit_toolset(BOT).tools
    assert "HN digest" in await tools["get_my_definition"].function()

    res = await tools["update_my_definition"].function(instructions="# Goal\nHN digest, plus broader coverage.",
                                                       tools=["hackernews_hn_top_stories", "web"])
    assert res.startswith("Updated") and "next message" in res
    after = spec.get_bot(BOT)
    assert "broader coverage" in after.instructions and after.title == "Self edit"  # untouched fields stay
    assert set(after.tools) == {"hackernews_hn_top_stories", "web_web_search", "web_fetch_url"}
    assert [e["type"] for e in got] == ["com.agenticbots.bot.updated"]
    e = audit.events(kind="bot.updated", bot=BOT, limit=1)[0]
    assert json.loads(e.detail_json)["via"] == "bot-chat"

    assert "runs by itself" in await tools["update_my_definition"].function(schedule="0 7 * * 1-5")
    assert spec.get_bot(BOT).trigger == "schedule"
    await tools["update_my_definition"].function(schedule="")
    assert spec.get_bot(BOT).schedule is None and "broader coverage" in spec.get_bot(BOT).instructions

    assert "Unknown tools" in await tools["update_my_definition"].function(tools=["shell_exec"])  # validated
    auth.current_user.set("bob")  # someone who may not change alice's bot
    assert (await tools["update_my_definition"].function(description="hijacked")).startswith("No bot")
    assert spec.get_bot(BOT).description == "d"


def test_runs_may_edit_but_a2a_may_not(bot):
    b, _ = bot
    run_tools = bot_runner._toolsets(b, editable=True, via="bot-run")
    assert "update_my_definition" in _tool_names(run_tools)
    assert "on your own" in str(bot_runner.build_agent(b, editable=True, via="bot-run")._instructions)


async def test_a_self_edit_gives_the_user_a_heads_up(bot):
    from app.db import db
    from app.models import Message
    from sqlmodel import select

    _, got = bot
    tools = builder_tools.self_edit_toolset(BOT, via="bot-run").tools
    res = await tools["update_my_definition"].function(tools=["hackernews_hn_top_stories", "web_fetch_url"], verbosity="medium")
    assert "notified" in res
    data = got[-1]["data"]
    assert data["via"] == "bot-run" and any("new tools" in c and "web_fetch_url" in c for c in data["changes"])
    assert "verbosity `short` → `medium`" in data["changes"]
    with db() as s:
        notes = s.exec(select(Message).where(Message.session_id == f"bot-{BOT}", Message.role == "notice")).all()
    assert notes and "updated itself" in notes[-1].content and data["notice"]["id"] == notes[-1].id
    # the same definition again: no change, no notice
    assert (await tools["update_my_definition"].function(verbosity="medium")).startswith("Nothing changed")
    assert len(got) == 1
