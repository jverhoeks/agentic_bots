"""Bots as A2A servers: Agent Card and message/send end to end over HTTP (ASGI, TestModel)."""

import asyncio
import uuid

import httpx
import pytest
from pydantic_ai.models.test import TestModel
from sqlmodel import select

from app import a2a_server, spec, tokens
from app.agents import bot_runner
from app.db import db
from app.main import app
from app.models import Run

BOT = "a2a-server-bot"


@pytest.fixture
async def client(monkeypatch):
    spec.save_bot(spec.BotSpec(name=BOT, title="Server Bot", description="Answers A2A requests.",
                               instructions="# Goal\nAnswer questions from other agents.", model="test-model"))
    monkeypatch.setattr(bot_runner, "make_model", lambda _m: TestModel(custom_output_text="the answer"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bots.test",
                                 headers={"Authorization": f"Bearer {tokens.issue(None)}"}) as c:
        yield c
    await a2a_server.mount.aclose()
    spec.delete_bot(BOT)


async def test_agent_card(client):
    r = await client.get(f"/a2a/{BOT}/.well-known/agent-card.json")
    assert r.status_code == 200
    card = r.json()
    assert card["name"] == "Server Bot" and card["skills"][0]["id"] == BOT
    assert card["supportedInterfaces"][0]["url"] == f"http://bots.test/a2a/{BOT}/"
    assert (await client.get("/a2a/no-such-bot/.well-known/agent-card.json")).status_code == 404


async def test_a2a_needs_a_token_for_the_bot_owner(client):
    card = f"/a2a/{BOT}/.well-known/agent-card.json"
    assert (await client.get(card, headers={"Authorization": ""})).status_code == 401
    assert (await client.get(card, headers={"Authorization": "Bearer forged.token.here"})).status_code == 401
    spec.save_bot(spec.get_bot(BOT).model_copy(update={"owner": "alice"}))
    assert (await client.get(card)).status_code == 404  # the shared subject cannot reach alice's bot
    alice = {"Authorization": f"Bearer {tokens.issue('alice')}"}
    assert (await client.get(card, headers=alice)).status_code == 200
    assert (await client.get(card, headers={"Authorization": f"Bearer {tokens.issue('bob')}"})).status_code == 404


async def test_message_send_runs_the_bot(client):
    msg = {"role": "user", "parts": [{"text": "what is new?"}], "messageId": str(uuid.uuid4())}
    r = await client.post(f"/a2a/{BOT}/", json={"jsonrpc": "2.0", "id": 1, "method": "message/send",
                                               "params": {"message": msg}})
    assert r.status_code == 200, r.text
    task_id = r.json()["result"]["task"]["id"]  # A2A 1.0: SendMessageResponse wraps the task
    for _ in range(50):
        r = await client.post(f"/a2a/{BOT}/", json={"jsonrpc": "2.0", "id": 2, "method": "tasks/get",
                                                   "params": {"id": task_id}})
        task = r.json()["result"]
        if task["status"]["state"] in ("completed", "failed"):
            break
        await asyncio.sleep(0.05)
    assert task["status"]["state"] == "completed", task
    assert task["artifacts"][0]["parts"][0]["text"] == "the answer"
    with db() as s:
        run = s.exec(select(Run).where(Run.bot_name == BOT)).one()
    assert run.trigger == "a2a:user:shared" and run.status == "ok"


async def test_remote_client_round_trip(client, monkeypatch, tmp_path):
    """a2a_remote_* tools against our own A2A server (in-process ASGI instead of the network)."""
    from app.agents import a2a
    from app.config import get_settings

    cfg = tmp_path / "a2a-agents.json"
    cfg.write_text('{"agents": {"server": {"url": "http://bots.test/a2a/%s/", "headers": {"Authorization": "Bearer %s"}}}}'
                   % (BOT, tokens.issue(None)))
    monkeypatch.setattr(get_settings(), "a2a_agents_config", cfg)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(a2a.httpx, "AsyncClient",
                        lambda **kw: real_client(transport=httpx.ASGITransport(app=app), **kw))
    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda s: real_sleep(min(s, 0.05)))  # poll fast

    tools = a2a.toolset("caller-bot").tools
    listed = await tools["a2a_remote_list_agents"].function()
    assert listed[0]["agent"] == "server" and listed[0]["name"] == "Server Bot"
    res = await tools["a2a_remote_send_message"].function("server", "hello")
    assert isinstance(res, dict), res
    assert res["status"]["state"] == "completed" and res["answer"] == "the answer"
    assert "not an allowed" in await tools["a2a_remote_send_message"].function("evil", "hi")
