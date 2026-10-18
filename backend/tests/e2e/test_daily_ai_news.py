"""Functional test of the first use case, end to end with real models and tools.

The user asks Main for a bot; the builder agent must create it from scratch, the bot must
run and produce a digest, and a follow-up question must work in the bot session.
Run with `make e2e` (needs .env with LiteLLM and `make up`). Artifacts land in data/e2e/.
"""

import asyncio
import os
import re

import pytest
from sqlmodel import select

from app import auth, gateway, spec
from app.agents import bot_runner, builder
from app.config import ROOT, get_settings
from app.db import db
from app.llm import list_models
from app.models import Message, Run, Session

pytestmark = pytest.mark.e2e

REQUEST = "create a bot to get the daily AI news from hackernews"
NUDGE = "Use sensible defaults for anything you are unsure about and create it now."
UPDATE = ("Update the daily AI news bot: keep at most 5 top stories, and add a line "
          "'*Why it matters for enterprises:*' to each story.")
FOLLOW_UP = "Find broader coverage of the second story: search the web and summarise what other sources say."
OUT = ROOT / "data" / "e2e"
USER = "e2e"  # owner of the bot the builder creates


async def say(gen) -> dict:
    """Drain an agent turn; return the final event plus the tool calls made."""
    auth.current_user.set(USER)  # builder tools act as the logged-in user
    tools, last = [], {}
    async for ev in gen:
        if ev["type"] == "tool_call":
            tools.append(ev["name"])
        elif ev["type"] in ("done", "error"):
            last = ev
    return {**last, "tools": tools}


@pytest.fixture(scope="module", autouse=True)
async def stack():
    if not os.environ.get("E2E"):
        pytest.skip("e2e only runs with E2E=1 (make e2e)")
    try:
        await list_models()
    except Exception as e:
        pytest.skip(f"no model provider reachable ({e}); check .env")
    try:
        await gateway.catalog(refresh=True)
    except Exception as e:
        pytest.skip(f"MCP gateway not reachable ({e}); run `make up`")
    with db() as s:
        if not s.get(Session, builder.main_session(USER)):
            s.add(Session(id=builder.main_session(USER), kind="main", owner=USER, title="Main"))
            s.commit()
    OUT.mkdir(parents=True, exist_ok=True)


async def test_builder_creates_bot_from_chat():
    turn = await say(builder.chat(USER, REQUEST))
    if not spec.list_bots():  # the builder may ask one round of questions first
        turn = await say(builder.chat(USER, NUDGE))
    assert turn["type"] == "done", turn
    bots = spec.list_bots()
    assert len(bots) == 1, f"expected one bot, builder said: {turn['message']['content']}"
    bot = bots[0]
    (OUT / "SKILL.md").write_text(spec.raw_skill_md(bot.name))

    assert any(t.startswith("hackernews_") for t in bot.tools), bot.tools
    assert len(bot.instructions) > 200, "instructions should be a real brief"


async def test_bot_run_produces_digest():
    bot = spec.list_bots()[0]
    run = bot_runner.start_run(bot)
    for _ in range(600):  # up to 10 minutes
        with db() as s:
            r = s.get(Run, run.id)
        if r.status != "running":
            break
        await asyncio.sleep(1)
    assert r.status == "ok", r.error

    with db() as s:
        msg = s.exec(select(Message).where(Message.run_id == run.id, Message.role == "assistant")).one()
        shown = s.exec(select(Message).where(Message.session_id == r.session_id, Message.role == "user")).all()
    (OUT / "digest.md").write_text(msg.content)
    links = re.findall(r"https?://\S+", msg.content)
    assert len(msg.content) > 500
    assert len(links) >= 3, "digest should link its sources"
    assert re.search(r"\bAI\b|LLM|model", msg.content)
    assert not shown, "the synthetic run prompt must not be shown as a user message"


async def test_follow_up_uses_web_tools():
    bot = spec.list_bots()[0]
    search = [t for t in bot.tools if "search" in t]
    assert search, f"builder should grant a search tool for follow-up research: {bot.tools}"
    turn = await say(bot_runner.chat(bot, FOLLOW_UP))
    (OUT / "follow-up.md").write_text(turn.get("message", {}).get("content", ""))
    assert turn["type"] == "done", turn
    assert any(t in search for t in turn["tools"]), f"follow-up should search the web: {turn['tools']}"


async def test_update_bot_by_prompt():
    before = spec.list_bots()[0]
    turn = await say(builder.chat(USER, UPDATE))
    assert turn["type"] == "done", turn
    assert "update_bot" in turn["tools"], turn["tools"]
    after = spec.get_bot(before.name)
    (OUT / "SKILL.updated.md").write_text(spec.raw_skill_md(after.name))
    assert len(spec.list_bots()) == 1, "update must not create a second bot"
    assert after.updated_at > before.updated_at
    assert "enterprise" in after.instructions.lower()
    assert set(before.tools) <= set(after.tools), "unrelated settings should be kept"
