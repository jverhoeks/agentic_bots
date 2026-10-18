"""Traces in the app database: a chat turn with its LLM and tool spans per user and session, gateway spans over OTLP."""

import json
import os

import httpx
import pytest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans, ScopeSpans, Span
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel
from pydantic_ai.toolsets import FunctionToolset
from sqlmodel import select

from app import auth, events, spec, telemetry, tracestore
from app.agents import bot_runner
from app.db import db
from app.main import app
from app.models import Message, TraceSpan, User

SECRET = "sk-proj-" + "Z9y8X7w6" * 4
BOT = "traced-bot"
GW = {"content-type": "application/x-protobuf", "x-trace-token": "test-trace-token"}


async def _client(user: str) -> httpx.AsyncClient:
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bots.test")
    r = await c.post("/api/auth/login", json={"username": user, "password": "correct horse"})
    assert r.status_code == 200, r.text
    return c


@pytest.fixture
async def users(monkeypatch):
    with db() as s:
        for u in s.exec(select(User)).all():
            s.delete(u)
        s.commit()
    auth.create_user("root", "correct horse")  # first account: admin
    auth.create_user("alice", "correct horse")
    auth.create_user("bob", "correct horse")
    bot = spec.save_bot(spec.BotSpec(name=BOT, title="Traced", description="d", instructions="# Goal\nAnswer from the inbox.",
                                     model="test-model", tools=[], owner="alice"))
    bot_runner.ensure_session(bot)

    tools = FunctionToolset()

    @tools.tool_plain
    def read_inbox() -> str:
        return f"Mail from Carol: the key is {SECRET}"

    monkeypatch.setattr(bot_runner, "build_agent", lambda b, editable=False, via="bot-chat": Agent(TestModel(call_tools=["read_inbox"]), toolsets=[tools]))
    clients = {u: await _client(u) for u in ("root", "alice", "bob")}
    yield clients
    for c in clients.values():
        await c.aclose()
    spec.delete_bot(BOT)


async def _chat(c: httpx.AsyncClient, sid: str, text: str) -> list[dict]:
    async with c.stream("POST", f"/api/sessions/{sid}/chat", json={"message": text}) as r:
        assert r.status_code == 200
        return [json.loads(line[5:]) async for line in r.aiter_lines() if line.startswith("data:")]


def _otlp(trace_id: str, parent: str) -> bytes:
    def kv(k, v):
        return KeyValue(key=k, value=AnyValue(string_value=v))

    span = Span(trace_id=bytes.fromhex(trace_id), span_id=os.urandom(8), parent_span_id=bytes.fromhex(parent),
                name="tools/call web_fetch_url", kind=Span.SPAN_KIND_SERVER, start_time_unix_nano=1,
                end_time_unix_nano=2_000_001, attributes=[kv("user", "alice"), kv("bot", BOT)])  # as agentgateway 1.6
    rs = ResourceSpans(scope_spans=[ScopeSpans(spans=[span])])
    rs.resource.attributes.append(kv("service.name", "agentgateway"))
    return ExportTraceServiceRequest(resource_spans=[rs]).SerializeToString()


@pytest.fixture
def published():
    got: list[dict] = []
    events.on(events.OWN, got.append)
    yield got
    events.off(got.append)


async def test_chat_turn_is_one_trace_per_user_and_session(users):
    sid = bot_runner.session_id_for(BOT)
    events = await _chat(users["alice"], sid, "what is in my inbox?")
    assert events[-1]["type"] == "done"
    telemetry.flush()

    listed = (await users["alice"].get(f"/api/sessions/{sid}/traces")).json()
    t = listed[0]
    assert t["kind"] == "chat" and t["name"] == "chat.turn"
    assert t["user"] == "alice" and t["session_id"] == sid and t["bot"] == BOT
    assert t["llm_calls"] >= 1 and t["tool_calls"] == 1
    with db() as s:
        msg = s.exec(select(Message).where(Message.session_id == sid, Message.role == "assistant")).all()[-1]
    assert msg.trace_id == t["trace_id"]  # the chat links each answer to its trace

    detail = (await users["alice"].get(f"/api/traces/{t['trace_id']}")).json()
    spans = detail["spans"]
    tool = next(sp for sp in spans if sp["attrs"].get("gen_ai.operation.name") == "execute_tool")
    assert tool["user"] == "alice" and tool["session_id"] == sid  # every span carries who and where
    blob = json.dumps(spans)
    assert "Mail from Carol" in blob and SECRET not in blob  # content kept, secrets redacted
    assert not any("gen_ai.input.messages" in sp["attrs"] for sp in spans)  # the repeated history is dropped
    root = next(sp for sp in spans if sp["parent_id"] is None)
    assert root["attrs"]["prompt"] == "what is in my inbox?"

    # someone else's trace is a 404; an admin sees it
    assert (await users["bob"].get(f"/api/traces/{t['trace_id']}")).status_code == 404
    assert (await users["bob"].get(f"/api/sessions/{sid}/traces")).status_code == 404
    assert (await users["root"].get(f"/api/traces/{t['trace_id']}")).status_code == 200
    admin = (await users["root"].get("/api/admin/traces", params={"user": "alice"})).json()
    assert t["trace_id"] in [x["trace_id"] for x in admin]
    assert (await users["alice"].get("/api/admin/traces")).status_code == 403

    # the gateway's spans for that turn arrive over OTLP and join the same trace
    r = await users["bob"].post("/otlp/v1/traces", content=_otlp(t["trace_id"], tool["span_id"]), headers=GW)
    assert r.status_code == 200
    after = tracestore.get(t["trace_id"])
    assert after.spans == t["spans"] + 1 and "agentgateway" in after.services.split()
    with db() as s:
        gw = s.exec(select(TraceSpan).where(TraceSpan.trace_id == t["trace_id"], TraceSpan.service == "agentgateway")).one()
    assert gw.user == "alice" and gw.session_id == sid and gw.kind == "server"  # where, from the trace


async def test_gateway_only_trace_is_kept_for_the_user(users):
    """An external MCP client calling the gateway directly: no backend span, still stored under its user."""
    tid = os.urandom(16).hex()
    body = _otlp(tid, "00" * 8)  # all-zero parent = none
    r = await users["root"].post("/otlp/v1/traces", content=body, headers=GW)
    assert r.status_code == 200
    t = tracestore.get(tid)
    assert t.kind == "gateway" and t.user == "alice" and t.name == "tools/call web_fetch_url"
    assert (await users["root"].post("/otlp/v1/traces", content=b"\xff\x00garbage", headers=GW)).status_code == 400


async def test_gateway_spans_need_the_ingest_token(users, monkeypatch):
    """Other containers on the compose network (connectors reading web pages and mail) cannot forge spans."""
    tid = os.urandom(16).hex()
    for headers in ({}, {**GW, "x-trace-token": "guess"}):
        r = await users["root"].post("/otlp/v1/traces", content=_otlp(tid, "00" * 8), headers=headers)
        assert r.status_code == 401
    monkeypatch.setattr(tracestore.get_settings(), "trace_ingest_token", "")  # not configured: refuse all
    r = await users["root"].post("/otlp/v1/traces", content=_otlp(tid, "00" * 8), headers={**GW, "x-trace-token": ""})
    assert r.status_code == 401
    assert tracestore.get(tid) is None


async def test_tool_llm_and_mcp_calls_are_cloudevents(users, published, monkeypatch):
    """Every tool, LLM and MCP call goes on the bus (and to EVENT_SINK_URL) as a CloudEvent, without content by default."""
    sent: list[list[dict]] = []
    monkeypatch.setattr(tracestore.get_settings(), "event_sink_url", "http://sink.test/events")
    monkeypatch.setattr(events, "_start_sender", lambda: None)  # drain the outbox by hand below
    sid = bot_runner.session_id_for(BOT)
    await users["alice"].delete(f"/api/sessions/{sid}/messages")  # a fresh conversation calls the tool
    await _chat(users["alice"], sid, "what is in my inbox?")
    telemetry.flush()

    tool = [e for e in published if e["type"] == "com.agenticbots.tool.called"]
    assert len(tool) == 1
    ev = tool[0]
    assert ev["specversion"] == "1.0" and ev["source"] == "/agentic-bots" and ev["subject"] == BOT
    d = ev["data"]
    assert d["tool"] == "read_inbox" and d["user"] == "alice" and d["session_id"] == sid and d["status"] != "error"
    assert ev["traceparent"] == f"00-{d['trace_id']}-{d['span_id']}-01"
    assert "result" not in d and "Mail from Carol" not in json.dumps(published)  # EVENT_CONTENT is off
    llm = [e for e in published if e["type"] == "com.agenticbots.llm.called"]
    assert llm and all(e["data"]["trace_id"] == d["trace_id"] for e in llm)

    # with content: redacted like the stored spans
    monkeypatch.setattr(tracestore.get_settings(), "event_content", True)
    published.clear()
    await users["alice"].delete(f"/api/sessions/{sid}/messages")
    await _chat(users["alice"], sid, "again")
    telemetry.flush()
    blob = json.dumps(published)
    assert "Mail from Carol" in blob and SECRET not in blob

    # gateway spans become mcp.called
    published.clear()
    r = await users["root"].post("/otlp/v1/traces", content=_otlp(os.urandom(16).hex(), "00" * 8), headers=GW)
    assert r.status_code == 200
    mcp = [e for e in published if e["type"] == "com.agenticbots.mcp.called"]
    assert len(mcp) == 1 and mcp[0]["data"].items() >= {"method": "tools/call", "target": "web_fetch_url", "user": "alice"}.items()

    # the sink gets them as a CloudEvents batch
    while not events._outbox.empty():
        sent.append([events._outbox.get_nowait()])
    assert any(b[0]["type"] == "com.agenticbots.mcp.called" for b in sent)
    posted = {}
    monkeypatch.setattr("requests.post", lambda url, data, headers, timeout: posted.update(url=url, data=data, headers=headers)
                        or type("R", (), {"status_code": 202})())
    events.send(sent[-1])
    assert posted["headers"]["content-type"] == "application/cloudevents-batch+json"
    assert json.loads(posted["data"])[0]["specversion"] == "1.0"


async def test_browser_stream_gets_only_bot_and_run_events():
    from app.main import UI_EVENTS

    assert events.cloudevent("run.finished", {})["type"].startswith(UI_EVENTS)
    for t in ("tool.called", "llm.called", "mcp.called", "a2a.called"):
        assert not events.cloudevent(t, {})["type"].startswith(UI_EVENTS)


def test_a2a_request_is_an_event(published):
    tid, sid = os.urandom(16).hex(), os.urandom(8).hex()
    tracestore.store([{"trace_id": tid, "span_id": sid, "parent_id": None, "name": "POST /a2a/traced-bot/",
                       "service": "agentic-bots", "kind": "server", "start": 1, "end": 1_000_001, "status": "ok",
                       "status_message": None, "attrs": {"url.path": "/a2a/traced-bot/", "user": "alice", "bot": BOT},
                       "events": [], "links": []}])
    ev = next(e for e in published if e["type"] == "com.agenticbots.a2a.called")
    assert ev["data"]["user"] == "alice" and ev["traceparent"] == f"00-{tid}-{sid}-01"
