"""HTTP API: sessions + streaming chat, bots, runs, tools, models and a CloudEvents stream."""

import asyncio
import json
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from opentelemetry.context import Context
from opentelemetry.trace import Status, StatusCode
from pydantic import BaseModel
from sqlmodel import select
from sse_starlette import EventSourceResponse

from app import telemetry

telemetry.setup()  # before the agents create their HTTP clients, so those calls are traced

from app import a2a_server, admin, audit, auth, events, gateway, memory, oidc, scheduler, spec, tracestore  # noqa: E402
from app.agents import bot_runner, builder  # noqa: E402
from app.agents.runtime import message_dict, session_lock  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import db, init_db  # noqa: E402
from app.llm import list_models  # noqa: E402
from app.models import Message, Run, Session, User as UserRow  # noqa: E402


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    auth.ensure_admin()
    for bot in spec.list_bots():  # bots added by hand (git) get a session too
        bot_runner.ensure_session(bot)
    bot_runner.fail_orphaned_runs()
    schedules = asyncio.create_task(scheduler.loop())
    yield
    schedules.cancel()
    await a2a_server.mount.aclose()
    telemetry.flush()


app = FastAPI(title="Agentic Bots", lifespan=lifespan)
app.mount(a2a_server.PREFIX, a2a_server.mount)  # each bot as an A2A server, reached via agentgateway
telemetry.instrument_app(app)
app.include_router(auth.router)  # login is public; everything else under /api needs a user
app.include_router(oidc.router)  # single sign-on, when configured
app.include_router(admin.router)  # admins only
app.include_router(tracestore.router)  # OTLP from agentgateway (compose network only, not proxied by nginx)
api = APIRouter(prefix="/api", dependencies=[Depends(auth.require_user)])
User = Depends(auth.require_user)  # the logged-in user id (dependency results are cached per request)


class ChatIn(BaseModel):
    message: str


class BotPatch(BaseModel):
    verbosity: spec.Verbosity | None = None


def _sse(gen):
    async def stream():
        async for ev in gen:
            yield {"data": json.dumps(ev, default=str)}

    return EventSourceResponse(stream(), ping=15)


def _bot_or_404(name: str, user: str) -> spec.BotSpec:
    """A bot the user may see; other users' bots are a 404, not a 403."""
    try:
        bot = spec.get_bot(name)
    except ValueError:
        bot = None
    if not bot or not spec.visible_to(bot, user):
        raise HTTPException(404, f"no bot named {name!r}")
    return bot


def _last_runs() -> dict[str, Run]:
    with db() as s:
        runs = s.exec(select(Run).order_by(Run.started_at)).all()
    return {r.bot_name: r for r in runs}


# --- sessions ---------------------------------------------------------------

def _session_or_404(session_id: str, user: str) -> Session:
    with db() as s:
        sess = s.get(Session, session_id)
    if sess and sess.kind == "main" and sess.owner == user:
        return sess
    if sess and sess.kind == "bot" and (b := spec.get_bot(sess.bot_name)) and spec.visible_to(b, user):
        return sess
    raise HTTPException(404, "unknown session")


@api.get("/sessions")
def sessions(user: str = User):
    builder.ensure_main_session(user)
    with db() as s:
        rows = s.exec(select(Session)).all()
    visible = {b.name for b in spec.list_bots(user)}
    return [{"id": r.id, "kind": r.kind, "bot_name": r.bot_name, "title": r.title} for r in rows
            if (r.kind == "main" and r.owner == user) or (r.kind == "bot" and r.bot_name in visible)]


@api.get("/sessions/{session_id}/messages")
def messages(session_id: str, user: str = User):
    _session_or_404(session_id, user)
    with db() as s:
        rows = s.exec(select(Message).where(Message.session_id == session_id).order_by(Message.id)).all()
    return [message_dict(m) for m in rows]


@api.post("/sessions/{session_id}/chat")
async def chat(session_id: str, body: ChatIn, user: str = User):
    sess = _session_or_404(session_id, user)
    if sess.kind == "bot":
        gen_factory = lambda: bot_runner.chat(_bot_or_404(sess.bot_name, user), body.message)  # noqa: E731
    else:
        gen_factory = lambda: builder.chat(user, body.message)  # noqa: E731

    lock = session_lock(session_id)
    if lock.locked() or bot_runner.is_busy(session_id):
        raise HTTPException(409, "this session is busy, wait for the current turn to finish")

    async def locked():
        auth.current_user.set(user)  # the stream runs in its own task: builder tools act as this user
        audit.scope(user=user, session_id=session_id)
        async with lock:
            # each turn is its own trace (the request span is not traced: see telemetry.instrument_app)
            attrs = {"trace.kind": "chat", "bot": sess.bot_name or "builder", "session.kind": sess.kind}
            if get_settings().trace_content:
                attrs["prompt"] = body.message
            with telemetry.tracer.start_as_current_span("chat.turn", context=Context(), attributes=attrs) as span:
                async for ev in gen_factory():
                    if ev.get("type") == "error":
                        span.set_status(Status(StatusCode.ERROR, str(ev.get("error"))[:500]))
                    yield ev

    return _sse(locked())


@api.get("/sessions/{session_id}/traces")
def session_traces(session_id: str, before: str | None = None, limit: int = 50, user: str = User):
    """The traces of one session: chat turns and runs, newest first."""
    _session_or_404(session_id, user)
    return [tracestore.trace_dict(t) for t in tracestore.traces(session_id=session_id, before=before, limit=limit)]


@api.get("/traces/{trace_id}")
def trace_detail(trace_id: str, user: str = User):
    """One trace with all its spans: yours (your user or a session you can see), or any for an admin."""
    t = tracestore.get(trace_id)
    if t is None:
        raise HTTPException(404, "unknown trace")
    with db() as s:
        me = s.get(UserRow, user)
    if not (me and me.role == auth.ADMIN) and t.user != user:
        if not t.session_id:
            raise HTTPException(404, "unknown trace")
        _session_or_404(t.session_id, user)
    return tracestore.detail(t)


@api.delete("/sessions/{session_id}/messages")
def clear_session(session_id: str, user: str = User):
    """Start the conversation over (history and visible messages)."""
    _session_or_404(session_id, user)
    audit.record("session.cleared", session_id=session_id)
    with db() as s:
        sess = s.get(Session, session_id)
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
        "next_run": scheduler.next_run(b.schedule),
        "last_run": bot_runner.run_dict(last) if last else None,
    }


@api.get("/bots")
def bots(user: str = User):
    last = _last_runs()
    return [_bot_dict(b, last.get(b.name)) for b in spec.list_bots(user)]


@api.get("/bots/{name}")
def bot(name: str, user: str = User):
    b = _bot_or_404(name, user)
    with db() as s:
        runs = s.exec(select(Run).where(Run.bot_name == name).order_by(Run.started_at.desc()).limit(20)).all()
    return {
        **_bot_dict(b, runs[0] if runs else None),
        "instructions": b.instructions,
        "skill_md": spec.raw_skill_md(name),
        "runs": [bot_runner.run_dict(r) for r in runs],
    }


@api.post("/bots/{name}/run")
async def run_bot(name: str, user: str = User):
    try:
        run = bot_runner.start_run(_bot_or_404(name, user), actor=user)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return bot_runner.run_dict(run)


@api.patch("/bots/{name}")
def patch_bot(name: str, body: BotPatch, user: str = User):
    """Small settings changes from the UI; anything bigger goes through the builder chat."""
    b = _bot_or_404(name, user)
    b = spec.save_bot(b.model_copy(update=body.model_dump(exclude_none=True)))
    events.publish("bot.updated", {"name": b.name, "title": b.title}, subject=b.name)
    audit.record("bot.updated", bot=b.name, detail={**body.model_dump(exclude_none=True), "via": "ui"})
    return _bot_dict(b, _last_runs().get(name))


@api.get("/bots/{name}/memory")
def bot_memory(name: str, user: str = User):
    _bot_or_404(name, user)
    return [{"key": m.key, "value": m.value, "updated_at": m.updated_at} for m in memory.items(name)]


@api.delete("/bots/{name}/memory")
def clear_bot_memory(name: str, key: str | None = None, user: str = User):
    _bot_or_404(name, user)
    audit.record("memory.deleted", bot=name, detail={"key": key})
    return {"deleted": memory.delete(name, key)}


@api.delete("/bots/{name}")
def delete_bot(name: str, user: str = User):
    _bot_or_404(name, user)
    spec.delete_bot(name)
    sid = bot_runner.session_id_for(name)
    with db() as s:
        for m in s.exec(select(Message).where(Message.session_id == sid)).all():
            s.delete(m)
        if sess := s.get(Session, sid):
            s.delete(sess)
        s.commit()
    memory.delete(name)
    audit.record("bot.deleted", bot=name)
    events.publish("bot.deleted", {"name": name}, subject=name)
    return {"ok": True}


@api.get("/runs/{run_id}")
def run(run_id: str, user: str = User):
    with db() as s:
        r = s.get(Run, run_id)
    if not r or not (b := spec.get_bot(r.bot_name)) or not spec.visible_to(b, user):
        raise HTTPException(404, "unknown run")
    return bot_runner.run_dict(r)


# --- catalog -----------------------------------------------------------------

@api.get("/tools")
async def tools(refresh: bool = False, user: str = User):
    try:
        return {"gateway": list(gateway.servers().values()), "tools": await gateway.catalog(refresh, user=user)}
    except Exception as e:
        raise HTTPException(502, f"MCP gateway unreachable: {e}")


@api.get("/models")
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

UI_EVENTS = (f"{events.OWN}bot.", f"{events.OWN}run.")


@api.get("/events")
async def event_stream(user: str = User):
    q = events.subscribe()

    def visible(ev: dict) -> bool:  # events name a bot in `subject`; only send the user's own
        try:
            b = spec.get_bot(ev.get("subject") or "")
        except ValueError:
            return True
        return b is None or spec.visible_to(b, user)

    async def stream():
        try:
            while True:
                ev = await q.get()
                # only the sidebar's updates: tool, LLM and MCP activity (arguments, other users) stays server-side
                if not ev["type"].startswith(UI_EVENTS) or not visible(ev):
                    continue
                yield {"data": json.dumps(ev, default=str)}
        except asyncio.CancelledError:
            raise
        finally:
            events.unsubscribe(q)

    return EventSourceResponse(stream(), ping=15)


app.include_router(api)


def run():
    import uvicorn

    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=True)
