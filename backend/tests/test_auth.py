"""Basic login and per-user scoping: bots, sessions, A2A and connector identity follow the bot owner."""

import httpx
import pytest

from app import gateway, spec
from app.agents import a2a, bot_runner, builder_tools
from app.auth import current_user
from app.db import db
from app.main import app
from app.models import User


@pytest.fixture
async def client():
    with db() as s:  # fresh users table: the first account may register itself
        for u in s.query(User).all():
            s.delete(u)
        s.commit()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bots.test") as c:
        yield c


async def _register(c: httpx.AsyncClient, user: str) -> httpx.Response:
    return await c.post("/api/auth/register", json={"username": user, "password": "correct horse"})


async def test_first_user_registers_then_signup_closes(client):
    assert (await client.get("/api/bots")).status_code == 401
    assert (await client.get("/api/auth/status")).json() == {"user": None, "can_register": True, "oidc": None}
    r = await _register(client, "alice")
    assert r.status_code == 200 and r.json()["id"] == "alice"
    assert (await client.get("/api/bots")).status_code == 200
    assert (await client.get("/api/auth/status")).json()["user"]["id"] == "alice"

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bots.test") as other:
        assert (await _register(other, "mallory")).status_code == 403
        bad = await other.post("/api/auth/login", json={"username": "alice", "password": "wrong password"})
        assert bad.status_code == 401

    await client.post("/api/auth/logout")
    client.cookies.clear()
    assert (await client.get("/api/bots")).status_code == 401
    ok = await client.post("/api/auth/login", json={"username": "alice", "password": "correct horse"})
    assert ok.status_code == 200 and (await client.get("/api/bots")).status_code == 200


async def test_tampered_cookie_is_rejected(client):
    await _register(client, "alice")
    cookie = client.cookies["bots_session"]
    client.cookies.set("bots_session", cookie.replace("alice", "bob", 1))
    assert (await client.get("/api/bots")).status_code == 401


def _bot(name: str, owner: str | None, tools: list[str] | None = None) -> spec.BotSpec:
    return spec.save_bot(spec.BotSpec(name=name, title=name, description="d", instructions="# Goal\nDo the task well.",
                                      model="test-model", tools=tools or [], owner=owner))


async def test_users_see_only_their_bots_and_sessions(client):
    await _register(client, "alice")
    names = ["alice-bot", "bob-bot", "shared-bot"]
    for n, owner in zip(names, ["alice", "bob", None]):
        bot_runner.ensure_session(_bot(n, owner))
    try:
        listed = {b["name"] for b in (await client.get("/api/bots")).json()}
        assert {"alice-bot", "shared-bot"} <= listed and "bob-bot" not in listed
        assert (await client.get("/api/bots/bob-bot")).status_code == 404
        assert (await client.post("/api/bots/bob-bot/run")).status_code == 404
        assert (await client.get("/api/sessions/bot-bob-bot/messages")).status_code == 404
        sessions = {s["id"] for s in (await client.get("/api/sessions")).json()}
        assert "main-alice" in sessions and "bot-bob-bot" not in sessions

        current_user.set("alice")
        assert "No bot named" in await builder_tools.get_bot("bob-bot")
        assert "bob-bot" not in {b["name"] for b in await builder_tools.list_bots()}
    finally:
        for n in names:
            spec.delete_bot(n)


async def test_runs_use_owner_identity_and_a2a_stays_within_owner():
    alice, bob = _bot("alice-a2a", "alice", ["a2a", "hackernews_hn_top_stories"]), _bot("bob-a2a", "bob")
    try:
        seen = {}
        real = gateway.toolsets
        gateway.toolsets = lambda **kw: seen.update(kw) or real(**kw)
        try:
            bot_runner._toolsets(alice)
        finally:
            gateway.toolsets = real
        assert seen["user"] == "alice"  # runs act as the bot owner, whoever triggered them
        tools = a2a.toolset(alice.name).tools
        assert "No bot named" in await tools["a2a_send_message"].function("bob-a2a", "hi")
        assert "bob-a2a" not in {b["name"] for b in await tools["a2a_list_agents"].function()}
    finally:
        spec.delete_bot(alice.name)
        spec.delete_bot(bob.name)
