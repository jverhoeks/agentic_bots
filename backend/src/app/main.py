"""HTTP API: sessions + streaming chat, bots, runs, tools, models and a CloudEvents stream."""

import asyncio
import json
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from sqlmodel import select
from sse_starlette import EventSourceResponse

from app import events, gateway, spec
from app.agents import bot_runner, builder
from app.agents.runtime import message_dict, session_lock
from app.db import db, init_db
from app.llm import list_models
from app.models import Message, Run, Session


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    with db() as s:
        if not s.get(Session, builder.MAIN_SESSION):
            s.add(Session(id=builder.MAIN_SESSION, kind="main", title="Main"))
            s.commit()
    for bot in spec.list_bots():  # bots added by hand (git) get a session too
        bot_runner.ensure_session(bot)
    bot_runner.fail_orphaned_runs()
    yield


app = FastAPI(title="Agentic Bots", lifespan=lifespan)


class ChatIn(BaseModel):
    message: str


def _sse(gen):
    async def stream():
        async for ev in gen:
            yield {"data": json.dumps(ev, default=str)}

    return EventSourceResponse(stream(), ping=15)


def _bot_or_404(name: str) -> spec.BotSpec:
    try:
        bot = spec.get_bot(name)
    except ValueError:
        bot = None
    if not bot:
        raise HTTPException(404, f"no bot named {name!r}")
    return bot


def _last_runs() -> dict[str, Run]:
    with db() as s:
        runs = s.exec(select(Run).order_by(Run.started_at)).all()
    return {r.bot_name: r for r in runs}


# --- sessions ---------------------------------------------------------------

@app.get("/api/sessions")
def sessions():
    with db() as s:
        rows = s.exec(select(Session)).all()
    return [{"id": r.id, "kind": r.kind, "bot_name": r.bot_name, "title": r.title} for r in rows]


@app.get("/api/sessions/{session_id}/messages")
def messages(session_id: str):
    with db() as s:
        rows = s.exec(select(Message).where(Message.session_id == session_id).order_by(Message.id)).all()
    return [message_dict(m) for m in rows]


@app.post("/api/sessions/{session_id}/chat")
async def chat(session_id: str, body: ChatIn):
    with db() as s:
        sess = s.get(Session, session_id)
    if not sess:
        raise HTTPException(404, "unknown session")
    if sess.kind == "bot":
        gen_factory = lambda: bot_runner.chat(_bot_or_404(sess.bot_name), body.message)  # noqa: E731
    else:
        gen_factory = lambda: builder.chat(body.message)  # noqa: E731

    lock = session_lock(session_id)
    if lock.locked() or bot_runner.is_busy(session_id):
        raise HTTPException(409, "this session is busy, wait for the current turn to finish")

    async def locked():
        async with lock:
            async for ev in gen_factory():
                yield ev

    return _sse(locked())


@app.delete("/api/sessions/{session_id}/messages")
def clear_session(session_id: str):
    """Start the conversation over (history and visible messages)."""
    with db() as s:
        sess = s.get(Session, session_id)
        if not sess:
            raise HTTPException(404, "unknown session")
        for m in s.exec(select(Message).where(Message.session_id == session_id)).all():
            s.delete(m)
        sess.history_json = "[]"
        s.add(sess)
        s.commit()
    return {"ok": True}


# --- bots & runs -------------------------------------------------------------

def _bot_dict(b: spec.BotSpec, last: Run | None) -> dict:
    return {
        **b.model_dump(exclude={"instructions"}),
        "session_id": bot_runner.session_id_for(b.name),
        "last_run": bot_runner.run_dict(last) if last else None,
    }


@app.get("/api/bots")
def bots():
    last = _last_runs()
    return [_bot_dict(b, last.get(b.name)) for b in spec.list_bots()]


@app.get("/api/bots/{name}")
def bot(name: str):
    b = _bot_or_404(name)
    with db() as s:
        runs = s.exec(select(Run).where(Run.bot_name == name).order_by(Run.started_at.desc()).limit(20)).all()
    return {
        **_bot_dict(b, runs[0] if runs else None),
        "instructions": b.instructions,
        "skill_md": spec.raw_skill_md(name),
        "runs": [bot_runner.run_dict(r) for r in runs],
    }


@app.post("/api/bots/{name}/run")
async def run_bot(name: str):
    try:
        run = bot_runner.start_run(_bot_or_404(name))
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return bot_runner.run_dict(run)


@app.delete("/api/bots/{name}")
def delete_bot(name: str):
    _bot_or_404(name)
    spec.delete_bot(name)
    sid = bot_runner.session_id_for(name)
    with db() as s:
        for m in s.exec(select(Message).where(Message.session_id == sid)).all():
            s.delete(m)
        if sess := s.get(Session, sid):
            s.delete(sess)
        s.commit()
    events.publish("bot.deleted", {"name": name}, subject=name)
    return {"ok": True}


@app.get("/api/runs/{run_id}")
def run(run_id: str):
    with db() as s:
        r = s.get(Run, run_id)
    if not r:
        raise HTTPException(404, "unknown run")
    return bot_runner.run_dict(r)


# --- catalog -----------------------------------------------------------------

@app.get("/api/tools")
async def tools(refresh: bool = False):
    try:
        return {"gateway": list(gateway.servers().values()), "tools": await gateway.catalog(refresh)}
    except Exception as e:
        raise HTTPException(502, f"MCP gateway unreachable: {e}")


@app.get("/api/models")
async def models():
    from app.config import get_settings

    s = get_settings()
    try:
        available = await list_models()
    except Exception as e:
        available, error = [], str(e)
    else:
        error = None
    return {"builder": s.builder_model, "default": s.default_bot_model, "models": available, "error": error}


# --- events (CloudEvents over SSE) ------------------------------------------

@app.get("/api/events")
async def event_stream():
    q = events.subscribe()

    async def stream():
        try:
            while True:
                ev = await q.get()
                yield {"data": json.dumps(ev, default=str)}
        except asyncio.CancelledError:
            raise
        finally:
            events.unsubscribe(q)

    return EventSourceResponse(stream(), ping=15)


def run():
    import uvicorn

    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=True)
